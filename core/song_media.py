"""音频探测：时长、降采样、文件消息里的下载链接。

容器里没有 ffmpeg（`maim-bot-core` 实测），宿主有但插件跑不到，所以这里
只按容器格式自己解析头部：

* wav —— ``wave`` 模块；
* mp3 —— 逐帧枚举（CBR/VBR 都准，5 分钟的 320 kbps 约 7800 帧，毫秒级）；
* flac —— ``STREAMINFO`` 块里的总采样数。
"""

from __future__ import annotations

import io
import ipaddress
import re
import wave
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from .audio import _numpy, _read_wav_pcm, _write_wav_pcm

_AUDIO_SUFFIXES = (".mp3", ".wav", ".flac")

# 文件消息的文本长这样：[文件] xxx.mp3，大小: 5.3 MB，链接: https://...
_URL_PATTERN = re.compile(r"https?://[^\s，。、）)】]+")
# 文件消息的文本里带原始文件名，链接本身可能没有扩展名（QQ 的 /download?file=...）
_AUDIO_NAME = re.compile(r"[\w\u4e00-\u9fff .\-]+\.(?:mp3|wav|flac)", re.IGNORECASE)

_ID3V1_SIZE = 128

# MPEG Audio Layer III 码率表（kbps），索引 0 是 free format
_BITRATES_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_BITRATES_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
# 版本位（b1 >> 3 & 0b11）：3=MPEG1，2=MPEG2，0=MPEG2.5
_RATES_BY_VERSION = {
    3: (44100, 48000, 32000, 0),
    2: (22050, 24000, 16000, 0),
    0: (11025, 12000, 8000, 0),
}
_SAMPLES_PER_FRAME = {3: 1152, 2: 576, 0: 576}


def guess_path(name_or_url: str) -> str:
    """从文件名或 URL 里取出路径部分（去掉 ``?`` 之后的签名参数）。"""
    text = str(name_or_url or "").strip()
    for mark in ("?", "#"):
        index = text.find(mark)
        if index >= 0:
            text = text[:index]
    return text


def guess_extension(name_or_url: str) -> str:
    suffix = Path(guess_path(name_or_url)).suffix.lower()
    return suffix if suffix in _AUDIO_SUFFIXES else ""


def extract_file_url(text: str) -> Optional[str]:
    """从入站的 ``[文件]`` 文本里找音频下载链接；非音频或没有链接返回 ``None``。

    扩展名可能只在文件名里（``[文件] 雨爱.mp3，链接: https://…/download?file=abc``），
    所以「链接带音频后缀」与「文本里有音频文件名」满足一个就认。
    """
    body = str(text or "")
    named = bool(_AUDIO_NAME.search(body))
    for match in _URL_PATTERN.finditer(body):
        url = match.group(0).rstrip(".,;，。、")
        if guess_extension(url) or named:
            return url
    return None


def parse_file_message(text: str) -> Tuple[str, int]:
    """从 ``[文件] 名字，大小: 1234`` 里取出文件名与字节数；不是文件消息返回 ``("", 0)``。

    适配器对私聊文件**不给链接**（只有 ``file_id``），所以要靠「文件名 + 大小」去历史消息里
    找回那条 file 段，再问适配器要下载地址。
    """
    body = str(text or "").strip()
    marker = "[文件]"
    if not body.startswith(marker):
        return "", 0
    body = body[len(marker) :].strip()
    size = 0
    size_match = re.search(r"[，,]\s*大小:\s*(?P<size>\d+)", body)
    if size_match is not None:
        size = int(size_match.group("size"))
        body = body[: size_match.start()]
    link_match = re.search(r"[，,]\s*链接:", body)
    if link_match is not None:
        body = body[: link_match.start()]
    return body.strip(), size


def probe_duration(data: bytes, suffix: str = "") -> float:
    """返回时长（秒）；无法识别时返回 ``0.0``，不抛异常。"""
    blob = bytes(data or b"")
    hint = (suffix or "").lower()
    if hint not in _AUDIO_SUFFIXES:
        hint = _sniff_suffix(blob)
    try:
        if hint == ".wav":
            return _wav_duration(blob)
        if hint == ".mp3":
            return _mp3_duration(blob)
        if hint == ".flac":
            return _flac_duration(blob)
    except (ValueError, IndexError, wave.Error, EOFError):
        return 0.0
    return 0.0


def _sniff_suffix(blob: bytes) -> str:
    if blob[:4] == b"RIFF" and blob[8:12] == b"WAVE":
        return ".wav"
    if blob[:4] == b"fLaC":
        return ".flac"
    if blob[:3] == b"ID3":
        return ".mp3"
    if len(blob) > 2 and blob[0] == 0xFF and (blob[1] & 0xE0) == 0xE0:
        return ".mp3"
    return ""


def _wav_duration(blob: bytes) -> float:
    with wave.open(io.BytesIO(blob), "rb") as fh:
        rate = fh.getframerate()
        frames = fh.getnframes()
    return float(frames) / rate if rate else 0.0


def _mp3_offset(blob: bytes) -> int:
    """跳过 ID3v2 头；返回第一个可能的帧位置。"""
    if blob[:3] != b"ID3" or len(blob) < 10:
        return 0
    size = (
        ((blob[6] & 0x7F) << 21)
        | ((blob[7] & 0x7F) << 14)
        | ((blob[8] & 0x7F) << 7)
        | (blob[9] & 0x7F)
    )
    footer = 10 if blob[5] & 0x10 else 0
    return min(len(blob), 10 + size + footer)


def _frame_header(blob: bytes, offset: int) -> Optional[Tuple[int, int, int, int]]:
    """解析一帧头：返回 (帧长, 采样率, 每帧采样数, 码率 bps)；不是帧返回 None。"""
    if offset + 4 > len(blob):
        return None
    b0, b1, b2, _b3 = blob[offset], blob[offset + 1], blob[offset + 2], blob[offset + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    if version == 1 or layer != 0x01:  # 只认 Layer III
        return None
    rates = _RATES_BY_VERSION.get(version, (0, 0, 0, 0))
    rate = rates[(b2 >> 2) & 0x03]
    if not rate:
        return None
    table = _BITRATES_V1 if version == 3 else _BITRATES_V2
    bitrate = table[(b2 >> 4) & 0x0F] * 1000
    if not bitrate:
        return None
    padding = (b2 >> 1) & 0x01
    per_frame = _SAMPLES_PER_FRAME.get(version, 1152)
    divisor = 144 if version == 3 else 72
    length = (divisor * bitrate) // rate + padding
    if length < 24:
        return None
    return length, rate, per_frame, bitrate


def _mp3_duration(blob: bytes) -> float:
    """逐帧累加；遇到破帧就尝试向后找下一个同步字，连续找不到就停。"""
    total_samples = 0
    rate_seen = 0
    offset = _mp3_offset(blob)
    limit = len(blob) - _ID3V1_SIZE
    frames = 0
    while offset < limit and frames < 400_000:
        header = _frame_header(blob, offset)
        if header is None:
            nxt = blob.find(b"\xff", offset + 1)
            if nxt < 0:
                break
            offset = nxt
            continue
        length, rate, per_frame, _bitrate = header
        total_samples += per_frame
        rate_seen = rate or rate_seen
        offset += length
        frames += 1
    if not rate_seen:
        return 0.0
    return float(total_samples) / rate_seen


def _flac_duration(blob: bytes) -> float:
    if blob[:4] != b"fLaC":
        return 0.0
    offset = 4
    while offset + 4 <= len(blob):
        header = blob[offset]
        block_type = header & 0x7F
        block_size = int.from_bytes(blob[offset + 1 : offset + 4], "big")
        body = blob[offset + 4 : offset + 4 + block_size]
        if block_type == 0 and len(body) >= 18:
            packed = int.from_bytes(body[10:18], "big")
            rate = (packed >> 44) & 0xFFFFF
            total = packed & 0xFFFFFFFFF
            return float(total) / rate if rate else 0.0
        if header & 0x80:  # 最后一个 metadata 块
            break
        offset += 4 + block_size
    return 0.0


def downsample_wav(data: bytes, rate: int = 16000) -> Optional[bytes]:
    """把 16-bit PCM WAV 降采样到 ``rate``；不可处理时返回 ``None``（调用方保留原文件）。

    mp3/flac 不可处理：没有 ffmpeg 就无法解码，所以只对 wav 生效。
    """
    np = _numpy()
    if np is None:
        return None
    if int(rate) <= 0:
        return None
    try:
        pcm, source_rate = _read_wav_pcm(data)
    except (ValueError, wave.Error, EOFError, RuntimeError):
        return None
    if source_rate <= 0 or len(pcm) == 0:
        return None
    if source_rate <= int(rate):
        return None

    # 先做窗口平均当低通，再按目标点数重采样，避免直接抽头的混叠
    window = max(1, round(source_rate / float(rate)))
    if window > 1:
        kernel = np.ones(window, dtype=np.float32) / float(window)
        pcm = np.convolve(pcm, kernel, mode="same").astype(np.float32)
    target_len = int(len(pcm) * int(rate) / float(source_rate))
    if target_len <= 0:
        return None
    if target_len < len(pcm):
        index = np.linspace(0, len(pcm) - 1, target_len).astype(np.int64)
        pcm = pcm[index]
    return _write_wav_pcm(pcm, int(rate))


def readable_size(size: int) -> str:
    """人类可读的字节数，用于命令回执。"""
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def duration_label(seconds: float) -> str:
    total = max(0, round(float(seconds)))
    return f"{total // 60}:{total % 60:02d}"


__all__: List[str] = [
    "downsample_wav",
    "duration_label",
    "extract_file_url",
    "guess_extension",
    "guess_path",
    "probe_duration",
    "readable_size",
]


_QQ_MEDIA_HOSTS = (".qq.com", ".qpic.cn", ".qqmail.com")
_QQ_MEDIA_EXACT = ("qq.com", "qpic.cn", "multimedia.nt.qq.com.cn")


def is_qq_media_url(url: str) -> bool:
    """判断链接是否出自 QQ 自己的文件/CDN 域名。

    文本里出现的下载链接不可信（任何群成员都能照着 ``[文件] x.mp3，链接: http://…`` 的格式
    构造一条），所以文本来源的链接只允许 QQ 域名；通过适配器 API 拿到的链接不走这条限制。
    """
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    host = (parts.hostname or "").lower().strip(".")
    if not host:
        return False
    if host in _QQ_MEDIA_EXACT or any(host.endswith(suffix) for suffix in _QQ_MEDIA_HOSTS):
        return True
    # 实测 QQ 文件 CDN 是 http://<公网 IP>:80/qqdownloadftnv5?...，路径前缀固定
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return (parts.path or "").lower().startswith("/qqdownload")
