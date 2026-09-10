"""TRELLIS.2 generation server for the Blender bridge.

Design constraints learned the hard way on this machine (Ryzen AI 9 HX PRO 375,
gfx1150 iGPU, unified memory shared with the host):

  * low_vram MUST stay True. Setting it False keeps all 16GB of weights resident
    in "GPU" memory, which on a UMA APU is the same physical RAM as the CPU copy;
    that pushed the WSL VM past its 30GB allotment and got a host process killed.
  * Exactly one job runs at a time. No concurrent GPU work.
  * The pipeline is loaded once at startup, so the ~101s from_pretrained cost is
    paid once instead of per generation.
  * Job state is persisted after every transition, so a crash or restart leaves a
    visible record instead of a silently vanished job.
"""
import os

os.environ.setdefault('OPENCV_IO_ENABLE_OPENEXR', '1')
os.environ.setdefault('PYTORCH_HIP_ALLOC_CONF', 'garbage_collection_threshold:0.6,max_split_size_mb:128')
os.environ.setdefault('HSA_XNACK', '1')
os.environ.setdefault('HSA_ENABLE_DXG_DETECTION', '1')
os.environ.setdefault('TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL', '1')
os.environ.setdefault('ATTN_BACKEND', 'sdpa')

import io
import json
import queue
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

ROOT = Path('/root/TRELLIS.2_rocm')
WORK = ROOT / 'server_data'
OUTPUTS = WORK / 'outputs'
UPLOADS = WORK / 'uploads'
JOBS_FILE = WORK / 'jobs.json'
for d in (WORK, OUTPUTS, UPLOADS):
    d.mkdir(parents=True, exist_ok=True)

# Refuse to start a job when the VM is already this tight; an OOM here can take
# down processes on the Windows host, not just this server.
MIN_FREE_GIB = 10.0

# Rough share of total runtime per stage, measured at 512 on this GPU. Used only
# to show an ETA; being wrong costs nothing but a slightly off progress bar.
STAGE_WEIGHTS = [
    ('preprocess_image', 0.01),
    ('get_cond', 0.03),
    ('sample_sparse_structure', 0.10),
    ('sample_shape_slat', 0.22),
    ('sample_shape_slat_cascade', 0.22),
    ('decode_shape_slat', 0.08),
    ('sample_tex_slat', 0.22),
    ('decode_tex_slat', 0.08),
    ('decode_latent', 0.05),
    ('simplify', 0.03),
    ('to_glb', 0.14),
    ('export', 0.02),
]
STAGE_LABELS = {
    'loading_model': '모델 로딩',
    'preprocess_image': '이미지 전처리',
    'get_cond': '이미지 특징 추출',
    'sample_sparse_structure': '희소 구조 샘플링',
    'sample_shape_slat': '형상 잠재 샘플링',
    'sample_shape_slat_cascade': '형상 잠재 샘플링 (캐스케이드)',
    'decode_shape_slat': '형상 디코딩',
    'sample_tex_slat': '텍스처 잠재 샘플링',
    'decode_tex_slat': '텍스처 디코딩',
    'decode_latent': '잠재 디코딩',
    'simplify': '메시 단순화',
    'to_glb': 'GLB 생성 (UV/텍스처)',
    'export': '파일 쓰기',
}

# GLB is the pipeline's native, fully-textured output. The other formats are
# produced on demand at download time (never during generation) by re-reading
# the already-exported .glb with trimesh, so a missing/broken trimesh install
# only affects format conversion, never generation itself. Requires
# `pip install trimesh` in the trellis2-gfx1150 conda env — see AGENTS.md.
SUPPORTED_FORMATS = ('glb', 'obj', 'ply', 'stl')
_FORMAT_MEDIA_TYPES = {
    'glb': 'model/gltf-binary',
    'obj': 'application/zip',   # bundled with its .mtl + texture image(s)
    'ply': 'application/octet-stream',
    'stl': 'model/stl',
}

# Minimal same-origin web UI so the server has a client that isn't the Blender
# addon or curl. Talks to the exact same JSON endpoints below — nothing here
# is UI-only API surface. Kept as one inline string (not a static/ file) so it
# stays inside trellis_server.py's existing WSL-mirroring rule in AGENTS.md
# instead of adding a second file that needs to be kept in sync separately.
_INDEX_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TRELLIS.2 ROCm Bridge</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 system-ui, sans-serif; max-width: 640px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.25rem; }
  label { display: block; margin: 0.6rem 0 0.2rem; font-size: 0.85rem; opacity: 0.8; }
  input, select, button { font: inherit; padding: 0.4rem; }
  input[type=number] { width: 8rem; }
  button { cursor: pointer; }
  #status { margin-top: 1rem; padding: 0.75rem; border-radius: 8px; background: #8882; display: none; }
  progress { width: 100%; }
  .row { display: flex; gap: 1rem; flex-wrap: wrap; }
  .err { color: #c0392b; }
</style>
</head>
<body>
<h1>TRELLIS.2 ROCm Bridge</h1>
<p>이미지를 업로드하면 텍스처가 입혀진 3D 메시를 생성합니다. 이 페이지는
<a href="/docs">/docs</a>(OpenAPI/Swagger)와 동일한 HTTP API 위에 얹은 참고용
클라이언트일 뿐이며, curl이나 Blender addon과 동시에 써도 서로 간섭하지 않습니다.</p>

<form id="f">
  <label for="image">이미지 (PNG 권장, 배경 제거가 필요하면 알파 채널 포함)</label>
  <input id="image" name="image" type="file" accept="image/*" required>

  <div class="row">
    <div>
      <label for="pipeline_type">해상도</label>
      <select id="pipeline_type" name="pipeline_type">
        <option value="512" selected>512</option>
        <option value="1024">1024</option>
        <option value="1024_cascade">1024 (cascade)</option>
        <option value="1536_cascade">1536 (cascade)</option>
      </select>
    </div>
    <div>
      <label for="seed">시드</label>
      <input id="seed" name="seed" type="number" value="42">
    </div>
    <div>
      <label for="texture_size">텍스처 크기</label>
      <input id="texture_size" name="texture_size" type="number" value="2048" step="256">
    </div>
    <div>
      <label for="decimation_target">목표 폴리곤 수</label>
      <input id="decimation_target" name="decimation_target" type="number" value="1000000" step="50000">
    </div>
  </div>

  <label for="format">다운로드 형식</label>
  <select id="format" name="format">
    <option value="glb" selected>GLB — 텍스처 포함, 기본값</option>
    <option value="obj">OBJ — 범용, .mtl+텍스처를 zip으로 묶음</option>
    <option value="ply">PLY — 지오메트리만</option>
    <option value="stl">STL — 지오메트리만</option>
  </select>

  <p>
    <button type="submit">생성 시작</button>
    <button type="button" id="cancel" disabled>취소</button>
  </p>
</form>

<div id="status">
  <div id="stage"></div>
  <progress id="progress" value="0" max="1"></progress>
  <div id="eta"></div>
  <div id="dl"></div>
</div>

<script>
const f = document.getElementById('f');
const statusBox = document.getElementById('status');
const stageEl = document.getElementById('stage');
const progressEl = document.getElementById('progress');
const etaEl = document.getElementById('eta');
const dlEl = document.getElementById('dl');
const cancelBtn = document.getElementById('cancel');
let jobId = null, polling = null;

function stopPolling() {
  clearInterval(polling);
  polling = null;
  cancelBtn.disabled = true;
}

f.addEventListener('submit', async (e) => {
  e.preventDefault();
  stopPolling();
  jobId = null;
  dlEl.innerHTML = '';
  etaEl.textContent = '';
  statusBox.style.display = 'block';
  stageEl.textContent = '업로드 중...';
  progressEl.value = 0;

  const body = new FormData(f);
  const format = body.get('format');
  body.delete('format'); // /generate doesn't take this; only /jobs/{id}/file does

  let res;
  try {
    res = await fetch('/generate', { method: 'POST', body });
  } catch (err) {
    stageEl.innerHTML = `<span class="err">요청 실패: ${err}</span>`;
    return;
  }
  if (!res.ok) {
    const detail = (await res.json().catch(() => ({}))).detail || res.statusText;
    stageEl.innerHTML = `<span class="err">${detail}</span>`;
    return;
  }
  const data = await res.json();
  jobId = data.job_id;
  cancelBtn.disabled = false;
  poll(format);
});

cancelBtn.addEventListener('click', async () => {
  if (!jobId) return;
  await fetch(`/jobs/${jobId}/cancel`, { method: 'POST' });
});

function poll(format) {
  polling = setInterval(async () => {
    if (!jobId) return;
    const res = await fetch(`/jobs/${jobId}`);
    if (!res.ok) return;
    const job = await res.json();
    stageEl.textContent = `${job.stage_label || job.status} (${Math.round((job.progress || 0) * 100)}%)`;
    progressEl.value = job.progress || 0;
    etaEl.textContent = job.eta_seconds != null ? `예상 남은 시간: 약 ${job.eta_seconds}초` : '';

    if (job.status === 'done') {
      stopPolling();
      dlEl.innerHTML = `<a href="/jobs/${jobId}/file?format=${format}" download>결과 다운로드 (${format})</a>`;
    } else if (['failed', 'cancelled', 'interrupted'].includes(job.status)) {
      stopPolling();
      stageEl.innerHTML = `<span class="err">${job.status}: ${job.error || ''}</span>`;
    }
  }, 2000);
}
</script>
</body>
</html>
"""

app = FastAPI(title='TRELLIS.2 Blender Bridge')

_state = {
    'pipeline': None,
    'model_status': 'starting',   # starting | loading | ready | failed
    'model_error': None,
    'load_seconds': None,
}
_jobs = {}                # job_id -> dict
_jobs_lock = threading.Lock()
_queue = queue.Queue()
_current_job_id = None
_cancel_requested = set()


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def _save_jobs():
    try:
        with _jobs_lock:
            data = sorted(_jobs.values(), key=lambda j: j['created_at'], reverse=True)[:100]
        tmp = JOBS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(JOBS_FILE)
    except Exception:
        traceback.print_exc()


def _load_jobs():
    if not JOBS_FILE.exists():
        return
    try:
        for job in json.loads(JOBS_FILE.read_text()):
            # Anything still marked running belongs to a previous process that
            # died; surface it rather than letting it disappear.
            if job.get('status') in ('running', 'queued'):
                job['status'] = 'interrupted'
                job['error'] = job.get('error') or '서버가 종료되어 작업이 중단되었습니다.'
                job['finished_at'] = job.get('finished_at') or _now()
            _jobs[job['id']] = job
    except Exception:
        traceback.print_exc()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _free_gib():
    """Available memory inside the VM, in GiB."""
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    return -1.0


def _update(job_id, **fields):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(fields)
    _save_jobs()


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
def _load_model():
    _state['model_status'] = 'loading'
    t0 = time.time()
    try:
        import torch
        from trellis2.pipelines import Trellis2ImageTo3DPipeline

        torch.cuda.set_per_process_memory_fraction(0.90)
        pipe = Trellis2ImageTo3DPipeline.from_pretrained('microsoft/TRELLIS.2-4B')
        # Never flip this to False on a UMA APU. See module docstring.
        pipe.low_vram = True
        pipe.cuda()
        _state['pipeline'] = pipe
        _state['load_seconds'] = round(time.time() - t0, 1)
        _state['model_status'] = 'ready'
        print(f'[server] model ready in {_state["load_seconds"]}s', flush=True)
    except Exception as e:
        _state['model_status'] = 'failed'
        _state['model_error'] = f'{type(e).__name__}: {e}'
        traceback.print_exc()


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    pass


def _instrument(pipe, job_id, started):
    """Wrap pipeline methods so each stage transition is visible to the client."""
    originals = {}
    order = [name for name, _ in STAGE_WEIGHTS]

    def wrap(name):
        orig = getattr(pipe, name)
        originals[name] = orig

        def inner(*a, **kw):
            if job_id in _cancel_requested:
                raise Cancelled()
            _set_stage(job_id, name, started)
            return orig(*a, **kw)
        return inner

    for name in order:
        if hasattr(pipe, name) and callable(getattr(pipe, name, None)):
            try:
                setattr(pipe, name, wrap(name))
            except Exception:
                pass
    return originals


def _restore(pipe, originals):
    for name, orig in originals.items():
        try:
            setattr(pipe, name, orig)
        except Exception:
            pass


def _set_stage(job_id, stage, started):
    done = 0.0
    for name, w in STAGE_WEIGHTS:
        if name == stage:
            break
        done += w
    elapsed = time.time() - started
    eta = None
    if done > 0.02:
        eta = max(0, round(elapsed / done - elapsed))
    _update(job_id,
            stage=stage,
            stage_label=STAGE_LABELS.get(stage, stage),
            progress=round(min(done, 0.99), 3),
            elapsed_seconds=round(elapsed, 1),
            eta_seconds=eta)


def _run_job(job):
    import torch
    import o_voxel
    from PIL import Image

    job_id = job['id']
    pipe = _state['pipeline']
    started = time.time()
    _update(job_id, status='running', started_at=_now(), stage='preprocess_image',
            stage_label=STAGE_LABELS['preprocess_image'], progress=0.0)

    originals = _instrument(pipe, job_id, started)
    try:
        torch.cuda.reset_peak_memory_stats()
        image = Image.open(job['image_path'])
        mesh = pipe.run(image, pipeline_type=job['pipeline_type'], seed=job['seed'])[0]

        if job_id in _cancel_requested:
            raise Cancelled()

        _set_stage(job_id, 'simplify', started)
        mesh.simplify(16777216)

        _set_stage(job_id, 'to_glb', started)
        glb = o_voxel.postprocess.to_glb(
            vertices=mesh.vertices, faces=mesh.faces, attr_volume=mesh.attrs,
            coords=mesh.coords, attr_layout=mesh.layout, voxel_size=mesh.voxel_size,
            aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
            decimation_target=job['decimation_target'],
            texture_size=job['texture_size'],
            remesh=True, remesh_band=1, remesh_project=0, verbose=False,
        )

        _set_stage(job_id, 'export', started)
        out = OUTPUTS / f'{job_id}.glb'
        try:
            glb.export(str(out), extension_webp=True)
        except Exception as e:
            print(f'[server] webp export failed ({e}); PNG fallback', flush=True)
            glb.export(str(out))

        peak = torch.cuda.max_memory_allocated() / 1024 ** 3
        _update(job_id, status='done', finished_at=_now(), progress=1.0,
                stage='done', stage_label='완료',
                elapsed_seconds=round(time.time() - started, 1),
                eta_seconds=0,
                output_file=str(out),
                output_bytes=out.stat().st_size,
                peak_gib=round(peak, 2))
        print(f'[server] job {job_id} done in {time.time()-started:.1f}s', flush=True)

    except Cancelled:
        _update(job_id, status='cancelled', finished_at=_now(),
                error='사용자가 취소했습니다.',
                elapsed_seconds=round(time.time() - started, 1))
    except Exception as e:
        _update(job_id, status='failed', finished_at=_now(),
                error=f'{type(e).__name__}: {e}',
                traceback=traceback.format_exc()[-4000:],
                elapsed_seconds=round(time.time() - started, 1))
        traceback.print_exc()
    finally:
        _restore(pipe, originals)
        _cancel_requested.discard(job_id)
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass


def _worker():
    global _current_job_id
    _load_model()
    while True:
        job = _queue.get()
        if job is None:
            break
        if _state['model_status'] != 'ready':
            _update(job['id'], status='failed', finished_at=_now(),
                    error=f'모델이 준비되지 않았습니다: {_state["model_error"]}')
            continue
        if job['id'] in _cancel_requested:
            _update(job['id'], status='cancelled', finished_at=_now(),
                    error='대기 중 취소되었습니다.')
            _cancel_requested.discard(job['id'])
            continue
        _current_job_id = job['id']
        try:
            _run_job(job)
        finally:
            _current_job_id = None


_worker_thread = threading.Thread(target=_worker, daemon=True)


@app.on_event('startup')
def _startup():
    _load_jobs()
    _save_jobs()
    _worker_thread.start()


# --------------------------------------------------------------------------- #
# output format conversion
# --------------------------------------------------------------------------- #
def _convert_output(job_id: str, glb_path: Path, fmt: str) -> Path:
    """Return `glb_path` re-exported as `fmt`, converting once and caching the
    result next to the .glb. Geometry-only formats (ply/stl) drop the texture
    atlas; obj keeps it via a sidecar .mtl + image, bundled into a zip since a
    single HTTP response can only carry one file."""
    if fmt == 'glb':
        return glb_path

    out = OUTPUTS / (f'{job_id}.obj.zip' if fmt == 'obj' else f'{job_id}.{fmt}')
    if out.exists():
        return out

    try:
        import trimesh
    except ImportError:
        raise HTTPException(
            501, f"'{fmt}' 변환에는 trimesh가 필요합니다. WSL의 trellis2-gfx1150 "
                 f"conda 환경에서 `pip install trimesh`를 실행한 뒤 서버를 재시작하세요.")

    try:
        loaded = trimesh.load(str(glb_path))
        if fmt == 'obj':
            obj_dir = OUTPUTS / f'{job_id}_obj'
            obj_dir.mkdir(exist_ok=True)
            try:
                loaded.export(str(obj_dir / 'model.obj'))
                tmp = out.with_suffix('.tmp')
                with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for f in obj_dir.iterdir():
                        zf.write(f, arcname=f.name)
                tmp.replace(out)
            finally:
                shutil.rmtree(obj_dir, ignore_errors=True)
        else:
            mesh = loaded
            if isinstance(loaded, trimesh.Scene):
                geoms = list(loaded.geometry.values())
                mesh = loaded.dump(concatenate=True) if len(geoms) != 1 else geoms[0]
            mesh.export(str(out))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"'{fmt}' 형식으로 변환하지 못했습니다: {e}")
    return out


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get('/', response_class=HTMLResponse)
def index():
    return _INDEX_HTML


@app.get('/health')
def health():
    with _jobs_lock:
        queued = sum(1 for j in _jobs.values() if j['status'] == 'queued')
    return {
        'model_status': _state['model_status'],
        'model_error': _state['model_error'],
        'load_seconds': _state['load_seconds'],
        'free_gib': round(_free_gib(), 1),
        'min_free_gib': MIN_FREE_GIB,
        'current_job': _current_job_id,
        'queued': queued,
        'output_formats': list(SUPPORTED_FORMATS),
    }


@app.post('/generate')
async def generate(
    image: UploadFile = File(...),
    pipeline_type: str = Form('512'),
    seed: int = Form(42),
    texture_size: int = Form(2048),
    decimation_target: int = Form(1000000),
):
    if pipeline_type not in ('512', '1024', '1024_cascade', '1536_cascade'):
        raise HTTPException(400, f'지원하지 않는 해상도: {pipeline_type}')

    free = _free_gib()
    if 0 <= free < MIN_FREE_GIB:
        raise HTTPException(
            507, f'메모리 부족으로 거부했습니다 (여유 {free:.1f} GiB < 최소 {MIN_FREE_GIB} GiB). '
                 f'다른 작업을 종료한 뒤 다시 시도하세요.')

    job_id = uuid.uuid4().hex[:12]
    suffix = Path(image.filename or 'input.png').suffix or '.png'
    dest = UPLOADS / f'{job_id}{suffix}'
    with dest.open('wb') as f:
        shutil.copyfileobj(image.file, f)

    # An image without alpha needs background removal. If no rembg model loaded,
    # say so here rather than letting the pipeline die on a NoneType attribute.
    try:
        from PIL import Image as _PILImage
        import numpy as _np
        with _PILImage.open(dest) as im:
            has_alpha = False
            if im.mode == 'RGBA':
                has_alpha = not bool((_np.array(im)[:, :, 3] == 255).all())
        pipe = _state.get('pipeline')
        if not has_alpha and pipe is not None and getattr(pipe, 'rembg_model', None) is None:
            dest.unlink(missing_ok=True)
            raise HTTPException(
                422,
                '이 이미지에는 알파 채널이 없어 배경 제거가 필요한데, 배경 제거 모델이 '
                '로드되지 않았습니다. 알파 채널이 있는 PNG를 사용하거나 서버를 재시작하세요.')
    except HTTPException:
        raise
    except Exception as e:
        print(f'[server] alpha pre-check skipped: {e}', flush=True)

    job = {
        'id': job_id,
        'status': 'queued',
        'stage': None,
        'stage_label': '대기 중',
        'progress': 0.0,
        'created_at': _now(),
        'started_at': None,
        'finished_at': None,
        'image_name': image.filename,
        'image_path': str(dest),
        'pipeline_type': pipeline_type,
        'seed': seed,
        'texture_size': texture_size,
        'decimation_target': decimation_target,
        'error': None,
        'output_file': None,
        'elapsed_seconds': 0.0,
        'eta_seconds': None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    _save_jobs()
    _queue.put(job)
    return {'job_id': job_id}


@app.get('/jobs')
def list_jobs(limit: int = 20):
    with _jobs_lock:
        data = sorted(_jobs.values(), key=lambda j: j['created_at'], reverse=True)[:limit]
    return {'jobs': [{k: v for k, v in j.items() if k not in ('traceback', 'image_path')}
                     for j in data]}


@app.get('/jobs/{job_id}')
def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, '작업을 찾을 수 없습니다.')
    return {k: v for k, v in job.items() if k != 'image_path'}


@app.post('/jobs/{job_id}/cancel')
def cancel_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, '작업을 찾을 수 없습니다.')
    if job['status'] in ('done', 'failed', 'cancelled', 'interrupted'):
        return {'ok': False, 'message': '이미 종료된 작업입니다.'}
    _cancel_requested.add(job_id)
    return {'ok': True, 'message': '취소를 요청했습니다. 현재 단계가 끝나면 중단됩니다.'}


@app.get('/jobs/{job_id}/file')
def download(job_id: str, format: str = 'glb'):
    fmt = format.lower()
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(
            400, f"지원하지 않는 형식: {format} (지원: {', '.join(SUPPORTED_FORMATS)})")
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None or not job.get('output_file'):
        raise HTTPException(404, '결과 파일이 없습니다.')
    out = _convert_output(job_id, Path(job['output_file']), fmt)
    ext = 'obj.zip' if fmt == 'obj' else fmt
    return FileResponse(out, media_type=_FORMAT_MEDIA_TYPES[fmt],
                        filename=f'trellis_{job_id}.{ext}')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=7861, log_level='info')
