"""MiMo 语音回复插件：把纯文本回复按策略替换成语音条。

设计要点：
- 合成走 MiMo 官方 `mimo-v2.5-tts-voiceclone`，参考音频固定、可预先用 ffmpeg 升调提速；
- 默认策略是“少量、短句”（`trigger.mode = "random"`，`probability = 0.05`，短句上限 60 字）；
- `trigger.mode = "random_llm"` 时随机抽签与模型主动请求并行：模型可以调 `mimo_voice_say`
  指定某轮用语音，随机抽签保证日常仍有一定出现频率；
- 合成前会对文本做一次朗读清洗（去表情/引号/波浪号/省略号，补齐句末标点），
  并把 `voice.style_instruction` 作为 user 消息随请求发送，用来压住音高漂移；
- 群聊/私聊支持 all / whitelist / blacklist 三种放行模式；
- 合成超时或失败默认回退为文字，保证消息不丢；
- 不做音频缓存：同一段文本重复出现的概率极低，缓存命中率不值得维护。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import aiohttp

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

SUPPORTED_REFERENCE_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".mpeg", ".ogg", ".opus", ".wav", ".wma"}
VOICE_SEGMENT_TYPE = "voice"
CLONE_PROMPT = "用原本的音色和语气说话，保持自然流畅，不要改变声线和说话习惯"
DEFAULT_STYLE_INSTRUCTION = "用同一个人的音色朗读，语速平稳，情绪自然，不要夸张，不要拖长尾音。"
_URL_PATTERN = re.compile(r"https?://\S+")
_CQ_PATTERN = re.compile(r"\[CQ:[^\]]+\]")
# 表情符号与零宽字符：朗读前必须清掉，否则会被念出来或者把音高带跑。
_EMOJI_PATTERN = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF\ufe0f\u200d]"
)
_LEAD_NOISE_PATTERN = re.compile(r"^[\s'\"“”‘’「」『』（）()\[\]【】《》<>~～\-—*_`#>、，,。]+")
_TAIL_NOISE_PATTERN = re.compile(r"[\s'\"“”‘’「」『』（）()\[\]【】《》<>*_`~～\-—、，,]+$")
_SENTENCE_END = "。！？!?"
# MiMo 侧可重试的 HTTP 状态：超时与 4xx 业务错误不重试，避免把总耗时翻倍。
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
# 小于这个字节数的 mp3 基本是空音频（约 0.15 秒），按失败处理。
_MIN_AUDIO_BYTES = 600
_SCOPE_TTL_SECONDS = 12 * 3600.0
_SCOPE_MAX_ENTRIES = 5000


class _RetryableSynthesisError(RuntimeError):
    """上游临时故障，可以按配置退避重试。"""


class _SynthesisBusy(RuntimeError):
    """并发已满，按 delivery.skip_when_busy 直接回退文字。"""



class PluginSectionConfig(PluginConfigBase):
    """插件基础开关"""

    __ui_label__ = "基础设置"

    enabled: bool = Field(default=True, description="是否启用语音回复")
    config_version: str = Field(default="0.2.1", description="配置文件版本号")


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
    probability: float = Field(default=0.05, ge=0.0, le=1.0, description="random / random_llm 模式下每条回复转语音的概率")
    random_decision_ttl: float = Field(default=8.0, ge=0.0, le=120.0, description="random 模式下同一聊天流的判定有效期（秒）")
    text_override_ttl: float = Field(default=15.0, ge=0.0, le=300.0, description="LLM 选择文字后，该聊天流保持文字的有效期（秒）")
    llm_request_ttl: float = Field(
        default=20.0,
        ge=0.0,
        le=300.0,
        description="random_llm 模式下，模型请求语音后该聊天流的语音有效期（秒）",
    )
    short_reply_max_chars: int = Field(
        default=60,
        ge=0,
        le=400,
        description="短句门槛：超过该字数的回复不转语音（0 表示不限制）；用于让语音集中在短句上",
    )
    overflow_policy: Literal["truncate", "text"] = Field(
        default="text",
        description="超过 general.max_text_length 时：truncate=截断后合成；text=保留文字不转语音",
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
        json_schema_extra={"placeholder": "例如: 589957759"},
    )
    private_user_ids: List[str] = Field(
        default_factory=list,
        description="私聊对方的 QQ 号列表（配合 private_mode 使用）",
        json_schema_extra={"placeholder": "例如: 1132855796"},
    )


class VoiceConfig(PluginConfigBase):
    """参考音频与音色"""

    __ui_label__ = "音色"

    reference_path: str = Field(default="reference.wav", description="固定参考音频（相对插件目录或绝对路径）")
    voice_dir: str = Field(default="voices", description="参考音频目录（reference_strategy 非 single_file 时使用）")
    reference_strategy: Literal["single_file", "best_single", "balanced", "full_merge"] = Field(
        default="single_file",
        description="single_file=直接用参考音频；best_single/balanced/full_merge 需要 ffmpeg，从 voice_dir 自动准备参考音频",
    )
    preferred_reference_file: str = Field(default="", description="best_single 时优先使用的文件名，留空自动挑选")
    max_clip_duration: float = Field(default=15.0, ge=1.0, le=60.0, description="best_single 截取的最长时长（秒）")
    balanced_clip_duration: float = Field(default=3.0, ge=1.0, le=30.0, description="balanced 每个文件截取的时长（秒）")
    silence_threshold_db: float = Field(default=-45.0, ge=-80.0, le=-10.0, description="静音判定阈值（dB）")
    sample_rate: int = Field(default=24000, ge=8000, le=48000, description="参考音频重采样率")
    reference_bitrate_kbps: int = Field(default=64, ge=16, le=320, description="full_merge 输出 MP3 的码率")
    max_reference_base64_mb: float = Field(default=9.5, ge=1.0, le=12.0, description="参考音频 Base64 上限（MiMo 限制约 10MB）")
    ffmpeg_path: str = Field(default="", description="ffmpeg 路径，留空则从 PATH 查找")
    clone_prompt: str = Field(default=CLONE_PROMPT, description="（已废弃，保留仅为兼容旧配置，不再生效）")
    style_instruction: str = Field(
        default=DEFAULT_STYLE_INSTRUCTION,
        description="随请求发送的朗读风格指令（user 消息）；用它压住音高漂移，留空表示不发送",
    )


class MiMoConfig(PluginConfigBase):
    """MiMo 合成参数"""

    __ui_label__ = "合成参数"

    api_key: str = Field(default="", description="MiMo 官方 API Key")
    api_base_url: str = Field(default="https://api.xiaomimimo.com/v1", description="MiMo API 基础地址")
    model: str = Field(default="mimo-v2.5-tts-voiceclone", description="TTS 模型名")
    audio_format: Literal["mp3", "wav"] = Field(default="mp3", description="返回音频格式")
    temperature: float = Field(default=0.15, ge=0.0, le=1.0, description="采样温度，越低音色越稳定")
    top_p: float = Field(default=1.0, ge=0.1, le=1.0, description="nucleus 采样")
    max_concurrent: int = Field(default=1, ge=1, le=4, description="同时合成路数，建议保持 1")
    retry_attempts: int = Field(default=0, ge=0, le=3, description="可重试错误（429/5xx/网络）的重试次数；超时不重试")
    retry_backoff_seconds: float = Field(default=1.5, ge=0.0, le=10.0, description="重试退避基数（秒），按 2 的幂指数增长")
    connect_timeout: float = Field(default=10.0, ge=1.0, le=60.0, description="建立连接的超时（秒），与整体超时分开")


class DeliveryConfig(PluginConfigBase):
    """发送行为"""

    __ui_label__ = "发送"

    fallback_to_text: bool = Field(default=True, description="合成失败/超时时是否回退发送文字")
    skip_when_busy: bool = Field(
        default=True,
        description="并发已满时直接发文字（True），或排队等待（False，可能把整条回复拖到超时）",
    )


class MiMoVoiceConfig(PluginConfigBase):
    """MiMo 语音回复配置"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    mimo: MiMoConfig = Field(default_factory=MiMoConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)


def _concat_path(path: Path) -> str:
    """ffmpeg concat demuxer 的 file 行：单引号内的单引号需要转义，否则带引号的文件名会解析错。"""
    return path.as_posix().replace("'", r"'\''")


class MiMoVoicePlugin(MaiBotPlugin):
    """把纯文本回复替换为 MiMo 语音条。"""

    config_model = MiMoVoiceConfig

    def __init__(self) -> None:
        super().__init__()
        self._session: Optional[aiohttp.ClientSession] = None
        self._random_decisions: Dict[str, Tuple[float, bool]] = {}
        self._text_override_sessions: Dict[str, float] = {}
        self._llm_voice_requests: Dict[str, float] = {}
        self._scope_cache: Dict[str, Tuple[float, Dict[str, str]]] = {}
        self._reference_uri: Optional[str] = None
        self._reference_signature: Optional[str] = None
        self._synthesis_slots = asyncio.Semaphore(1)

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            self.ctx.logger.info("MiMo 语音插件已加载（未启用）")
            return
        self._synthesis_slots = asyncio.Semaphore(max(1, int(self.config.mimo.max_concurrent)))
        if not self.config.mimo.api_key.strip():
            self.ctx.logger.warning("MiMo 语音插件未配置 api_key，所有回复将保持文字")
        strategy = self.config.voice.reference_strategy
        self.ctx.logger.info(
            "MiMo 语音插件已加载：mode=%s, probability=%.2f, 短句上限=%d字, 合成=%s, "
            "风格指令=%s, 参考策略=%s, 群聊放行=%s, 私聊放行=%s",
            self.config.trigger.mode,
            self.config.trigger.probability,
            int(self.config.trigger.short_reply_max_chars),
            self.config.mimo.model,
            "开" if self.config.voice.style_instruction.strip() else "关",
            strategy,
            self.config.filters.group_mode,
            self.config.filters.private_mode,
        )

    async def on_unload(self) -> None:
        await self._close_session()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        await self._close_session()
        self._random_decisions.clear()
        self._text_override_sessions.clear()
        self._llm_voice_requests.clear()
        self._scope_cache.clear()
        self._reference_uri = None
        self._reference_signature = None
        self._synthesis_slots = asyncio.Semaphore(max(1, int(self.config.mimo.max_concurrent)))
        self.ctx.logger.info("MiMo 语音插件配置已热更新")

    async def _close_session(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------ 参考音频与音色

    def _plugin_dir(self) -> Path:
        return Path(__file__).resolve().parent

    def _resolve_path(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (self._plugin_dir() / path)

    def _resolve_inside_plugin(self, raw: str, default_name: str, label: str) -> Path:
        """把配置里的路径限制在插件目录内，越界时退回默认文件，避免读到插件目录外的内容。"""
        plugin_dir = self._plugin_dir().resolve()
        resolved = self._resolve_path(raw).resolve()
        try:
            resolved.relative_to(plugin_dir)
            return resolved
        except ValueError:
            self.ctx.logger.warning("%s 指向插件目录之外（%s），已回退为默认的 %s", label, resolved, default_name)
            return (plugin_dir / default_name).resolve()

    def _reference_path(self) -> Path:
        return self._resolve_inside_plugin(self.config.voice.reference_path, "reference.wav", "voice.reference_path")

    def _voice_dir(self) -> Path:
        return self._resolve_inside_plugin(self.config.voice.voice_dir, "voices", "voice.voice_dir")

    def _audio_files(self) -> List[Path]:
        directory = self._voice_dir()
        if not directory.is_dir():
            return []
        return sorted(
            (item for item in directory.iterdir() if item.is_file() and item.suffix.lower() in SUPPORTED_REFERENCE_SUFFIXES),
            key=lambda item: item.name,
        )

    def _reference_source_files(self) -> List[Path]:
        strategy = self.config.voice.reference_strategy
        if strategy != "single_file":
            files = self._audio_files()
            if files:
                return files
        fixed = self._reference_path()
        return [fixed] if fixed.is_file() else []

    def _ffmpeg(self) -> Optional[str]:
        explicit = self.config.voice.ffmpeg_path.strip()
        if explicit:
            candidate = Path(explicit)
            if candidate.is_file():
                return str(candidate)
        return shutil.which("ffmpeg")

    def _build_reference_signature(self, files: List[Path]) -> str:
        payload = {
            "strategy": self.config.voice.reference_strategy,
            "files": [[item.name, item.stat().st_mtime_ns, item.stat().st_size] for item in files],
            "preferred": self.config.voice.preferred_reference_file,
            "clip": self.config.voice.max_clip_duration,
            "balanced": self.config.voice.balanced_clip_duration,
            "silence": self.config.voice.silence_threshold_db,
            "rate": self.config.voice.sample_rate,
            "bitrate": self.config.voice.reference_bitrate_kbps,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    def _cache_dir(self) -> Path:
        directory = self._runtime_cache_root() / "reference-cache"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _runtime_cache_root(self) -> Path:
        """优先使用宿主提供的 runtime_dir，避免把缓存写进插件源码目录。"""
        paths = getattr(self.ctx, "paths", None)
        runtime_dir = getattr(paths, "runtime_dir", None) if paths is not None else None
        if runtime_dir:
            try:
                root = Path(runtime_dir)
                root.mkdir(parents=True, exist_ok=True)
                return root
            except OSError:
                pass
        return self._plugin_dir()

    def _pick_best_single(self, files: List[Path]) -> Optional[Path]:
        preferred = self.config.voice.preferred_reference_file.strip()
        if preferred:
            for item in files:
                if item.name == preferred:
                    return item
        # 没有 ffprobe 时退化为“时长最接近上限”的近似选择：优先较大的文件，再截断。
        target = self.config.voice.max_clip_duration
        candidates = [item for item in files if item.stat().st_size > 8 * 1024]
        if not candidates:
            return None
        return max(candidates, key=lambda item: min(item.stat().st_size / 48000.0, target))

    def _build_reference_with_ffmpeg(self, files: List[Path]) -> Optional[bytes]:
        ffmpeg = self._ffmpeg()
        if ffmpeg is None:
            return None
        strategy = self.config.voice.reference_strategy
        rate = int(self.config.voice.sample_rate)
        workdir = self._cache_dir()
        output = workdir / f"reference-{self._reference_signature or 'x'}.wav"
        if output.is_file() and output.stat().st_size > 0:
            return output.read_bytes()
        try:
            if strategy == "best_single":
                source = self._pick_best_single(files)
                if source is None:
                    return None
                limit = float(self.config.voice.max_clip_duration)
                silence = float(self.config.voice.silence_threshold_db)
                filter_chain = (
                    f"silenceremove=start_periods=1:start_silence=0.1:start_threshold={silence}dB,"
                    f"areverse,silenceremove=start_periods=1:start_silence=0.1:start_threshold={silence}dB,areverse,"
                    f"atrim=0:{limit},asetpts=N/SR/TB"
                )
                command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                           "-af", filter_chain, "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", str(output)]
            elif strategy == "balanced":
                clip = float(self.config.voice.balanced_clip_duration)
                pieces = []
                for index, item in enumerate(files):
                    piece = workdir / f"part-{index}.wav"
                    subprocess.run(
                        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(item),
                         "-af", f"atrim=0:{clip},asetpts=N/SR/TB", "-ac", "1", "-ar", str(rate),
                         "-c:a", "pcm_s16le", str(piece)],
                        check=True, capture_output=True,
                    )
                    pieces.append(piece)
                if not pieces:
                    return None
                listing = workdir / "parts.txt"
                listing.write_text("".join(f"file '{_concat_path(piece)}'\n" for piece in pieces), encoding="utf-8")
                command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                           "-i", str(listing), "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", str(output)]
            else:  # full_merge
                listing = workdir / "merge.txt"
                listing.write_text("".join(f"file '{_concat_path(item)}'\n" for item in files), encoding="utf-8")
                merged = workdir / f"reference-{self._reference_signature or 'x'}.mp3"
                command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                           "-i", str(listing), "-ac", "1", "-ar", str(rate),
                           "-c:a", "libmp3lame", "-b:a", f"{int(self.config.voice.reference_bitrate_kbps)}k",
                           str(merged)]
                subprocess.run(command, check=True, capture_output=True)
                return merged.read_bytes()
            subprocess.run(command, check=True, capture_output=True)
            if output.is_file() and output.stat().st_size > 0:
                return output.read_bytes()
        except (OSError, subprocess.SubprocessError) as exc:
            self.ctx.logger.warning("参考音频预处理失败，回退为原始参考文件：%s", exc)
        return None

    def _reference_bytes(self) -> Optional[bytes]:
        files = self._reference_source_files()
        if not files:
            return None
        if self.config.voice.reference_strategy == "single_file":
            return files[0].read_bytes()
        signature = self._build_reference_signature(files)
        if signature != self._reference_signature:
            self._reference_signature = signature
            self._reference_uri = None
        prepared = self._build_reference_with_ffmpeg(files)
        if prepared is not None:
            return prepared
        return files[0].read_bytes()

    async def _ensure_reference_uri(self) -> str:
        if self._reference_uri:
            return self._reference_uri
        # ffmpeg 预处理是同步阻塞调用，放到线程里跑，避免卡住 Runner 事件循环。
        payload = await asyncio.to_thread(self._reference_bytes)
        if not payload:
            raise ValueError(f"未找到参考音频：{self._reference_path()}")
        limit = int(self.config.voice.max_reference_base64_mb * 1024 * 1024)
        if len(payload) > limit:
            raise ValueError(f"参考音频过大（{len(payload) / 1024 / 1024:.1f}MB），超过 {self.config.voice.max_reference_base64_mb}MB 限制")
        suffix = self._reference_path().suffix.lower().lstrip(".") or "wav"
        mime = f"audio/{suffix}" if suffix not in {"mpeg", "m4a"} else "audio/mpeg"
        encoded = base64.b64encode(payload).decode("ascii")
        self._reference_uri = f"data:{mime};base64,{encoded}"
        return self._reference_uri

    # ------------------------------------------------------------------- 合成

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=float(self.config.general.timeout),
                connect=float(self.config.mimo.connect_timeout),
            )
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _api_endpoint(self) -> str:
        base = self.config.mimo.api_base_url.strip().rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    async def _synthesize(self, text: str) -> bytes:
        api_key = self.config.mimo.api_key.strip()
        if not api_key:
            raise RuntimeError("未配置 MiMo api_key")
        reference = await self._ensure_reference_uri()
        messages: List[Dict[str, str]] = []
        style = (self.config.voice.style_instruction or "").strip()
        if style:
            # MiMo 只接受 user / assistant 两种角色；把风格指令放在 user 消息里，
            # 实测能把同文本多次合成的音高离散度从 σ≈35Hz 压到 σ≈6–14Hz。
            messages.append({"role": "user", "content": style})
        messages.append({"role": "assistant", "content": text})
        body = {
            "model": self.config.mimo.model,
            "messages": messages,
            "modalities": ["audio"],
            "temperature": float(self.config.mimo.temperature),
            "top_p": float(self.config.mimo.top_p),
            "audio": {
                "format": self.config.mimo.audio_format,
                "voice": reference,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "api-key": api_key,
        }
        session = await self._get_session()
        async with session.post(self._api_endpoint(), json=body, headers=headers) as response:
            if response.status != 200:
                # 只留一小段用于排障：完整响应体可能很长，也不该原样进日志
                detail = " ".join((await response.text())[:400].split())[:120]
                if response.status in _RETRYABLE_STATUS:
                    raise _RetryableSynthesisError(f"MiMo TTS 暂时不可用: {response.status} {detail}")
                raise RuntimeError(f"MiMo TTS 请求失败: {response.status} {detail}")
            payload = await response.json()
        try:
            audio_b64 = payload["choices"][0]["message"]["audio"]["data"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("MiMo TTS 返回中缺少音频数据") from exc
        if not isinstance(audio_b64, str) or not audio_b64.strip():
            raise _RetryableSynthesisError("MiMo TTS 返回空音频")
        try:
            audio = base64.b64decode(audio_b64)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("MiMo TTS 返回的音频不是合法 Base64") from exc
        if len(audio) < _MIN_AUDIO_BYTES:
            raise _RetryableSynthesisError(f"MiMo TTS 返回音频过短（{len(audio)} 字节）")
        return audio

    async def _synthesize_guarded(self, text: str) -> bytes:
        timeout = float(self.config.general.timeout)
        attempts = max(0, int(self.config.mimo.retry_attempts))
        backoff = max(0.0, float(self.config.mimo.retry_backoff_seconds))
        last_error: Optional[BaseException] = None
        for attempt in range(attempts + 1):
            if self._synthesis_slots.locked():
                if self.config.delivery.skip_when_busy:
                    raise _SynthesisBusy("已有语音正在合成")
                await self._synthesis_slots.acquire()
                acquired = True
            else:
                await self._synthesis_slots.acquire()
                acquired = True
            try:
                # 超时单独处理：重试只会让最坏耗时翻倍，这里直接失败并回退文字。
                return await asyncio.wait_for(self._synthesize(text), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"语音合成超时（{timeout:.0f}s）") from exc
            except (_RetryableSynthesisError, aiohttp.ClientError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                await asyncio.sleep(backoff * (2 ** attempt))
            except Exception as exc:  # noqa: BLE001 - 交给上层统一降级
                raise RuntimeError(f"语音合成失败: {exc}") from exc
            finally:
                if acquired:
                    self._synthesis_slots.release()
        raise RuntimeError(f"语音合成失败: {last_error}")

    # ------------------------------------------------------------------- 文本清洗

    @staticmethod
    def _sanitize_for_tts(text: str) -> Optional[str]:
        """把聊天文本整理成适合朗读的句子。

        去表情/零宽字符/首尾引号、把 ～ 和省略号折成逗号、补句末标点。
        实测这些符号会让合成音高漂移 2–4 个半音，并带来首尾的爆音与长静音。
        """
        cleaned = _URL_PATTERN.sub("", text)
        cleaned = _CQ_PATTERN.sub("", cleaned)
        cleaned = _EMOJI_PATTERN.sub("", cleaned)
        # 零宽字符/方向控制符会让模型把停顿读错；引号括号则会被念成奇怪的语气
        cleaned = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", cleaned)
        cleaned = re.sub(r"[「」『』“”‘’\"'（）()【】\[\]《》〈〉]", "", cleaned)
        cleaned = cleaned.replace("\u3000", " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = _LEAD_NOISE_PATTERN.sub("", cleaned)
        cleaned = _TAIL_NOISE_PATTERN.sub("", cleaned)
        cleaned = re.sub(r"[~～]+", "，", cleaned)
        cleaned = re.sub(r"(…+|\.{3,}|。{2,})", "，", cleaned)
        cleaned = re.sub(r"[!！]{2,}", "！", cleaned)
        cleaned = re.sub(r"[?？]{2,}", "？", cleaned)
        cleaned = re.sub(r"[，,、]{2,}", "，", cleaned)
        cleaned = re.sub(r"，+([。！？])", r"\1", cleaned)
        cleaned = cleaned.strip("，,、 ")
        if not cleaned:
            return None
        if cleaned[-1] not in _SENTENCE_END:
            cleaned += "。"
        return cleaned

    # ------------------------------------------------------------------- 过滤

    def _normalize_ids(self, values: List[str]) -> set[str]:
        return {str(item).strip() for item in values if str(item).strip()}

    def _filter_allows(self, scope: str, group_id: str, user_id: str) -> bool:
        if scope == "group":
            mode = self.config.filters.group_mode
            if mode == "all":
                return True
            ident = group_id
            listed = ident in self._normalize_ids(self.config.filters.group_ids)
        else:
            mode = self.config.filters.private_mode
            if mode == "all":
                return True
            ident = user_id
            listed = ident in self._normalize_ids(self.config.filters.private_user_ids)
        if not ident:
            # 拿不到对方身份（映射过期等）时保守处理：保持文字，避免误放行
            return False
        return listed if mode == "whitelist" else not listed

    def _remember_scope(self, scope: str, session_id: str, group_id: str, user_id: str) -> None:
        if not session_id:
            return
        now = time.monotonic()
        if len(self._scope_cache) > _SCOPE_MAX_ENTRIES:
            self._scope_cache = {key: value for key, value in self._scope_cache.items() if value[0] > now}
        self._scope_cache[session_id] = (now + _SCOPE_TTL_SECONDS, {"scope": scope, "group_id": group_id, "user_id": user_id})

    def _lookup_scope(self, session_id: str) -> Dict[str, str]:
        entry = self._scope_cache.get(session_id)
        if entry is None:
            return {}
        expires_at, data = entry
        if expires_at < time.monotonic():
            self._scope_cache.pop(session_id, None)
            return {}
        return data

    @HookHandler(
        "chat.receive.before_process",
        name="mimo_voice_remember_scope",
        description="记录会话的群号与私聊对方 QQ 号，供出站语音放行规则使用",
        mode=HookMode.OBSERVE,
        order=HookOrder.EARLY,
    )
    async def remember_scope(self, message: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        if self.config.filters.group_mode == "all" and self.config.filters.private_mode == "all":
            return  # 全放行不需要维护映射，避免每条入站消息都走一遍
        payload = message if isinstance(message, dict) else {}
        session_id = str(payload.get("session_id") or "").strip()
        scope = str(payload.get("scope") or "").strip()
        info = payload.get("message_info") if isinstance(payload.get("message_info"), dict) else {}
        group_info = info.get("group_info") if isinstance(info.get("group_info"), dict) else {}
        user_info = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
        self._remember_scope(scope, session_id, str(group_info.get("group_id") or ""), str(user_info.get("user_id") or ""))

    # ------------------------------------------------------------------- 触发

    def _stream_id(self, message: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
        return str(kwargs.get("stream_id") or message.get("session_id") or "").strip()

    def _plain_text_message(self, message: Dict[str, Any]) -> Optional[str]:
        raw = message.get("raw_message")
        if not isinstance(raw, list) or not raw:
            return None
        segments = [item for item in raw if isinstance(item, dict)]
        # 引用回复会带一个 reply 段，不能因为它就把整条消息当成“非纯文本”跳过：
        # 转语音时引用段本来就会被移除。这里只接受 text(+reply) 组合，且必须恰好一个 text。
        types = [str(segment.get("type") or "") for segment in segments]
        if any(kind not in {"text", "reply"} for kind in types) or types.count("text") != 1:
            return None
        segment = segments[types.index("text")]
        data = segment.get("data")
        text = data if isinstance(data, str) else (data or {}).get("text") if isinstance(data, dict) else None
        if not isinstance(text, str):
            return None
        text = _CQ_PATTERN.sub("", text).strip()
        return text or None

    def _text_override_active(self, stream_id: str) -> bool:
        expires_at = self._text_override_sessions.get(stream_id, 0.0)
        if expires_at <= time.monotonic():
            self._text_override_sessions.pop(stream_id, None)
            return False
        return True

    def _random_voice_enabled(self, stream_id: str) -> bool:
        now = time.monotonic()
        entry = self._random_decisions.get(stream_id)
        if entry is not None and entry[0] >= now:
            return entry[1]
        enabled = random.random() < float(self.config.trigger.probability)
        self._random_decisions[stream_id] = (now + float(self.config.trigger.random_decision_ttl), enabled)
        if len(self._random_decisions) > _SCOPE_MAX_ENTRIES:
            self._random_decisions = {key: value for key, value in self._random_decisions.items() if value[0] >= now}
        return enabled

    def _llm_voice_request_active(self, stream_id: str) -> bool:
        expires_at = self._llm_voice_requests.get(stream_id, 0.0)
        if expires_at <= time.monotonic():
            self._llm_voice_requests.pop(stream_id, None)
            return False
        return True

    def _prepare_text(self, text: str) -> Optional[str]:
        cleaned = self._sanitize_for_tts(text)
        if not cleaned:
            return None
        if len(cleaned) > int(self.config.general.max_text_length):
            if self.config.trigger.overflow_policy == "text":
                return None
            cleaned = cleaned[: int(self.config.general.max_text_length)].rstrip("，,、 ") + "。"
        return cleaned

    def _short_enough(self, text: str) -> bool:
        cap = int(self.config.trigger.short_reply_max_chars)
        return cap <= 0 or len(text) <= cap

    def _should_convert(self, text: str, stream_id: str, scope: str, group_id: str, user_id: str) -> bool:
        if scope not in ("group", "private"):
            return False
        if not self._filter_allows(scope, group_id, user_id):
            return False
        mode = self.config.trigger.mode
        if mode == "voice_only":
            return True
        if mode == "llm_trigger":
            return not self._text_override_active(stream_id)
        if mode == "random_llm" and self._llm_voice_request_active(stream_id):
            # 模型点名要语音时不受短句上限约束，长句也可以，只是要过 max_text_length
            return True
        if not self._short_enough(text):
            return False
        return self._random_voice_enabled(stream_id)

    # --------------------------------------------------------------- 出站 Hook

    @HookHandler(
        "send_service.before_send",
        name="mimo_voice_before_send",
        description="按配置把纯文本回复原位转换为语音，失败时按配置回退文字",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        # 默认配置（timeout 20s、retry 0）下 130s 已很宽松；如果调大 general.timeout 或
        # retry_attempts，整条出站链路可能触及这个上限，此时宿主会放弃本 hook、按文字原样发送。
        timeout_ms=130000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def convert_outbound_text(self, message: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if not self.config.plugin.enabled:
            return None
        outbound = message if isinstance(message, dict) else {}
        text = self._plain_text_message(outbound)
        if text is None:
            return None
        stream_id = self._stream_id(outbound, kwargs)
        if not stream_id:
            return None

        scope = str(outbound.get("scope") or "").strip()
        info = outbound.get("message_info") if isinstance(outbound.get("message_info"), dict) else {}
        group_info = info.get("group_info") if isinstance(info.get("group_info"), dict) else {}
        group_id = str(group_info.get("group_id") or "").strip()
        cached = self._lookup_scope(stream_id)
        user_id = cached.get("user_id", "")
        scope = scope or cached.get("scope", "")
        if not scope:
            scope = "group" if group_id else "private"

        prepared = self._prepare_text(text)
        if prepared is None:
            return None
        if not self._should_convert(prepared, stream_id, scope, group_id, user_id):
            return None

        try:
            audio = await self._synthesize_guarded(prepared)
        except Exception as exc:  # noqa: BLE001 - 统一降级
            self.ctx.logger.warning("语音合成失败（%s），%s", exc, "回退文字" if self.config.delivery.fallback_to_text else "拦截消息")
            return None if self.config.delivery.fallback_to_text else {"action": "abort"}

        outbound["raw_message"] = [
            {
                "type": VOICE_SEGMENT_TYPE,
                "data": "",
                "hash": hashlib.sha256(audio).hexdigest(),
                "binary_data_base64": base64.b64encode(audio).decode("ascii"),
            }
        ]
        outbound["processed_plain_text"] = prepared
        had_reply = bool(kwargs.get("set_reply")) or bool(outbound.get("reply_to"))
        outbound["reply_to"] = None
        kwargs["message"] = outbound
        kwargs["set_reply"] = False
        kwargs["reply_message_id"] = ""
        # 模型的一次性语音许可用掉就清掉，避免影响下一轮
        self._llm_voice_requests.pop(stream_id, None)
        self._text_override_sessions.pop(stream_id, None)
        if had_reply:
            self.ctx.logger.info("QQ 语音不支持引用组合，已移除会话 %s 的引用段", stream_id)
        self.ctx.logger.info("已将会话 %s 的纯文本回复替换为语音（%d 字）", stream_id, len(prepared))
        return {"action": "continue", "modified_kwargs": kwargs}

    # ------------------------------------------------------------ 规划器工具

    _TOOL_TEXT_REPLY = "mimo_voice_text_reply"
    _TOOL_VOICE_SAY = "mimo_voice_say"

    @HookHandler(
        "maisaka.planner.before_request",
        name="mimo_voice_planner_mode",
        description="按触发模式隐藏无关的语音工具，避免模型误调",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def configure_planner_tools(self, **kwargs: Any) -> Dict[str, Any]:
        tool_definitions = kwargs.get("tool_definitions")
        if not isinstance(tool_definitions, list):
            return None
        # random / voice_only 模式由抽样决定，不需要模型参与；
        # llm_trigger 只保留"改文字"工具，random_llm 只保留"要语音"工具。
        allowed = {
            "llm_trigger": {self._TOOL_TEXT_REPLY},
            "random_llm": {self._TOOL_VOICE_SAY},
        }.get(self.config.trigger.mode, set())
        hidden = {self._TOOL_TEXT_REPLY, self._TOOL_VOICE_SAY} - allowed
        if not hidden:
            return None
        tools = [definition for definition in tool_definitions if self._tool_definition_name(definition) not in hidden]
        if len(tools) == len(tool_definitions):
            return None
        kwargs["tool_definitions"] = tools
        return {"action": "continue", "modified_kwargs": kwargs}

    @staticmethod
    def _tool_definition_name(definition: Any) -> str:
        if isinstance(definition, dict):
            function = definition.get("function")
            if isinstance(function, dict):
                return str(function.get("name") or "")
            return str(definition.get("name") or "")
        return str(getattr(definition, "name", "") or "")

    @Tool(
        "mimo_voice_text_reply",
        description="让当前这一轮回复保留文字，不要转换成语音。适合代码、表格、网址或较长的说明。",
        visibility="visible",
        parameters={"type": "object", "properties": {}},
    )
    async def allow_text_reply(self, **kwargs: Any) -> Dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or kwargs.get("session_id") or "").strip()
        if not stream_id:
            return {"success": False, "content": "无法获取真实聊天流 session_id"}
        self._text_override_sessions[stream_id] = time.monotonic() + float(self.config.trigger.text_override_ttl)
        self._llm_voice_requests.pop(stream_id, None)
        return {"success": True, "content": "这一轮将使用文字回复。"}

    @Tool(
        "mimo_voice_say",
        description="让这一轮的回复用语音说出来。适合对方想听声音、或这句话用语音更自然的时候；"
        "调用后正常生成回复即可，内容会自动转成语音发出。",
        visibility="visible",
        parameters={"type": "object", "properties": {}},
    )
    async def request_voice_reply(self, **kwargs: Any) -> Dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or kwargs.get("session_id") or "").strip()
        if not stream_id:
            return {"success": False, "content": "无法获取真实聊天流 session_id"}
        self._llm_voice_requests[stream_id] = time.monotonic() + float(self.config.trigger.llm_request_ttl)
        self._text_override_sessions.pop(stream_id, None)
        return {"success": True, "content": "这一轮会用语音回复，正常给出回复内容即可。"}


def create_plugin() -> MiMoVoicePlugin:
    return MiMoVoicePlugin()
