#!/usr/bin/env bash
# 一键出片：配乐 → 逐帧渲染画面 → 合成（响度归一到 -14 LUFS）
set -euo pipefail
cd "$(dirname "$0")"
python music.py
python render.py video
ffmpeg -y -loglevel error -i out/promo_silent.mp4 -i out/music.wav \
  -af "loudnorm=I=-14:TP=-1.5:LRA=11" -c:v copy -c:a aac -b:a 192k -ar 48000 -shortest \
  -movflags +faststart out/CloudSense_promo.mp4
echo "完成：$(pwd)/out/CloudSense_promo.mp4"
