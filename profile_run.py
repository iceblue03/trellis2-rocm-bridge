"""Instrumented TRELLIS.2 run: per-stage timings written to a clean log file
(separate from stdout so tqdm progress bars don't clobber them).

TRELLIS_LOW_VRAM=0 disables the pipeline's low_vram mode, which on a UMA APU
shuffles weights between 'GPU' and 'CPU' memory that is physically the same RAM.
"""
import os, time, json
os.environ['OPENCV_IO_ENABLE_OPENEXR'] = '1'
os.environ["PYTORCH_HIP_ALLOC_CONF"] = "garbage_collection_threshold:0.6,max_split_size_mb:128"
os.environ["HSA_XNACK"] = "1"
os.environ["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"

PIPELINE_TYPE = os.environ.get("TRELLIS_PIPELINE_TYPE", "512")
LOW_VRAM = os.environ.get("TRELLIS_LOW_VRAM", "1") == "1"
PROFILE_OUT = os.environ.get("TRELLIS_PROFILE_OUT", "/root/TRELLIS.2_rocm/profile.json")

import torch
torch.cuda.set_per_process_memory_fraction(0.90)

T0 = time.time()
EVENTS = []

def log(name, dt, peak=None):
    rec = {"stage": name, "seconds": round(dt, 2),
           "peak_gib": round(torch.cuda.max_memory_allocated()/1024**3, 2)}
    EVENTS.append(rec)
    print(f"### PROFILE {rec}", flush=True)
    with open(PROFILE_OUT, "w") as f:
        json.dump({"pipeline_type": PIPELINE_TYPE, "low_vram": LOW_VRAM,
                   "total_so_far": round(time.time()-T0, 2), "stages": EVENTS}, f, indent=2)

def timed(obj, name):
    """Wrap a bound method so each call is timed."""
    orig = getattr(obj, name)
    def wrapper(*a, **kw):
        torch.cuda.synchronize(); t = time.time()
        try:
            return orig(*a, **kw)
        finally:
            torch.cuda.synchronize(); log(name, time.time()-t)
    setattr(obj, name, wrapper)

t = time.time()
from PIL import Image
from trellis2.pipelines import Trellis2ImageTo3DPipeline
import o_voxel
log("imports", time.time()-t)

t = time.time()
pipeline = Trellis2ImageTo3DPipeline.from_pretrained("microsoft/TRELLIS.2-4B")
log("from_pretrained (disk->cpu)", time.time()-t)

pipeline.low_vram = LOW_VRAM
print(f"### low_vram = {pipeline.low_vram}", flush=True)

t = time.time()
pipeline.cuda()
log("pipeline.cuda()", time.time()-t)

for m in ["get_cond", "sample_sparse_structure", "sample_shape_slat",
          "sample_shape_slat_cascade", "decode_shape_slat",
          "sample_tex_slat", "decode_tex_slat", "decode_latent",
          "preprocess_image"]:
    if hasattr(pipeline, m):
        timed(pipeline, m)

image = Image.open("assets/example_image/T.png")
t = time.time()
mesh = pipeline.run(image, pipeline_type=PIPELINE_TYPE)[0]
log("TOTAL pipeline.run", time.time()-t)

t = time.time()
mesh.simplify(16777216)
log("mesh.simplify", time.time()-t)

t = time.time()
glb = o_voxel.postprocess.to_glb(
    vertices=mesh.vertices, faces=mesh.faces, attr_volume=mesh.attrs,
    coords=mesh.coords, attr_layout=mesh.layout, voxel_size=mesh.voxel_size,
    aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
    decimation_target=1000000, texture_size=4096,
    remesh=True, remesh_band=1, remesh_project=0, verbose=False,
)
log("to_glb (postprocess)", time.time()-t)

out = f"/root/TRELLIS.2_rocm/sample_{PIPELINE_TYPE}_lv{int(LOW_VRAM)}.glb"
t = time.time()
try:
    glb.export(out, extension_webp=True)
except Exception as e:
    print(f"[WARN] webp export failed ({e}); PNG fallback", flush=True)
    glb.export(out)
log("glb.export", time.time()-t)

log("=== TOTAL ===", time.time()-T0)
print("SUCCESS", out, os.path.getsize(out), "bytes", flush=True)
