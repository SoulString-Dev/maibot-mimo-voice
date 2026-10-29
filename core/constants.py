"""插件常量：阈值、默认值与参考音频参数。

这些值不暴露给用户配置（要么是协议细节，要么改了就破坏音频指标），
所以集中放在一处，避免散落在业务代码里。
"""

from __future__ import annotations

SUPPORTED_REFERENCE_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".mpeg", ".ogg", ".opus", ".wav", ".wma"}
VOICE_SEGMENT_TYPE = "voice"
DEFAULT_STYLE_INSTRUCTION = "用同一个人的音色说话，像平时聊天一样语速稍快、句子连贯，不要拖长尾音，情绪自然。"

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
# 小于这个字节数的 mp3 基本是空音频（约 0.15 秒），按失败处理。
_MIN_AUDIO_BYTES = 600
_DEFAULT_API_BASE = "https://api.xiaomimimo.com/v1"
_MIN_AUDIO_SECONDS = 0.3
# 音频时长上限不是一个固定值：长文本（llm_trigger / voice_only / 模型点名）本来就该更长。
# 这里按“中文字符数 × 0.7 秒”估算，下限 _MAX_AUDIO_SECONDS，上限 _AUDIO_SECONDS_CAP。
_MAX_AUDIO_SECONDS = 20.0
_AUDIO_SECONDS_CAP = 90.0
_AUDIO_SECONDS_PER_CHAR = 0.7
# 各类按会话缓存的上限；超过就整体清空，避免长期运行缓慢泄漏。
_TTL_CACHE_MAX_ENTRIES = 512
_MIN_RMS_DBFS = -40.0
_SILENCE_DBFS = -45.0
_PEAK_TARGET_DBFS = -1.5
_PEAK_GAIN_MIN = 0.25
_PEAK_GAIN_MAX = 4.0
_TIMBRE_MAX_GAIN_DB = 6.0
_PEAK_GAIN_EPSILON = 0.01
_TAIL_KEEP_SECONDS = 0.05

_SCOPE_TTL_SECONDS = 12 * 3600.0
_SCOPE_CACHE_MAX_ENTRIES = 5000
# 内置音色：随插件分发的标准参考音频（由 MiMo 预置音色按同一套升调/裁剪流程生成）。
PRESET_REFERENCE_FILES = {
    "bingtang": "reference-bingtang.wav",
    "jasmine": "reference-jasmine.wav",
}

_REFERENCE_SAMPLE_RATE = 24000
_REFERENCE_BITRATE_KBPS = 64
_REFERENCE_MAX_CLIP_SECONDS = 15.0
_REFERENCE_BALANCED_CLIP_SECONDS = 3.0
_REFERENCE_SILENCE_DB = -45.0
_FFMPEG_BIN = "ffmpeg"
# MiMo 侧参考音频 Base64 上限约 10MB，留一点余量。
_REFERENCE_MAX_BYTES = int(9.5 * 1024 * 1024)
