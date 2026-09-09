#!/bin/bash
# Start the TRELLIS.2 bridge server detached, so it survives the shell that
# launched it. Safe to run repeatedly: an existing server is left alone.
source /root/miniconda3/etc/profile.d/conda.sh
conda activate trellis2-gfx1150
cd /root/TRELLIS.2_rocm


if curl -s --max-time 3 http://127.0.0.1:7861/health >/dev/null 2>&1; then
  echo "ALREADY RUNNING"
  curl -s http://127.0.0.1:7861/health
  exit 0
fi

mkdir -p /root/TRELLIS.2_rocm/server_data
nohup python3 /root/TRELLIS.2_rocm/trellis_server.py \
  > /root/TRELLIS.2_rocm/server_data/server.log 2>&1 &
echo "STARTED pid=$!"
sleep 5
tail -5 /root/TRELLIS.2_rocm/server_data/server.log
