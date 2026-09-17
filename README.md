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

## Is this the repo you want?

"ROCm" in the name doesn't mean "any AMD GPU," and this isn't the only TRELLIS.2
ROCm port. Find your hardware before installing anything:

| Your hardware | Use this instead |
|---|---|
| **NVIDIA GPU** | [microsoft/TRELLIS.2](https://microsoft.github.io/TRELLIS.2/) directly, with CUDA. None of this repo's WSL/iGPU workarounds apply to you. |
| **Discrete AMD GPU** (RX 7700/7800/7900, RX 9060/9070, Radeon PRO W7000-series — RDNA3/RDNA4) | [Cardboard-box-a/TRELLIS.2_rocm](https://github.com/Cardboard-box-a/TRELLIS.2_rocm) directly. That's the pipeline repo this bridge is built on, and it's tested on exactly that class of card (RX 9070 XT). You don't need WSL2, the UMA/`low_vram` workarounds, or this bridge at all. |
| **AMD Ryzen AI 9 iGPU (Radeon 890M/880M, gfx1150, "Strix Point") on Windows** | ✅ This repo is for you. |
| **Any other Ryzen AI iGPU** — Radeon 780M (gfx1103, Phoenix/Hawk Point) or Radeon 8050S/8060S (gfx1151/1152/1153, "Strix Halo") | Untested by this project. Read [Hardware support status](#hardware-support-status-read-this-before-opening-an-issue) below before assuming it works. |
| **Native Linux**, any AMD APU (no WSL) | You don't need the Windows/WSL parts of this repo. Build [our fork of the pipeline](https://github.com/iceblue03/TRELLIS.2_rocm) and run `trellis_server.py` directly; skip the `wsl.exe`-launching bits in the Blender addon. |

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
  [`iceblue03/TRELLIS.2_rocm`](https://github.com/iceblue03/TRELLIS.2_rocm)'s
  `setup.sh` — our own fork with the gfx1150 build target already applied (see
  [Repo layout](#repo-layout) for why it's not
  [`Cardboard-box-a/TRELLIS.2_rocm`](https://github.com/Cardboard-box-a/TRELLIS.2_rocm)
  directly anymore)
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
**not** in this repo — it lives in
[**iceblue03/TRELLIS.2_rocm**](https://github.com/iceblue03/TRELLIS.2_rocm)
(branch `rocm`), our own fork of the ROCm port. It used to point straight at
[Cardboard-box-a/TRELLIS.2_rocm](https://github.com/Cardboard-box-a/TRELLIS.2_rocm)
(itself built on [Lamothe/TRELLIS.2_rocm](https://github.com/Lamothe/TRELLIS.2_rocm)
and upstream [microsoft/TRELLIS.2](https://microsoft.github.io/TRELLIS.2/)) —
we forked it because the actual gfx1150 build fix (`GPU_ARCHS=gfx1150` in
`setup.sh`, one line) existed only as an uncommitted local edit on the one
machine running the server, with no copy anywhere else. It's a real GitHub
fork, not a hard copy, specifically so future fixes from Cardboard-box-a or
upstream microsoft/TRELLIS.2 can still be pulled in later — this only fixes
where the one load-bearing patch lives, it doesn't try to go it alone on the
model/pipeline code. This repo (`trellis2-rocm-bridge`) is only the bridge
layer on top of it. `server_data/` (job logs, uploads, outputs) is runtime
state and is likewise never copied here.

## Setup

1. In WSL (`Ubuntu-24.04`), clone
   [`iceblue03/TRELLIS.2_rocm`](https://github.com/iceblue03/TRELLIS.2_rocm)
   (our fork — already has the `GPU_ARCHS=gfx1150` build fix committed) to
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

## Hardware support status (read this before opening an issue)

**This section changed since it was first written — AMD's own support matrix
moved while this repo stood still.** As of ROCm 7.2.1 (what this repo was
built against), AMD's WSL2 support matrix listed only discrete cards, and
gfx1150 support was an open feature request, not a shipped target. As of
**ROCm 10.0.0** (current as of this writing), AMD's
[compatibility matrix](https://rocm.docs.amd.com/en/latest/compatibility/compatibility-matrix.html)
now lists **gfx1103, gfx1150, gfx1151, gfx1152, and gfx1153 — every current
Ryzen AI iGPU — as officially supported**, and its
[Windows support matrix](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/compatibility/compatibilityryz/windows/windows_compatibility.html)
lists PyTorch 2.9.1 on gfx1150/gfx1151 working on **native Windows** (no WSL2)
as of ROCm 7.2.1 already — with the caveat that "the entire ROCm stack is not
yet supported on Windows," only specific components. We have not tested
either of these newer, more-official paths; everything this repo actually
does is still the WSL2 + ROCm 7.2.1 + manual `GPU_ARCHS` route. If AMD's
native-Windows path matures enough to run TRELLIS.2's HIP extensions
(flash-attn, o-voxel, FlexGEMM) without WSL2 at all, that would obsolete a
good chunk of this repo's reason to exist — that's a real possibility, not
hypothetical, and worth watching rather than ignoring.

None of that changes what's actually verified here: `GPU_ARCHS=gfx1150` in
[our fork's `setup.sh`](https://github.com/iceblue03/TRELLIS.2_rocm/blob/rocm/setup.sh)
compiles and runs, on this specific chip, under WSL2, today. Whether the same
trick extends to other Ryzen AI iGPUs is **unverified — we have not tested
any of the following**:

- **Radeon 8050S/8060S** (gfx1151/1152/1153, "Strix Halo," e.g. Ryzen AI Max
  385/390/395) — same RDNA3.5 family as gfx1150, one architecture generation
  up, and now the *most credible* untested case: 
  [kyuz0/amd-strix-halo-toolboxes](https://github.com/kyuz0/amd-strix-halo-toolboxes)
  already has a **working, benchmarked TRELLIS.2 port for gfx1151** (~95s at
  512³, confirmed real mesh output) — but as a native-Linux Podman/Distrobox
  container, not WSL2, and with no Blender/Windows integration. If you have
  this chip, that repo's [PR #71](https://github.com/kyuz0/amd-strix-halo-toolboxes/pull/71)
  is worth reading before you try adapting this bridge — it documents four
  gfx1151-specific numerical bugs (MIOpen fp16 NaN, FlexGEMM precision,
  BiRefNet fp32/fp16 mismatch, a CuMesh HIP crash) that swapping `GPU_ARCHS`
  alone will not fix.
- **Radeon 780M** (gfx1103, Phoenix/Hawk Point) — an older RDNA3 iGPU without
  the matrix-core hardware gfx1150/1151 have. A straight `GPU_ARCHS` swap is
  less likely to just work; community effort here (e.g.
  [likelovewant/ROCmLibs-for-gfx1103-AMD780M-APU](https://github.com/likelovewant/ROCmLibs-for-gfx1103-AMD780M-APU),
  895 stars) mostly centers on prebuilt libraries and
  `HSA_OVERRIDE_GFX_VERSION` rather than a native rebuild like this repo does.

If you try any of these and get a result — working or not — please open an
issue, ideally using the [quickstart below](#try-this-on-your-hardware--we-need-testers).
That's the specific kind of report this repo's license asks for, and it's the
only way this table gets more accurate over time.

## Try this on your hardware — we need testers

Everything in [Verified working](#verified-working-2026-09-09) below is real,
not aspirational — but it's exactly one chip. We don't own a 780M or a Strix
Halo machine, so we can't extend that table ourselves. This is a call for
testers, written so it can be handed to an AI coding agent (Claude Code,
Cursor, Copilot, etc.) as a single self-contained task rather than followed
by hand.

**One thing an agent can't do for you first**: get a Hugging Face account
with access to the gated `facebook/dinov3-vitl16-pretrain-lvd1689m` model
(request access on its model page, approval isn't instant) and run
`huggingface-cli login` once. Everything after that can run unattended.

Wall-clock time is dominated by GPU kernel compilation (roughly 15-30 min for
flash-attn alone) and a ~15GB model download, not by decision-making — an
agent following this shouldn't need to stop and ask what to do next at any
point before step 7.

1. **Find your gfx code** — the only thing that changes per machine:
   ```bash
   wsl.exe -d Ubuntu-24.04 -u root -- bash -lc 'rocminfo | grep -i gfx | head -1'
   ```
   Expect `gfx1103`, `gfx1150`, `gfx1151`, `gfx1152`, or `gfx1153`. If this
   errors or prints nothing, ROCm/WSL2 itself isn't set up — stop and follow
   [Requirements](#requirements) first; that's a separate, already-solved
   problem, not what this test is about.

2. **Clone our fork and point the build at your gfx code** (skip the `sed`
   if step 1 printed `gfx1150`):
   ```bash
   wsl.exe -d Ubuntu-24.04 -u root -- bash -lc '
     cd /root && git clone -b rocm https://github.com/iceblue03/TRELLIS.2_rocm.git TRELLIS.2_rocm
     cd TRELLIS.2_rocm && sed -i "s/GPU_ARCHS=gfx1150/GPU_ARCHS=<your gfx code>/" setup.sh
   '
   ```

3. **Build the conda env** (the slow step — flash-attn compiles from
   source):
   ```bash
   wsl.exe -d Ubuntu-24.04 -u root -- bash -lc '
     source /root/miniconda3/etc/profile.d/conda.sh
     conda env create -f /root/TRELLIS.2_rocm/conda-env.yaml -n trellis2-gfx1150
     conda activate trellis2-gfx1150
     pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/rocm6.2.4
     cd /root/TRELLIS.2_rocm
     export FLASH_ATTENTION_TRITON_AMD_ENABLE=TRUE
     bash setup.sh --basic --flash-attn --flexgemm --o-voxel --nvdiffrast
   '
   ```
   If an extension fails to compile, that failure *is* the result — save the
   last ~50 lines of output for the issue.

4. **Copy in the bridge server and start it**:
   ```bash
   wsl.exe -d Ubuntu-24.04 -u root -- bash -lc '
     curl -sL https://raw.githubusercontent.com/iceblue03/trellis2-rocm-bridge/master/trellis_server.py -o /root/TRELLIS.2_rocm/trellis_server.py
     curl -sL https://raw.githubusercontent.com/iceblue03/trellis2-rocm-bridge/master/start_server.sh -o /root/TRELLIS.2_rocm/start_server.sh
     bash /root/TRELLIS.2_rocm/start_server.sh
   '
   ```

5. **Poll `/health` until it settles** (expect `ready` within ~5 min on
   first run; `failed` means step 3/4 broke something — check
   `server_data/server.log` in `/root/TRELLIS.2_rocm/`):
   ```bash
   wsl.exe -d Ubuntu-24.04 -u root -- bash -lc 'sleep 30 && curl -s http://127.0.0.1:7861/health'
   ```

6. **Run one real generation**, not just a model-load check, with any RGBA
   PNG:
   ```bash
   curl -F "image=@/path/to/any/rgba.png" http://127.0.0.1:7861/generate
   curl http://127.0.0.1:7861/jobs/<job_id_from_above>
   ```

7. **Open an issue** at
   [iceblue03/trellis2-rocm-bridge/issues](https://github.com/iceblue03/trellis2-rocm-bridge/issues/new)
   with: your exact chip and gfx code, which step (if any) failed and its
   last ~50 lines of output, or — if it worked — the elapsed time and peak
   memory from the `/jobs/{id}` response. Native Linux instead of WSL2? Say
   so; that's untested here too and just as useful to know.

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
  — the ROCm port this bridge is built on. We maintain our own fork,
  [iceblue03/TRELLIS.2_rocm](https://github.com/iceblue03/TRELLIS.2_rocm), with
  the gfx1150 build target and an ungated background-removal fallback
  committed on top. PyTorch, ROCm itself, and the TRELLIS.2 model weights are
  still pulled from their real upstreams at install time, not vendored.
- Split out of `trellis2-blender-bridge`, which originally bundled this server
  with a Blender-specific client, so the server can stand on its own and be
  driven by any HTTP client. The Blender addon now lives at
  [iceblue03/trellis2-blender-addon](https://github.com/iceblue03/trellis2-blender-addon).

## License

[MIT-based, with an attribution + issue-reporting notice](LICENSE) — use it for
anything, just credit the repo and report bugs/improvements if you find them.
Covers the bridge server and orchestration scripts only; the TRELLIS.2 fork it
talks to has its own license.
