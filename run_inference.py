import os, time, sys
os.environ['OPENCV_IO_ENABLE_OPENEXR'] = '1'
# NOTE: expandable_segments:True breaks under WSL's DXG paravirtualization
# (hipErrorInvalidValue on plain .to(device)) — keep the repo's original setting.
os.environ["PYTORCH_HIP_ALLOC_CONF"] = "garbage_collection_threshold:0.6,max_split_size_mb:128"
os.environ["HSA_XNACK"] = "1"
# memory-efficient / flash attention kernels on RDNA3.5 (avoids materializing the
# full O(N^2) attention matrix, which OOM'd at 12.57 GiB on the 1024 cascade)
os.environ["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"

PIPELINE_TYPE = os.environ.get("TRELLIS_PIPELINE_TYPE", "512")

import torch
torch.cuda.set_per_process_memory_fraction(0.90)

def stamp(msg):
    torch.cuda.synchronize()
    peak = torch.cuda.max_memory_allocated() / 1024**3
    cur = torch.cuda.memory_allocated() / 1024**3
    print(f"[{time.time()-T0:7.1f}s] {msg} | cur {cur:.2f} GiB, peak {peak:.2f} GiB", flush=True)

T0 = time.time()
from PIL import Image
from trellis2.pipelines import Trellis2ImageTo3DPipeline
import o_voxel
stamp("imports done")

pipeline = Trellis2ImageTo3DPipeline.from_pretrained("microsoft/TRELLIS.2-4B")
stamp("pipeline loaded (cpu)")

pipeline.cuda()
stamp("pipeline -> gpu")

print(f"=== running pipeline_type={PIPELINE_TYPE} ===", flush=True)
image = Image.open("assets/example_image/T.png")
mesh = pipeline.run(image, pipeline_type=PIPELINE_TYPE)[0]
stamp("PIPELINE RUN COMPLETE")

mesh.simplify(16777216)
stamp("mesh simplified")

glb = o_voxel.postprocess.to_glb(
    vertices=mesh.vertices, faces=mesh.faces, attr_volume=mesh.attrs,
    coords=mesh.coords, attr_layout=mesh.layout, voxel_size=mesh.voxel_size,
    aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
    decimation_target=1000000, texture_size=4096,
    remesh=True, remesh_band=1, remesh_project=0, verbose=True,
)
stamp("glb built")

out = f"/root/TRELLIS.2_rocm/sample_{PIPELINE_TYPE}.glb"
try:
    glb.export(out, extension_webp=True)
except Exception as e:
    # pillow-simd lacks newer WebP attrs; fall back to PNG textures rather than
    # discarding a multi-minute generation.
    print(f"[WARN] webp export failed ({type(e).__name__}: {e}); retrying with PNG textures", flush=True)
    glb.export(out)
stamp(f"EXPORTED {out}")
print("SUCCESS", os.path.getsize(out), "bytes")
