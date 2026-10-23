#!/usr/bin/env bash
# 在宿主机上把一段人声做成 MiMo 克隆用的参考音频。
# 用法：./tools/build_reference.sh source.wav [输出路径] [音高倍率] [语速倍率]
# 默认：升 12% 音高、提速 8%，24kHz 单声道 WAV。
set -euo pipefail

SRC="${1:?用法: build_reference.sh <source.wav> [out.wav] [pitch=1.12] [tempo=1.08]}"
OUT="${2:-reference.wav}"
PITCH="${3:-1.12}"
TEMPO="${4:-1.08}"

command -v ffmpeg >/dev/null 2>&1 || { echo "需要 ffmpeg" >&2; exit 1; }

# 先统一到 24kHz，再做 asetrate 升调（asetrate 依赖输入采样率，顺序不能反）
ffmpeg -hide_banner -loglevel error -y -i "$SRC" \
  -af "aresample=24000,asetrate=24000*${PITCH},aresample=24000,atempo=${TEMPO}" \
  -ac 1 -ar 24000 -c:a pcm_s16le "$OUT"

echo "已生成 $OUT："
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$OUT"
