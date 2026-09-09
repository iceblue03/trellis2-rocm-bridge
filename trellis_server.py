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
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

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
# API
# --------------------------------------------------------------------------- #
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
def download(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None or not job.get('output_file'):
        raise HTTPException(404, '결과 파일이 없습니다.')
    return FileResponse(job['output_file'], media_type='model/gltf-binary',
                        filename=f'trellis_{job_id}.glb')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=7861, log_level='info')
