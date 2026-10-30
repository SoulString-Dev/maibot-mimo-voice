"""配置模型：用户可见的所有配置项，以及 ``api_base_url`` 的端点校验。"""

from __future__ import annotations

import ipaddress
from typing import List, Literal
from urllib.parse import urlsplit

from maibot_sdk import Field, PluginConfigBase
from pydantic import model_validator

from .constants import _DEFAULT_API_BASE, DEFAULT_STYLE_INSTRUCTION

_CONFIG_WARNINGS: List[str] = []


def add_config_warning(message: str) -> None:
    """记下一条配置告警，启动时由插件统一打印（避免校验器里直接打日志）。"""
    _CONFIG_WARNINGS.append(message)


def drain_config_warnings() -> List[str]:
    """取出并清空当前累积的配置告警。"""
    warnings = list(_CONFIG_WARNINGS)
    _CONFIG_WARNINGS.clear()
    return warnings


class PluginSectionConfig(PluginConfigBase):
    """插件基础开关"""

    __ui_label__ = "基础设置"

    enabled: bool = Field(default=True, description="是否启用语音回复")
    config_version: str = Field(default="0.5.2", description="配置文件版本号（内部使用，无需修改）")


class GeneralConfig(PluginConfigBase):
    """通用文本与超时设置"""

    __ui_label__ = "通用"

    max_text_length: int = Field(
        default=300,
        ge=20,
        le=1000,
        description="单条语音可承载的最大文字长度；超长时按 trigger.overflow_policy 处理",
    )
    timeout: float = Field(
        default=20.0,
        ge=3.0,
        le=120.0,
        description="单次语音合成超时（秒）；超时按 delivery.fallback_to_text 处理",
    )


class TriggerConfig(PluginConfigBase):
    """触发策略"""

    __ui_label__ = "触发策略"

    mode: Literal["random", "random_llm", "llm_trigger", "voice_only"] = Field(
        default="random",
        description="random=按概率转语音；random_llm=概率抽签 + 模型主动请求（mimo_voice_say）；"
        "llm_trigger=语音优先、LLM 判断内容更适合文字时改文字；voice_only=所有纯文本都转语音",
    )
    probability: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="random / random_llm 模式下，每条回复转语音的概率；建议 0.15 以上，低于 0.1 几乎感知不到",
    )
    decision_ttl: float = Field(
        default=10.0,
        ge=0.0,
        le=300.0,
        description="判定有效期（秒）：同一条聊天流在这么短时间内复用上一次的抽签/模型选择结果，避免逐条翻来覆去",
    )
    short_reply_max_chars: int = Field(
        default=60,
        ge=0,
        le=400,
        description="短句门槛：超过该字数的回复不转语音，0 表示不限制。只约束随机触发，模型主动要语音不受限",
    )
    overflow_policy: Literal["truncate", "text"] = Field(
        default="text",
        description="超过 general.max_text_length 时：truncate=截断后合成；text=保留文字不发语音",
    )
    english_policy: Literal["keep", "strip_tics", "strip"] = Field(
        default="strip_tics",
        description="英文处理：keep=原样念（会把 ww/emmm 读成字母）；"
        "strip_tics=只删 ww/emmm/ohhhhhhh 这类口癖，保留 LLM/token/cos 等真词；"
        "strip=删掉所有两个字母以上的拉丁串（会误伤真词，慎用）",
    )
    random_requires_chinese: bool = Field(
        default=True,
        description="随机触发时，清洗后仍含英文字母的回复改为发文字（避免把 token/LLM 念成字母）；"
        "模型主动要语音、llm_trigger、voice_only 不受此限制",
    )


class FilterConfig(PluginConfigBase):
    """群聊 / 私聊放行规则"""

    __ui_label__ = "放行规则"

    group_mode: Literal["all", "whitelist", "blacklist"] = Field(
        default="all",
        description="群聊放行方式：all=全部放行；whitelist=仅 group_ids 内的群转语音；blacklist=group_ids 内的群保持文字",
    )
    private_mode: Literal["all", "whitelist", "blacklist"] = Field(
        default="all",
        description="私聊放行方式：all=全部放行；whitelist=仅 private_user_ids 内的对方转语音；blacklist=private_user_ids 内的对方保持文字",
    )
    group_ids: List[str] = Field(
        default_factory=list,
        description="群号列表（配合 group_mode 使用）",
        json_schema_extra={"placeholder": "例如: 123456789"},
    )
    private_user_ids: List[str] = Field(
        default_factory=list,
        description="私聊对方的 QQ 号列表（配合 private_mode 使用）",
        json_schema_extra={"placeholder": "例如: 10000002"},
    )


class VoiceConfig(PluginConfigBase):
    """参考音频与音色"""

    __ui_label__ = "音色"

    preset: Literal["bingtang", "jasmine", "custom"] = Field(
        default="bingtang",
        description="音色：bingtang=内置冰糖（偏亮）、jasmine=内置茉莉（偏低偏柔）、custom=用 reference_path 指定的自定义参考音频",
    )
    reference_path: str = Field(
        default="reference.wav",
        description="自定义参考音频文件名（仅 preset=custom 时生效），必须放在插件目录内",
        json_schema_extra={"placeholder": "例如: reference.wav"},
    )
    reference_strategy: Literal["single_file", "best_single", "balanced", "full_merge"] = Field(
        default="single_file",
        description="single_file=直接用一个文件（不需要 ffmpeg）；其余三种需要插件目录下有 ffmpeg，从 voices/ 自动截取或拼接参考音频",
    )
    auto_prepare_reference: bool = Field(
        default=True,
        description="single_file 且参考是 WAV 时，发送前自动切成单句、去掉长静音、拼到约 3.7 秒（3–5 秒区间）并峰值归一到 -3 dBFS，"
        "结果缓存在运行时目录；已经是短而干净的素材则原样使用。实测长参考（8 秒、多段、静音 18%）更容易出现音色漂移和刺耳，建议保持开启",
    )
    style_instruction: str = Field(
        default=DEFAULT_STYLE_INSTRUCTION,
        description="随请求发送的朗读风格指令。它决定语速和语气，改动后听感变化很大；留空表示不发送",
    )


class MiMoConfig(PluginConfigBase):
    """MiMo 合成参数"""

    __ui_label__ = "合成参数"

    api_key: str = Field(default="", description="MiMo 官方 API Key")
    api_base_url: str = Field(
        default=_DEFAULT_API_BASE,
        description="MiMo API 基础地址，必须是以 https:// 开头的公网地址；写错会自动回退到官方地址",
    )
    allow_private_base_url: bool = Field(
        default=False,
        description="允许把参考音频发往内网/本机地址（只有自建代理时才需要，打开后会记一条警告）",
    )
    model: str = Field(default="mimo-v2.5-tts-voiceclone", description="TTS 模型名")
    audio_format: Literal["mp3", "wav"] = Field(default="mp3", description="返回音频格式")
    temperature: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="采样温度：越低音色越稳但可能发闷；实测 0.15 最稳，0.6 以上偶尔会听出换人",
    )
    max_concurrent: int = Field(default=1, ge=1, le=4, description="同时合成路数，建议保持 1")
    retry_attempts: int = Field(
        default=0,
        ge=0,
        le=3,
        description="仅对 429/5xx/网络错误重试（超时另由 retry_on_timeout 控制）",
    )
    retry_on_timeout: bool = Field(
        default=True,
        description="单次合成超过 general.timeout 时换一次请求重来（只重试一次，最坏耗时翻倍）",
    )
    retry_backoff_seconds: float = Field(default=1.5, ge=0.0, le=10.0, description="重试退避基数（秒），按 2 的幂指数增长")
    connect_timeout: float = Field(default=10.0, ge=1.0, le=60.0, description="建立连接的超时（秒），与整体超时分开")

    @model_validator(mode="after")
    def _check_base_url(self) -> "MiMoConfig":
        cleaned = (self.api_base_url or "").strip() or _DEFAULT_API_BASE
        problem = _api_base_url_problem(cleaned, allow_private=self.allow_private_base_url)
        if problem:
            message = f"mimo.api_base_url 不可用（{problem}），已回退为 {_DEFAULT_API_BASE}"
            add_config_warning(message)
            object.__setattr__(self, "api_base_url", _DEFAULT_API_BASE)
        return self


class DeliveryConfig(PluginConfigBase):
    """发送行为"""

    __ui_label__ = "发送"

    fallback_to_text: bool = Field(default=True, description="合成失败/超时时是否回退发送文字")
    skip_when_busy: bool = Field(
        default=True,
        description="并发已满时直接发文字（True），或排队等待（False，可能把整条回复拖到超时）",
    )
    timbre_match: bool = Field(
        default=True,
        description="音色对齐：按参考音频的频谱补偿每条输出，让每次合成的音色更接近参考（默认开启）",
    )
    timbre_match_strength: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="音色对齐强度，0=不处理、1=完全对齐；补偿量始终限制在 ±6 dB 以内，"
        "且频谱距离超过 8 dB 后会自动按比例减弱（距离远说明这条本来就偏离参考，硬对齐只会更闷）",
    )
    trim_tail_silence_ms: int = Field(
        default=250,
        ge=0,
        le=3000,
        description="尾部静音超过这个毫秒数就裁掉（只留 150ms），0=不裁；实测合成尾部静音 94–474ms 不稳定",
    )
    tail_pad_ms: int = Field(
        default=120,
        ge=0,
        le=1000,
        description="处理完后在尾部补多少毫秒静音，避免播放端把最后一个字吃掉，0=不补",
    )
    peak_normalize: bool = Field(
        default=True,
        description="把峰值统一拉到 -1.5 dBFS，解决有的条小声有的条刺耳",
    )


def _is_private_host(host: str) -> bool:
    """判断主机名是否指向本机、内网、链路本地或云元数据地址。"""
    lowered = (host or "").strip().strip(".").lower()
    if not lowered:
        return True
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        # 不含点的裸主机名（例如 docker 服务名）一律按内网处理
        return "." not in lowered
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _api_base_url_problem(url: str, *, allow_private: bool) -> str:
    """返回不可用的原因；空字符串表示没问题。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "不是合法的地址"
    if parts.scheme not in {"http", "https"}:
        return "不是 http(s) 地址"
    if parts.scheme == "http" and not allow_private:
        return "不是 https"
    host = parts.hostname or ""
    if not host:
        return "缺少主机名"
    if not allow_private and _is_private_host(host):
        return "指向本机或内网地址"
    return ""


class MiMoVoiceConfig(PluginConfigBase):
    """MiMo 语音回复配置"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    mimo: MiMoConfig = Field(default_factory=MiMoConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)


# `from __future__ import annotations` 让 pydantic 收到字符串注解：统一在这里重建一次，
# 宿主加载、单测直接 exec 模块都不会再报 "not fully defined"。
for _config_model in (
    PluginSectionConfig,
    GeneralConfig,
    TriggerConfig,
    FilterConfig,
    VoiceConfig,
    MiMoConfig,
    DeliveryConfig,
    MiMoVoiceConfig,
):
    _config_model.model_rebuild(_types_namespace=globals())
del _config_model

