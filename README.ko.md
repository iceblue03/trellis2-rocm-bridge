*다른 언어로 보기: [English](README.md).*

# TRELLIS.2 ROCm Bridge

한 장의 이미지를 텍스처가 입혀진 3D 메시로 변환하는 로컬 HTTP 작업 큐 서버입니다.
**TRELLIS.2**를 사용하며, **AMD Ryzen AI 9 내장 GPU**에서 WSL2를 통한 ROCm만으로
전부 동작합니다 — 클라우드 API도, 별도의 디스크리트 GPU도 필요 없습니다.

이미지를 POST하고, job ID로 상태를 폴링하고, 메시를 다운로드합니다(기본값은 GLB이며
OBJ/PLY/STL도 지원합니다. 아래 [출력 형식](#출력-형식과-그-외-사용-방법) 참고).
[Blender addon](https://github.com/iceblue03/trellis2-blender-addon)이 레퍼런스
클라이언트이지만, HTTP를 말할 수 있는 클라이언트라면 무엇이든 이 서버를 구동할 수
있습니다 — 별도 클라이언트 없이 바로 쓸 수 있는, 빠른 1회성 생성을 위한 내장 웹
UI(`/`)도 포함해서요.

## 이 저장소가 맞는지 확인하기

이름에 "ROCm"이 들어있다고 해서 "아무 AMD GPU"를 뜻하지는 않으며, TRELLIS.2용
ROCm 포팅이 이것 하나만 있는 것도 아닙니다. 설치하기 전에 자신의 하드웨어를
먼저 확인하세요:

| 내 하드웨어 | 대신 이걸 쓰세요 |
|---|---|
| **NVIDIA GPU** | CUDA로 [microsoft/TRELLIS.2](https://microsoft.github.io/TRELLIS.2/)를 바로 사용하세요. 이 저장소의 WSL/iGPU 우회책은 전혀 필요 없습니다. |
| **디스크리트 AMD GPU** (RX 7700/7800/7900, RX 9060/9070, Radeon PRO W7000 시리즈 — RDNA3/RDNA4) | [Cardboard-box-a/TRELLIS.2_rocm](https://github.com/Cardboard-box-a/TRELLIS.2_rocm)을 바로 쓰세요. 이 브리지가 기반으로 삼는 파이프라인 저장소이고, 바로 그런 종류의 카드(RX 9070 XT)에서 테스트되었습니다. WSL2도, UMA/`low_vram` 우회책도, 이 브리지 자체도 필요 없습니다. |
| **Windows에서 AMD Ryzen AI 9 iGPU (Radeon 890M/880M, gfx1150, "Strix Point")** | ✅ 바로 이 저장소가 맞습니다. |
| **그 외 Ryzen AI iGPU** — Radeon 780M(gfx1103, Phoenix/Hawk Point) 또는 Radeon 8050S/8060S(gfx1151/1152/1153, "Strix Halo") | 이 프로젝트에서 검증되지 않았습니다. 동작한다고 가정하기 전에 아래 [하드웨어 지원 현황](#하드웨어-지원-현황-이슈를-올리기-전에-읽어주세요)을 먼저 읽으세요. |
| **네이티브 Linux**(WSL 아님), 아무 AMD APU | 이 저장소의 Windows/WSL 관련 부분은 필요 없습니다. [저희가 포크한 파이프라인 저장소](https://github.com/iceblue03/TRELLIS.2_rocm)를 빌드하고 `trellis_server.py`를 직접 실행하세요 — Blender addon의 `wsl.exe` 실행 관련 부분은 건너뛰면 됩니다. |

## 왜 이 프로젝트가 존재하는가

ROCm 위에서 동작하는 TRELLIS.2 포팅은 이미 커뮤니티에 존재합니다 — 하지만 우리가
찾은 모든 포팅은 **디스크리트** RDNA3 카드(7800 XT, 7900 XTX)를 대상으로 합니다.
Ryzen AI 9의 **gfx1150 iGPU**는 전혀 다른 문제입니다:

- 전용 VRAM이 없습니다 — GPU가 CPU와 시스템 RAM을 공유하므로(UMA), 16GB 가중치를
  전부 "GPU에" 상주시키는 순진한 포팅은 WSL2 VM의 메모리 한도를 넘겨 호스트 프로세스가
  죽는 결과를 낳습니다.
- Strix/Strix Halo APU용 AMD의 WSL2 ROCm 경로(`librocdxg`, `/dev/dxg` 브리지)는
  ROCm 7.2.1에서야 공식 지원되기 시작했습니다 — 아직 신생 기능이라 VRAM 매핑 관련
  [알려진 버그](https://github.com/ROCm/ROCm/issues/6022)가 있습니다.
- PyTorch의 HIP 할당자는 WSL의 DXG 반가상화 환경에서 다르게 동작합니다: 단편화에
  대한 일반적인 권장 설정인 `expandable_segments:True`가 여기서는
  `hipErrorInvalidValue`를 던지므로 꺼둬야 합니다.

이 저장소는 바로 이 조합을 위한 해결책입니다 — `low_vram` 강제 활성화, 작업을
한 번에 하나씩 순차 처리, WSL 전용 할당자/환경변수 우회책을 수동 CLI 스크립트
대신 FastAPI 작업 큐 서버로 감쌌습니다. 이 조합을 실제로 빌드·테스트한 머신에
대해서는 아래 [동작 확인됨](#동작-확인됨-2026-09-09)을 참고하세요.

## 아키텍처

```
Any HTTP client                          WSL2 "Ubuntu-24.04"
(Blender addon, curl,          HTTP     ┌───────────────────────────────┐
 your own script, ...)  :7861  ───────► │ /root/TRELLIS.2_rocm          │
                                         │  conda env: trellis2-gfx1150  │
                         ◄────────────  │  trellis_server.py (FastAPI)  │
                       mesh file + JSON  │  → TRELLIS.2 pipeline (HIP)   │
                                         │  GPU: gfx1150 iGPU, UMA RAM   │
                                         └───────────────────────────────┘
```

`trellis_server.py`는 `/`(간단한 내장 웹 UI, 아래 참고), `/docs`(FastAPI가 자동
제공하는 대화형 OpenAPI/Swagger 문서), `/health`, `/generate`(multipart 이미지
업로드), `/jobs`, `/jobs/{id}`, `/jobs/{id}/file`(메시 다운로드, 아래
[출력 형식](#출력-형식과-그-외-사용-방법) 참고), `/jobs/{id}/cancel`을 제공합니다.
작업 상태는 디스크에 저장되므로, 서버가 죽거나 재시작되어도 조용히 사라지는 대신
눈에 보이는 기록이 남습니다.

## 출력 형식과 그 외 사용 방법

`/generate`는 내부적으로 항상 파이프라인의 네이티브 포맷인 **GLB**를 생성합니다 —
이 부분은 그대로입니다. 새로 추가된 것은 `/jobs/{id}/file`이 이제 선택적
`?format=` 쿼리 파라미터를 받아, 다운로드 시점에 그 GLB를 다른 범용 메시 형식으로
변환(1회 변환 후 캐싱)해 준다는 점입니다:

| `format=` | 내용 | 비고 |
|---|---|---|
| `glb` (기본값) | 텍스처 포함 전체 메시, 단일 파일 | 변경 없음 — `format`을 전달하지 않는 기존 클라이언트(Blender addon, curl 스크립트)는 이전과 완전히 동일한 결과를 받습니다 |
| `obj` | `model.obj` + `.mtl` + 텍스처 이미지를 담은 `.zip` | 가장 범용적으로 지원되는 교환 형식. OBJ의 텍스처 참조는 여러 파일로 나뉘고 HTTP 응답 하나에는 파일 하나만 담을 수 있어 zip으로 묶었습니다 |
| `ply` | 단일 파일, 지오메트리(+가능한 경우 vertex color) | UV 텍스처 아틀라스 없음 |
| `stl` | 단일 파일, 지오메트리만 | 색상/텍스처 전혀 없음 |

```bash
curl -O -J "http://127.0.0.1:7861/jobs/<job_id>/file?format=obj"
```

변환은 다운로드 시점에 [`trimesh`](https://trimesh.org/)로 이루어집니다 — 생성
단계의 의존성이 아니므로, `trimesh`가 설치되어 있지 않으면 GLB가 아닌 형식만
`501`을 반환합니다(자세한 내용은 [AGENTS.md](AGENTS.md) 참고). GLB 다운로드와
생성 자체는 어느 쪽이든 영향을 받지 않습니다.

Blender addon과 순수 `curl` 외에도, 이제 서버 자체가 두 가지 사용 방법을 더
제공합니다. 둘 다 위와 완전히 동일한 엔드포인트 위에서 동작하며, UI 전용으로
특별 취급되는 API 표면은 전혀 없습니다:

- **간단한 내장 웹 UI** (`http://127.0.0.1:7861/`) — 이미지를 업로드하고,
  해상도/형식을 고르고, 진행 상황을 지켜보고, 결과를 다운로드합니다. curl이나
  Blender 없이 빠르게 한 번 생성해볼 때 유용합니다.
- **대화형 API 문서** (`http://127.0.0.1:7861/docs`) — FastAPI가 자동 생성하는
  OpenAPI/Swagger UI로, Postman이나 Blender 자체 HTTP 클라이언트, 앞으로 추가될
  Blender 외 통합 등 HTTP/OpenAPI를 다룰 수 있는 어떤 도구든 이 README를 읽지
  않고도 API를 살펴보고 호출할 수 있습니다.

## 요구 사항

- **WSL2**, `Ubuntu-24.04` 배포판, [ROCm on Ryzen (WSL)](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installryz/wsl/howto_wsl.html)
  7.2.1 이상 설치(`/dev/dxg` GPU 브리지를 제공하는 버전)
- **AMD Ryzen AI 9** 또는 다른 gfx1150/Strix Point/Strix Halo APU
- TRELLIS.2와 그 ROCm 전용 확장(flash-attn, FlexGEMM, o-voxel, nvdiffrast-hip)이
  [`iceblue03/TRELLIS.2_rocm`](https://github.com/iceblue03/TRELLIS.2_rocm)
  (gfx1150 빌드 타깃이 이미 적용된 저희 자체 포크)의 `setup.sh`대로 빌드된
  conda 환경(`trellis2-gfx1150`) — 왜 더 이상
  [`Cardboard-box-a/TRELLIS.2_rocm`](https://github.com/Cardboard-box-a/TRELLIS.2_rocm)을
  직접 쓰지 않는지는 [저장소 구성](#저장소-구성) 참고
- `microsoft/TRELLIS.2-4B`와 gated 모델 `facebook/dinov3-vitl16-pretrain-lvd1689m`에
  접근 권한이 있는 Hugging Face 계정 (필수 — DINOv3 접근 권한이 없으면 파이프라인
  로딩 자체가 실패합니다)
- 실제로 사용할 클라이언트 — 예: [Blender addon](https://github.com/iceblue03/trellis2-blender-addon),
  `curl`, 또는 서버 자체 내장 웹 UI(이 경우 추가 설치가 전혀 필요 없습니다)
- (선택) `trellis2-gfx1150` conda 환경에 [`trimesh`](https://trimesh.org/)
  (`pip install trimesh`) — `/jobs/{id}/file?format=`으로 OBJ/PLY/STL을 받고
  싶을 때만 필요하며, GLB 다운로드는 이것 없이도 동작합니다

## 저장소 구성

| 파일 | 설명 |
|---|---|
| [`trellis_server.py`](trellis_server.py) | FastAPI 브리지 서버: 작업 큐, `/health`, `/generate`, 메시 내보내기(GLB/OBJ/PLY/STL), 내장 `/` 웹 UI |
| [`start_server.sh`](start_server.sh) | 서버를 분리(detached) 실행; 멱등적(이미 실행 중이면 그대로 둠) |
| [`run_trellis.sh`](run_trellis.sh) / [`run_inference.py`](run_inference.py) | 서버 없이 파이프라인을 한 번 실행하는 수동 테스트/디버깅용 스크립트 |
| [`profile_run.py`](profile_run.py) / [`profile_lv0.json`](profile_lv0.json) | 단계별 타이밍 계측 + 캡처된 실행 결과, `low_vram`/메모리 설정 튜닝에 사용 |

여기 있는 파일들은 전부 WSL에서 **미러링**된 것입니다 — 실제로 실행되는 원본은
`Ubuntu-24.04` 배포판 안의 `/root/TRELLIS.2_rocm/<동일한 파일명>`입니다. 이 중
하나를 수정한다면, WSL 쪽을 먼저 고치고 그 다음 이 저장소로 다시 복사하세요 —
그렇지 않으면 이 저장소가 실제 배포본과 조용히 어긋나게 됩니다. 전체 근거와 정확한
명령어는 [AGENTS.md](AGENTS.md)를 참고하세요.

TRELLIS.2 모델/파이프라인 코드 자체(`trellis2/`, `app.py`, `assets/`, `configs/`,
`data_toolkit/`, `train.py`, 그 자체의 `setup.sh`/`conda-env.yaml`)는 이 저장소에
**없습니다** — [**iceblue03/TRELLIS.2_rocm**](https://github.com/iceblue03/TRELLIS.2_rocm)
(`rocm` 브랜치), 즉 저희가 직접 포크한 ROCm 포트에 있습니다. 원래는
[Cardboard-box-a/TRELLIS.2_rocm](https://github.com/Cardboard-box-a/TRELLIS.2_rocm)
(이는 [Lamothe/TRELLIS.2_rocm](https://github.com/Lamothe/TRELLIS.2_rocm)과 상위
[microsoft/TRELLIS.2](https://microsoft.github.io/TRELLIS.2/) 위에 만들어짐)를
바로 가리켰지만, 실제 gfx1150 빌드 수정(`setup.sh`의 `GPU_ARCHS=gfx1150` 한 줄)이
서버를 실행하던 그 머신 하나에만 커밋되지 않은 채로 존재했고 다른 곳엔 사본이
전혀 없었기 때문에 포크했습니다. 완전히 분리된 사본이 아니라 정식 GitHub 포크로
유지하는 이유는, Cardboard-box-a나 상위 microsoft/TRELLIS.2 쪽에서 나오는 향후
수정 사항을 계속 가져올 수 있게 하기 위해서입니다 — 이건 유일하게 중요한 그 패치가
어디 있는지를 고친 것이지, 모델/파이프라인 코드를 독자적으로 유지하겠다는 게
아닙니다. 이 저장소(`trellis2-rocm-bridge`)는 그 위에 얹힌 브리지 레이어일
뿐입니다. `server_data/`(작업 로그, 업로드, 출력물)는 런타임 상태이며 마찬가지로
이 저장소에 절대 복사되지 않습니다.

## 설치

1. WSL(`Ubuntu-24.04`)에서 [`iceblue03/TRELLIS.2_rocm`](https://github.com/iceblue03/TRELLIS.2_rocm)
   (저희 포크 — `GPU_ARCHS=gfx1150` 빌드 수정이 이미 커밋되어 있음)을
   `/root/TRELLIS.2_rocm`에 클론하고, 그 저장소의 `setup.sh`를 따라
   `trellis2-gfx1150` conda 환경을 빌드합니다(정확한 플래그와 처음부터 진행하는
   절차는 [AGENTS.md](AGENTS.md) 참고).
2. 이 저장소의 `trellis_server.py`와 `start_server.sh`를 `/root/TRELLIS.2_rocm/`로
   복사합니다(두 파일 모두 conda 환경이 활성화된 상태로 그 위치에서 실행되는 것을
   전제로 합니다).
3. 실행: `wsl.exe -d Ubuntu-24.04 -u root -- bash -lc 'bash /root/TRELLIS.2_rocm/start_server.sh'`
4. 정상 기동 확인: `curl http://127.0.0.1:7861/health`를 실행하면 ~170초 이내에
   `"model_status":"ready"`가 표시되어야 합니다.
5. `http://127.0.0.1:7861`을 클라이언트에서 사용합니다 — 예를 들어
   [Blender addon](https://github.com/iceblue03/trellis2-blender-addon)을 설치하면
   `server_url` 기본값이 정확히 이 주소이고, 아니면 브라우저에서
   `http://127.0.0.1:7861/`을 열어 내장 UI를 바로 사용할 수도 있습니다.
6. (선택) GLB 외에 `?format=obj|ply|stl` 다운로드도 쓰고 싶다면 같은 conda
   환경에 `pip install trimesh`를 실행하세요 — 재시작 없이, 첫 GLB 외 형식
   다운로드 전까지만 import 가능하면 됩니다.

## 알려진 한계 / 남은 과제

- 이 머신의 iGPU(Ryzen AI 9 HX PRO 375, gfx1150)는 CPU와 시스템 RAM을
  공유하므로(UMA), `trellis_server.py`는 `low_vram=True`를 하드코딩하고 작업을
  한 번에 하나씩 순차 처리합니다 — 이유는 해당 파일의 모듈 docstring 참고.
- 1024³ 캐스케이드 생성은 이 하드웨어에서 엔드투엔드로 검증되지 않았습니다;
  512³가 테스트를 마친 권장 경로입니다.
- OBJ/PLY/STL 변환과 `/` 웹 UI는 합성 테스트 메시로 작성·검증되었을 뿐(AGENTS.md
  참고), WSL/ROCm 머신에서 실제 TRELLIS.2 출력으로 검증되지는 않았습니다 —
  실제 `/generate` 결과로 다시 확인한 뒤 신뢰하세요.

## 하드웨어 지원 현황 (이슈를 올리기 전에 읽어주세요)

이 중 어느 것도 AMD로부터 공식 지원받는 조합이 아닙니다, 어떤 OS에서든요.
ROCm 7.2.1 기준
[공식 WSL2 지원 매트릭스](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/compatibility/compatibilityrad/wsl/wsl_compatibility.html)에는
디스크리트 카드(RX 7700 XT~9070 XT, W7000 시리즈)만 올라가 있고, gfx1150을
포함해 Ryzen AI iGPU는 어디에도 없습니다. gfx1150 지원 자체가
[AMD 자체 플랫폼 매트릭스에 추가해달라는 오픈 기능 요청](https://github.com/ROCm/TheRock/issues/8186)
상태이지, 공식 문서화된 타깃이 아닙니다. 이 저장소의 모든 것은 AMD 툴체인이
아직 공식적으로 인정하지 않는 아키텍처를 대상으로 빌드한 결과물이지, 지원되는
경로를 따른 게 아닙니다.

그럼에도 [저희 포크의 `setup.sh`](https://github.com/iceblue03/TRELLIS.2_rocm/blob/rocm/setup.sh)에
있는 `GPU_ARCHS=gfx1150`은 바로 이 칩에서, WSL2 위에서 실제로 컴파일되고
동작합니다. 같은 트릭이 다른 Ryzen AI iGPU에도 통하는지는 **검증되지
않았습니다 — 아래 어느 것도 테스트하지 않았습니다**:

- **Radeon 8050S/8060S** (gfx1151/1152/1153, "Strix Halo", 예: Ryzen AI Max
  385/390/395) — gfx1150과 같은 RDNA3.5 계열로, 한 세대 위 칩입니다. 위 포크의
  `setup.sh`에서 `GPU_ARCHS=gfx1150`을 해당 코드로 바꿔보는 게 가장 먼저
  시도해볼 만하고, 지금 동작하는 것과 가장 가까운 조합이지만, 이 브리지에서
  실제로 됐다고 보고된 적은 없습니다.
- **Radeon 780M** (gfx1103, Phoenix/Hawk Point) — gfx1150/1151에 있는
  매트릭스 코어 하드웨어가 없는 더 오래된 RDNA3 iGPU입니다. `GPU_ARCHS`만
  바꿔서 그대로 될 가능성은 더 낮고, 커뮤니티에서 보고된 대안은 네이티브
  재빌드가 아니라 `HSA_OVERRIDE_GFX_VERSION`으로, 이 저장소의 방식과는
  성격이 다르고 더 불안정한 우회책입니다.

이 중 하나를 시도해서 결과가 나오면(되든 안 되든) 이슈를 올려주세요. 이
저장소의 라이선스가 요청하는 바로 그런 종류의 제보이고, 이 표가 시간이
지나며 더 정확해질 수 있는 유일한 방법입니다.

## 동작 확인됨 (2026-09-09)

클라이언트의 "서버 시작" 동작과 똑같이 WSL에서 `start_server.sh`를 실행했습니다
(`wsl.exe -d Ubuntu-24.04 -u root -- bash start_server.sh`): 서버가 기동되었고,
모델은 약 127초 만에 로딩을 마쳤으며, `/health`는 `"model_status":"ready"`를
보고했습니다. 브리지 서버 ↔ WSL ↔ ROCm 경로가 AMD Ryzen AI 9 HX PRO 375(gfx1150)에서
엔드투엔드로 동작함을 확인했습니다. 이번 확인에서 실제 이미지→GLB `/generate` 작업을
다시 실행하지는 않았습니다 — 그 경로는 이미 Blender addon을 통해 검증되었습니다
(참고용 산출물은 해당 저장소 참고).

## 크레딧 / 출처

- [microsoft/TRELLIS.2](https://microsoft.github.io/TRELLIS.2/) — 원본 모델과
  파이프라인.
- [Lamothe/TRELLIS.2_rocm](https://github.com/Lamothe/TRELLIS.2_rocm)과
  [Cardboard-box-a/TRELLIS.2_rocm](https://github.com/Cardboard-box-a/TRELLIS.2_rocm)
  — 이 브리지가 기반으로 삼는 ROCm 포트. 저희는 자체 포크
  [iceblue03/TRELLIS.2_rocm](https://github.com/iceblue03/TRELLIS.2_rocm)을
  유지하며, gfx1150 빌드 타깃과 gated되지 않은 배경 제거 폴백을 그 위에
  커밋해뒀습니다. PyTorch, ROCm 자체, TRELLIS.2 모델 가중치는 여전히 설치
  시점에 각자의 실제 upstream에서 받아오며, 이 저장소에 벤더링되지
  않습니다.
- 원래 이 서버와 Blender 전용 클라이언트를 함께 묶고 있던 `trellis2-blender-bridge`에서
  분리되어, 서버가 독립적으로 존재하며 어떤 HTTP 클라이언트로도 구동될 수 있게
  되었습니다. Blender addon은 이제
  [iceblue03/trellis2-blender-addon](https://github.com/iceblue03/trellis2-blender-addon)에
  있습니다.

## 라이선스

[MIT 기반, 출처 표기 + 이슈 제보 조건 포함](LICENSE) — 무엇에든 사용해도 좋으며,
저장소를 credit하고 버그/개선점을 발견하면 제보해 주세요. 브리지 서버와 오케스트레이션
스크립트에만 적용되며, 이 서버가 통신하는 TRELLIS.2 포크는 자체 라이선스를 따릅니다.
