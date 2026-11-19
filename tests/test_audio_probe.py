"""音频探测：wav/mp3/flac 时长、降采样、文件链接提取。"""
from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __bootstrap import Checker, load_plugin

module = load_plugin()
media = sys.modules[f"{module.__name__}.core.song_media"]
checker = Checker("音频探测")


def wav_bytes(seconds: float = 1.5, rate: int = 24000, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x10" * channels * int(rate * seconds))
    return buffer.getvalue()


def wav_info(audio: bytes) -> tuple[int, int, float]:
    with wave.open(io.BytesIO(audio), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.getnframes()
        return handle.getnchannels(), rate, frames / rate


def mp3_frame(count: int, bitrate_index: int = 9, rate_index: int = 0) -> bytes:
    """MPEG1 Layer III 的帧；默认 128 kbps / 44100 Hz，帧长 417 字节。"""
    header = bytes([0xFF, 0xFB, (bitrate_index << 4) | (rate_index << 2), 0x64])
    frame_len = 144 * 128000 // 44100
    return (header + b"\x00" * (frame_len - 4)) * count


def flac_bytes(total_samples: int = 44100 * 3, rate: int = 44100) -> bytes:
    packed = (rate << 44) | (0 << 41) | (15 << 36) | total_samples
    body = b"\x10\x00\x10\x00" + b"\x00" * 6 + packed.to_bytes(8, "big") + b"\x00" * 16
    assert len(body) == 34
    return b"fLaC" + bytes([0x80 | 0, 0, 0, len(body)]) + body


# ------------------------------------------------------------------- 扩展名
checker.equal("URL 取扩展名", media.guess_extension("https://a/b/c.MP3?rkey=1"), ".mp3")
checker.equal("非音频扩展名", media.guess_extension("https://a/b/c.zip"), "")
checker.equal("去查询串", media.guess_path("https://a/b.mp3?x=1#y"), "https://a/b.mp3")

# ------------------------------------------------------------------- 下载链接
text = "[文件] 雨爱.mp3，大小: 5.3 MB，链接: https://multimedia.nt.qq.com.cn/download?file=abc"
checker.equal(
    "从文件文本取链接",
    media.extract_file_url(text),
    "https://multimedia.nt.qq.com.cn/download?file=abc",
)
checker.equal("非音频链接不取", media.extract_file_url("[文件] a.zip，链接: https://x/a.zip"), None)
checker.equal("普通聊天文本", media.extract_file_url("今天天气不错 https://example.com"), None)
checker.equal("句末标点不带走", media.extract_file_url("链接: https://x/a.mp3。"), "https://x/a.mp3")

# --------------------------------------------------------------------- wav
audio = wav_bytes(1.5, 24000)
checker.check("wav 时长", abs(media.probe_duration(audio, ".wav") - 1.5) < 0.01)
checker.check("wav 无后缀也能嗅探", abs(media.probe_duration(audio) - 1.5) < 0.01, "RIFF 头")
checker.equal("坏数据返回 0", media.probe_duration(b"not audio", ".wav"), 0.0)
checker.equal("空数据返回 0", media.probe_duration(b"", ""), 0.0)

# --------------------------------------------------------------------- mp3
frames = 40
mp3 = mp3_frame(frames)
expected = frames * 1152 / 44100
checker.check("mp3 逐帧时长", abs(media.probe_duration(mp3, ".mp3") - expected) < 0.01, f"{expected:.3f}s")
id3 = b"ID3\x03\x00\x00" + bytes([0, 0, 0x02, 0x00]) + b"\x00" * 256
checker.check("跳过 ID3v2", abs(media.probe_duration(id3 + mp3, ".mp3") - expected) < 0.01)
checker.check("无后缀嗅探 ID3", abs(media.probe_duration(id3 + mp3) - expected) < 0.01)
checker.check("垃圾前缀能重新同步", abs(media.probe_duration(b"\x00" * 10 + mp3, ".mp3") - expected) < 0.01)
checker.equal("空 mp3", media.probe_duration(b"", ".mp3"), 0.0)

# -------------------------------------------------------------------- flac
flac = flac_bytes()
checker.check("flac 时长", abs(media.probe_duration(flac, ".flac") - 3.0) < 0.01)
checker.check("flac 无后缀嗅探", abs(media.probe_duration(flac) - 3.0) < 0.01)
checker.equal("坏 flac", media.probe_duration(b"fLaC", ".flac"), 0.0)

# ------------------------------------------------------------------ 降采样
source = wav_bytes(1.5, 48000, channels=2)
lowered = media.downsample_wav(source, 16000)
checker.check("降采样有结果", lowered is not None)
if lowered:
    channels, rate, duration = wav_info(lowered)
    checker.equal("目标采样率", rate, 16000)
    checker.equal("降采样后单声道", channels, 1)
    checker.check("时长保持", abs(duration - 1.5) < 0.05, f"{duration:.3f}s")
    checker.check("体积变小", len(lowered) < len(source))
checker.equal("已是低采样率返回 None", media.downsample_wav(wav_bytes(1.0, 8000), 16000), None)
checker.equal("非 wav 返回 None", media.downsample_wav(mp3, 16000), None)
checker.equal("坏 wav 返回 None", media.downsample_wav(b"RIFFxxxx", 16000), None)

# ------------------------------------------------------------------ 显示格式
checker.equal("字节数 B", media.readable_size(512), "512 B")
checker.equal("字节数 MB", media.readable_size(5 * 1024 * 1024), "5.0 MB")
checker.equal("时长标签", media.duration_label(139.78), "2:20")
checker.equal("时长未知时给人看的话", media.duration_label(0), "时长未知")

sys.exit(checker.report())
