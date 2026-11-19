"""MiMo TTS 语音回复插件：把纯文本回复按策略替换成语音条。

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
import contextlib
import copy
import hashlib
import io
import json
import math
import random
import shutil
import subprocess
import time
import tomllib
import unicodedata
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast
from urllib.parse import urlsplit

import aiohttp
from maibot_sdk import API, CONFIG_RELOAD_SCOPE_SELF, Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .core import commands as _commands
from .core import config as _config
from .core import schedule as _schedule
from .core import song_intent as _song_intent
from .core import song_library as _songs
from .core import song_media as _song_media
from .core import text as _text
from .core import timeout as _timeout
from .core import trigger as _trigger
from .core import triggers as _triggers
from .core.audio import (
    _apply_gain_curve,
    _concat_path,
    _numpy,
    _read_wav_pcm,
    _spectral_curve,
    _stderr_excerpt,
    _write_wav_pcm,
    analyze_reference,
    prepare_reference,
    smooth_gain_bands,
    soft_limit,
)
from .core.config import MiMoVoiceConfig
from .core.constants import (
    _AUDIO_SECONDS_CAP,
    _AUDIO_SECONDS_PER_CHAR,
    _FFMPEG_BIN,
    _INTENT_ARMED_TTL_SECONDS,
    _INTENT_MIN_INTERVAL_SECONDS,
    _KEEP_TEXT_DELAY_SECONDS,
    _MAIBOT_CONFIG_PATHS,
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
    _REFERENCE_PREPARE_GAP_SECONDS,
    _REFERENCE_PREPARE_MAX_SECONDS,
    _REFERENCE_PREPARE_MIN_SEGMENT_SECONDS,
    _REFERENCE_PREPARE_PEAK_DBFS,
    _REFERENCE_PREPARE_TARGET_SECONDS,
    _REFERENCE_SAMPLE_RATE,
    _REFERENCE_SILENCE_DB,
    _REFERENCE_WARN_LONGEST_SILENCE_SECONDS,
    _REFERENCE_WARN_SECONDS,
    _REFERENCE_WARN_SILENCE_RATIO,
    _RETRYABLE_STATUS,
    _SCOPE_CACHE_MAX_ENTRIES,
    _SCOPE_TTL_SECONDS,
    _SELF_SENT_MAX_ENTRIES,
    _SELF_SENT_TEXT_TTL_SECONDS,
    _SILENCE_DBFS,
    _SOFT_LIMIT_CEILING,
    _SONG_CUE_TTL_SECONDS,
    _SONG_HOUSEKEEPING_SECONDS,
    _SONG_TRASH_TTL_SECONDS,
    _TAIL_KEEP_SECONDS,
    _TIMBRE_FULL_STRENGTH_DB,
    _TIMBRE_MAX_GAIN_DB,
    _TRIGGER_MIN_INTERVAL_SECONDS,
    _TRIGGER_TTL_MARGIN_SECONDS,
    _TTL_CACHE_MAX_ENTRIES,
    PRESET_ALIASES,
    PRESET_DIR_NAME,
    PRESET_DISPLAY_NAMES,
    PRESET_REFERENCE_FILES,
    PRESET_REFERENCE_NAMES,
    SUPPORTED_REFERENCE_SUFFIXES,
    VOICE_SEGMENT_TYPE,
)
from .core.pending import PendingItem, PendingPool


class _RetryableSynthesisError(RuntimeError):
    """上游临时故障，可以按配置退避重试。"""


class _SynthesisBusy(RuntimeError):
    """并发已满，按 delivery.skip_when_busy 直接回退文字。"""


def _payload_seconds(audio: bytes) -> Optional[float]:
    """WAV 载荷的真实时长（只读头，不解样本）；其它格式返回 None。

    只用于日志：``audio_format`` 默认是 wav，后处理也要求 wav，所以线上几乎总能拿到值；
    mp3 等格式没有解码器可用，返回 None 让日志写「音频 未知」。
    """
    try:
        with wave.open(io.BytesIO(audio), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
    except (wave.Error, EOFError):
        return None
    if rate <= 0 or frames <= 0:
        return None
    return frames / float(rate)


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
        # 被 @ / 点名后的一次性优先窗口（只在 trigger.mention_priority 打开时记录）
        self._mention_priority = _trigger.MentionPriority()
        # 对外 API 的上次调用时间（全局限流用；宿主不传调用方身份）
        self._api_last_call: float = 0.0
        # 内建定时语音（0.6.0）：后台循环 + 当天已跑过的条目
        self._schedule_task: Optional[asyncio.Task] = None
        self._schedule_done: Dict[Tuple[str, int, int], float] = {}
        self._schedule_runs: set[asyncio.Task] = set()
        # 命令层（0.5.5）：会话级开关（进程内，重启复位）与近期合成样本
        self._session_switch: Dict[str, bool] = {}
        self._recent_durations: List[float] = []
        self._synth_stats = {"ok": 0, "timeout": 0, "failed": 0}
        self._synth_failures: Dict[str, int] = {}
        # 触发词（0.5.6）：session_id -> (词, 位置, 过期时间)；命中只在出站消费一次
        self._trigger_requests: Dict[str, Tuple[str, str, float]] = {}
        self._trigger_last_hit: Dict[str, float] = {}
        # 并存（0.6.0）：文字与语音分两条发，语音先时用这个集合挂延迟发文字的任务
        self._pending_text_tasks: set[asyncio.Task] = set()
        # 插件自己发出的文字（并存文字段 / 命令回执 / 定时文字）：(会话, 文字) -> 过期时间。
        # 这些文字也会经过 send_service.before_send，认出来直接放行，不能再转成语音。
        self._self_sent_texts: Dict[Tuple[str, str], float] = {}
        # 唱歌点歌（0.7.0）：暂存池句柄、曲库目录缓存与点歌冷却（会话/个人）
        self._pending_cache: Optional[PendingPool] = None
        # 刚被 /voice sing add 认领过的音频（sha256 → 记录时间）：钩子有时会为同一条消息再跑一次，
        # 不挡住的话会在入库后又往 pending/ 里补一份永远没人认领的副本。
        self._consumed_media: Dict[str, float] = {}
        self._sing_group_last: Dict[str, float] = {}
        self._sing_user_last: Dict[str, float] = {}
        # 意图层（0.7.0）：会话 -> (过期时间, 入站线索)；planner 请求前消费一次
        self._song_cues: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._intent_last: Dict[str, float] = {}
        self._intent_patterns_cache: Optional[Tuple[Tuple[str, ...], List[Any]]] = None
        self._song_tool_stamp: Optional[Tuple[int, int]] = None
        # 只在真的命中意图的那一轮把播放工具暴露给模型：session -> 过期时间
        self._intent_armed: Dict[str, float] = {}
        # 曲库读改写要串行（两个管理员同时 add/set 会互相覆盖）；后台维护任务负责清暂存池与 .trash
        self._songs_lock = asyncio.Lock()
        self._housekeeping_task: Optional[asyncio.Task] = None
        # 「点名」识别的称呼集合（bot 昵称 + alias_names + 配置里的 intent_names）
        self._mention_names: List[str] = []

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            self.ctx.logger.info("MiMo 语音插件已加载（未启用）")
            return
        self._synthesis_slots = asyncio.Semaphore(max(1, int(self.config.mimo.max_concurrent)))
        if not self.config.mimo.api_key.strip():
            self.ctx.logger.warning("MiMo 语音插件未配置 api_key，所有回复将保持文字")
        self._check_admin_entries()
        data_dir = self._data_dir()
        if data_dir is not None:
            try:
                (data_dir / "references").mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.ctx.logger.warning("无法创建参考音频目录 %s：%s", data_dir / "references", exc)
        strategy = self.config.voice.reference_strategy
        self.ctx.logger.info(
            "MiMo 语音插件已加载：音色=%s, mode=%s, probability=%.2f, 短句上限=%d字, 合成=%s, "
            "风格指令=%s, 参考策略=%s, 英文=%s, 随机需纯中文=%s, 超时=%.0fs, "
            "音色对齐=%s, 尾部裁剪=%dms, 尾部补静音=%dms, 峰值归一=%s, 群聊放行=%s, 私聊放行=%s, "
            "点名优先=%s(%.2f), 情绪加权=%s(+%.2f/上限%.2f), 触发词=%s, 定时=%s, 唱歌点播=%s",
            PRESET_DISPLAY_NAMES.get(self.config.voice.preset, self.config.voice.preset),
            self.config.trigger.mode,
            self.config.trigger.probability,
            int(self.config.trigger.short_reply_max_chars),
            self.config.mimo.model,
            "开" if self.config.voice.style_instruction.strip() else "关",
            strategy,
            self.config.trigger.english_policy,
            "开" if self.config.trigger.random_requires_chinese else "关",
            float(self.config.general.timeout),
            "开" if self.config.delivery.timbre_match else "关",
            int(self.config.delivery.trim_tail_silence_ms),
            int(self.config.delivery.tail_pad_ms),
            "开" if self.config.delivery.peak_normalize else "关",
            self.config.filters.group_mode,
            self.config.filters.private_mode,
            "开" if self.config.trigger.mention_priority else "关",
            float(self.config.trigger.mention_probability),
            "开" if self.config.trigger.emotion_weighting else "关",
            float(self.config.trigger.emotion_step),
            float(self.config.trigger.emotion_cap),
            (
                f"开({len(self._trigger_rules())} 条)"
                if self.config.triggers.enabled and self.config.triggers.rules
                else "关"
            ),
            (
                f"开({len(self._scheduled_entries())} 条)"
                if self.config.scheduled.enabled and self._scheduled_entries()
                else "关"
            ),
            (
                f"开({len(self._load_songs())} 首)"
                if self.config.songs.enabled
                else "关"
            ),
        )
        if data_dir is not None:
            self.ctx.logger.info("参考音频目录：%s（自定义音色把文件放这里）", data_dir / "references")
        self._prune_reference_cache()
        try:
            self._report_reference_health()
        except Exception as exc:  # 体检失败不影响加载
            self.ctx.logger.debug("参考音频体检跳过：%s", exc)
        self._flush_config_warnings()
        base_host = urlsplit(self.config.mimo.api_base_url).hostname or ""
        if base_host and not base_host.endswith("xiaomimimo.com"):
            self.ctx.logger.warning(
                "待合成文本与参考音频会发送到自建/第三方地址 %s，请确认可信；"
                "若只是误填，把 mimo.api_base_url 改回 https://api.xiaomimimo.com/v1",
                base_host,
            )
        self._start_scheduler()
        if self._songs_enabled() and self._housekeeping_task is None:
            self._housekeeping_task = asyncio.create_task(self._song_housekeeping_loop())

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
        if self._housekeeping_task is not None:
            self._housekeeping_task.cancel()
            self._housekeeping_task = None
        await self._stop_scheduler()
        for task in list(self._pending_text_tasks):
            task.cancel()
        self._pending_text_tasks.clear()
        await self._close_session()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        await self._stop_scheduler()
        self._schedule_done.clear()
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
        self._start_scheduler()

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
        """内置音色文件：随插件分发的 ``presets/<name>.wav``，也允许在数据目录覆盖。

        查找顺序：数据目录 → 插件目录，都先按新布局 ``presets/xxx.wav``，
        再按 0.5.2 及更早的旧布局（插件根目录）兜底，老版本升级不会突然找不到音色。
        """
        data_dir = self._data_dir()
        plugin_dir = self._plugin_dir()
        legacy_name = preset_file.rsplit("/", 1)[-1]
        candidates: List[Path] = []
        for base in (data_dir, plugin_dir):
            if base is None:
                continue
            candidates.append(base / preset_file)
            if legacy_name != preset_file:
                candidates.append(base / legacy_name)
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_file():
                if resolved.parent.name != PRESET_DIR_NAME:
                    # 商店审核（#1256）建议：旧布局还能读，但升级后给一次迁移提示
                    self._warn_once(
                        f"preset:layout:{resolved.name}",
                        "音色文件 %s 还在旧布局（插件根目录），建议移到 %s/ 下；当前版本仍会读取",
                        resolved.name,
                        PRESET_DIR_NAME,
                    )
                return resolved
        return None

    def _reference_path(self) -> Path:
        """解析当前要用的参考音频：内置预设优先，找不到再退回 voice.reference_path。"""
        raw_preset = str(getattr(self.config.voice, "preset", "custom") or "custom").strip()
        preset = PRESET_ALIASES.get(raw_preset) or PRESET_ALIASES.get(raw_preset.lower()) or raw_preset
        preset_file = PRESET_REFERENCE_FILES.get(preset)
        if preset_file:
            candidate = self._preset_path(preset_file)
            if candidate is not None:
                return candidate
            self._warn_once(
                f"builtin:{preset}",
                "内置音色 %s 的参考音频缺失（%s），回退到 voice.reference_path",
                PRESET_DISPLAY_NAMES.get(preset, preset),
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

    def _report_reference_health(self) -> None:
        """启动体检：参考音频过长/静音过多是"偶尔换人、句子刺耳"的高危因素，只告警不阻断。"""
        path = self._reference_path()
        if not path.is_file():
            self.ctx.logger.warning("参考音频不存在：%s（语音会合成失败并回退文字）", path)
            return
        if path.suffix.lower() != ".wav" or _numpy() is None:
            self.ctx.logger.info("参考音频：%s（非 WAV 或缺少 numpy，未做体检）", path.name)
            return
        if path.name in PRESET_REFERENCE_NAMES:
            self.ctx.logger.info("参考音频：%s（内置音色，已随版本校验，跳过体检）", path.name)
            return
        try:
            pcm, rate = _read_wav_pcm(path.read_bytes())
        except Exception as exc:
            self.ctx.logger.warning("参考音频无法解析：%s（%s）", path.name, exc)
            return
        health = analyze_reference(pcm, rate)
        problems = []
        if health["duration"] > _REFERENCE_WARN_SECONDS:
            problems.append(f"时长 {health['duration']:.1f}s（建议 2–5s）")
        if health["silence_ratio"] > _REFERENCE_WARN_SILENCE_RATIO:
            problems.append(f"静音占 {health['silence_ratio'] * 100:.0f}%（建议 <15%）")
        if health["longest_silence"] > _REFERENCE_WARN_LONGEST_SILENCE_SECONDS:
            problems.append(f"最长静音 {health['longest_silence']:.2f}s（建议 <0.4s）")
        if problems:
            self.ctx.logger.warning(
                "参考音频体检未通过（%s）：%s。实测这类素材更容易出现音色漂移和句子刺耳，"
                "建议换成 2–5 秒的干净单句；发送前插件也会自动瘦身",
                path.name,
                "、".join(problems),
            )
        else:
            self.ctx.logger.info(
                "参考音频体检通过：%s，%.2fs，静音 %.0f%%",
                path.name,
                health["duration"],
                health["silence_ratio"] * 100,
            )

    def _prepare_signature(self, path: Path) -> str:
        try:
            stat = path.stat()
        except OSError:
            return "missing"
        payload = {
            "path": str(path),
            "mtime": stat.st_mtime_ns,
            "size": stat.st_size,
            "target": _REFERENCE_PREPARE_TARGET_SECONDS,
            "max": _REFERENCE_PREPARE_MAX_SECONDS,
            "peak": _REFERENCE_PREPARE_PEAK_DBFS,
            "rate": _REFERENCE_SAMPLE_RATE,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    def _prepared_reference_bytes(self, path: Path) -> Optional[bytes]:
        """single_file 的 WAV 参考自动瘦身：切句、去长静音、拼到 3.7s、峰值 -3 dBFS。

        已经是"短且干净"的素材就返回 None（用原文件，不做无谓的重编码）；
        结果按文件指纹缓存在 runtime_dir，换文件或改参数后自动重算。
        """
        if not self.config.voice.auto_prepare_reference:
            return None
        if path.suffix.lower() != ".wav" or _numpy() is None:
            return None
        if path.name in PRESET_REFERENCE_NAMES:
            return None  # 内置音色是随版本校验过的成品，不做二次加工
        output = self._cache_dir() / f"prepared-{self._prepare_signature(path)}.wav"
        if output.is_file() and output.stat().st_size > 0:
            return output.read_bytes()
        try:
            pcm, rate = _read_wav_pcm(path.read_bytes())
            health = analyze_reference(pcm, rate)
            if health["duration"] <= _REFERENCE_PREPARE_MAX_SECONDS and health["silence_ratio"] <= 0.05:
                return None
            prepared, meta = prepare_reference(
                pcm,
                rate,
                target_seconds=_REFERENCE_PREPARE_TARGET_SECONDS,
                max_seconds=_REFERENCE_PREPARE_MAX_SECONDS,
                target_peak_dbfs=_REFERENCE_PREPARE_PEAK_DBFS,
                gap_seconds=_REFERENCE_PREPARE_GAP_SECONDS,
                min_segment_seconds=_REFERENCE_PREPARE_MIN_SEGMENT_SECONDS,
            )
            payload = _write_wav_pcm(prepared, rate)
        except Exception as exc:
            self._warn_once("reference:prepare", "参考音频自动瘦身失败（%s），改用原文件", exc)
            return None
        with contextlib.suppress(OSError):
            output.write_bytes(payload)
        self.ctx.logger.info(
            "参考音频已自动瘦身：%s %.2fs → %.2fs（%d 段），峰值 %.1f dBFS",
            path.name,
            meta["source_seconds"],
            meta["duration"],
            meta["segments"],
            _REFERENCE_PREPARE_PEAK_DBFS,
        )
        return payload

    def _reference_bytes(self) -> Optional[bytes]:
        files = self._reference_source_files()
        if not files:
            return None
        if self.config.voice.reference_strategy == "single_file":
            self._reference_built_suffix = files[0].suffix.lower() or ".wav"
            prepared = self._prepared_reference_bytes(files[0])
            if prepared is not None:
                self._reference_built_suffix = ".wav"
                return prepared
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
                    distance_db = float(np.sqrt(np.mean((target_arr - curve) ** 2)))
                    # 距离越远说明这条本来就"不是同一个人"：继续按满强度硬怼只会把音色掰闷，
                    # 超过 _TIMBRE_FULL_STRENGTH_DB 后按距离反比降强度。
                    strength = float(delivery.timbre_match_strength)
                    if distance_db > _TIMBRE_FULL_STRENGTH_DB:
                        strength *= _TIMBRE_FULL_STRENGTH_DB / distance_db
                    gains = np.clip(
                        (target_arr - curve) * strength,
                        -_TIMBRE_MAX_GAIN_DB,
                        _TIMBRE_MAX_GAIN_DB,
                    )
                    gains = smooth_gain_bands(gains)
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
            if peak > _SOFT_LIMIT_CEILING:
                # EQ 会把原本就削顶的条子推得更破，软限幅压回天花板内再归一。
                samples = soft_limit(samples, _SOFT_LIMIT_CEILING)
                peak = float(np.max(np.abs(samples))) if samples.size else 0.0
                changed = True
            if delivery.peak_normalize and peak > 1e-6:
                target_peak = 10 ** (_PEAK_TARGET_DBFS / 20.0)
                gain = min(max(target_peak / peak, _PEAK_GAIN_MIN), _PEAK_GAIN_MAX)
                if abs(gain - 1.0) > _PEAK_GAIN_EPSILON:
                    samples = samples * gain
                    peak = min(1.0, peak * gain)
                    changed = True
            if peak > _SOFT_LIMIT_CEILING:
                samples = soft_limit(samples, _SOFT_LIMIT_CEILING)
                peak = min(_SOFT_LIMIT_CEILING, peak)
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
            payload = self._prepared_reference_bytes(path)
            if payload is None:
                payload = path.read_bytes()
            pcm, rate = _read_wav_pcm(payload)
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

    async def _synthesize_once(self, text: str, timeout: float, attempt: int = 1) -> bytes:
        """合成一次，并给这次请求埋点。

        成功记「语音合成耗时：3.42s（timeout 25.0s，第 1 次，12 字，音频 3.33s）」，失败记
        「语音合成失败（类型=timeout/429/5xx/network/audio/other，耗时…，timeout …，第 N 次，M 字）：…」。
        字数与音频时长是 0.5.4 补的：0.5.3 复盘时发现超时可能和文本长度有关，但成功行
        只有耗时，配不出「多少字 → 多长时间」。

        0.5.4 起**一条语音只发一次请求**：11-06 的名额实验（``docs/analysis-dual-draft.md``）
        证明 MiMo 每个账号的 TTS 并发名额只有 1，客户端超时后服务端那条仍在生成、仍占名额
        （实测最长 72.7 秒），所以"超时后补发"必然撞 429（0.3–0.5 秒内被拒）。补发与
        动态超时（0.5.3 的 p80×1.4、12–25 秒）都已删除，超时即回退文字。
        """
        chars = len(text)
        started = time.monotonic()
        try:
            payload = await asyncio.wait_for(self._synthesize(text), timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._synth_stats["timeout"] += 1
            self.ctx.logger.warning(
                "语音合成失败（类型=timeout，耗时 %.2fs，timeout %.1fs，第 %d 次，%d 字）",
                time.monotonic() - started,
                timeout,
                attempt,
                chars,
            )
            raise TimeoutError(f"语音合成超时（{timeout:.0f}s）") from exc
        except Exception as exc:
            self._synth_stats["failed"] += 1
            kind = _timeout.classify_failure(exc)
            self._synth_failures[kind] = self._synth_failures.get(kind, 0) + 1
            self.ctx.logger.warning(
                "语音合成失败（类型=%s，耗时 %.2fs，timeout %.1fs，第 %d 次，%d 字）：%s",
                kind,
                time.monotonic() - started,
                timeout,
                attempt,
                chars,
                exc,
            )
            raise
        elapsed = time.monotonic() - started
        self._synth_stats["ok"] += 1
        self._recent_durations.append(elapsed)
        del self._recent_durations[:-20]
        seconds = _payload_seconds(payload)
        audio_text = f"{seconds:.2f}s" if seconds is not None else "未知"
        self.ctx.logger.info(
            "语音合成耗时：%.2fs（timeout %.1fs，第 %d 次，%d 字，音频 %s）",
            elapsed,
            timeout,
            attempt,
            chars,
            audio_text,
        )
        return payload

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
                return await self._synthesize_once(text, timeout, attempt + 1)
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

    @HookHandler(
        "chat.receive.after_process",
        name="mimo_voice_remember_mention",
        description="记下这一次被 @ / 点名的会话，供出站时提高语音概率（trigger.mention_priority）",
        mode=HookMode.OBSERVE,
        order=HookOrder.EARLY,
    )
    async def remember_mention(self, message: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        if not self.config.trigger.mention_priority:
            return
        payload = message if isinstance(message, dict) else {}
        # 宿主已算好两个布尔字段（message_utils.py 的 _session_message_to_dict）：
        # is_at=@ 了 bot，is_mentioned=文本里提到 bot 的名字/昵称。@所有人 不算。
        if not (payload.get("is_at") or payload.get("is_mentioned")):
            return
        session_id = str(payload.get("session_id") or kwargs.get("stream_id") or "").strip()
        if not session_id:
            return
        self._mention_priority.record(session_id, float(self.config.trigger.mention_ttl_seconds))
        self._mention_priority.prune()
        self.ctx.logger.info(
            "会话 %s 被点名，下一次回复的语音概率提到 %.2f（有效期 %.0fs）",
            session_id,
            max(float(self.config.trigger.probability), float(self.config.trigger.mention_probability)),
            float(self.config.trigger.mention_ttl_seconds),
        )

    @HookHandler(
        "chat.receive.after_process",
        name="mimo_voice_remember_trigger",
        description="文本命中触发词、且被 @ / 点名 / 私聊时，让这条消息对应的回复改用语音",
        mode=HookMode.OBSERVE,
        order=HookOrder.EARLY,
    )
    async def remember_trigger(self, message: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        config = self.config.triggers
        if not config.enabled or not config.rules:
            return
        payload = message if isinstance(message, dict) else {}
        text = str(payload.get("processed_plain_text") or payload.get("plain_text") or "").strip()
        if not text or text[0] in ("/", "／"):
            # 命令消息与命令回执是两回事，别让 /voice test 之类的文本误触发
            return
        session_id = str(payload.get("session_id") or kwargs.get("stream_id") or "").strip()
        if not session_id:
            return
        info = payload.get("message_info")
        info = info if isinstance(info, dict) else {}
        group_info = info.get("group_info")
        group_info = group_info if isinstance(group_info, dict) else {}
        group_id = str(group_info.get("group_id") or "").strip()
        scope = str(payload.get("scope") or "").strip()
        private = scope == "private" or (not group_id and scope != "group")
        if not (private or payload.get("is_at") or payload.get("is_mentioned")):
            return
        rules = self._trigger_rules()
        hit = _triggers.match_rules(text, rules)
        if hit is None:
            return
        now = time.monotonic()
        last = self._trigger_last_hit.get(session_id, 0.0)
        if now - last < _TRIGGER_MIN_INTERVAL_SECONDS:
            # 群里连发同一条/同一批消息时只记一次，避免刷屏
            return
        self._trigger_last_hit[session_id] = now
        word, mode = hit
        ttl = self._trigger_ttl()
        self._trigger_requests[session_id] = (word, mode, now + ttl)
        if len(self._trigger_requests) > _TTL_CACHE_MAX_ENTRIES:
            self._trigger_requests = {
                key: value for key, value in self._trigger_requests.items() if value[2] >= now
            }
        self.ctx.logger.info(
            "会话 %s 命中触发词「%s」（位置=%s），这条回复改用语音（窗口 %.0fs）",
            session_id,
            word,
            _triggers.mode_label(mode),
            ttl,
        )

    # ---------------------------------------------------------- 唱歌点歌（0.7.0）

    def _songs_enabled(self) -> bool:
        return bool(self.config.plugin.enabled and self.config.songs.enabled)

    def _songs_root(self) -> Optional[Path]:
        """曲库目录：``songs.directory`` 相对插件 data 目录，也可以写绝对路径。"""
        data_dir = self._data_dir()
        raw = (self.config.songs.directory or "songs").strip() or "songs"
        target = Path(raw).expanduser()
        if not target.is_absolute():
            if data_dir is None:
                return None
            target = data_dir / target
        return target

    def _songs_file(self) -> Optional[Path]:
        root = self._songs_root()
        return root / "songs.json" if root is not None else None

    def _pending_root(self) -> Optional[Path]:
        data_dir = self._data_dir()
        return data_dir / "pending" if data_dir is not None else None

    async def _song_housekeeping_loop(self) -> None:
        """每 10 分钟清一次暂存池过期条目和回收站里的旧文件。

        ``PendingPool.prune`` 只在 put 时跑；整晚没人发音频的话过期文件会一直躺着。
        """
        try:
            while True:
                await asyncio.sleep(_SONG_HOUSEKEEPING_SECONDS)
                if not self._songs_enabled():
                    continue
                await self._song_housekeeping_once()
        except asyncio.CancelledError:
            raise

    async def _song_housekeeping_once(self) -> None:
        config = self.config.songs
        pool = self._pending_pool()
        if pool is not None:
            removed = await asyncio.to_thread(
                pool.prune,
                ttl_seconds=float(config.pending_ttl_seconds),
                max_bytes=int(float(config.pending_max_mb) * 1024 * 1024),
                max_entries=int(config.pending_max_entries),
            )
            if removed:
                self.ctx.logger.info("暂存池清理：移除 %d 条过期/超量音频", removed)
        root = self._songs_root()
        if root is not None:
            purged = await asyncio.to_thread(self._purge_trash, root)
            if purged:
                self.ctx.logger.info("回收站清理：删除 %d 个超过 7 天的文件", purged)

    @staticmethod
    def _purge_trash(root: Path, *, ttl_seconds: float = _SONG_TRASH_TTL_SECONDS) -> int:
        trash = root / ".trash"
        if not trash.is_dir():
            return 0
        cutoff = time.time() - max(0.0, ttl_seconds)
        removed = 0
        for item in trash.iterdir():
            try:
                if item.is_file() and item.stat().st_mtime < cutoff:
                    item.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def _songs_total_bytes(root: Path) -> int:
        """曲库已占用字节数（不含元数据与回收站）。"""
        total = 0
        try:
            entries = list(root.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if entry.name == "songs.json" or entry.name == ".trash":
                continue
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
        return total

    def _pending_pool(self) -> Optional[PendingPool]:
        root = self._pending_root()
        if root is None:
            return None
        if self._pending_cache is None or self._pending_cache.root != root:
            self._pending_cache = PendingPool(root)
        return self._pending_cache

    def _load_songs(self) -> List[Any]:
        path = self._songs_file()
        if path is None:
            return []
        songs, warnings = _songs.load_library(path)
        for message in warnings:
            self._warn_once(f"songs:{message}", "%s", message)
        return list(songs)

    def _save_songs(self, songs: List[Any]) -> None:
        path = self._songs_file()
        if path is not None:
            _songs.save_library(path, songs)

    # ------------------------------------------------------- 意图层（0.7.0）

    def _intent_patterns(self) -> List[Any]:
        """编译后的点歌正则；配置里写错的行跳过并只告警一次。"""
        raw = tuple(str(item or "") for item in (self.config.songs.intent_patterns or []))
        cached = self._intent_patterns_cache
        if cached is not None and cached[0] == raw:
            return cached[1]
        patterns, warnings = _song_intent.compile_patterns(list(raw))
        for message in warnings:
            self._warn_once(f"intent:{message}", "%s", message)
        self._intent_patterns_cache = (raw, patterns)
        return patterns

    def _load_mention_names(self) -> List[str]:
        """把宿主的 bot 昵称 / alias_names 与配置里的额外称呼合起来。

        宿主的 ``is_mentioned`` 在 ``chat.receive.after_process`` 阶段还没算出来
        （它要等 register_message 才补），所以点名判断得自己兜一层。
        """
        names: List[str] = []
        for raw in self.config.songs.intent_names or []:
            text = str(raw or "").strip()
            if text:
                names.append(text)
        for path in _MAIBOT_CONFIG_PATHS:
            try:
                with open(path, "rb") as handle:
                    data = tomllib.load(handle)
            except (OSError, ValueError):
                continue
            bot = data.get("bot")
            if not isinstance(bot, dict):
                continue
            for key in ("nickname", "alias_names"):
                value = bot.get(key)
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
                elif isinstance(value, list):
                    names.extend(str(item).strip() for item in value if str(item).strip())
            break
        # 去重、去掉过短的（单字称呼几乎必定误伤）
        seen: List[str] = []
        for name in names:
            if len(name) >= 2 and name not in seen:
                seen.append(name)
        return seen

    @staticmethod
    def _text_mentions_bot(text: str, names: List[str]) -> bool:
        body = unicodedata.normalize("NFKC", str(text or "")).lower()
        return any(name.lower() in body for name in names)

    def _cue_mentions_bot(self, text: str, payload: Dict[str, Any]) -> bool:
        if payload.get("is_at") or payload.get("is_mentioned"):
            return True
        if not self._mention_names:
            self._mention_names = self._load_mention_names()
        return self._text_mentions_bot(text, self._mention_names)

    def _song_cue(self, session_id: str) -> Optional[Dict[str, Any]]:
        """取本会话最近一条入站线索（文本 + @ 状态），过期即丢。"""
        entry = self._song_cues.get(session_id)
        if entry is None:
            return None
        expires_at, cue = entry
        if expires_at < time.monotonic():
            self._song_cues.pop(session_id, None)
            return None
        return cue

    @HookHandler(
        "chat.receive.after_process",
        name="mimo_voice_song_cue",
        description="记下一条可能是点歌的入站消息（文本与 @ 状态），planner 请求前消费（OBSERVE）",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
    )
    async def remember_song_cue(self, message: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        if not self._songs_enabled():
            return
        payload = message if isinstance(message, dict) else {}
        text = str(payload.get("processed_plain_text") or payload.get("plain_text") or "").strip()
        if not text or text[0] in ("/", "／"):
            return
        session_id = str(payload.get("session_id") or kwargs.get("stream_id") or "").strip()
        if not session_id:
            return
        info = payload.get("message_info")
        info = info if isinstance(info, dict) else {}
        group_info = info.get("group_info")
        group_info = group_info if isinstance(group_info, dict) else {}
        user_info = info.get("user_info")
        user_info = user_info if isinstance(user_info, dict) else {}
        scope = str(payload.get("scope") or "").strip()
        group_id = str(group_info.get("group_id") or "").strip()
        if not scope:
            scope = "group" if group_id else "private"
        now = time.monotonic()
        if len(self._song_cues) > _TTL_CACHE_MAX_ENTRIES:
            self._song_cues = {key: value for key, value in self._song_cues.items() if value[0] >= now}
            if len(self._song_cues) > _TTL_CACHE_MAX_ENTRIES:
                self._song_cues.clear()
        self._song_cues[session_id] = (
            now + _SONG_CUE_TTL_SECONDS,
            {
                "text": text,
                "scope": scope,
                "group_id": group_id,
                "user_id": str(user_info.get("user_id") or ""),
                "is_at": bool(payload.get("is_at")),
                "is_mentioned": self._cue_mentions_bot(text, payload),
            },
        )

    @HookHandler(
        "maisaka.planner.before_request",
        name="mimo_voice_song_intent",
        description="命中点歌正则时，把曲库候选注入本轮 planner 请求（不拦截、不改写用户消息）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_song_intent(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if not self._songs_enabled():
            return None
        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return None
        cue = self._song_cue(session_id)
        if cue is None:
            return None
        scope = str(cue.get("scope") or "")
        group_id = str(cue.get("group_id") or "")
        user_id = str(cue.get("user_id") or "")
        if not self._filter_allows(scope, group_id, user_id):
            return None
        hit = _song_intent.match_intent(str(cue.get("text") or ""), self._intent_patterns())
        if hit is None:
            return None
        is_group = scope == "group"
        if is_group and self.config.songs.require_mention and not (cue.get("is_at") or cue.get("is_mentioned")):
            # 命中了但没被 @ / 点名：记一条，方便排查「为什么这条没放歌」
            self.ctx.logger.info(
                "点歌线索命中但未 @ / 未点名，已拦下（会话 %s，关键词=%r，文本=%r）",
                session_id,
                hit.keyword,
                str(cue.get("text") or "")[:40],
            )
            return None
        now = time.monotonic()
        if now - self._intent_last.get(session_id, 0.0) < _INTENT_MIN_INTERVAL_SECONDS:
            return None
        songs = self._load_songs()
        if not songs:
            return None
        matches = _songs.match_songs(songs, hit.keyword) if hit.keyword else []
        matched_ids = {song.id for song in matches}
        ordered = list(matches) + [song for song in songs if song.id not in matched_ids]
        limit = max(1, int(self.config.songs.intent_candidates))
        candidates = [song.label() for song in ordered[:limit]]
        note = _song_intent.build_hint(candidates, hit.keyword, limit)
        items = kwargs.get("items")
        if not isinstance(items, list):
            return None
        # 上一轮注入的提示可能还留在历史里，先清掉再说，避免模型重复点歌
        _song_intent.strip_stale_notes(items)
        inserted = _song_intent.append_hint(
            items,
            note,
            item_id=f"mimo-voice-song-{uuid.uuid4().hex}",
            timestamp=datetime.now().astimezone().isoformat(),
        )
        if not inserted:
            return None
        self._intent_last[session_id] = now
        self._intent_armed[session_id] = now + _INTENT_ARMED_TTL_SECONDS
        self._song_cues.pop(session_id, None)
        if len(self._intent_last) > _TTL_CACHE_MAX_ENTRIES:
            self._intent_last.clear()
        reason = "at" if cue.get("is_at") else ("mentioned" if cue.get("is_mentioned") else "private")
        self.ctx.logger.info(
            "已向 planner 注入点歌候选（会话 %s，原因=%s，正则=%r，关键词=%r，候选=%d）",
            session_id,
            reason,
            hit.pattern,
            hit.keyword,
            len(candidates),
        )
        kwargs["items"] = items
        return {"action": "continue", "modified_kwargs": kwargs}

    def _song_armed(self, session_id: str) -> bool:
        """这一轮是不是刚由意图层放行过点歌（播放工具据此可见）。"""
        if not session_id:
            return False
        expires_at = self._intent_armed.get(session_id, 0.0)
        if expires_at <= time.monotonic():
            self._intent_armed.pop(session_id, None)
            return False
        return True

    def _song_tool_ready(self) -> bool:
        """曲库非空才把播放工具暴露给模型；没有歌的时候它只会误导模型。"""
        if not self._songs_enabled():
            return False
        root = self._songs_root()
        path = self._songs_file()
        if root is None or path is None:
            return False
        stamp = (path.stat().st_mtime_ns if path.exists() else 0, len(self._load_songs()))
        cached = getattr(self, "_song_tool_stamp", None)
        if cached is not None and cached == stamp:
            return bool(stamp[1])
        self._song_tool_stamp = stamp
        return bool(stamp[1])


    @HookHandler(
        "chat.receive.after_process",
        name="mimo_voice_collect_song_media",
        description="把入站语音条/音频文件旁路下载到待入库池（OBSERVE：不拦截、不改写消息）",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        timeout_ms=30000,
    )
    async def collect_song_media(self, message: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        """旁路收集：其他插件照旧拿到原始消息，我们只是额外下载一份。"""
        if not self._songs_enabled():
            return
        pool = self._pending_pool()
        if pool is None:
            return
        payload = message if isinstance(message, dict) else {}
        info = payload.get("message_info")
        info = info if isinstance(info, dict) else {}
        group_info = info.get("group_info")
        group_info = group_info if isinstance(group_info, dict) else {}
        user_info = info.get("user_info")
        user_info = user_info if isinstance(user_info, dict) else {}
        session_id = str(payload.get("session_id") or kwargs.get("stream_id") or "").strip()
        group_id = str(group_info.get("group_id") or "").strip()
        user_id = str(user_info.get("user_id") or "").strip()
        scope = str(payload.get("scope") or "").strip() or ("group" if group_id else "private")
        if not session_id or not self._filter_allows(scope, group_id or session_id, user_id):
            return
        segments = payload.get("raw_message")
        segments = segments if isinstance(segments, list) else []
        kinds = sorted({str(seg.get("type") or "") for seg in segments if isinstance(seg, dict)})
        audioish = (
            VOICE_SEGMENT_TYPE in kinds
            or "file" in kinds
            or any(
                isinstance(seg, dict)
                and seg.get("type") == "text"
                and "文件" in str(seg.get("data") or "")
                for seg in segments
            )
        )
        if audioish:
            self.ctx.logger.info(
                "入站音频候选：session=%s scope=%s 段=%s",
                session_id,
                scope,
                ",".join(kinds) or "-",
            )
        try:
            candidates = await self._inbound_audio(payload, group_id=group_id, user_id=user_id)
        except Exception as exc:  # 收集失败不能影响聊天链路
            self.ctx.logger.warning("入站音频收集失败：%s", exc)
            return
        if audioish and not candidates:
            self.ctx.logger.info(
                "入站音频没取到字节（段=%s）：语音条缺 binary_data_base64，或文件消息没有可下载的链接",
                ",".join(kinds) or "-",
            )
        for data, suffix, source in candidates:
            try:
                await self._stash_song_media(
                    pool,
                    data,
                    suffix=suffix,
                    source=source,
                    session_id=session_id,
                    user_id=user_id,
                    sender=str(user_info.get("cardname") or user_info.get("nickname") or ""),
                    message_id=str(payload.get("message_id") or ""),
                )
            except OSError as exc:
                self.ctx.logger.warning("暂存音频失败（%s）", exc)

    async def _inbound_audio(
        self, payload: Dict[str, Any], *, group_id: str = "", user_id: str = ""
    ) -> List[Tuple[bytes, str, str]]:
        """从消息段里挑出音频：语音条直接有 wav 字节，文件消息只有下载链接。"""
        segments = payload.get("raw_message")
        if not isinstance(segments, list):
            return []
        found: List[Tuple[bytes, str, str]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            kind = str(segment.get("type") or "")
            if kind == VOICE_SEGMENT_TYPE:
                data = self._decode_segment_audio(segment)
                if data:
                    found.append((data, ".wav", "voice"))
                continue
            if kind == "file":
                # 有的适配器把文件消息留成 file 段（data 里带 url/name），没转成文本。
                raw = segment.get("data")
                text = raw if isinstance(raw, str) else " ".join(str(raw.get(key) or "") for key in ("url", "file", "name")) if isinstance(raw, dict) else ""
                url = _song_media.extract_file_url(text)
                if not url:
                    continue
                data = await self._download_media(url)
                if data:
                    found.append((data, _song_media.guess_extension(url) or ".mp3", "file"))
                continue
            if kind != "text":
                continue
            text = str(segment.get("data") or "")
            url = _song_media.extract_file_url(text)
            from_text = bool(url)
            if not url:
                url = await self._resolve_file_url(text, group_id=group_id, user_id=user_id)
            if not url:
                continue
            data = await self._download_media(url, text_source=from_text)
            if data:
                found.append((data, _song_media.guess_extension(url) or ".mp3", "file"))
        return found

    async def _call_adapter_api(self, api_name: str, params: Dict[str, Any]) -> Any:
        """通过宿主 ``api.call`` 调适配器的公开 API，返回响应里的 ``data`` 字段。

        适配器的公开 API 统一长这样：``async def api_x(self, params: dict | None = None)``，
        返回 NapCat 的原始响应 ``{"status": "ok", "retcode": 0, "data": {...}}``。
        """
        call = getattr(self.ctx, "call_capability", None)
        if call is None:
            return None
        try:
            result = await call("api.call", api_name=api_name, args={"params": params})
        except Exception as exc:  # 适配器没这个 API / 宿主不支持，都不该影响聊天
            self._warn_once(f"songs:api:{api_name}", "调用适配器 API %s 失败：%s", api_name, exc)
            return None
        return self._unwrap_adapter_response(result, api_name)

    def _unwrap_adapter_response(self, result: Any, api_name: str) -> Any:
        """把 ``api.call`` 的返回拆成业务数据。

        实测（2026-11-20）返回的是 NapCat 原始信封：``{"status": "ok", "retcode": 0, "data": {...}}``；
        有的宿主播还会再包一层 ``{"success": true, "result": <信封>}``，两种都认。
        """
        payload = result
        if isinstance(payload, dict) and payload.get("success") is True:
            payload = payload.get("result")
        if isinstance(payload, dict) and payload.get("status") in ("ok", "async") and "data" in payload:
            return payload.get("data")
        if isinstance(payload, dict) and payload.get("success") is False:
            error = str(payload.get("error") or "未知原因")
            self._warn_once(f"songs:api:{api_name}", "适配器 API %s 不可用：%s", api_name, error)
            return None
        self._warn_once(
            f"songs:api:{api_name}",
            "适配器 API %s 返回了无法识别的结构：%r",
            api_name,
            result if not isinstance(result, str) else result[:200],
        )
        return None

    async def _resolve_file_url(self, text: str, *, group_id: str, user_id: str) -> str:
        """文件消息只有文件名时，回查最近历史拿 ``file_id``，再向适配器要下载地址。

        适配器把 ``file`` 段压成 ``[文件] 名字，大小: N``，私聊连 url 都没有；但它公开了
        ``message.get_*_msg_history`` 与 ``file.get_*_file_url``，插件层就能自己补齐，
        不需要改适配器。
        """
        if not self.config.songs.lookup_file_url:
            return ""
        name, size = _song_media.parse_file_message(text)
        if not name or not _song_media.guess_extension(name):
            return ""
        if group_id:
            history = await self._call_adapter_api(
                "adapter.napcat.message.get_group_msg_history", {"group_id": group_id, "count": 5}
            )
        else:
            history = await self._call_adapter_api(
                "adapter.napcat.message.get_friend_msg_history", {"user_id": user_id, "count": 5}
            )
        file_id = self._pick_file_id(history, name=name, size=size)
        if not file_id:
            self.ctx.logger.info("文件消息没找回 file_id：%s（大小 %s）", name, size or "?")
            return ""
        if group_id:
            detail = await self._call_adapter_api(
                "adapter.napcat.file.get_group_file_url", {"group_id": group_id, "file_id": file_id}
            )
        else:
            detail = await self._call_adapter_api(
                "adapter.napcat.file.get_private_file_url", {"file_id": file_id}
            )
        if isinstance(detail, dict):
            return str(detail.get("url") or "").strip()
        return ""

    @staticmethod
    def _pick_file_id(history: Any, *, name: str, size: int) -> str:
        """从历史消息里找出同名（同大小优先）的 file 段，返回它的 ``file_id``。"""
        messages = history.get("messages") if isinstance(history, dict) else history
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            segments = message.get("message") if isinstance(message, dict) else None
            if not isinstance(segments, list):
                continue
            for segment in segments:
                if not isinstance(segment, dict) or str(segment.get("type") or "") != "file":
                    continue
                data = segment.get("data")
                if not isinstance(data, dict):
                    continue
                file_name = str(data.get("file") or data.get("name") or "").strip()
                file_size = str(data.get("file_size") or "").strip()
                if name and file_name and file_name != name:
                    continue
                if size and file_size and file_size != str(size):
                    continue
                file_id = str(data.get("file_id") or "").strip()
                if file_id:
                    return file_id
        return ""

    @staticmethod
    def _decode_segment_audio(segment: Dict[str, Any]) -> bytes:
        raw = segment.get("binary_data_base64")
        if not isinstance(raw, str) or not raw.strip():
            return b""
        try:
            return base64.b64decode(raw, validate=False)
        except (ValueError, TypeError):
            return b""

    async def _download_media(self, url: str, *, text_source: bool = False) -> bytes:
        """从 QQ 文件链接下载音频；只认 http(s) + 公网主机，大小按配置截断。

        QQ 文件的 CDN 地址目前只有 ``http://<公网 IP>:80/qqdownloadftnv5?...``（2026-11-19 实测），
        所以不能强制 https；作为补偿，主机必须是公网地址（内网 / 本机 / 元数据地址一律拒绝）。
        """
        config = self.config.songs
        limit = max(1, int(float(config.max_file_mb) * 1024 * 1024))
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            self._warn_once("songs:scheme", "只下载 http(s) 链接（收到 %s），已跳过：", url[:48])
            return b""
        host = urlsplit(url).hostname or ""
        if not host or _config._is_private_host(host):
            self._warn_once("songs:host", "拒绝下载内网/本机地址（%s），已跳过", host or "?")
            return b""
        if text_source and not _song_media.is_qq_media_url(url):
            # 文本里的链接任何人都能照着 [文件] x.mp3，链接: http://… 的格式伪造
            self._warn_once("songs:text-url", "文本里的下载链接不是 QQ 域名（%s），已跳过", host)
            return b""
        timeout = aiohttp.ClientTimeout(total=float(config.download_timeout_seconds))
        try:
            session = await self._get_session()
            async with session.get(url, timeout=timeout) as response:
                if response.status != 200:
                    self.ctx.logger.warning("音频下载失败：HTTP %s", response.status)
                    return b""
                chunks: List[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > limit:
                        self.ctx.logger.warning(
                            "音频超过 %.1f MB 上限（%s），未暂存",
                            float(config.max_file_mb),
                            _song_media.readable_size(total),
                        )
                        return b""
                    chunks.append(chunk)
            return b"".join(chunks)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            self.ctx.logger.warning("音频下载失败（%s）", exc)
            return b""

    async def _stash_song_media(
        self,
        pool: PendingPool,
        data: bytes,
        *,
        suffix: str,
        source: str,
        session_id: str,
        user_id: str,
        sender: str,
        message_id: str,
    ) -> None:
        config = self.config.songs
        if not data:
            return
        limit = max(1, int(float(config.max_file_mb) * 1024 * 1024))
        if len(data) > limit:
            return
        ext = (suffix or ".wav").lower()
        payload = data
        if ext == ".wav" and config.downsample:
            try:
                lowered = await asyncio.to_thread(_song_media.downsample_wav, data, int(config.downsample_rate))
            except (ValueError, RuntimeError, OSError) as exc:
                self.ctx.logger.debug("降采样跳过：%s", exc)
                lowered = None
            if lowered:
                payload = lowered
        digest = hashlib.sha256(payload).hexdigest()
        # 去重键带 message_id：同一条消息被钩子跑两遍要挡；10 分钟内真·重发同一首则放行
        consumed_key = f"{message_id}|{digest}" if message_id else digest
        consumed_at = self._consumed_media.get(consumed_key)
        if consumed_at is not None:
            if time.monotonic() - consumed_at < 600.0:
                return
            self._consumed_media.pop(consumed_key, None)
        duration = await asyncio.to_thread(_song_media.probe_duration, payload, ext)
        if duration > float(config.max_duration_seconds) + 1.0:
            self.ctx.logger.info(
                "音频 %.0fs 超过 %.0fs 上限，未暂存（/voice sing 不收超长曲目）",
                duration,
                float(config.max_duration_seconds),
            )
            return
        item = await asyncio.to_thread(
            pool.put,
            payload,
            suffix=ext,
            duration_s=duration,
            session_id=session_id,
            user_id=user_id,
            sender=sender,
            message_id=message_id,
            source=source,
        )
        await asyncio.to_thread(
            pool.prune,
            ttl_seconds=float(config.pending_ttl_seconds),
            max_bytes=int(float(config.pending_max_mb) * 1024 * 1024),
            max_entries=int(config.pending_max_entries),
        )
        self.ctx.logger.info(
            "已暂存音频 %s（%s，%s，来源=%s）：%d 秒内可 /voice sing add <歌名>",
            _song_media.readable_size(item.size),
            _song_media.duration_label(item.duration_s),
            item.file,
            source,
            int(float(config.add_window_seconds)),
        )

    async def _handle_sing_command(
        self,
        stream_id: str,
        group_id: str,
        user_id: str,
        platform: str,
        rest: str,
    ) -> None:
        """``/voice sing`` 的所有分支；群里也能用（其他子命令仍然只限私聊管理员）。"""
        if not self._songs_enabled():
            await self._command_reply(stream_id, "唱歌点播没开：让管理员把 songs.enabled 打开。")
            return
        scope = "group" if group_id else "private"
        if not self._filter_allows(scope, group_id or stream_id, user_id):
            await self._command_reply(stream_id, "这个会话不在语音放行名单里。")
            return
        action, name, field_name, value = _songs.parse_sing(rest)
        admin = _commands.admin_matches(user_id, platform, self.config.commands.admins)
        if action in ("add", "addforce", "del", "set") and not admin:
            await self._command_reply(stream_id, "入库、删除、改名只有管理员能用。")
            return
        songs = self._load_songs()
        if action == "list":
            await self._sing_list(stream_id, songs)
        elif action == "sing":
            await self._sing_play(stream_id, songs, name, user_id)
        elif action in ("add", "addforce"):
            await self._sing_add(
                stream_id, songs, name, force=action == "addforce", user_id=user_id, group_id=group_id
            )
        elif action == "del":
            await self._sing_delete(stream_id, songs, name)
        elif action == "set":
            await self._sing_set(stream_id, songs, name, field_name, value)

    async def _stash_latest_file_from_history(
        self, *, group_id: str, user_id: str, session_id: str
    ) -> Optional[Any]:
        """``add`` 时的兜底：从最近历史里取本人在窗口内发的音频文件，现下载现暂存。

        正常路径是入站钩子已经落盘；这里只处理漏网的情况（插件刚重启、适配器没给链接）。
        """
        config = self.config.songs
        pool = self._pending_pool()
        if pool is None or not config.lookup_file_url:
            return None
        if group_id:
            history = await self._call_adapter_api(
                "adapter.napcat.message.get_group_msg_history", {"group_id": group_id, "count": 10}
            )
        else:
            history = await self._call_adapter_api(
                "adapter.napcat.message.get_friend_msg_history", {"user_id": user_id, "count": 10}
            )
        messages = history.get("messages") if isinstance(history, dict) else history
        if not isinstance(messages, list):
            return None
        now = time.time()
        window = float(config.add_window_seconds)
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if user_id and str(message.get("user_id") or "") != str(user_id):
                continue
            stamp = float(message.get("time") or 0.0)
            if stamp and now - stamp > window:
                continue
            segments = message.get("message")
            if not isinstance(segments, list):
                continue
            for segment in segments:
                if not isinstance(segment, dict) or str(segment.get("type") or "") != "file":
                    continue
                data = segment.get("data")
                data = data if isinstance(data, dict) else {}
                file_name = str(data.get("file") or "")
                ext = _song_media.guess_extension(file_name)
                file_id = str(data.get("file_id") or "").strip()
                if not ext or not file_id:
                    continue
                if group_id:
                    detail = await self._call_adapter_api(
                        "adapter.napcat.file.get_group_file_url", {"group_id": group_id, "file_id": file_id}
                    )
                else:
                    detail = await self._call_adapter_api(
                        "adapter.napcat.file.get_private_file_url", {"file_id": file_id}
                    )
                url = str(detail.get("url") or "").strip() if isinstance(detail, dict) else ""
                if not url:
                    continue
                blob = await self._download_media(url)
                if not blob:
                    continue
                await self._stash_song_media(
                    pool,
                    blob,
                    suffix=ext,
                    source="file",
                    session_id=session_id,
                    user_id=user_id,
                    sender=str((message.get("sender") or {}).get("nickname") or ""),
                    message_id=str(message.get("message_id") or ""),
                )
                return await asyncio.to_thread(
                    pool.claim, session_id=session_id, user_id=user_id, window_seconds=window
                )
        return None

    async def _sing_list(self, stream_id: str, songs: List[Any]) -> None:
        lines = _songs.render_library(songs)
        limit = 60
        body = lines[:limit]
        if len(lines) > limit:
            body.append(f"……还有 {len(lines) - limit} 首")
        header = f"曲库共 {len(songs)} 首（上限 {int(self.config.songs.max_songs)}）："
        await self._command_reply(stream_id, "\n".join([header, *body]))

    def _sing_cooldown(self, stream_id: str, user_id: str) -> str:
        """点歌冷却：先判会话，再判个人；通过时顺手记时间。"""
        now = time.monotonic()
        group_seconds = max(0.0, float(self.config.songs.sing_group_cooldown_seconds))
        user_seconds = max(0.0, float(self.config.songs.sing_user_cooldown_seconds))
        last_group = self._sing_group_last.get(stream_id, 0.0)
        if group_seconds and now - last_group < group_seconds:
            wait = int(group_seconds - (now - last_group)) + 1
            return f"这个会话刚点过，{wait} 秒后再来"
        last_user = self._sing_user_last.get(user_id, 0.0) if user_id else 0.0
        if user_seconds and user_id and now - last_user < user_seconds:
            wait = int(user_seconds - (now - last_user)) + 1
            return f"点得有点快，{wait} 秒后再试"
        self._sing_group_last[stream_id] = now
        if user_id:
            self._sing_user_last[user_id] = now
        if len(self._sing_group_last) > _TTL_CACHE_MAX_ENTRIES:
            self._sing_group_last.clear()
        if len(self._sing_user_last) > _TTL_CACHE_MAX_ENTRIES:
            self._sing_user_last.clear()
        return ""

    async def _sing_play(self, stream_id: str, songs: List[Any], query: str, user_id: str) -> None:
        if not query:
            await self._command_reply(stream_id, "点歌要给歌名：/voice sing <歌名/别名/标签>")
            return
        if not self._session_switch.get(stream_id, True):
            await self._command_reply(stream_id, "本会话语音已关闭（/voice on 可恢复）。")
            return
        matches = _songs.match_songs(songs, query)
        if not matches:
            await self._command_reply(stream_id, f"没有找到「{query}」，/voice sing list 看看有哪些。")
            return
        blocked = self._sing_cooldown(stream_id, user_id)
        if blocked:
            await self._command_reply(stream_id, blocked)
            return
        song = random.choice(matches)
        root = self._songs_root()
        if root is None:
            await self._command_reply(stream_id, "找不到插件数据目录，无法读取曲库。")
            return
        path = root / song.file
        try:
            audio = path.read_bytes()
        except OSError:
            await self._command_reply(stream_id, f"「{song.name}」的音频文件不见了，让管理员重新入库。")
            return
        limit = max(1, int(float(self.config.songs.max_file_mb) * 1024 * 1024))
        if len(audio) > limit:
            await self._command_reply(stream_id, f"「{song.name}」文件超过 {float(self.config.songs.max_file_mb):.1f} MB 上限。")
            return
        if not await self._send_voice(stream_id, audio):
            await self._command_reply(stream_id, "语音发送失败，稍后再试。")
            return
        self.ctx.logger.info(
            "点歌已发送：%s → %s（%s，%.1f MB，命中 %d 条）",
            song.name,
            stream_id,
            _song_media.duration_label(song.duration_s or len(audio) / 16000.0),
            len(audio) / 1048576.0,
            len(matches),
        )

    async def _sing_add(
        self,
        stream_id: str,
        songs: List[Any],
        name: str,
        *,
        force: bool,
        user_id: str,
        group_id: str = "",
    ) -> None:
        clean = (name or "").strip()
        if not clean:
            await self._command_reply(stream_id, "要一个歌名：/voice sing add <歌名>")
            return
        async with self._songs_lock:
            await self._sing_add_locked(stream_id, clean, force=force, user_id=user_id, group_id=group_id)

    async def _sing_add_locked(
        self,
        stream_id: str,
        clean: str,
        *,
        force: bool,
        user_id: str,
        group_id: str,
    ) -> None:
        config = self.config.songs
        # 拿到锁后重新读盘：并发场景下调用方传进来的是加锁前的快照
        songs = self._load_songs()
        pool = self._pending_pool()
        if pool is None:
            await self._command_reply(stream_id, "找不到插件数据目录，无法入库。")
            return
        item = await asyncio.to_thread(
            pool.claim,
            session_id=stream_id,
            user_id=user_id,
            window_seconds=float(config.add_window_seconds),
        )
        if item is None:
            # 兜底：钩子没收到（适配器当时没给链接、插件刚重启等）时，直接回查历史里的文件消息。
            item = await self._stash_latest_file_from_history(
                group_id=group_id, user_id=user_id, session_id=stream_id
            )
        if item is None:
            await self._command_reply(
                stream_id,
                f"最近 {int(float(config.add_window_seconds))} 秒内没收到音频：先发一条语音或 mp3/wav/flac，"
                f"再马上发 /voice sing add {clean}",
            )
            return
        root = self._songs_root()
        if root is None:
            await self._command_reply(stream_id, "找不到插件数据目录，无法入库。")
            return
        try:
            audio = await asyncio.to_thread(item.path_in(pool.root).read_bytes)
        except OSError:
            await self._command_reply(stream_id, "暂存的音频已经过期，重新发一次再入库。")
            return
        limit = max(1, int(float(config.max_file_mb) * 1024 * 1024))
        if len(audio) > limit:
            await self._command_reply(stream_id, f"这个文件超过 {float(config.max_file_mb):.1f} MB 上限。")
            return
        ext = item.ext or ".wav"
        file_name = f"{item.sha256[:16]}{ext}"
        duration = item.duration_s or float(await asyncio.to_thread(_song_media.probe_duration, audio, ext))
        if duration > float(config.max_duration_seconds) + 1.0:
            await self._command_reply(
                stream_id,
                f"这首歌 {_song_media.duration_label(duration)}，超过 {int(float(config.max_duration_seconds))} 秒上限。",
            )
            return
        previous = _songs.find_conflict(songs, [clean])
        if previous is None:
            # 容量上限只拦「新增」；同名替换是原地换文件，不再额外占空间
            used = await asyncio.to_thread(self._songs_total_bytes, root)
            cap = int(float(config.max_total_mb) * 1024 * 1024)
            if used + len(audio) > cap:
                await self._command_reply(
                    stream_id,
                    f"曲库已占 {_song_media.readable_size(used)} / "
                    f"{_song_media.readable_size(cap)}，先 /voice sing del <歌名> 删几首再入库。",
                )
                return
        try:
            await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread((root / file_name).write_bytes, audio)
        except OSError as exc:
            await self._command_reply(stream_id, f"写入曲库失败：{exc}")
            return
        song, error = _songs.add_song(
            songs,
            clean,
            file_name=file_name,
            duration_s=duration,
            added_by=user_id,
            now=time.time(),
            max_songs=int(config.max_songs),
            force=force,
        )
        if song is None or error:
            await asyncio.to_thread(self._quiet_unlink, root / file_name)
            await self._command_reply(stream_id, error or "入库失败。")
            return
        try:
            self._save_songs(songs)
        except OSError as exc:
            await self._command_reply(stream_id, f"保存曲库失败：{exc}")
            return
        if previous is not None and previous.file and previous.file != file_name:
            await asyncio.to_thread(self._quiet_unlink, root / previous.file)
        await asyncio.to_thread(pool.discard, item)
        self._remember_consumed(item)
        verb = "已替换" if previous is not None else "已入库"
        await self._command_reply(
            stream_id,
            f"{verb}「{song.name}」（{_song_media.duration_label(song.duration_s)}，"
            f"{_song_media.readable_size(len(audio))}）：/voice sing {song.name}",
        )
        self.ctx.logger.info("曲库新增：%s（%s，%s）", song.name, file_name, _song_media.readable_size(len(audio)))

    async def _sing_delete(self, stream_id: str, songs: List[Any], name: str) -> None:
        async with self._songs_lock:
            songs = self._load_songs()
            await self._sing_delete_locked(stream_id, songs, name)

    async def _sing_delete_locked(self, stream_id: str, songs: List[Any], name: str) -> None:
        song, error = _songs.delete_song(songs, name)
        if song is None or error:
            await self._command_reply(stream_id, error or "删除失败。")
            return
        try:
            self._save_songs(songs)
        except OSError as exc:
            await self._command_reply(stream_id, f"保存曲库失败：{exc}")
            return
        root = self._songs_root()
        if root is not None and song.file:
            trash = root / ".trash"
            try:
                await asyncio.to_thread(trash.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread((root / song.file).replace, trash / song.file)
            except OSError:
                pass
        await self._command_reply(stream_id, f"已删除「{song.name}」（音频移到 .trash，可手工找回）。")
        self.ctx.logger.info("曲库删除：%s", song.name)

    async def _sing_set(self, stream_id: str, songs: List[Any], name: str, field_name: str, value: str) -> None:
        async with self._songs_lock:
            songs = self._load_songs()
            await self._sing_set_locked(stream_id, songs, name, field_name, value)

    async def _sing_set_locked(
        self, stream_id: str, songs: List[Any], name: str, field_name: str, value: str
    ) -> None:
        song, error = _songs.set_song_field(songs, name, field_name, value)
        if song is None or error:
            await self._command_reply(stream_id, error or "修改失败。")
            return
        try:
            self._save_songs(songs)
        except OSError as exc:
            await self._command_reply(stream_id, f"保存曲库失败：{exc}")
            return
        await self._command_reply(stream_id, f"已更新「{song.name}」的 {field_name}。")

    def _remember_consumed(self, item: PendingItem) -> None:
        """记下「这条消息的这份音频已经被入库」，挡掉钩子的重复投递。"""
        key = f"{item.message_id}|{item.sha256}" if item.message_id else item.sha256
        self._consumed_media[key] = time.monotonic()
        if len(self._consumed_media) > 200:
            self._consumed_media.clear()

    @staticmethod
    def _quiet_unlink(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def _trigger_rules(self) -> List[Tuple[str, str]]:
        """把配置里的规则换成 ``(词, 位置)``；空词直接跳过。"""

        rules: List[Tuple[str, str]] = []
        for rule in self.config.triggers.rules:
            word = str(getattr(rule, "word", "") or "").strip()
            if not word:
                continue
            rules.append((word, _triggers.normalize_mode(getattr(rule, "mode", "")) or _triggers.MODE_ANY))
        return rules

    def _trigger_ttl(self) -> float:
        """回复落地窗口：从收到消息算起，等于合成超时再留 10 秒。"""

        return max(5.0, float(self.config.general.timeout) + _TRIGGER_TTL_MARGIN_SECONDS)

    def _active_trigger(self, stream_id: str) -> Optional[Tuple[str, str]]:
        entry = self._trigger_requests.get(stream_id)
        if entry is None:
            return None
        word, mode, expires_at = entry
        if expires_at < time.monotonic():
            self._trigger_requests.pop(stream_id, None)
            return None
        return word, mode

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

    def _should_convert(
        self,
        text: str,
        stream_id: str,
        scope: str,
        group_id: str,
        user_id: str,
        *,
        raw_text: Optional[str] = None,
    ) -> Tuple[bool, str, float]:
        """返回 ``(是否转语音, 触发来源, 当时的概率)``，来源与概率只用于日志。

        ``text`` 是清洗后的朗读文本，``raw_text`` 是模型原始回复。
        情绪加权必须看原始回复：清洗会把 ``！！`` 折成一个 ``！``、``？？`` 折成 ``？``、
        ``～``/``…``/emoji 直接删掉，用清洗后的文本判断会让大部分情绪标记永远命中不了。
        """
        if scope not in ("group", "private"):
            return False, "scope", 0.0
        if self._session_switch.get(stream_id) is False:
            # /voice off 只影响这个会话，进程内有效；没设过的会话按开启算
            return False, "off", 0.0
        if not self._filter_allows(scope, group_id, user_id):
            return False, "filter", 0.0
        # 触发词（0.5.6）：用户明确要求这一次用语音，优先于模式与抽签；
        # 不受短句上限、英文策略约束，但要过放行规则与会话开关（上面两道）。
        trigger_hit = self._active_trigger(stream_id)
        if trigger_hit is not None:
            return True, f"trigger:{trigger_hit[0]}", 1.0
        mode = self.config.trigger.mode
        if mode == "voice_only":
            return True, "voice_only", 1.0
        if mode == "llm_trigger":
            return (not self._text_override_active(stream_id)), "llm_trigger", 1.0
        if mode == "random_llm" and self._llm_voice_request_active(stream_id):
            # 模型点名要语音时不受短句上限约束，长句也可以，只是要过 max_text_length
            return True, "llm_request", 1.0
        if not self._short_enough(text):
            return False, "short_reply", 0.0

        mention = self.config.trigger.mention_priority and self._mention_priority.active(stream_id)
        if self.config.trigger.random_requires_chinese and _text.contains_latin(text) and not mention:
            # 随机抽中的一次机会，宁可发文字也别把 token/LLM 念成字母；
            # 放在抽签之前，避免白耗掉 decision_ttl 内的这次判定。
            # 点名那一次不受此限（用户就是要听），但英文仍按 english_policy 清洗过。
            return False, "english", 0.0

        probability = float(self.config.trigger.probability)
        hits: List[str] = []
        if self.config.trigger.emotion_weighting:
            # 用原始回复判断情绪：清洗后的文本已经丢掉大部分标记（见 docstring）
            bonus, hits = _trigger.emotion_bonus(
                text if raw_text is None else raw_text,
                step=float(self.config.trigger.emotion_step),
                cap=float(self.config.trigger.emotion_cap),
            )
            probability = min(1.0, probability + bonus)
        if mention:
            probability = max(probability, float(self.config.trigger.mention_probability))

        if mention or self.config.trigger.emotion_weighting:
            # 点名/情绪让概率逐条变化，decision_ttl 的缓存会把上一条的结论套到下一条上，这里不用缓存
            enabled = probability >= 1.0 or random.random() < probability
        else:
            enabled = self._random_voice_enabled(stream_id)

        if enabled and mention:
            self._mention_priority.consume(stream_id)
        if not enabled:
            return False, "miss", probability
        if mention:
            return True, "mention", probability
        if hits:
            return True, "emotion(" + "/".join(hits) + ")", probability
        return True, "random", probability

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
        # 自己发的文字（并存文字段、命令回执、定时文字）原样放行，否则会被再转一次语音
        if self._consume_self_sent(stream_id, text):
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
        should_convert, reason, probability = self._should_convert(
            prepared, stream_id, scope, group_id, user_id, raw_text=text
        )
        if not should_convert:
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

        # 语音单独成一条。文字另发：一条消息里混 [text, voice] 时，适配器可能只落地语音段
        # （2026-11-16 线上实测：同一条混合消息里文字被吞，拆成两条后稳定）。
        outbound["raw_message"] = [self._voice_segment(audio)]
        if self.config.delivery.keep_text:
            if self.config.delivery.text_first:
                await self._send_text(stream_id, text)
            else:
                self._spawn_text(stream_id, text)
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
        self._mention_priority.consume(stream_id)
        self._trigger_requests.pop(stream_id, None)
        if had_reply:
            self.ctx.logger.info("QQ 语音不支持引用组合，已移除会话 %s 的引用段", stream_id)
        self.ctx.logger.info(
            "已将会话 %s 的纯文本回复替换为语音（%d 字，来源=%s，概率=%.2f，并存=%s）",
            stream_id,
            len(prepared),
            reason,
            probability,
            self._keep_text_label(),
        )
        return {"action": "continue", "modified_kwargs": kwargs}

    @staticmethod
    def _voice_segment(audio: bytes) -> Dict[str, Any]:
        """出站语音段。文字不再塞进同一条消息，见 ``convert_outbound_text``。"""

        return {
            "type": VOICE_SEGMENT_TYPE,
            "data": "",
            "hash": hashlib.sha256(audio).hexdigest(),
            "binary_data_base64": base64.b64encode(audio).decode("ascii"),
        }

    def _spawn_text(self, stream_id: str, text: str) -> None:
        """语音先时，等语音落地后再补一条文字（不等结果，失败只告警）。"""

        async def delayed() -> None:
            await asyncio.sleep(_KEEP_TEXT_DELAY_SECONDS)
            await self._send_text(stream_id, text)

        task = asyncio.create_task(delayed())
        self._pending_text_tasks.add(task)
        task.add_done_callback(self._pending_text_tasks.discard)

    def _keep_text_label(self) -> str:
        if not self.config.delivery.keep_text:
            return "关"
        return "开(文字先)" if self.config.delivery.text_first else "开(语音先)"

    # ------------------------------------------------------------ 规划器工具

    _TOOL_TEXT_REPLY = "mimo_voice_text_reply"
    _TOOL_VOICE_SAY = "mimo_voice_say"
    _TOOL_PLAY_SONG = "mimo_voice_play_song"

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
        # 播放工具只在「这一轮刚命中点歌意图」时暴露：曲库为空、或没命中时都给模型看见，
        # 它就会自己找机会放歌（线上确实发生过没点歌也播了一次）。
        if not self._song_tool_ready() or not self._song_armed(str(kwargs.get("session_id") or "")):
            hidden.add(self._TOOL_PLAY_SONG)
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

    @Tool(
        "mimo_voice_play_song",
        description=(
            "从本地曲库挑一首歌，直接以语音条发到当前会话。只有当用户明确点了歌、"
            "且歌名出现在上下文给出的曲库候选里时才调用；曲库里没有的歌不要调用。"
        ),
        visibility="visible",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "曲库里的歌名，原样照抄候选列表（别名也可以，但歌名最稳）",
                }
            },
            "required": ["name"],
        },
    )
    async def play_song(self, **kwargs: Any) -> Dict[str, Any]:
        """意图层的落地点：模型点歌 → 这里直接发语音条，不依赖标记文本。"""
        stream_id = str(kwargs.get("stream_id") or kwargs.get("session_id") or "").strip()
        name = str(kwargs.get("name") or "").strip()
        if not stream_id:
            return {"success": False, "content": "无法获取当前会话，直接用文字回复。"}
        if not self._songs_enabled():
            return {"success": False, "content": "曲库没开启，直接用文字回复。"}
        if not name:
            return {"success": False, "content": "缺少歌名。"}
        matches = _songs.match_songs(self._load_songs(), name)
        if not matches:
            return {
                "success": False,
                "content": f"曲库里没有「{name}」，不要编造，直接告诉对方曲库里没有这首。",
            }
        song = random.choice(matches)
        cached = self._lookup_scope(stream_id)
        blocked = self._sing_cooldown(stream_id, str(cached.get("user_id") or ""))
        if blocked:
            return {"success": False, "content": f"{blocked}，用文字说明即可。"}
        root = self._songs_root()
        path = root / song.file if root is not None else None
        if path is None:
            return {"success": False, "content": "找不到曲库目录，直接用文字回复。"}
        try:
            audio = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            self.ctx.logger.warning("点歌读取音频失败（%s）：%s", song.file, exc)
            return {"success": False, "content": "音频文件读不出来，改用文字告诉对方。"}
        delivered = await self._send_voice(stream_id, audio)
        self._intent_armed.pop(stream_id, None)
        if not delivered:
            return {"success": False, "content": "语音发送失败了，用文字解释一下。"}
        self.ctx.logger.info(
            "意图层点歌已发送（会话 %s，歌名=%s，来源=%s）",
            stream_id,
            song.name,
            song.file,
        )
        return {
            "success": True,
            "content": f"已把「{song.name}」作为语音条发出，不必再把歌名念一遍，也不用说自己在唱。",
        }

    # ---------------------------------------------------------- 手动命令（0.5.5）

    @Command(
        "voice",
        description="MiMo 语音手动命令：/voice on|off|test|status|help|say|sing（除 sing 外仅私聊、仅管理员）",
        pattern=r"^/(?:voice|vc)(?:\s+.*)?$",
    )
    async def handle_voice_command(self, **kwargs: Any) -> Tuple[bool, Optional[str], int]:
        """处理 ``/voice`` 与 ``/vc``。

        权限不交给宿主的 ``operator``：那套白名单在宿主配置里，插件拿不到多管理员列表。
        这里自己判 ``commands.admins``（可写 ``10000002`` 或 ``qq:10000002``），失败也给回执。
        除 ``/voice sing list`` 与 ``/voice sing <歌名>``（点歌）外，命令只在私聊生效——群里的
        ``/voice`` 按普通消息处理，避免在群里刷屏；入库 / 删除 / 改名因此也只能私聊。
        """
        parsed = _commands.parse_command(str(kwargs.get("text") or ""))
        if parsed is None or not self.config.commands.enabled:
            return True, None, 0

        stream_id = str(kwargs.get("stream_id") or "").strip()
        group_id = str(kwargs.get("group_id") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip()
        platform = str(kwargs.get("platform") or "qq").strip() or "qq"

        sub, rest = parsed
        if sub == "sing":
            # 群里只放行 list 和点歌；入库 / 删除 / 改名跟 test、say 一样只限私聊，
            # 不合规时直接落到下面那条「只在私聊里可用」的回执，文案与它们完全一致。
            action = _songs.parse_sing(rest)[0]
            if action in ("list", "sing") or not group_id:
                await self._handle_sing_command(stream_id, group_id, user_id, platform, rest)
                return True, None, 1

        if group_id:
            if stream_id:
                await self._command_reply(stream_id, "这些命令只在私聊里可用，请私聊我。")
            return True, None, 1

        if not _commands.admin_matches(user_id, platform, self.config.commands.admins):
            denied = (
                "还没有配置管理员：请把 QQ 号填进 commands.admins（可填多个）。"
                if not self.config.commands.admins
                else "你没有权限使用这些命令。"
            )
            if stream_id:
                await self._command_reply(stream_id, denied)
            return True, None, 1

        if sub in ("", "help"):
            if stream_id:
                await self._command_reply(stream_id, _commands.render_help())
        elif sub in ("on", "off"):
            if stream_id:
                await self._command_switch(stream_id, group_id, user_id, sub == "on")
        elif sub == "status":
            if stream_id:
                snapshot = self._status_snapshot(stream_id, group_id, user_id)
                await self._command_reply(stream_id, _commands.render_status(snapshot))
        elif sub == "test":
            if stream_id:
                await self._command_test(stream_id, rest)
        elif sub == "say":
            if stream_id:
                await self._command_say(stream_id, rest, user_id, platform)
        else:
            if stream_id:
                await self._command_reply(
                    stream_id,
                    f"不认识的子命令 {sub!r}。\n{_commands.render_help()}",
                )
        return True, None, 1

    async def _command_reply(self, stream_id: str, text: str) -> None:
        """命令回执必须自己发：宿主只把 handler 的 response 写日志，不会替我们发出去。"""
        await self._send_text(stream_id, text)

    async def _command_switch(self, stream_id: str, group_id: str, user_id: str, enabled: bool) -> None:
        # 没显式设置过时按开启算；所以首次 /voice on 回"已开启"，重复才回"本来就是开启的"
        previous = self._session_switch.get(stream_id)
        self._session_switch[stream_id] = enabled
        label = self._session_label(stream_id, group_id=group_id, user_id=user_id)
        state = "开启" if enabled else "关闭"
        if previous == enabled:
            await self._command_reply(stream_id, f"{label}语音本来就是{state}的")
        else:
            await self._command_reply(stream_id, f"{label}语音已{state}")

    def _session_label(self, stream_id: str, *, group_id: str = "", user_id: str = "") -> str:
        """给回执用的会话名：群 123 / QQ 10000002；缓存里没有就退回简易标签。"""
        cached = self._lookup_scope(stream_id)
        group_id = group_id or cached.get("group_id", "")
        user_id = user_id or cached.get("user_id", "")
        if group_id:
            return f"群 {group_id}"
        if user_id:
            return f"QQ {user_id}"
        return f"会话 {stream_id[:12]}"

    def _command_prepare_text(self, raw: str) -> Tuple[Optional[str], str]:
        """命令文本的清洗与长度校验：超限直接拒绝，不静默截断。"""
        text = (raw or "").strip()
        if not text:
            return None, ""
        limit = min(
            int(self.config.commands.test_max_chars),
            int(self.config.general.max_text_length),
        )
        if len(text) > limit:
            return None, f"文本太长（{len(text)} 字，上限 {limit} 字）。"
        prepared = self._sanitize_for_tts(text)
        if not prepared:
            return None, "这段文本没有可朗读的内容。"
        return prepared, ""

    async def _command_synthesize(self, prepared: str) -> Tuple[bytes, str]:
        """合成并后处理；返回 ``(音频字节, 错误文案)``，两者必有其一为空。"""
        try:
            audio = await self._synthesize_guarded(prepared)
        except _SynthesisBusy:
            return b"", "正在合成上一条语音，请稍后再试。"
        except Exception as exc:
            reason = str(exc)
            if reason.startswith("语音合成失败: "):
                reason = reason[len("语音合成失败: ") :]
            return b"", f"语音合成失败：{reason}。可发 /voice status 查看状态。"
        try:
            audio = await asyncio.to_thread(self._postprocess, audio)
        except Exception as exc:  # pragma: no cover - 后处理失败不该阻断命令
            self._warn_once("post:command", "语音后处理失败（%s），本条按原样发出", exc)
        return audio, ""

    async def _command_test(self, stream_id: str, raw: str) -> None:
        prepared, error = self._command_prepare_text(raw)
        if error:
            await self._command_reply(stream_id, error)
            return
        if not prepared:
            await self._command_reply(stream_id, f"用法：{_commands.usage_of('test')}")
            return
        audio, error = await self._command_synthesize(prepared)
        if error:
            await self._command_reply(stream_id, error)
            return
        if not await self._deliver_voice(stream_id, raw, audio):
            await self._command_reply(stream_id, "语音发送失败，详细原因见日志。")

    async def _command_say(self, stream_id: str, rest: str, user_id: str, platform: str) -> None:
        target, raw_text = _commands.split_target_and_text(rest)
        if not target or not raw_text:
            await self._command_reply(stream_id, f"用法：{_commands.usage_of('say')}")
            return
        kind, ident = _commands.parse_target(target)
        if not ident:
            await self._command_reply(stream_id, f"用法：{_commands.usage_of('say')}")
            return
        if ident == user_id:
            # 发给自己没有意义（这条私聊本来就能用 test），省一次合成
            await self._command_reply(stream_id, "目标就是当前私聊，直接用 /voice test 即可。")
            return

        prepared, error = self._command_prepare_text(raw_text)
        if error:
            await self._command_reply(stream_id, error)
            return
        if not prepared:
            await self._command_reply(stream_id, f"用法：{_commands.usage_of('say')}")
            return

        target_stream, actual_kind, error = await self._resolve_command_target(kind, ident, platform)
        if error:
            await self._command_reply(stream_id, error)
            return
        audio, error = await self._command_synthesize(prepared)
        if error:
            await self._command_reply(stream_id, error)
            return
        if not await self._deliver_voice(target_stream, raw_text, audio):
            await self._command_reply(stream_id, "语音发送失败，详细原因见日志。")
            return
        label = "群" if actual_kind == "group" else "私聊"
        seconds = _payload_seconds(audio)
        suffix = f"（语音 {seconds:.1f}s）" if seconds is not None else ""
        await self._command_reply(stream_id, f"已发送到{label} {ident}{suffix}")

    async def _resolve_command_target(
        self, kind: Optional[str], ident: str, platform: str
    ) -> Tuple[str, str, str]:
        """按「活跃会话 → 新建会话」解析目标，群与私聊都试；返回 ``(stream_id, kind, 错误)``。"""
        candidates = [kind] if kind else ["group", "private"]
        for candidate in candidates:
            target = await self._lookup_stream(str(candidate), ident, platform)
            if target:
                return target, str(candidate), ""
        for candidate in candidates:
            target = await self._open_stream(str(candidate), ident, platform)
            if target:
                return target, str(candidate), ""
        return (
            "",
            "",
            f"找不到目标 {ident}：既不是活跃群/私聊，也无法新建会话。可用 g{ident} / u{ident} 指定类型。",
        )

    async def _lookup_stream(self, kind: str, ident: str, platform: str) -> str:
        try:
            if kind == "group":
                result = await self.ctx.chat.get_stream_by_group_id(ident, platform)
            else:
                result = await self.ctx.chat.get_stream_by_user_id(ident, platform)
        except Exception as exc:
            self.ctx.logger.warning("命令查找会话失败（%s）", exc)
            return ""
        return _stream_session_id(result)

    async def _open_stream(self, kind: str, ident: str, platform: str) -> str:
        try:
            if kind == "group":
                result = await self.ctx.chat.open_session(
                    platform=platform, chat_type="group", group_id=ident
                )
            else:
                result = await self.ctx.chat.open_session(
                    platform=platform, chat_type="private", user_id=ident
                )
        except Exception as exc:
            self.ctx.logger.warning("命令新建会话失败（%s）", exc)
            return ""
        return _stream_session_id(result)

    async def _send_voice(self, stream_id: str, audio: bytes) -> bool:
        segment = {
            "type": VOICE_SEGMENT_TYPE,
            "data": "",
            "hash": hashlib.sha256(audio).hexdigest(),
            "binary_data_base64": base64.b64encode(audio).decode("ascii"),
        }
        try:
            await self.ctx.send.hybrid(segments=[segment], stream_id=stream_id)
        except Exception as exc:
            self.ctx.logger.warning("命令语音发送失败（%s）", exc)
            return False
        return True

    # ------------------------------------------------------------ 定时语音

    def _scheduled_entries(self) -> List[Any]:
        return [entry for entry in self.config.scheduled.entries if entry.enabled]

    def _start_scheduler(self) -> None:
        if not self.config.scheduled.enabled or not self._scheduled_entries():
            return
        if self._schedule_task is not None and not self._schedule_task.done():
            return
        self._schedule_task = asyncio.create_task(self._scheduler_loop())

    async def _stop_scheduler(self) -> None:
        task = self._schedule_task
        self._schedule_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _scheduler_loop(self) -> None:
        """每分钟整点检查一次；条目本身可能耗几十秒，交给独立任务跑。"""
        while True:
            now = time.time()
            await asyncio.sleep(max(1.0, 60.0 - now % 60.0))
            try:
                await self._scheduler_tick(datetime.now())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.warning("定时语音检查失败（%s）", exc)

    async def _scheduler_tick(self, moment: datetime) -> None:
        for index, entry in self._due_entries(moment):
            task = asyncio.create_task(self._run_scheduled(index, entry))
            self._schedule_runs.add(task)
            task.add_done_callback(self._schedule_runs.discard)

    def _due_entries(self, moment: datetime) -> List[Tuple[int, Any]]:
        """返回这一分钟该跑的条目，并把它们记为「已跑」，避免同一分钟重复触发。"""
        if not self.config.scheduled.enabled:
            return []
        day = moment.strftime("%Y-%m-%d")
        slot = moment.hour * 60 + moment.minute
        due: List[Tuple[int, Any]] = []
        for index, entry in enumerate(self.config.scheduled.entries):
            if not entry.enabled:
                continue
            if not _schedule.entry_due(entry.time, entry.days, moment):
                continue
            key = (day, index, slot)
            if key in self._schedule_done:
                continue
            self._schedule_done[key] = time.monotonic()
            due.append((index, entry))
        if len(self._schedule_done) > 512:
            for key in sorted(self._schedule_done)[:-128]:
                self._schedule_done.pop(key, None)
        return due

    async def _run_scheduled(self, index: int, entry: Any) -> None:
        label = f"{entry.time} {entry.days}".strip()
        content = str(entry.content or "text").strip().lower()
        if content != "text":
            self._warn_once(
                f"scheduled:{index}:content",
                "定时条目 %s 的 content=%r 暂不支持，已跳过（目前只支持固定文本）",
                label,
                entry.content,
            )
            return
        raw = str(entry.text or "").strip()
        target = str(entry.target or "").strip()
        if not raw or not target:
            self._warn_once(
                f"scheduled:{index}:empty",
                "定时条目 %s 缺少 text 或 target，已跳过",
                label,
            )
            return
        stream_id, _kind, error = await self._resolve_stream(target)
        if error:
            self._warn_once(f"scheduled:{index}:target", "定时条目 %s %s", label, error)
            return
        if self._session_switch.get(stream_id) is False:
            self.ctx.logger.info("定时语音跳过：会话 %s 已关闭语音", stream_id)
            return
        if self._synthesis_slots.locked():
            self.ctx.logger.info("定时语音跳过：正在合成上一条语音")
            return
        prepared = self._prepare_text(raw)
        if prepared is None:
            self._warn_once(f"scheduled:{index}:text", "定时条目 %s 没有可朗读的内容，已跳过", label)
            return
        started = time.monotonic()
        try:
            audio = await self._synthesize_guarded(prepared)
            audio = await asyncio.to_thread(self._postprocess, audio)
        except _SynthesisBusy:
            self.ctx.logger.info("定时语音跳过：正在合成上一条语音")
            return
        except Exception as exc:
            self.ctx.logger.warning("定时语音失败（%s）：%s", label, exc)
            return
        if not await self._deliver_voice(stream_id, raw, audio):
            self.ctx.logger.warning("定时语音发送失败：%s → %s", label, stream_id)
            return
        self.ctx.logger.info(
            "定时语音已发送：%s → %s（%d 字，%.1fs）",
            label,
            stream_id,
            len(raw),
            time.monotonic() - started,
        )

    async def _resolve_stream(self, target: str, platform: str = "qq") -> Tuple[str, str, str]:
        """把 ``g123`` / ``u456`` / ``123`` 解析成 stream_id，供命令与定时共用。"""
        kind, ident = _commands.parse_target(target)
        if not ident:
            return "", "", f"目标 {target!r} 无法解析（可用 g群号 / uQQ号）"
        return await self._resolve_command_target(kind, ident, platform)

    async def _send_text(self, stream_id: str, text: str) -> None:
        """发一条纯文字。先记一笔，避免它再经过出站钩子被转成语音。"""

        self._remember_self_sent(stream_id, text)
        try:
            await self.ctx.send.text(text, stream_id=stream_id)
        except Exception as exc:
            self.ctx.logger.warning("文字发送失败（%s）", exc)

    def _remember_self_sent(self, stream_id: str, text: str) -> None:
        now = time.monotonic()
        for key, expiry in list(self._self_sent_texts.items()):
            if expiry <= now:
                self._self_sent_texts.pop(key, None)
        self._self_sent_texts[(stream_id, text.strip())] = now + _SELF_SENT_TEXT_TTL_SECONDS
        while len(self._self_sent_texts) > _SELF_SENT_MAX_ENTRIES:
            self._self_sent_texts.pop(next(iter(self._self_sent_texts)))

    def _consume_self_sent(self, stream_id: str, text: str) -> bool:
        expiry = self._self_sent_texts.pop((stream_id, text.strip()), None)
        return expiry is not None and expiry > time.monotonic()

    async def _deliver_voice(self, stream_id: str, text: str, audio: bytes) -> bool:
        """发语音；``delivery.keep_text`` 打开时文字另发一条（顺序看 ``text_first``）。"""

        keep = self.config.delivery.keep_text
        if keep and self.config.delivery.text_first:
            await self._send_text(stream_id, text)
        ok = await self._send_voice(stream_id, audio)
        if ok and keep and not self.config.delivery.text_first:
            self._spawn_text(stream_id, text)
        return ok

    # ------------------------------------------------------------ 对外 API

    @API(
        "voice_say",
        description="合成语音：给 stream_id 就发送到该会话，不给只返回音频",
        version="1",
        public=True,
    )
    async def handle_voice_say(
        self,
        text: str = "",
        stream_id: str = "",
        return_audio: bool = False,
        token: str = "",
    ) -> Dict[str, Any]:
        """0.6.0 对外 API：其他插件或脚本可以直接要一段语音。

        宿主不会把调用方身份传给 handler（``_cap_api_call`` 只透传 ``args``），
        所以这里用 ``api.token`` 做可选的共享口令，而不是按插件 id 白名单。
        不给 ``stream_id`` 时只合成并返回 base64；给了就发送到该会话。
        """
        api = self.config.api
        if not api.enabled:
            return {"success": False, "error": "语音 API 已关闭"}
        expected = (api.token or "").strip()
        if expected and str(token or "") != expected:
            return {"success": False, "error": "token 不正确"}
        raw = str(text or "").strip()
        if not raw:
            return {"success": False, "error": "text 不能为空"}
        limit = int(self.config.general.max_text_length)
        if len(raw) > limit:
            return {"success": False, "error": f"text 太长（{len(raw)} 字，上限 {limit} 字）"}
        interval = float(api.rate_limit_seconds)
        now = time.monotonic()
        if interval > 0 and now - self._api_last_call < interval:
            return {"success": False, "error": f"调用太频繁，请 {interval:g} 秒后再试"}
        self._api_last_call = now
        prepared = self._prepare_text(raw)
        if prepared is None:
            return {"success": False, "error": "没有可朗读的内容"}
        try:
            audio = await self._synthesize_guarded(prepared)
        except _SynthesisBusy:
            return {"success": False, "error": "正在合成上一条语音，请稍后再试"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            audio = await asyncio.to_thread(self._postprocess, audio)
        except Exception as exc:
            self._warn_once("api:post", "语音后处理失败（%s），本条按原样返回", exc)
        result: Dict[str, Any] = {"success": True, "seconds": _payload_seconds(audio) or 0.0}
        target = str(stream_id or "").strip()
        if target:
            if not await self._send_voice(target, audio):
                return {"success": False, "error": "语音发送失败"}
            if return_audio:
                result["audio_base64"] = base64.b64encode(audio).decode("ascii")
            return result
        result["audio_base64"] = base64.b64encode(audio).decode("ascii")
        return result

    def _check_admin_entries(self) -> None:
        """启动时提醒 ``commands.admins`` 里写错、永远匹配不到的条目。"""
        entries = [str(item).strip() for item in self.config.commands.admins if str(item).strip()]
        usable = 0
        for entry in entries:
            problem = _commands.admin_entry_problem(entry)
            if problem is None:
                usable += 1
                continue
            self._warn_once(
                f"admins:{entry}",
                "commands.admins 里的 %r 不可用（%s），这一项会被忽略",
                entry,
                problem,
            )
        if entries and usable == 0:
            self.ctx.logger.warning(
                "commands.admins 里没有可用的账号，所有 /voice 命令都会回「还没有配置管理员」"
            )

    def _status_snapshot(self, stream_id: str, group_id: str, user_id: str) -> _commands.StatusSnapshot:
        trigger = self.config.trigger
        reference = "未找到参考音频"
        try:
            path = self._reference_path()
        except Exception:  # pragma: no cover - 兜底
            path = None
        if path is not None:
            reference = path.name
        return _commands.StatusSnapshot(
            version=str(self.config.plugin.config_version),
            preset=str(self.config.voice.preset),
            reference=reference,
            mode=str(trigger.mode),
            probability=float(trigger.probability),
            short_reply_max_chars=int(trigger.short_reply_max_chars),
            english_policy=str(trigger.english_policy),
            timeout=float(self.config.general.timeout),
            max_concurrent=int(self.config.mimo.max_concurrent),
            mention_enabled=bool(trigger.mention_priority),
            mention_probability=float(trigger.mention_probability),
            trigger_enabled=bool(self.config.triggers.enabled),
            trigger_ttl=self._trigger_ttl(),
            trigger_rules=tuple(
                f"{word}({_triggers.mode_label(mode)})" for word, mode in self._trigger_rules()
            ),
            trigger_active=self._active_trigger(stream_id) is not None,
            emotion_enabled=bool(trigger.emotion_weighting),
            emotion_step=float(trigger.emotion_step),
            emotion_cap=float(trigger.emotion_cap),
            session_on=self._session_switch.get(stream_id, True),
            session_label=self._session_label(stream_id, group_id=group_id, user_id=user_id),
            synth_ok=self._synth_stats["ok"],
            synth_timeout=self._synth_stats["timeout"],
            synth_failed=self._synth_stats["failed"],
            synth_failures=tuple(
                sorted(self._synth_failures.items(), key=lambda item: (-item[1], item[0]))
            ),
            durations=tuple(self._recent_durations),
        )


def _stream_session_id(result: Any) -> str:
    """从 ``get_stream_*`` / ``open_session`` 的返回里挖出 session_id。"""
    if not isinstance(result, dict):
        return ""
    stream = result.get("stream")
    if isinstance(stream, dict):
        value = stream.get("session_id") or stream.get("stream_id")
        return str(value or "").strip()
    value = result.get("session_id") or result.get("stream_id")
    return str(value or "").strip()


def create_plugin() -> MiMoVoicePlugin:
    return MiMoVoicePlugin()
