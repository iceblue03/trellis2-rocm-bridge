# TRELLIS.2 Blender Bridge

A Blender addon that turns a single image into a textured 3D mesh using **TRELLIS.2**,
generated entirely locally on an **AMD Ryzen AI 9 integrated GPU** via ROCm inside
WSL2 — no cloud API, no discrete GPU required.

Click a button in Blender's sidebar → the addon boots a local generation server in
WSL → the mesh lands in your scene as a GLB a few minutes later.

## Why this exists

TRELLIS.2-on-ROCm already exists as community work — but every port we could find
targets a **discrete** RDNA3 card (7800 XT, 7900 XTX). Ryzen AI 9's **gfx1150 iGPU**
is a different problem:

- No dedicated VRAM — the GPU shares system RAM with the CPU (UMA), so a naive port
  that keeps all 16GB of weights "on GPU" pushes the WSL2 VM past its memory limit
  and gets a host process killed.
- AMD's WSL2 ROCm path for Strix/Strix Halo APUs (`librocdxg`, the `/dev/dxg`
  bridge) only became officially supported in ROCm 7.2.1 — it's brand new and still
  has [open bugs](https://github.com/ROCm/ROCm/issues/6022) around VRAM mapping.
- PyTorch's HIP allocator behaves differently under WSL's DXG paravirtualization:
  `expandable_segments:True` (the normal recommendation for fragmentation) throws
  `hipErrorInvalidValue` here and has to stay off.

This repo is the fix for that specific combination — `low_vram` forced on, jobs
serialized to one at a time, the allocator/env-var workarounds for WSL — wrapped in
a FastAPI job-queue server and a one-click Blender UI instead of a manual CLI
script. See [Verified working](#verified-working-2026-09-09) below for the machine
this was built and tested on.

## Architecture

```
Blender (Windows)                        WSL2 "Ubuntu-24.04"
┌───────────────────────┐   HTTP        ┌───────────────────────────────┐
│ trellis_bridge_        │   :7861       │ /root/TRELLIS.2_rocm          │
│ addon.py               │ ─────────────►│  conda env: trellis2-gfx1150  │
│ (View3D > N > TRELLIS) │◄──────────────│  trellis_server.py (FastAPI)  │
└───────────────────────┘   GLB + JSON   │  → TRELLIS.2 pipeline (HIP)   │
                                          │  GPU: gfx1150 iGPU, UMA RAM   │
                                          └───────────────────────────────┘
```

The addon launches/stops the server via `wsl.exe`, polls `/health` and `/jobs/*`,
and downloads the finished GLB straight into the Blender scene. No manual steps
between "pick an image" and "mesh in viewport" once the server is up.

## Requirements

- **Windows 11** + Blender 3.6+
- **WSL2**, distro `Ubuntu-24.04`, with
  [ROCm on Ryzen (WSL)](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installryz/wsl/howto_wsl.html)
  7.2.1+ set up (this is what provides the `/dev/dxg` GPU bridge)
- An **AMD Ryzen AI 9** or other gfx1150/Strix Point/Strix Halo APU
- A conda env (`trellis2-gfx1150`) with TRELLIS.2 + its ROCm-specific extensions
  (flash-attn, FlexGEMM, o-voxel, nvdiffrast-hip) built per
  [`Cardboard-box-a/TRELLIS.2_rocm`](https://github.com/Cardboard-box-a/TRELLIS.2_rocm)'s
  `setup.sh`
- A Hugging Face account with access to `microsoft/TRELLIS.2-4B` and the gated
  `facebook/dinov3-vitl16-pretrain-lvd1689m` (required — the pipeline load fails
  without DINOv3 access)

## Repo layout

| File | What it is | Lives |
|---|---|---|
| [`trellis_bridge_addon.py`](trellis_bridge_addon.py) | The Blender addon (UI, job polling, GLB import) | Windows only — this *is* the addon |
| `sample_512.glb` | A real 512³ generation, kept locally as a reference | Windows only, **not tracked in git** (kept out to keep clones small — regenerate any time via the addon) |
| [`trellis_server.py`](trellis_server.py) | FastAPI bridge server: job queue, `/health`, `/generate`, GLB export | **Mirrored** — canonical copy runs in WSL |
| [`start_server.sh`](start_server.sh) | Launches the server detached; idempotent | **Mirrored** |
| [`run_trellis.sh`](run_trellis.sh) / [`run_inference.py`](run_inference.py) | One-shot pipeline run with no server, for manual testing | **Mirrored** |
| [`profile_run.py`](profile_run.py) / [`profile_lv0.json`](profile_lv0.json) | Per-stage timing instrumentation + a captured run | **Mirrored** |
| `trellis2/`, `app.py`, `assets/`, `configs/`, `data_toolkit/`, `train.py`, `setup.sh`, `conda-env.yaml` | The vendored TRELLIS.2 ROCm fork itself | **WSL only** — own git repo, not duplicated here |
| `server_data/` (jobs, uploads, outputs, logs) | Runtime state | **WSL only** — not source, never copied here |

**Source of truth for the six "Mirrored" files is WSL**
(`/root/TRELLIS.2_rocm/`), since that's what the running server actually executes.
If you change `trellis_server.py` (etc.), edit it in WSL first, then re-copy it into
this repo — otherwise this repo silently drifts out of sync with what's deployed.

The vendored fork lives in its own git repo
(`https://github.com/Cardboard-box-a/TRELLIS.2_rocm.git`, branch `rocm`), itself
built on [Lamothe/TRELLIS.2_rocm](https://github.com/Lamothe/TRELLIS.2_rocm) and
upstream [microsoft/TRELLIS.2](https://microsoft.github.io/TRELLIS.2/). This repo
does not vendor or duplicate that code — only the bridge layer on top of it.

## Setup

1. **WSL side** — inside `Ubuntu-24.04`, with `Cardboard-box-a/TRELLIS.2_rocm`
   cloned to `/root/TRELLIS.2_rocm` and the `trellis2-gfx1150` conda env built per
   its `setup.sh --basic --flash-attn --flexgemm --o-voxel --nvdiffrast` (ROCm
   flavor), copy this repo's `trellis_server.py` and `start_server.sh` into that
   directory (they expect to run from `/root/TRELLIS.2_rocm`, with the conda env
   active).
2. **Blender side** — install [`trellis_bridge_addon.py`](trellis_bridge_addon.py)
   via Edit > Preferences > Add-ons > Install.
3. In the addon preferences, confirm the defaults match your setup:
   - `server_url`: `http://127.0.0.1:7861`
   - `wsl_distro`: `Ubuntu-24.04`
   - `start_script`: `/root/TRELLIS.2_rocm/start_server.sh`

## Using the addon

Open the `TRELLIS` tab in the View3D sidebar (`N` panel).

- **서버 시작 (Start server)** launches the WSL server via `wsl.exe` if it isn't
  already running, and polls `/health` until the model finishes loading (~170s).
- Pick an input image (PNG with alpha recommended), resolution (512³ recommended,
  ~11 min / 4GB peak; 1024³ cascade is much slower/heavier), seed, texture size.
- **생성 시작 (Generate)** submits the job, polls progress, and auto-imports the GLB
  into the scene on completion (toggle via `auto_import` in preferences).
- The 작업 내역 (job history) panel lists recent jobs with re-import/cancel actions.

## Known limitations / loose ends

- On this machine the iGPU (Ryzen AI 9 HX PRO 375, gfx1150) shares system RAM with
  the CPU (UMA), so `trellis_server.py` hardcodes `low_vram=True` and serializes
  jobs to one at a time — see the module docstring in that file for why.
- 1024³ cascade generation is unverified end-to-end on this hardware; 512³ is the
  tested/recommended path.

## Verified working (2026-09-09)

Ran `start_server.sh` in WSL exactly as the addon's "서버 시작" button would
(`wsl.exe -d Ubuntu-24.04 -u root -- bash start_server.sh`): server came up, model
finished loading in ~127s, and `/health` reported `"model_status":"ready"`. Bridge
server ↔ WSL ↔ ROCm path confirmed working end-to-end on an AMD Ryzen AI 9 HX PRO
375 (gfx1150). A full image→GLB `/generate` job was not re-run in this pass — that
path was already exercised to produce `sample_512.glb` and the jobs recorded in
`server_data/outputs`.

## Credits / provenance

- [microsoft/TRELLIS.2](https://microsoft.github.io/TRELLIS.2/) — the original
  model and pipeline.
- [Lamothe/TRELLIS.2_rocm](https://github.com/Lamothe/TRELLIS.2_rocm) and
  [Cardboard-box-a/TRELLIS.2_rocm](https://github.com/Cardboard-box-a/TRELLIS.2_rocm)
  — the ROCm port this bridge runs on top of.
- Moved out of `C:\Users\andre\LLM` (which was a mixed dump of TRELLIS/Blender,
  FastFlowLM, Ryzen AI, and llama.cpp files) into its own project folder so it
  doesn't get lost among unrelated LLM model/runtime files.

## License

[MIT-based, with an attribution + issue-reporting notice](LICENSE) — use it for
anything, just credit the repo and report bugs/improvements if you find them.
Covers the addon and bridge scripts only; the TRELLIS.2 fork it talks to has its
own license.
