#!/usr/bin/env bash
# 在宿主机上把一段人声做成 MiMo 克隆用的参考音频。
# 用法：./tools/build_reference.sh source.wav [输出路径] [音高倍率] [语速倍率] [裁剪秒数]
# 默认：升 12% 音高、提速 8%、裁掉首尾静音、20ms 淡入淡出，24kHz 单声道 WAV。
#
# 为什么要裁首尾：参考音频开头的爆发式起音和结尾的长静音会被克隆模型学走，
# 输出里就会出现"开头像被切掉半个字""结尾拖一截静音"的现象。
set -euo pipefail

SRC="${1:?用法: build_reference.sh <source.wav> [out.wav] [pitch=1.12] [tempo=1.08] [trim=0.15]}"
OUT="${2:-reference.wav}"
PITCH="${3:-1.12}"
TEMPO="${4:-1.08}"
TRIM="${5:-0.15}"

command -v ffmpeg >/dev/null 2>&1 || { echo "需要 ffmpeg" >&2; exit 1; }

# 先统一到 24kHz，再做 asetrate 升调（asetrate 依赖输入采样率，顺序不能反）；
# 最后裁首尾静音并加淡入淡出，避免爆音。
ffmpeg -hide_banner -loglevel error -y -i "$SRC" \
  -af "aresample=24000,asetrate=24000*${PITCH},aresample=24000,atempo=${TEMPO},silenceremove=start_periods=1:start_threshold=-45dB:start_silence=${TRIM},areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=${TRIM},areverse,afade=t=in:st=0:d=0.02,afade=t=out:st=-0.02:d=0.02" \
  -ac 1 -ar 24000 -c:a pcm_s16le "$OUT"

echo "已生成 $OUT："
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$OUT"
