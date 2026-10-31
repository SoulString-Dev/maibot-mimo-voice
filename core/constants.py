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
# +12 dB（4.0）会把安静输出里的呼吸、底噪和削顶一起抬出来（实测单条最多 +4.3 dB 就破音），
# 收到 +6 dB（2.0），剩下的响度差交给软限幅兜底。
_PEAK_GAIN_MAX = 2.0
_TIMBRE_MAX_GAIN_DB = 6.0
# 实测音色距离经常在 6–8 dB：超过这个距离时 EQ 需要的补偿已经顶格，硬怼只会把音色掰闷，
# 所以从这里开始按 dB 比例调低强度。
_TIMBRE_FULL_STRENGTH_DB = 8.0
_PEAK_GAIN_EPSILON = 0.01
# 裁剪后保留的尾音长度：50ms 太短，最后一个字的尾音经常被切掉，留 150ms。
_TAIL_KEEP_SECONDS = 0.15
# 软限幅天花板：超过它的峰值用 tanh 压回，避免 EQ/归一之后出现削顶破音。
_SOFT_LIMIT_CEILING = 0.97

_SCOPE_TTL_SECONDS = 12 * 3600.0
_SCOPE_CACHE_MAX_ENTRIES = 5000
# 内置音色：随插件分发的标准参考音频，统一放在插件根目录的 presets/ 下。
# 键就是配置里显示的中文名，内部不再另设短 id。
PRESET_DIR_NAME = "presets"
PRESET_REFERENCE_FILES = {
    "冰糖": "presets/reference-bingtang.wav",
    "茉莉": "presets/reference-jasmine.wav",
    "理塘王": "presets/reference-litang.wav",
    "可爱少女": "presets/reference-keai.wav",
}
# 内置音色的文件名集合：用来判断"这条参考是不是随版本分发的"（跳过体检与自动瘦身）。
PRESET_REFERENCE_NAMES = frozenset(item.rsplit("/", 1)[-1] for item in PRESET_REFERENCE_FILES.values())
# 配置里只显示中文名；旧写法（含大小写/空格变体）仍然能读，读进来立刻换成中文名。
PRESET_ALIASES = {
    "冰糖": "冰糖",
    "bingtang": "冰糖",
    "茉莉": "茉莉",
    "jasmine": "茉莉",
    "理塘王": "理塘王",
    "litang": "理塘王",
    "可爱少女": "可爱少女",
    "keai": "可爱少女",
    "cute": "可爱少女",
    "自定义": "自定义",
    "custom": "自定义",
}
PRESET_DISPLAY_NAMES = {
    "冰糖": "冰糖",
    "茉莉": "茉莉",
    "理塘王": "理塘王",
    "可爱少女": "可爱少女",
    "自定义": "自定义",
}

_REFERENCE_SAMPLE_RATE = 24000
_REFERENCE_BITRATE_KBPS = 64
_REFERENCE_MAX_CLIP_SECONDS = 15.0
_REFERENCE_BALANCED_CLIP_SECONDS = 3.0
_REFERENCE_SILENCE_DB = -45.0
# single_file 的 WAV 参考自动瘦身：切成单句、拼到 3.7s 左右（上下限 5s），峰值归一 -3 dBFS。
# 依据：线上 8.41s / 4 段 / 18.5% 静音的参考，换到 3.73s 拼接版后盲听与客观离散度都明显更好。
_REFERENCE_PREPARE_TARGET_SECONDS = 3.7
_REFERENCE_PREPARE_MAX_SECONDS = 5.0
_REFERENCE_PREPARE_PEAK_DBFS = -3.0
_REFERENCE_PREPARE_MIN_SEGMENT_SECONDS = 0.25
_REFERENCE_PREPARE_GAP_SECONDS = 0.15
# 参考音频体检门槛：超过任一条就在启动时告警（不是阻断）。
_REFERENCE_WARN_SECONDS = 6.0
_REFERENCE_WARN_SILENCE_RATIO = 0.15
_REFERENCE_WARN_LONGEST_SILENCE_SECONDS = 0.4
_FFMPEG_BIN = "ffmpeg"
# MiMo 侧参考音频 Base64 上限约 10MB，留一点余量。
_REFERENCE_MAX_BYTES = int(9.5 * 1024 * 1024)

# 自适应超时（0.5.3）：先埋点拿到真实耗时，再用 p80 × 1.4 定超时。
# 样本不足时用 general.timeout（20s）兜底，样本够了才切动态值，避免冷启动就乱定。
_TIMEOUT_SAMPLE_SIZE = 50
_TIMEOUT_MIN_SAMPLES = 10
_TIMEOUT_FACTOR = 1.4
_TIMEOUT_MIN_SECONDS = 12.0
_TIMEOUT_MAX_SECONDS = 25.0
_TIMEOUT_STEP_SECONDS = 0.5
# 超时后补一次新请求的总时长预算：单条语音最多占用单路这么久。
# 40s 与 0.5.2 的最坏耗时持平（固定 20s + 补 20s），但自适应最多只到 25s，
# 所以正常情况补的那次只分到 15–28s；只在剩余不足 8s（固定模式把 timeout 调得很大）
# 时才放弃补发，避免"再等一次注定超时"。
_SYNTH_TOTAL_BUDGET_SECONDS = 40.0
_SYNTH_MIN_RETRY_SECONDS = 8.0
