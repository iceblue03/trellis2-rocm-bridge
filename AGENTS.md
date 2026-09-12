# AGENTS.md

Guidance for AI coding agents (Claude Code, Cursor, Codex, etc.) working in this
repo. Read this before editing anything — the code that actually runs lives on a
different machine than this repo, and that split is not optional.

## What this repo is

The HTTP bridge server for a local TRELLIS.2 image-to-3D generation pipeline: a
job queue on top of TRELLIS.2, running inside WSL2 on an AMD Ryzen AI 9 iGPU
(gfx1150) via ROCm. It's client-agnostic — the
[Blender addon](https://github.com/iceblue03/trellis2-blender-addon) is the
reference client, but anything that speaks HTTP can drive it, including the
server's own built-in web UI at `/`. See [README.md](README.md) /
[README.ko.md](README.ko.md) for the full picture, architecture diagram, and
why this hardware combination needed custom work in the first place.

## README is bilingual — keep both in sync

[README.md](README.md) (English) is canonical; [README.ko.md](README.ko.md)
(한국어) is a parallel full translation. GitHub does **not** pick a README by
the viewer's browser/account language — it always renders `README.md` on the
repo homepage regardless of who's looking, so `README.ko.md` is only reachable
by direct link. Both files carry a one-line language-switcher at the very top
linking to the other, which is the whole mechanism.

If you change one, change the other in the same commit — a change to setup
steps, endpoints, requirements, or anything else user-facing that only lands
in one language is a bug, not a partial fix. If you can't produce a faithful
Korean translation yourself, say so explicitly rather than leaving
`README.ko.md` silently stale.

## The one invariant you must not break

**This repo and the WSL install are two different filesystems that happen to
share six filenames.** `trellis_server.py`, `start_server.sh`, `run_trellis.sh`,
`run_inference.py`, `profile_run.py`, and `profile_lv0.json` here are **mirrors**
— the copy that actually runs is at `/root/TRELLIS.2_rocm/<same name>` inside the
`Ubuntu-24.04` WSL distro, not the one in this repo.

- If you edit one of those files here, it does **nothing** until you also copy it
  into WSL.
- If you edit one of those files in WSL to test something, **copy it back here**
  afterward or this repo silently goes stale.
- Never assume "I edited the file in the repo" means "the running server changed."
  Always state which side you edited and whether the other side needs syncing.

This is also why the built-in web UI (served at `/`) is a Python string
(`_INDEX_HTML`) inside `trellis_server.py` rather than its own `static/`
file: a second file would mean a seventh filename to remember to mirror.
Keep it that way — don't split it out just for tidiness.

Everything else that makes TRELLIS.2 actually work (`trellis2/` pipeline source,
`app.py`, `assets/`, `configs/`, `data_toolkit/`, `train.py`, the fork's own
`setup.sh`/`conda-env.yaml`) lives *only* in WSL, in its own git repo
(`Cardboard-box-a/TRELLIS.2_rocm`, branch `rocm`). Do not copy it into this repo —
that's a deliberate scope boundary, not an oversight. If a task requires touching
that code, edit it in place inside WSL and say so; don't vendor it here.

`server_data/` (job logs, uploads, generated GLBs, `jobs.json`) is runtime state,
not source. Never commit it, never copy it into this repo.

## Environment access

The WSL side is reached via `wsl.exe` from Windows. Three gotchas discovered while
building this:

1. **Run commands as root**: `wsl.exe -d Ubuntu-24.04 -u root -- bash -lc '...'`.
   Without `-u root` you'll get permission errors under `/root/TRELLIS.2_rocm`.
2. **Wrap everything in `bash -lc '...'`, never pass a bare path as an argument.**
   `wsl.exe -d Ubuntu-24.04 -- bash /root/foo.sh` gets mangled by MSYS/Git-Bash path
   auto-conversion into a Windows path (`C:/Program Files/Git/root/foo.sh`) before
   `wsl.exe` ever sees it. Always do
   `wsl.exe -d Ubuntu-24.04 -u root -- bash -lc 'bash /root/foo.sh'` instead.
3. **Escape `$` as `\$` inside the `-lc` string** if you need the *inner* (WSL)
   shell to expand a variable — e.g. `bash -lc 'x=5; echo "\$x"'`. The command
   passes through an outer shell first, which will otherwise expand `$x` (to
   empty) before `wsl.exe` even runs. This bit us on both a loop counter and a
   `DEST` path variable during initial setup — assignments silently evaluated to
   empty with no error.

## Getting a working environment from scratch

1. In WSL (`Ubuntu-24.04`), clone `https://github.com/Cardboard-box-a/TRELLIS.2_rocm.git`
   (branch `rocm`) to `/root/TRELLIS.2_rocm`.
2. Build the `trellis2-gfx1150` conda env and run that repo's `setup.sh` with the
   ROCm-relevant flags (`--basic --flash-attn --flexgemm --o-voxel --nvdiffrast`;
   see its `--help`). Install ROCm PyTorch first
   (`torch==2.6.0`/`torchvision==0.21.0`, `--index-url .../rocm6.2.4`).
3. `huggingface-cli login`, and make sure the account has been granted access to
   the gated `facebook/dinov3-vitl16-pretrain-lvd1689m` — the pipeline load fails
   without it (see `run_trellis.sh`'s pre-flight check for this exact failure mode).
4. Copy this repo's `trellis_server.py` and `start_server.sh` into
   `/root/TRELLIS.2_rocm/` (they assume they run from there, with the conda env
   active).
5. Start it and confirm it's healthy (see "Validating a change" below), then point
   a client at `http://127.0.0.1:7861` — e.g. install the
   [Blender addon](https://github.com/iceblue03/trellis2-blender-addon), or open
   `http://127.0.0.1:7861/` for the built-in web UI.
6. (Optional) `pip install trimesh` in the same conda env to enable
   `/jobs/{id}/file?format=obj|ply|stl` — GLB downloads work without it.

## Validating a change

Don't assume — check.

```bash
wsl.exe -d Ubuntu-24.04 -u root -- bash -lc 'bash /root/TRELLIS.2_rocm/start_server.sh'
# then poll:
wsl.exe -d Ubuntu-24.04 -u root -- bash -lc 'curl -s http://127.0.0.1:7861/health'
```

Expect `model_status` to move `loading` → `ready` in ~170s (~127s measured on the
reference machine). `"model_status":"failed"` with a `model_error` field means the
pipeline load itself broke — check `server_data/server.log` in WSL, not this repo.

A full `/generate` job (POST an image, poll `/jobs/<id>`, GET
`/jobs/<id>/file`) takes ~11 minutes at 512³ and ~4GB peak — expensive enough that
you shouldn't run it speculatively. Prefer the health check above to confirm the
bridge itself works; only run a full generation when the actual pipeline behavior
is what's being changed.

### Validating format conversion / the web UI without a GPU

`/jobs/{id}/file?format=obj|ply|stl` and `/` (the web UI) are regular FastAPI
code with no GPU or WSL dependency of their own — `_convert_output()` just
re-reads an already-exported `.glb` with `trimesh`. That means you can exercise
them anywhere with `pip install fastapi uvicorn python-multipart httpx trimesh`
and `starlette.testclient.TestClient`, using any `.glb` (even a throwaway one
built with `trimesh.creation.box()`) in place of a real generation — no need to
touch WSL for this part. That's how this code was written and checked: against
synthetic meshes, not a real TRELLIS.2 output. It still needs one real
`/generate` → `/jobs/{id}/file?format=obj` (etc.) round trip on the actual
WSL/ROCm machine before you trust it in production — TRELLIS.2's real output
(UV layout, multi-material scenes, webp textures) may hit trimesh edge cases a
plain textured box won't.

## Hardware constraints that are not optional

These aren't style preferences — violating them has previously OOM'd and killed
host processes on this machine:

- `low_vram` must stay `True` in `trellis_server.py`. This is a UMA iGPU: "GPU"
  memory and system RAM are the same physical RAM, and `low_vram=False` tried to
  keep the full 16GB of weights resident, exceeding the WSL VM's memory allotment.
- Jobs are serialized to one at a time — no concurrent GPU work.
- Do not set `expandable_segments:True` in `PYTORCH_HIP_ALLOC_CONF` — it throws
  `hipErrorInvalidValue` under WSL's DXG paravirtualization. The current value
  (`garbage_collection_threshold:0.6,max_split_size_mb:128`) is deliberate.
- Required env vars (already set as defaults in `trellis_server.py` /
  `run_inference.py`, don't remove): `HSA_XNACK=1`, `HSA_ENABLE_DXG_DETECTION=1`,
  `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1`, `ATTN_BACKEND=sdpa`.

## Report what you find

This repo's [LICENSE](LICENSE) grants broad permission to use, modify, and
redistribute this code for any purpose — in exchange, it asks (as a good-faith
condition, not a legal threat) that anyone who finds a problem or improvement
reports it. If you're an agent working in this repo, that means you too:

- Notice a bug, a stale assumption, a security issue, or something that
  contradicts this file or the README while you're in here for an unrelated
  task? Don't silently fix it and move on, and don't silently ignore it
  either — say so explicitly in your response to whoever is running you, and
  if the repo has been pushed, open an issue at
  `https://github.com/iceblue03/trellis2-rocm-bridge/issues` (adjust if the
  repo has moved).
- Same for improvement ideas you noticed but weren't asked to implement.
- This applies even if it's outside the scope of what you were asked to do —
  the point is surfacing it, not necessarily fixing it unprompted.

## Do not

- Do not copy `trellis2/`, `app.py`, `assets/`, `configs/`, `data_toolkit/`, or
  `server_data/` from WSL into this repo.
- Do not add a Blender-specific (or any other client-specific) dependency here —
  this repo is the server; clients live in their own repos.
- Do not remove the `low_vram=True` hardcode or the job serialization "to make it
  faster" — both are load-bearing on this hardware.
- Do not re-add `expandable_segments:True` to the HIP allocator config.
- Do not run a full `/generate` job as a routine verification step (see
  "Validating a change" above) — use `/health` instead.
- Do not change `/jobs/{id}/file`'s default (no `format=` given) away from
  `glb` — existing clients (Blender addon, any saved curl script) rely on that
  default staying exactly what it always returned.
- Do not split the web UI into a separate `static/` file (see "The one
  invariant you must not break" above) or edit `README.md`/`README.ko.md`
  out of sync (see "README is bilingual" above).
