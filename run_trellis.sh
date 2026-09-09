#!/bin/bash
# TRELLIS.2 on gfx1150 — run after HuggingFace login + DINOv3 access approval.
source /root/miniconda3/etc/profile.d/conda.sh
conda activate trellis2-gfx1150
cd /root/TRELLIS.2_rocm

# verify DINOv3 access before spending time on the 16GB pipeline load
python3 -c "
from huggingface_hub import snapshot_download
try:
    snapshot_download('facebook/dinov3-vitl16-pretrain-lvd1689m')
    print('DINOv3 ACCESS OK')
except Exception as e:
    print('DINOv3 ACCESS FAILED:', type(e).__name__)
    print(str(e)[:300])
    raise SystemExit(1)
" || exit 1

export HSA_ENABLE_DXG_DETECTION=1
export ATTN_BACKEND=sdpa
python3 /root/TRELLIS.2_rocm/run_inference.py
