*Read this in other languages: [한국어](README.ko.md).*

# TRELLIS.2 ROCm Bridge

A local HTTP job-queue server that turns a single image into a textured 3D mesh
using **TRELLIS.2**, running entirely on an **AMD Ryzen AI 9 integrated GPU** via
ROCm inside WSL2 — no cloud API, no discrete GPU required.

POST an image, poll a job ID, download the mesh (GLB by default; OBJ/PLY/STL
also available, see [Output formats](#output-formats-and-other-ways-to-use-it)
below). The [Blender addon](https://github.com/iceblue03/trellis2-blender-addon)
is the reference client, but any HTTP client can drive it — including the
built-in web UI at `/` for a quick one-off generation with no client at all.

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
a FastAPI job-queue server instead of a manual CLI script. See
[Verified working](#verified-working-2026-09-09) below for the machine this was
built and tested on.

## Architecture

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

`trellis_server.py` exposes `/` (a small built-in web UI, see below), `/docs`
(interactive OpenAPI/Swagger docs, provided automatically by FastAPI), `/health`,
`/generate` (multipart image upload), `/jobs`, `/jobs/{id}`,
`/jobs/{id}/file` (mesh download, see [Output formats](#output-formats-and-other-ways-to-use-it)
below), and `/jobs/{id}/cancel`. Jobs are persisted to disk so a crash or
restart leaves a visible record instead of a silently vanished job.

## Output formats and other ways to use it

`/generate` always produces the pipeline's native **GLB** internally — that
part is unchanged. What's new is `/jobs/{id}/file` now takes an optional
`?format=` query parameter to convert that GLB to another common mesh format
on download, converted once and cached:

| `format=` | Contents | Notes |
|---|---|---|
| `glb` (default) | full textured mesh, single file | unchanged — existing clients (Blender addon, curl scripts) that don't pass `format` keep getting exactly what they got before |
| `obj` | `.zip` containing `model.obj` + `.mtl` + texture image(s) | most universally-supported interchange format; zipped because OBJ's texture reference is multiple files, and one HTTP response can only carry one file |
| `ply` | single file, geometry (+ vertex color where applicable) | no UV texture atlas |
| `stl` | single file, geometry only | no color/texture at all |

```bash
curl -O -J "http://127.0.0.1:7861/jobs/<job_id>/file?format=obj"
```

Conversion happens with [`trimesh`](https://trimesh.org/) at download time —
it's not a generation-time dependency, so a missing `trimesh` install only
makes non-GLB formats return `501` (see [AGENTS.md](AGENTS.md)); GLB downloads
and generation itself are unaffected either way.

Besides the Blender addon and raw `curl`, the server itself now serves two
other ways to drive it, both built on the exact same endpoints above —
nothing below is special-cased UI-only API surface:

- **A minimal built-in web UI** at `http://127.0.0.1:7861/` — upload an
  image, pick a resolution/format, watch progress, download the result.
  Useful for a quick one-off generation without curl or Blender.
- **Interactive API docs** at `http://127.0.0.1:7861/docs` — FastAPI's
  auto-generated OpenAPI/Swagger UI, letting any HTTP/OpenAPI-aware tool
  (Postman, Blender's own HTTP client, a future non-Blender integration)
  introspect and call the API without reading this README.

## Requirements

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
- A client to actually use it — e.g. the
  [Blender addon](https://github.com/iceblue03/trellis2-blender-addon), `curl`,
  or the server's own built-in web UI (nothing extra to install for that one)
- (Optional) [`trimesh`](https://trimesh.org/) (`pip install trimesh`) in the
  `trellis2-gfx1150` conda env, only if you want `/jobs/{id}/file?format=` to
  offer OBJ/PLY/STL — GLB downloads work without it

## Repo layout

| File | What it is |
|---|---|
| [`trellis_server.py`](trellis_server.py) | FastAPI bridge server: job queue, `/health`, `/generate`, mesh export (GLB/OBJ/PLY/STL), and the built-in `/` web UI |
| [`start_server.sh`](start_server.sh) | Launches the server detached; idempotent (leaves an already-running server alone) |
| [`run_trellis.sh`](run_trellis.sh) / [`run_inference.py`](run_inference.py) | One-shot pipeline run with no server, for manual testing/debugging |
| [`profile_run.py`](profile_run.py) / [`profile_lv0.json`](profile_lv0.json) | Per-stage timing instrumentation + a captured run, used to tune `low_vram`/memory settings |

Everything here is **mirrored** from WSL — the canonical copy that actually runs
is `/root/TRELLIS.2_rocm/<same filename>` inside the `Ubuntu-24.04` distro. If you
change one of these files, edit it in WSL first, then re-copy it into this repo —
otherwise this repo silently drifts out of sync with what's deployed. See
[AGENTS.md](AGENTS.md) for the full rationale and the exact commands.

The TRELLIS.2 model/pipeline code itself (`trellis2/`, `app.py`, `assets/`,
`configs/`, `data_toolkit/`, `train.py`, its own `setup.sh`/`conda-env.yaml`) is
**not** in this repo — it's the vendored ROCm fork, in its own git repo at
`https://github.com/Cardboard-box-a/TRELLIS.2_rocm.git` (branch `rocm`), itself
built on [Lamothe/TRELLIS.2_rocm](https://github.com/Lamothe/TRELLIS.2_rocm) and
upstream [microsoft/TRELLIS.2](https://microsoft.github.io/TRELLIS.2/). This repo
is only the bridge layer on top of it. `server_data/` (job logs, uploads, outputs)
is runtime state and is likewise never copied here.

## Setup

1. In WSL (`Ubuntu-24.04`), clone `Cardboard-box-a/TRELLIS.2_rocm` to
   `/root/TRELLIS.2_rocm` and build the `trellis2-gfx1150` conda env per its
   `setup.sh` (see [AGENTS.md](AGENTS.md) for the exact flags and a from-scratch
   walkthrough).
2. Copy this repo's `trellis_server.py` and `start_server.sh` into
   `/root/TRELLIS.2_rocm/` (they expect to run from there, with the conda env
   active).
3. Start it: `wsl.exe -d Ubuntu-24.04 -u root -- bash -lc 'bash /root/TRELLIS.2_rocm/start_server.sh'`
4. Confirm it's up: `curl http://127.0.0.1:7861/health` should show
   `"model_status":"ready"` within ~170s.
5. Point a client at `http://127.0.0.1:7861` — e.g. install the
   [Blender addon](https://github.com/iceblue03/trellis2-blender-addon), whose
   `server_url` preference defaults to exactly that, or just open
   `http://127.0.0.1:7861/` in a browser for the built-in UI.
6. (Optional) `pip install trimesh` in the same conda env if you want
   `?format=obj|ply|stl` downloads in addition to GLB — no restart-required
   config, just make it importable before the first non-GLB download.

## Known limitations / loose ends

- On this machine the iGPU (Ryzen AI 9 HX PRO 375, gfx1150) shares system RAM with
  the CPU (UMA), so `trellis_server.py` hardcodes `low_vram=True` and serializes
  jobs to one at a time — see the module docstring in that file for why.
- 1024³ cascade generation is unverified end-to-end on this hardware; 512³ is the
  tested/recommended path.
- OBJ/PLY/STL conversion and the `/` web UI were written and exercised against
  synthetic test meshes (see AGENTS.md), not against a real TRELLIS.2 output on
  the WSL/ROCm machine — re-verify with an actual `/generate` result before
  relying on them.

## Verified working (2026-09-09)

Ran `start_server.sh` in WSL exactly as a client's "start server" action would
(`wsl.exe -d Ubuntu-24.04 -u root -- bash start_server.sh`): server came up, model
finished loading in ~127s, and `/health` reported `"model_status":"ready"`. Bridge
server ↔ WSL ↔ ROCm path confirmed working end-to-end on an AMD Ryzen AI 9 HX PRO
375 (gfx1150). A full image→GLB `/generate` job was not re-run in this pass — that
path was already exercised via the Blender addon (see its repo for the reference
output produced).

## Credits / provenance

- [microsoft/TRELLIS.2](https://microsoft.github.io/TRELLIS.2/) — the original
  model and pipeline.
- [Lamothe/TRELLIS.2_rocm](https://github.com/Lamothe/TRELLIS.2_rocm) and
  [Cardboard-box-a/TRELLIS.2_rocm](https://github.com/Cardboard-box-a/TRELLIS.2_rocm)
  — the ROCm port this bridge runs on top of.
- Split out of `trellis2-blender-bridge`, which originally bundled this server
  with a Blender-specific client, so the server can stand on its own and be
  driven by any HTTP client. The Blender addon now lives at
  [iceblue03/trellis2-blender-addon](https://github.com/iceblue03/trellis2-blender-addon).

## License

[MIT-based, with an attribution + issue-reporting notice](LICENSE) — use it for
anything, just credit the repo and report bugs/improvements if you find them.
Covers the bridge server and orchestration scripts only; the TRELLIS.2 fork it
talks to has its own license.
