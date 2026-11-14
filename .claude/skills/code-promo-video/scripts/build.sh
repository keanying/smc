#!/usr/bin/env bash
# 一键出片：逐帧渲染画面 → 按同一份转场点合成配乐 → 合成并把响度归一到 -14 LUFS
# 用法：bash build.sh PAGE.html [OUT_DIR] [--preview]
#   --preview 先出半分辨率小样（快约 3 倍），确认节奏后再出正式版
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PAGE=""; OUT=""; SCALE=1
for a in "$@"; do
  case "$a" in
    --preview) SCALE=0.5 ;;
    *) if [[ -z "$PAGE" ]]; then PAGE="$a"; else OUT="$a"; fi ;;
  esac
done
OUT="${OUT:-$(dirname "$PAGE")/out}"
mkdir -p "$OUT"
python "$HERE/render.py" "$PAGE" --out "$OUT" --scale "$SCALE"
SILENT="$OUT/silent.mp4"; [[ "$SCALE" != "1" ]] && SILENT="$OUT/silent_preview.mp4"
NAME="$(basename "${PAGE%.*}")"; [[ "$SCALE" != "1" ]] && NAME="${NAME}_preview"
MOOD="$(python -c "import json;print(json.load(open('$OUT/video.json')).get('mood','tech'))")"
if [[ "$MOOD" == "none" ]]; then
  cp "$SILENT" "$OUT/$NAME.mp4"
else
  python "$HERE/music.py" --config "$OUT/video.json" --out "$OUT/music.wav"
  # 两遍响度归一：先测量，再按实测值精确归一到 -14 LUFS
  LN="$(ffmpeg -hide_banner -i "$OUT/music.wav" -af loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json -f null - 2>&1 \
    | python -c "import sys,json,re;d=json.loads(re.search(r'\{[^{}]*\}',sys.stdin.read(),re.S).group());print(f\"measured_I={d['input_i']}:measured_TP={d['input_tp']}:measured_LRA={d['input_lra']}:measured_thresh={d['input_thresh']}:offset={d['target_offset']}\")")"
  ffmpeg -y -loglevel error -i "$SILENT" -i "$OUT/music.wav" \
    -af "loudnorm=I=-14:TP=-1.5:LRA=11:$LN:linear=true" -c:v copy -c:a aac -b:a 192k -ar 48000 -shortest \
    -movflags +faststart "$OUT/$NAME.mp4"
fi
echo "完成：$OUT/$NAME.mp4"
