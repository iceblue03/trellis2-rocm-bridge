bl_info = {
    "name": "TRELLIS.2 Bridge",
    "author": "TRELLIS.2 ROCm bridge",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > TRELLIS",
    "description": "Generate 3D meshes with a local TRELLIS.2 server and import them",
    "category": "3D View",
}

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, IntProperty, StringProperty)
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup

TIMEOUT = 15  # seconds; every network call is bounded so the UI can never wedge


# --------------------------------------------------------------------------- #
# HTTP helpers - all run on worker threads, never on Blender's UI thread
# --------------------------------------------------------------------------- #
def _base_url(context):
    prefs = context.preferences.addons[__name__].preferences
    return prefs.server_url.rstrip("/")


def _get_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_multipart(url, fields, file_field, file_path, timeout=120):
    """Minimal multipart/form-data POST (no external deps in Blender's Python)."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()

    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        payload = f.read()
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="{file_field}"; '
             f'filename="{filename}"\r\n').encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += payload + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url, data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _download(url, dest, timeout=300):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    return dest


def _err_text(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail")
            if detail:
                return f"HTTP {e.code}: {detail}"
        except Exception:
            pass
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"서버에 연결할 수 없습니다: {e.reason}"
    return f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# state (module-level; Blender props can't hold arbitrary python objects)
# --------------------------------------------------------------------------- #
class Bridge:
    health = None
    health_error = None
    jobs = []
    active_job = None
    last_message = ""
    last_error = ""
    busy = False
    starting = False


STATUS_KO = {
    "queued": "대기 중",
    "running": "생성 중",
    "done": "완료",
    "failed": "실패",
    "cancelled": "취소됨",
    "interrupted": "중단됨",
}
STATUS_ICON = {
    "queued": "SORTTIME",
    "running": "PLAY",
    "done": "CHECKMARK",
    "failed": "ERROR",
    "cancelled": "CANCEL",
    "interrupted": "ERROR",
}


def _fmt_secs(s):
    try:
        s = int(round(float(s)))
    except (TypeError, ValueError):
        return "-"
    return f"{s//60}분 {s%60}초" if s >= 60 else f"{s}초"


# --------------------------------------------------------------------------- #
# preferences
# --------------------------------------------------------------------------- #
class TRELLIS_Prefs(AddonPreferences):
    bl_idname = __name__

    server_url: StringProperty(
        name="서버 주소",
        description="TRELLIS.2 브릿지 서버 URL",
        default="http://127.0.0.1:7861",
    )
    auto_import: BoolProperty(
        name="완료 시 자동 임포트",
        description="생성이 끝나면 GLB를 씬에 자동으로 불러옵니다",
        default=True,
    )
    wsl_distro: StringProperty(
        name="WSL 배포판",
        description="서버가 설치된 WSL 배포판 이름",
        default="Ubuntu-24.04",
    )
    start_script: StringProperty(
        name="시작 스크립트",
        description="WSL 안에서 실행할 서버 시작 스크립트 경로",
        default="/root/TRELLIS.2_rocm/start_server.sh",
    )

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "server_url")
        col.prop(self, "auto_import")
        col.separator()
        col.label(text="서버 자동 실행 (Windows + WSL)")
        col.prop(self, "wsl_distro")
        col.prop(self, "start_script")


# --------------------------------------------------------------------------- #
# scene properties
# --------------------------------------------------------------------------- #
class TRELLIS_Props(PropertyGroup):
    image_path: StringProperty(
        name="이미지",
        description="입력 이미지 (알파 채널이 있는 PNG 권장)",
        subtype="FILE_PATH",
    )
    pipeline_type: EnumProperty(
        name="해상도",
        items=[
            ("512", "512³ (권장)", "약 11분, 피크 4GB"),
            ("1024_cascade", "1024³ 캐스케이드", "훨씬 느리고 메모리 요구가 큽니다"),
        ],
        default="512",
    )
    seed: IntProperty(name="시드", default=42, min=0)
    texture_size: EnumProperty(
        name="텍스처 크기",
        items=[("1024", "1024", ""), ("2048", "2048", ""), ("4096", "4096", "")],
        default="2048",
    )


# --------------------------------------------------------------------------- #
# operators
# --------------------------------------------------------------------------- #
class TRELLIS_OT_refresh(Operator):
    bl_idname = "trellis.refresh"
    bl_label = "새로고침"
    bl_description = "서버 상태와 작업 내역을 다시 불러옵니다"

    def execute(self, context):
        base = _base_url(context)

        def work():
            try:
                Bridge.health = _get_json(f"{base}/health")
                Bridge.health_error = None
            except Exception as e:
                Bridge.health = None
                Bridge.health_error = _err_text(e)
            try:
                Bridge.jobs = _get_json(f"{base}/jobs?limit=12").get("jobs", [])
            except Exception:
                pass
            _tag_redraw()

        threading.Thread(target=work, daemon=True).start()
        return {"FINISHED"}


class TRELLIS_OT_start_server(Operator):
    bl_idname = "trellis.start_server"
    bl_label = "서버 시작"
    bl_description = ("WSL에서 TRELLIS.2 서버를 실행하고 준비될 때까지 기다립니다 "
                      "(모델 로딩에 약 3분 소요)")

    _timer = None
    _t0 = 0.0
    _launched = False
    _launch_error = None
    _base = ""

    @classmethod
    def poll(cls, context):
        return not Bridge.starting

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        if os.name != "nt":
            self.report({"ERROR"}, "자동 실행은 Windows의 Blender에서만 지원됩니다.")
            return {"CANCELLED"}

        self._base = _base_url(context)
        self._t0 = time.time()
        self._launched = False
        self._launch_error = None
        Bridge.starting = True
        Bridge.last_error = ""
        Bridge.last_message = "WSL에서 서버를 실행하는 중..."

        distro = prefs.wsl_distro
        script = prefs.start_script

        def launch():
            try:
                # Detached, no console window; the script itself is idempotent and
                # leaves an already-running server alone.
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                subprocess.Popen(
                    ["wsl.exe", "-d", distro, "-u", "root", "--", "bash", script],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, creationflags=flags,
                )
            except FileNotFoundError:
                self._launch_error = "wsl.exe를 찾을 수 없습니다. WSL이 설치되어 있는지 확인하세요."
            except Exception as e:
                self._launch_error = f"{type(e).__name__}: {e}"
            finally:
                self._launched = True
                _tag_redraw()

        threading.Thread(target=launch, daemon=True).start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(2.0, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if not self._launched:
            return {"RUNNING_MODAL"}
        if self._launch_error:
            Bridge.last_error = self._launch_error
            Bridge.last_message = ""
            self.report({"ERROR"}, self._launch_error)
            return self._finish(context)

        elapsed = time.time() - self._t0
        try:
            h = _get_json(f"{self._base}/health", timeout=8)
            Bridge.health = h
            Bridge.health_error = None
            st = h.get("model_status")
            if st == "ready":
                Bridge.last_message = f"서버 준비 완료 ({int(elapsed)}초)"
                bpy.ops.trellis.refresh()
                return self._finish(context)
            if st == "failed":
                Bridge.last_error = f"모델 로딩 실패: {h.get('model_error')}"
                return self._finish(context)
            Bridge.last_message = f"모델 로딩 중... {int(elapsed)}초 경과 (약 170초 소요)"
        except Exception:
            Bridge.last_message = f"서버 기동 대기 중... {int(elapsed)}초"

        _tag_redraw()
        if elapsed > 420:
            Bridge.last_error = "서버가 시간 내에 준비되지 않았습니다. WSL 로그를 확인하세요."
            return self._finish(context)
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None
        Bridge.starting = False
        _tag_redraw()
        return {"FINISHED"}


class TRELLIS_OT_stop_server(Operator):
    bl_idname = "trellis.stop_server"
    bl_label = "서버 중지"
    bl_description = "WSL에서 실행 중인 TRELLIS.2 서버를 종료합니다"

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        if os.name != "nt":
            self.report({"ERROR"}, "Windows의 Blender에서만 지원됩니다.")
            return {"CANCELLED"}

        h = Bridge.health or {}
        if h.get("current_job"):
            self.report({"ERROR"}, "작업이 실행 중입니다. 완료 후 다시 시도하세요.")
            return {"CANCELLED"}

        distro = prefs.wsl_distro

        def work():
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(
                    ["wsl.exe", "-d", distro, "-u", "root", "--",
                     "pkill", "-f", "trellis_server.py"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags, timeout=30,
                )
                Bridge.health = None
                Bridge.health_error = "서버가 중지되었습니다."
                Bridge.last_message = "서버를 중지했습니다."
            except Exception as e:
                Bridge.last_error = _err_text(e)
            _tag_redraw()

        threading.Thread(target=work, daemon=True).start()
        return {"FINISHED"}


class TRELLIS_OT_generate(Operator):
    bl_idname = "trellis.generate"
    bl_label = "생성 시작"
    bl_description = "서버에 생성 작업을 제출하고 진행 상황을 추적합니다"

    _timer = None
    _job_id = None
    _base = ""
    _submit_error = None
    _submitted = False

    @classmethod
    def poll(cls, context):
        return not Bridge.busy

    def execute(self, context):
        props = context.scene.trellis_props
        path = bpy.path.abspath(props.image_path or "")
        if not path or not os.path.isfile(path):
            self.report({"ERROR"}, "입력 이미지를 선택하세요.")
            return {"CANCELLED"}

        self._base = _base_url(context)
        self._submit_error = None
        self._submitted = False
        self._job_id = None
        Bridge.busy = True
        Bridge.last_error = ""
        Bridge.last_message = "작업 제출 중..."

        fields = {
            "pipeline_type": props.pipeline_type,
            "seed": str(props.seed),
            "texture_size": props.texture_size,
            "decimation_target": "1000000",
        }
        base = self._base

        def submit():
            try:
                res = _post_multipart(f"{base}/generate", fields, "image", path)
                self._job_id = res.get("job_id")
                Bridge.last_message = f"작업 {self._job_id} 제출됨"
            except Exception as e:
                self._submit_error = _err_text(e)
            finally:
                self._submitted = True
                _tag_redraw()

        threading.Thread(target=submit, daemon=True).start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(1.5, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        if not self._submitted:
            return {"RUNNING_MODAL"}

        if self._submit_error:
            Bridge.last_error = self._submit_error
            Bridge.last_message = ""
            self.report({"ERROR"}, self._submit_error)
            return self._finish(context)

        if not self._job_id:
            Bridge.last_error = "서버가 작업 ID를 반환하지 않았습니다."
            return self._finish(context)

        try:
            job = _get_json(f"{self._base}/jobs/{self._job_id}", timeout=10)
        except Exception as e:
            # A transient poll failure should not kill the job; keep waiting and
            # show the reason so the user is not left guessing.
            Bridge.last_message = f"상태 조회 재시도 중 ({_err_text(e)})"
            _tag_redraw()
            return {"RUNNING_MODAL"}

        Bridge.active_job = job
        status = job.get("status")
        _tag_redraw()

        if status in ("queued", "running"):
            return {"RUNNING_MODAL"}

        if status == "done":
            Bridge.last_message = (
                f"완료 · {_fmt_secs(job.get('elapsed_seconds'))} · "
                f"피크 {job.get('peak_gib', '?')} GiB")
            prefs = context.preferences.addons[__name__].preferences
            if prefs.auto_import:
                try:
                    dest = os.path.join(tempfile.gettempdir(),
                                        f"trellis_{self._job_id}.glb")
                    _download(f"{self._base}/jobs/{self._job_id}/file", dest)
                    bpy.ops.import_scene.gltf(filepath=dest)
                    Bridge.last_message += " · 씬에 임포트됨"
                except Exception as e:
                    Bridge.last_error = f"임포트 실패: {_err_text(e)}"
                    self.report({"ERROR"}, Bridge.last_error)
        else:
            Bridge.last_error = f"{STATUS_KO.get(status, status)}: {job.get('error') or ''}"
            self.report({"WARNING"}, Bridge.last_error)

        bpy.ops.trellis.refresh()
        return self._finish(context)

    def _finish(self, context):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None
        Bridge.busy = False
        Bridge.active_job = None
        _tag_redraw()
        return {"FINISHED"}


class TRELLIS_OT_cancel(Operator):
    bl_idname = "trellis.cancel"
    bl_label = "취소"
    bl_description = "실행 중인 작업의 취소를 요청합니다"

    job_id: StringProperty()

    def execute(self, context):
        base = _base_url(context)
        job_id = self.job_id or (Bridge.active_job or {}).get("id")
        if not job_id:
            return {"CANCELLED"}

        def work():
            try:
                req = urllib.request.Request(f"{base}/jobs/{job_id}/cancel", method="POST")
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    Bridge.last_message = json.loads(r.read().decode()).get("message", "")
            except Exception as e:
                Bridge.last_error = _err_text(e)
            _tag_redraw()

        threading.Thread(target=work, daemon=True).start()
        return {"FINISHED"}


class TRELLIS_OT_import_job(Operator):
    bl_idname = "trellis.import_job"
    bl_label = "임포트"
    bl_description = "이 작업의 결과를 씬에 불러옵니다"

    job_id: StringProperty()

    def execute(self, context):
        base = _base_url(context)
        try:
            dest = os.path.join(tempfile.gettempdir(), f"trellis_{self.job_id}.glb")
            _download(f"{base}/jobs/{self.job_id}/file", dest)
            bpy.ops.import_scene.gltf(filepath=dest)
            self.report({"INFO"}, "임포트 완료")
        except Exception as e:
            self.report({"ERROR"}, _err_text(e))
            return {"CANCELLED"}
        return {"FINISHED"}


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def _tag_redraw():
    try:
        for w in bpy.context.window_manager.windows:
            for area in w.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass


class TRELLIS_PT_main(Panel):
    bl_label = "TRELLIS.2"
    bl_idname = "TRELLIS_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TRELLIS"

    def draw(self, context):
        layout = self.layout
        props = context.scene.trellis_props

        # --- server status -------------------------------------------------
        box = layout.box()
        row = box.row(align=True)
        row.label(text="서버", icon="WORLD")
        row.operator("trellis.refresh", text="", icon="FILE_REFRESH")

        if Bridge.starting:
            box.label(text="서버 시작 중...", icon="SORTTIME")
        elif Bridge.health_error:
            box.label(text=Bridge.health_error[:60], icon="ERROR")
            box.operator("trellis.start_server", icon="PLAY")
        elif Bridge.health is None:
            box.label(text="상태 미확인", icon="QUESTION")
            row = box.row(align=True)
            row.operator("trellis.refresh", text="상태 확인", icon="FILE_REFRESH")
            row.operator("trellis.start_server", text="서버 시작", icon="PLAY")
        else:
            h = Bridge.health
            st = h.get("model_status")
            if st == "ready":
                box.label(text=f"준비됨 (로딩 {h.get('load_seconds')}초)", icon="CHECKMARK")
            elif st == "loading":
                box.label(text="모델 로딩 중... (약 170초)", icon="SORTTIME")
            elif st == "failed":
                box.label(text="모델 로딩 실패", icon="ERROR")
                box.label(text=str(h.get("model_error"))[:80])
                box.operator("trellis.start_server", text="다시 시작", icon="FILE_REFRESH")
            else:
                box.label(text=f"상태: {st}", icon="INFO")
            box.label(text=f"여유 메모리 {h.get('free_gib')} GiB "
                           f"(최소 {h.get('min_free_gib')} GiB)")
            if st == "ready" and not h.get("current_job"):
                box.operator("trellis.stop_server", icon="QUIT")

        # --- input ---------------------------------------------------------
        box = layout.box()
        box.label(text="입력", icon="IMAGE_DATA")
        box.prop(props, "image_path", text="")
        box.prop(props, "pipeline_type", text="")
        row = box.row(align=True)
        row.prop(props, "seed")
        row.prop(props, "texture_size", text="")
        box.label(text="알파 채널이 있는 PNG를 권장합니다", icon="INFO")

        # --- run -----------------------------------------------------------
        job = Bridge.active_job
        if Bridge.busy and job:
            box = layout.box()
            box.label(text=f"{STATUS_KO.get(job.get('status'), '')}", icon="PLAY")
            box.label(text=f"단계: {job.get('stage_label') or '-'}")
            prog = float(job.get("progress") or 0.0)
            box.progress(factor=prog, text=f"{int(prog*100)}%") if hasattr(box, "progress") \
                else box.label(text=f"진행률 {int(prog*100)}%")
            box.label(text=f"경과 {_fmt_secs(job.get('elapsed_seconds'))}"
                           f" · 남음 약 {_fmt_secs(job.get('eta_seconds'))}")
            op = box.operator("trellis.cancel", icon="CANCEL")
            op.job_id = job.get("id", "")
        else:
            layout.operator("trellis.generate", icon="PLAY", text="생성 시작")

        if Bridge.last_message:
            layout.label(text=Bridge.last_message, icon="INFO")
        if Bridge.last_error:
            layout.label(text=Bridge.last_error[:90], icon="ERROR")


class TRELLIS_PT_history(Panel):
    bl_label = "작업 내역"
    bl_idname = "TRELLIS_PT_history"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TRELLIS"
    bl_parent_id = "TRELLIS_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        if not Bridge.jobs:
            layout.label(text="내역이 없습니다 — 새로고침을 누르세요")
            return

        for job in Bridge.jobs:
            status = job.get("status")
            box = layout.box()
            row = box.row(align=True)
            row.label(text=STATUS_KO.get(status, status),
                      icon=STATUS_ICON.get(status, "DOT"))
            row.label(text=f"{job.get('pipeline_type')}³")

            name = job.get("image_name") or "-"
            box.label(text=name[:34])
            box.label(text=f"{_fmt_secs(job.get('elapsed_seconds'))}"
                           + (f" · {round((job.get('output_bytes') or 0)/1048576, 1)}MB"
                              if job.get("output_bytes") else ""))
            if job.get("peak_gib"):
                box.label(text=f"피크 {job['peak_gib']} GiB")
            if job.get("error"):
                box.label(text=str(job["error"])[:70], icon="ERROR")
            if status == "done":
                op = box.operator("trellis.import_job", icon="IMPORT")
                op.job_id = job["id"]


CLASSES = (
    TRELLIS_Prefs,
    TRELLIS_Props,
    TRELLIS_OT_refresh,
    TRELLIS_OT_start_server,
    TRELLIS_OT_stop_server,
    TRELLIS_OT_generate,
    TRELLIS_OT_cancel,
    TRELLIS_OT_import_job,
    TRELLIS_PT_main,
    TRELLIS_PT_history,
)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.trellis_props = bpy.props.PointerProperty(type=TRELLIS_Props)


def unregister():
    del bpy.types.Scene.trellis_props
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
