"""MiMo 语音回复插件：把纯文本回复按策略替换成语音条。

本文件是插件入口：生命周期、Hook 派发、过滤策略、参考音频管理与合成编排。
可以复用的纯逻辑拆在同目录的 ``core`` 子包里：

* ``core.constants`` —— 阈值、默认值与参考音频参数
* ``core.config``    —— 配置模型与 ``api_base_url`` 端点校验
* ``core.text``      —— 朗读清洗与英文口癖策略
* ``core.audio``     —— WAV 读写、频谱曲线、音色对齐矩阵

行为说明见 README。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import math
import random
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast
from urllib.parse import urlsplit

import aiohttp
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .core import config as _config
from .core import text as _text
from .core.audio import (
    _apply_gain_curve,
    _concat_path,
    _numpy,
    _read_wav_pcm,
    _spectral_curve,
    _stderr_excerpt,
    _write_wav_pcm,
)
from .core.config import MiMoVoiceConfig
from .core.constants import (
    _AUDIO_SECONDS_CAP,
    _AUDIO_SECONDS_PER_CHAR,
    _FFMPEG_BIN,
    _MAX_AUDIO_SECONDS,
    _MIN_AUDIO_BYTES,
    _MIN_AUDIO_SECONDS,
    _MIN_RMS_DBFS,
    _PEAK_GAIN_EPSILON,
    _PEAK_GAIN_MAX,
    _PEAK_GAIN_MIN,
    _PEAK_TARGET_DBFS,
    _REFERENCE_BALANCED_CLIP_SECONDS,
    _REFERENCE_BITRATE_KBPS,
    _REFERENCE_MAX_BYTES,
    _REFERENCE_MAX_CLIP_SECONDS,
    _REFERENCE_SAMPLE_RATE,
    _REFERENCE_SILENCE_DB,
    _RETRYABLE_STATUS,
    _SCOPE_CACHE_MAX_ENTRIES,
    _SCOPE_TTL_SECONDS,
    _SILENCE_DBFS,
    _TAIL_KEEP_SECONDS,
    _TIMBRE_MAX_GAIN_DB,
    _TTL_CACHE_MAX_ENTRIES,
    PRESET_REFERENCE_FILES,
    SUPPORTED_REFERENCE_SUFFIXES,
    VOICE_SEGMENT_TYPE,
)


class _RetryableSynthesisError(RuntimeError):
    """上游临时故障，可以按配置退避重试。"""


class _SynthesisBusy(RuntimeError):
    """并发已满，按 delivery.skip_when_busy 直接回退文字。"""


class MiMoVoicePlugin(MaiBotPlugin):
    """把纯文本回复替换为 MiMo 语音条。"""

    config_model = MiMoVoiceConfig

    @property
    def config(self) -> "MiMoVoiceConfig":
        """宿主注入的配置实例。这里收窄类型，让静态检查能看懂各分节字段。"""
        instance = self._plugin_config_instance
        if instance is None:
            raise RuntimeError("插件配置尚未完成注入")
        return cast("MiMoVoiceConfig", instance)

    def __init__(self) -> None:
        super().__init__()
        self._session: Optional[aiohttp.ClientSession] = None
        self._random_decisions: Dict[str, Tuple[float, bool]] = {}
        self._text_override_sessions: Dict[str, float] = {}
        self._llm_voice_requests: Dict[str, float] = {}
        self._scope_cache: Dict[str, Tuple[float, Dict[str, str]]] = {}
        self._reference_uri: Optional[str] = None
        self._reference_stamp: str = ""
        self._reference_signature: Optional[str] = None
        self._timbre_target_cache: Optional[List[float]] = None
        self._timbre_target_stamp: str = ""
        self._reference_built_suffix: Optional[str] = None
        self._warned: set[str] = set()
        self._synthesis_slots = asyncio.Semaphore(1)

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            self.ctx.logger.info("MiMo 语音插件已加载（未启用）")
            return
        self._synthesis_slots = asyncio.Semaphore(max(1, int(self.config.mimo.max_concurrent)))
        if not self.config.mimo.api_key.strip():
            self.ctx.logger.warning("MiMo 语音插件未配置 api_key，所有回复将保持文字")
        data_dir = self._data_dir()
        if data_dir is not None:
            try:
                (data_dir / "references").mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.ctx.logger.warning("无法创建参考音频目录 %s：%s", data_dir / "references", exc)
        strategy = self.config.voice.reference_strategy
        self.ctx.logger.info(
            "MiMo 语音插件已加载：音色=%s, mode=%s, probability=%.2f, 短句上限=%d字, 合成=%s, "
            "风格指令=%s, 参考策略=%s, 英文=%s, 随机需纯中文=%s, 超时重试=%s, 音色对齐=%s, "
            "尾部裁剪=%dms, 尾部补静音=%dms, 峰值归一=%s, 群聊放行=%s, 私聊放行=%s",
            self.config.voice.preset,
            self.config.trigger.mode,
            self.config.trigger.probability,
            int(self.config.trigger.short_reply_max_chars),
            self.config.mimo.model,
            "开" if self.config.voice.style_instruction.strip() else "关",
            strategy,
            self.config.trigger.english_policy,
            "开" if self.config.trigger.random_requires_chinese else "关",
            "开" if self.config.mimo.retry_on_timeout else "关",
            "开" if self.config.delivery.timbre_match else "关",
            int(self.config.delivery.trim_tail_silence_ms),
            int(self.config.delivery.tail_pad_ms),
            "开" if self.config.delivery.peak_normalize else "关",
            self.config.filters.group_mode,
            self.config.filters.private_mode,
        )
        if data_dir is not None:
            self.ctx.logger.info("参考音频目录：%s（自定义音色把文件放这里）", data_dir / "references")
        self._prune_reference_cache()
        self._flush_config_warnings()
        base_host = urlsplit(self.config.mimo.api_base_url).hostname or ""
        if base_host and not base_host.endswith("xiaomimimo.com"):
            self.ctx.logger.warning(
                "待合成文本与参考音频会发送到自建/第三方地址 %s，请确认可信；"
                "若只是误填，把 mimo.api_base_url 改回 https://api.xiaomimimo.com/v1",
                base_host,
            )

    def _flush_config_warnings(self) -> None:
        """把本轮配置校验攒下的告警按类别去重后打一次就清空。

        同一节里多个字段同时填错时，避免刷出一屏相似的 warning：
        每类（按冒号前的字段分组）只打第一条，最多 5 类，其余汇总成一行。
        """
        shown: set[str] = set()
        buckets = 0
        for warning in _config.drain_config_warnings():
            key = warning.split(":", 1)[0]
            if key in shown:
                continue
            shown.add(key)
            if buckets >= 5:
                continue
            buckets += 1
            self.ctx.logger.warning(warning)
        hidden = len(shown) - buckets
        if hidden > 0:
            self.ctx.logger.warning("另有 %d 类配置告警未展开，详见 config.toml 与插件文档", hidden)

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
        self._reference_stamp = ""
        self._reference_signature = None
        self._timbre_target_cache = None
        self._timbre_target_stamp = ""
        self._reference_built_suffix = None
        self._warned.clear()
        self._synthesis_slots = asyncio.Semaphore(max(1, int(self.config.mimo.max_concurrent)))
        self.ctx.logger.info("MiMo 语音插件配置已热更新")
        self._flush_config_warnings()

    async def _close_session(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------ 参考音频与音色

    def _plugin_dir(self) -> Path:
        return Path(__file__).resolve().parent

    def _data_dir(self) -> Optional[Path]:
        """插件数据目录（宿主注入，通常是 data/plugins/<插件 id>）；取不到时返回 None。"""
        paths = getattr(self.ctx, "paths", None)
        raw = getattr(paths, "data_dir", None)
        if not raw:
            return None
        try:
            return Path(raw).expanduser().resolve()
        except (OSError, ValueError):
            return None

    def _user_roots(self) -> List[Path]:
        """用户素材的查找顺序：数据目录 references/ → 数据目录 → 插件目录（兼容旧布局）。"""
        roots: List[Path] = []
        data_dir = self._data_dir()
        if data_dir is not None:
            roots.append(data_dir / "references")
            roots.append(data_dir)
        roots.append(self._plugin_dir())
        return roots

    def _locate_user_file(self, raw: str, default_name: str, label: str) -> Path:
        """在用户素材目录里找文件；只接受相对路径，越界或找不到时给出可预期的落点。"""
        name = (raw or "").strip() or default_name
        parts = Path(name).parts
        if Path(name).is_absolute() or ".." in parts:
            self._warn_once(
                f"path:{label}:{name}",
                "%s 只接受相对路径（%s），已回退为默认的 %s",
                label,
                name,
                default_name,
            )
            name = default_name
        fallback: Optional[Path] = None
        for root in self._user_roots():
            root = root.resolve()
            candidate = (root / name).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if fallback is None:
                fallback = candidate
            if candidate.is_file():
                return candidate
        return fallback or (self._plugin_dir() / default_name).resolve()

    def _preset_path(self, preset_file: str) -> Optional[Path]:
        """内置音色文件：随插件分发（插件目录），也允许在数据目录覆盖。"""
        for root in self._user_roots():
            candidate = (root / preset_file).resolve()
            if candidate.is_file():
                return candidate
        return None

    def _reference_path(self) -> Path:
        """解析当前要用的参考音频：内置预设优先，找不到再退回 voice.reference_path。"""
        preset = str(getattr(self.config.voice, "preset", "custom") or "custom").strip().lower()
        preset_file = PRESET_REFERENCE_FILES.get(preset)
        if preset_file:
            candidate = self._preset_path(preset_file)
            if candidate is not None:
                return candidate
            self._warn_once(
                f"builtin:{preset}",
                "内置音色 %s 的参考音频缺失（%s），回退到 voice.reference_path",
                preset,
                preset_file,
            )
        return self._locate_user_file(self.config.voice.reference_path, "reference.wav", "voice.reference_path")

    def _voice_dir(self) -> Path:
        data_dir = self._data_dir()
        if data_dir is not None and (data_dir / "voices").is_dir():
            return (data_dir / "voices").resolve()
        return (self._plugin_dir() / "voices").resolve()

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
        return shutil.which(_FFMPEG_BIN)

    def _build_reference_signature(self, files: List[Path]) -> str:
        payload = {
            "strategy": self.config.voice.reference_strategy,
            "files": [[item.name, item.stat().st_mtime_ns, item.stat().st_size] for item in files],
            "clip": _REFERENCE_MAX_CLIP_SECONDS,
            "balanced": _REFERENCE_BALANCED_CLIP_SECONDS,
            "silence": _REFERENCE_SILENCE_DB,
            "rate": _REFERENCE_SAMPLE_RATE,
            "bitrate": _REFERENCE_BITRATE_KBPS,
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

    def _prune_reference_cache(self, keep: int = 6) -> None:
        """参考音频缓存只增不减会慢慢占满卷：按修改时间只留最近几个。"""
        try:
            directory = self._cache_dir()
            files = sorted(
                (path for path in directory.iterdir() if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        removed = 0
        for path in files[keep:]:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        # balanced 的分片目录、full_merge 的列表文件不在顶层，一并收掉
        try:
            extras = list(directory.iterdir())
        except OSError:
            extras = []
        for extra in extras:
            try:
                if extra.is_dir() and extra.name.startswith("parts-"):
                    shutil.rmtree(extra)
                    removed += 1
                elif extra.is_file() and extra.name.startswith("merge-"):
                    extra.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            self.ctx.logger.info("参考音频缓存已清理 %d 个旧文件", removed)

    def _pick_best_single(self, files: List[Path]) -> Optional[Path]:
        """挑一段最接近上限时长的素材：能读 WAV 头就按真实时长，读不了才按体积估算。"""
        limit = float(_REFERENCE_MAX_CLIP_SECONDS)
        bytes_per_second = float(_REFERENCE_SAMPLE_RATE) * 2.0
        candidates = [item for item in files if item.stat().st_size > 8 * 1024]
        if not candidates:
            return None

        def score(item: Path) -> float:
            try:
                with wave.open(str(item), "rb") as handle:
                    seconds = handle.getnframes() / float(handle.getframerate() or 1)
            except (OSError, wave.Error, EOFError):
                seconds = item.stat().st_size / bytes_per_second
            return min(seconds, limit)

        return max(candidates, key=score)

    def _run_ffmpeg(self, command: List[str], what: str) -> bool:
        """统一跑 ffmpeg：不进 shell、不读 stdin、失败只留一行摘要。"""
        try:
            subprocess.run(command, check=True, capture_output=True, stdin=subprocess.DEVNULL)
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            self._warn_once(f"ffmpeg:{what}", "参考音频%s失败：%s", what, _stderr_excerpt(exc))
            return False

    def _encode_reference(self, source: Path, output: Path, filter_chain: str, what: str) -> bool:
        ffmpeg = self._ffmpeg()
        if ffmpeg is None:
            return False
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                   "-af", filter_chain, "-ac", "1", "-ar", str(int(_REFERENCE_SAMPLE_RATE)),
                   "-c:a", "pcm_s16le", str(output)]
        return self._run_ffmpeg(command, what)

    def _concat_reference(self, listing: Path, output: Path, codec_args: List[str], what: str) -> bool:
        ffmpeg = self._ffmpeg()
        if ffmpeg is None:
            return False
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                   "-i", str(listing), "-ac", "1", "-ar", str(int(_REFERENCE_SAMPLE_RATE))]
        command = [*command, *codec_args, str(output)]
        return self._run_ffmpeg(command, what)

    def _build_reference_with_ffmpeg(self, files: List[Path]) -> Optional[bytes]:
        strategy = self.config.voice.reference_strategy
        signature = self._reference_signature or "x"
        workdir = self._cache_dir()
        suffix = ".mp3" if strategy == "full_merge" else ".wav"
        output = workdir / f"reference-{signature}{suffix}"
        if output.is_file() and output.stat().st_size > 0:
            return output.read_bytes()
        limit = float(_REFERENCE_MAX_CLIP_SECONDS)
        silence = float(_REFERENCE_SILENCE_DB)
        trim_chain = (
            f"silenceremove=start_periods=1:start_silence=0.1:start_threshold={silence}dB,"
            f"areverse,silenceremove=start_periods=1:start_silence=0.1:start_threshold={silence}dB,areverse,"
        )
        ok = False
        if strategy == "best_single":
            source = self._pick_best_single(files)
            if source is None:
                return None
            ok = self._encode_reference(
                source, output, f"{trim_chain}atrim=0:{limit},asetpts=N/SR/TB", "裁剪"
            )
        elif strategy == "balanced":
            clip = float(_REFERENCE_BALANCED_CLIP_SECONDS)
            pieces_dir = workdir / f"parts-{signature}"
            pieces_dir.mkdir(parents=True, exist_ok=True)
            pieces = []
            for index, item in enumerate(files):
                piece = pieces_dir / f"part-{index}.wav"
                if self._encode_reference(item, piece, f"atrim=0:{clip},asetpts=N/SR/TB", "截段"):
                    pieces.append(piece)
            if not pieces:
                return None
            listing = pieces_dir / "parts.txt"
            listing.write_text(
                "".join(f"file '{_concat_path(piece)}'\n" for piece in pieces), encoding="utf-8"
            )
            ok = self._concat_reference(listing, output, ["-c:a", "pcm_s16le"], "拼接")
        else:  # full_merge
            listing = workdir / f"merge-{signature}.txt"
            listing.write_text(
                "".join(f"file '{_concat_path(item)}'\n" for item in files), encoding="utf-8"
            )
            ok = self._concat_reference(
                listing,
                output,
                ["-c:a", "libmp3lame", "-b:a", f"{int(_REFERENCE_BITRATE_KBPS)}k"],
                "合并",
            )
        if ok and output.is_file() and output.stat().st_size > 0:
            return output.read_bytes()
        return None

    def _reference_bytes(self) -> Optional[bytes]:
        files = self._reference_source_files()
        if not files:
            return None
        if self.config.voice.reference_strategy == "single_file":
            self._reference_built_suffix = files[0].suffix.lower() or ".wav"
            return files[0].read_bytes()
        signature = self._build_reference_signature(files)
        if signature != self._reference_signature:
            self._reference_signature = signature
            self._reference_uri = None
        prepared = self._build_reference_with_ffmpeg(files)
        if prepared is not None:
            # full_merge 走 libmp3lame，实际字节是 mp3，data URI 的 mime 要跟着变
            self._reference_built_suffix = (
                ".mp3" if self.config.voice.reference_strategy == "full_merge" else ".wav"
            )
            return prepared
        self._reference_built_suffix = files[0].suffix.lower() or ".wav"
        return files[0].read_bytes()

    def _reference_stamp_value(self) -> str:
        """参考音频的"指纹"：换文件或重新生成后要能让缓存的 data URI 失效。"""
        try:
            path = self._reference_path()
            stat = path.stat()
        except OSError:
            return "missing"
        return f"{path}|{stat.st_mtime_ns}|{stat.st_size}"

    async def _ensure_reference_uri(self) -> str:
        stamp = self._reference_stamp_value()
        if self._reference_uri and stamp == self._reference_stamp:
            return self._reference_uri
        # ffmpeg 预处理是同步阻塞调用，放到线程里跑，避免卡住 Runner 事件循环。
        payload = await asyncio.to_thread(self._reference_bytes)
        if not payload:
            raise ValueError(f"未找到参考音频：{self._reference_path()}")
        limit = _REFERENCE_MAX_BYTES
        if len(payload) > limit:
            raise ValueError(f"参考音频过大（{len(payload) / 1024 / 1024:.1f}MB），超过 9.5MB 限制")
        built = (self._reference_built_suffix or "").lstrip(".").lower()
        suffix = built or self._reference_path().suffix.lower().lstrip(".") or "wav"
        mime = f"audio/{suffix}" if suffix not in {"mpeg", "m4a"} else "audio/mpeg"
        encoded = base64.b64encode(payload).decode("ascii")
        self._reference_uri = f"data:{mime};base64,{encoded}"
        self._reference_stamp = stamp
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
            "audio": {
                "format": self._request_audio_format(),
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
        return self._validate_audio(audio, self._request_audio_format(), text)

    def _max_audio_seconds(self, text: str) -> float:
        """按文本长度估算可接受的音频上限：短句 20 秒封底，长句最多放宽到 90 秒。"""
        estimated = len(text) * _AUDIO_SECONDS_PER_CHAR
        return min(_AUDIO_SECONDS_CAP, max(_MAX_AUDIO_SECONDS, estimated))

    def _validate_audio(self, audio: bytes, audio_format: str, text: str = "") -> bytes:
        """拦住空音频、截断音频、几乎无声的音频：发出去只会让用户听到空气。"""
        if len(audio) < _MIN_AUDIO_BYTES:
            raise _RetryableSynthesisError(f"MiMo TTS 返回音频过短（{len(audio)} 字节）")
        if audio_format != "wav":
            return audio
        try:
            with wave.open(io.BytesIO(audio), "rb") as handle:
                frames = handle.getnframes()
                rate = handle.getframerate()
                width = handle.getsampwidth()
                channels = handle.getnchannels()
                raw = handle.readframes(frames)
        except (wave.Error, EOFError) as exc:
            raise _RetryableSynthesisError(f"MiMo TTS 返回的 WAV 无法解析（{exc}）") from exc
        if rate <= 0 or frames <= 0 or not raw:
            raise _RetryableSynthesisError("MiMo TTS 返回的音频没有采样")
        duration = frames / float(rate)
        if duration < _MIN_AUDIO_SECONDS:
            raise _RetryableSynthesisError(f"MiMo TTS 返回音频过短（{duration:.2f}s）")
        limit = self._max_audio_seconds(text)
        if duration > limit:
            # 确定性错误：重试一次只会得到同样长的音频，直接按失败处理
            raise RuntimeError(f"MiMo TTS 返回音频过长（{duration:.1f}s，上限 {limit:.0f}s）")
        if width != 2:
            return audio
        np = _numpy()
        if np is None:
            return audio
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size == 0 or channels <= 0:
            raise _RetryableSynthesisError("MiMo TTS 返回的音频为空")
        rms = float(np.sqrt(np.mean(np.square(samples))))
        dbfs = 20.0 * math.log10(max(rms, 1e-9))
        if dbfs < _MIN_RMS_DBFS:
            raise _RetryableSynthesisError(f"MiMo TTS 返回的音频几乎无声（{dbfs:.1f} dBFS）")
        return audio

    def _postprocess(self, audio: bytes) -> bytes:
        """音色对齐 + 尾音裁剪 + 峰值归一 + 尾部补静音，一次读完、一次写回。

        以前是三个函数、两次线程切换、两次 WAV 解析/写回，每条语音多花几十毫秒
        还刷三条日志；合并后单条只解析一次、只写一次、只打一行。
        """
        delivery = self.config.delivery
        if not self._needs_pcm_processing():
            return audio
        np = _numpy()
        if np is None:
            self._warn_once("numpy:post", "后处理需要 numpy，当前环境没有，已跳过音色对齐与尾音处理")
            return audio
        try:
            samples, rate = _read_wav_pcm(audio)
        except Exception:
            return audio
        if samples.size == 0 or rate <= 0:
            return audio
        before_seconds = samples.size / float(rate)
        distance_db = None
        changed = False
        if delivery.timbre_match:
            target = self._timbre_target()
            if target is not None:
                try:
                    target_arr = np.asarray(target, dtype=np.float32)
                    curve = _spectral_curve(samples, rate)
                    gains = np.clip(
                        (target_arr - curve) * float(delivery.timbre_match_strength),
                        -_TIMBRE_MAX_GAIN_DB,
                        _TIMBRE_MAX_GAIN_DB,
                    )
                    distance_db = float(np.sqrt(np.mean((target_arr - curve) ** 2)))
                    samples = _apply_gain_curve(samples, rate, gains)
                    changed = True
                except Exception as exc:
                    self._warn_once("timbre:apply", "音色对齐失败（%s），本条按原样处理", exc)
        try:
            if delivery.trim_tail_silence_ms > 0:
                threshold = 10 ** (_SILENCE_DBFS / 20.0)
                active = np.flatnonzero(np.abs(samples) > threshold)
                if active.size:
                    last = int(active[-1]) + 1
                    tail_ms = (samples.size - last) / float(rate) * 1000.0
                    if tail_ms > delivery.trim_tail_silence_ms:
                        keep = min(samples.size, last + int(rate * _TAIL_KEEP_SECONDS))
                        samples = samples[:keep]
                        changed = True
            peak = float(np.max(np.abs(samples))) if samples.size else 0.0
            if delivery.peak_normalize and peak > 1e-6:
                target_peak = 10 ** (_PEAK_TARGET_DBFS / 20.0)
                gain = min(max(target_peak / peak, _PEAK_GAIN_MIN), _PEAK_GAIN_MAX)
                if abs(gain - 1.0) > _PEAK_GAIN_EPSILON:
                    samples = samples * gain
                    peak = min(1.0, peak * gain)
                    changed = True
            if delivery.tail_pad_ms > 0:
                pad_frames = int(rate * delivery.tail_pad_ms / 1000.0)
                if pad_frames > 0:
                    samples = np.concatenate([samples, np.zeros(pad_frames, dtype=np.float32)])
                    changed = True
            result = _write_wav_pcm(samples, rate)
        except Exception as exc:
            self._warn_once("post:tail", "尾音处理失败（%s），本条按原样发出", exc)
            return audio
        if not changed and distance_db is None:
            self.ctx.logger.debug(
                "语音后处理无需调整：%.2fs，峰值 %.1f dBFS", before_seconds, 20.0 * float(np.log10(peak)) if peak > 1e-9 else -120.0
            )
            return result
        peak_dbfs = 20.0 * float(np.log10(peak)) if peak > 1e-9 else -120.0
        after_seconds = samples.size / float(rate)
        if distance_db is None:
            self.ctx.logger.info(
                "语音后处理：%.2fs → %.2fs，峰值 %.1f dBFS", before_seconds, after_seconds, peak_dbfs
            )
        else:
            self.ctx.logger.info(
                "语音后处理：音色距离 %.2f dB，%.2fs → %.2fs，峰值 %.1f dBFS",
                distance_db,
                before_seconds,
                after_seconds,
                peak_dbfs,
            )
        return result

    # --------------------------------------------------------------- 音色对齐

    def _needs_pcm_processing(self) -> bool:
        """只要有一个后处理开关打开，就需要 WAV 才能真正生效。"""
        delivery = self.config.delivery
        return bool(
            delivery.timbre_match
            or delivery.peak_normalize
            or delivery.trim_tail_silence_ms > 0
            or delivery.tail_pad_ms > 0
        )

    def _request_audio_format(self) -> str:
        """要处理 PCM 时直接向 MiMo 要 WAV；否则按配置（mp3 更省流量）。"""
        if self._needs_pcm_processing():
            return "wav"
        return self.config.mimo.audio_format

    def _timbre_target(self) -> Optional[List[float]]:
        stamp = self._reference_stamp_value()
        if self._timbre_target_cache is not None and stamp == self._timbre_target_stamp:
            return self._timbre_target_cache
        path = self._reference_path()
        if path.suffix.lower() != ".wav":
            self._warn_once("timbre:format", "音色对齐需要 WAV 参考音频（当前 %s），本次按关闭处理", path.name)
            return None
        if _numpy() is None:
            self._warn_once("numpy:timbre", "音色对齐需要 numpy，当前环境没有，按关闭处理")
            return None
        try:
            pcm, rate = _read_wav_pcm(path.read_bytes())
        except Exception as exc:
            self._warn_once("timbre:read", "音色对齐读取参考音频失败（%s），按关闭处理", exc)
            return None
        self._timbre_target_cache = _spectral_curve(pcm, rate).tolist()
        self._timbre_target_stamp = stamp
        return self._timbre_target_cache

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        """同一类告警只打一次；key 用于区分，热重载后自动重置。"""
        if key in self._warned:
            return
        self._warned.add(key)
        self.ctx.logger.warning(message, *args)

    async def _synthesize_once(self, text: str, timeout: float) -> bytes:
        """合成一次；超时按 retry_on_timeout 最多再重来一次。

        实测超时的请求大多直接跳到 30 秒以上（不是刚好卡在边界），
        所以"再给它一次机会"比"把超时放宽到 30/40 秒"更划算：
        失败率从约 18% 降到 3–4%，正常时段体感不变。
        """
        retries = 1 if self.config.mimo.retry_on_timeout else 0
        while True:
            try:
                return await asyncio.wait_for(self._synthesize(text), timeout=timeout)
            except asyncio.TimeoutError as exc:
                if retries > 0:
                    retries -= 1
                    self.ctx.logger.warning("语音合成超时（%.0fs），换一次请求重来", timeout)
                    continue
                raise TimeoutError(f"语音合成超时（{timeout:.0f}s）") from exc

    async def _synthesize_guarded(self, text: str) -> bytes:
        timeout = float(self.config.general.timeout)
        attempts = max(0, int(self.config.mimo.retry_attempts))
        backoff = max(0.0, float(self.config.mimo.retry_backoff_seconds))
        last_error: Optional[BaseException] = None
        for attempt in range(attempts + 1):
            if self._synthesis_slots.locked() and self.config.delivery.skip_when_busy:
                raise _SynthesisBusy("已有语音正在合成")
            # 并发没满时 acquire 是即时的；满了且允许排队时在这里等
            await self._synthesis_slots.acquire()
            retryable: Optional[BaseException] = None
            try:
                return await self._synthesize_once(text, timeout)
            except (_RetryableSynthesisError, aiohttp.ClientError) as exc:
                last_error = exc
                if attempt < attempts:
                    retryable = exc
            except Exception as exc:
                raise RuntimeError(f"语音合成失败: {exc}") from exc
            finally:
                # 先释放槽位再退避，否则退避期间别的聊天流会一直被判"忙"
                self._synthesis_slots.release()
            if retryable is None:
                break
            await asyncio.sleep(backoff * (2 ** attempt))
        raise RuntimeError(f"语音合成失败: {last_error}")

    # ------------------------------------------------------------------- 文本清洗

    def _sanitize_for_tts(self, text: str) -> Optional[str]:
        """把聊天文本整理成适合朗读的句子（实现见 ``core.text``）。"""
        return _text.sanitize_for_tts(text, self.config.trigger.english_policy)

    def _apply_english_policy(self, text: str) -> str:
        """按 ``trigger.english_policy`` 处理拉丁字母（实现见 ``core.text``）。"""
        return _text.apply_english_policy(text, self.config.trigger.english_policy)

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
            # 拿不到对方身份（入站映射过期等）：whitelist 保守拦截；
            # blacklist 无法确认命中就放行，否则私聊语音会在闲置 12 小时后静默失效
            if mode == "blacklist":
                self._warn_once(
                    f"filter:{scope}",
                    "%s 的对方身份未知（入站映射已过期），按 blacklist 规则放行；如需严格拦截请用 whitelist",
                    scope,
                )
                return True
            return False
        return listed if mode == "whitelist" else not listed

    def _remember_scope(self, scope: str, session_id: str, group_id: str, user_id: str) -> None:
        if not session_id:
            return
        now = time.monotonic()
        if len(self._scope_cache) > _SCOPE_CACHE_MAX_ENTRIES:
            self._scope_cache = {key: value for key, value in self._scope_cache.items() if value[0] > now}
            if len(self._scope_cache) > _SCOPE_CACHE_MAX_ENTRIES:
                self._scope_cache.clear()
        self._scope_cache[session_id] = (now + _SCOPE_TTL_SECONDS, {"scope": scope, "group_id": group_id, "user_id": user_id})

    @staticmethod
    def _prune_ttl_cache(cache: Dict[str, float]) -> None:
        """TTL 字典兜底：超过上限先清过期项，还超就直接清空（这些缓存丢了只是多抽一次签）。"""
        if len(cache) <= _TTL_CACHE_MAX_ENTRIES:
            return
        now = time.monotonic()
        for key, expires_at in list(cache.items()):
            if expires_at <= now:
                cache.pop(key, None)
        if len(cache) > _TTL_CACHE_MAX_ENTRIES:
            cache.clear()

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
        info = payload.get("message_info")
        info = info if isinstance(info, dict) else {}
        group_info = info.get("group_info")
        group_info = group_info if isinstance(group_info, dict) else {}
        user_info = info.get("user_info")
        user_info = user_info if isinstance(user_info, dict) else {}
        self._remember_scope(scope, session_id, str(group_info.get("group_id") or ""), str(user_info.get("user_id") or ""))

    # ------------------------------------------------------------------- 触发

    def _stream_id(self, message: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
        return str(kwargs.get("stream_id") or message.get("session_id") or "").strip()

    def _plain_text_message(self, message: Dict[str, Any]) -> Optional[str]:
        """取出纯文本内容（实现见 ``core.text``）。"""
        return _text.plain_text_message(message)


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
        self._random_decisions[stream_id] = (now + float(self.config.trigger.decision_ttl), enabled)
        if len(self._random_decisions) > _TTL_CACHE_MAX_ENTRIES:
            self._random_decisions = {
                key: value for key, value in self._random_decisions.items() if value[0] >= now
            }
            if len(self._random_decisions) > _TTL_CACHE_MAX_ENTRIES:
                self._random_decisions.clear()
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
        if self.config.trigger.random_requires_chinese and _text.contains_latin(text):
            # 随机抽中的一次机会，宁可发文字也别把 token/LLM 念成字母；
            # 放在抽签之前，避免白耗掉 decision_ttl 内的这次判定
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
        # 拷一份再改：宿主传进来的 dict 可能还被后续 hook/观察者使用
        outbound = copy.deepcopy(message) if isinstance(message, dict) else {}
        text = self._plain_text_message(outbound)
        if text is None:
            return None
        stream_id = self._stream_id(outbound, kwargs)
        if not stream_id:
            return None

        scope = str(outbound.get("scope") or "").strip()
        info = outbound.get("message_info")
        info = info if isinstance(info, dict) else {}
        group_info = info.get("group_info")
        group_info = group_info if isinstance(group_info, dict) else {}
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
        except Exception as exc:
            self.ctx.logger.warning("语音合成失败（%s），%s", exc, "回退文字" if self.config.delivery.fallback_to_text else "拦截消息")
            return None if self.config.delivery.fallback_to_text else {"action": "abort"}

        # 音色对齐、尾音裁剪、峰值归一合并成一次线程调用（见 _postprocess）。
        # 后处理只是增强：任何异常都退回原始音频，不能把整条语音拖垮。
        try:
            audio = await asyncio.to_thread(self._postprocess, audio)
        except Exception as exc:  # pragma: no cover - 兜底，正常不该触发
            self._warn_once("post:failed", "语音后处理失败（%s），本条按原样发出", exc)

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
        # 模型的一次性语音许可、文字许可，以及这次随机抽签都用掉即清：
        # 不清的话 decision_ttl 内同一条聊天流的每条回复都会继续转语音（线上出现过 10 秒内连发两条）
        self._llm_voice_requests.pop(stream_id, None)
        self._text_override_sessions.pop(stream_id, None)
        self._random_decisions.pop(stream_id, None)
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
    async def configure_planner_tools(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
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
        self._text_override_sessions[stream_id] = time.monotonic() + float(self.config.trigger.decision_ttl)
        self._prune_ttl_cache(self._text_override_sessions)
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
        self._llm_voice_requests[stream_id] = time.monotonic() + float(self.config.trigger.decision_ttl)
        self._prune_ttl_cache(self._llm_voice_requests)
        self._text_override_sessions.pop(stream_id, None)
        return {"success": True, "content": "这一轮会用语音回复，正常给出回复内容即可。"}

def create_plugin() -> MiMoVoicePlugin:
    return MiMoVoicePlugin()
