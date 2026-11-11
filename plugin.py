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
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast
from urllib.parse import urlsplit

import aiohttp
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .core import commands as _commands
from .core import config as _config
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
    _SILENCE_DBFS,
    _SOFT_LIMIT_CEILING,
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
        # 命令层（0.5.5）：会话级开关（进程内，重启复位）与近期合成样本
        self._session_switch: Dict[str, bool] = {}
        self._recent_durations: List[float] = []
        self._synth_stats = {"ok": 0, "timeout": 0, "failed": 0}
        self._synth_failures: Dict[str, int] = {}
        # 触发词（0.5.6）：session_id -> (词, 位置, 过期时间)；命中只在出站消费一次
        self._trigger_requests: Dict[str, Tuple[str, str, float]] = {}
        self._trigger_last_hit: Dict[str, float] = {}

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
            "点名优先=%s(%.2f), 情绪加权=%s(+%.2f/上限%.2f), 触发词=%s",
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

        outbound["raw_message"] = self._outbound_segments(text, audio)
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

    def _outbound_segments(self, text: str, audio: bytes) -> List[Dict[str, Any]]:
        """拼出站 ``raw_message``：只发语音，或按 ``delivery.keep_text`` 带上原文。"""
        voice = {
            "type": VOICE_SEGMENT_TYPE,
            "data": "",
            "hash": hashlib.sha256(audio).hexdigest(),
            "binary_data_base64": base64.b64encode(audio).decode("ascii"),
        }
        if not self.config.delivery.keep_text:
            return [voice]
        plain = {"type": "text", "data": text}
        return [plain, voice] if self.config.delivery.text_first else [voice, plain]

    def _keep_text_label(self) -> str:
        if not self.config.delivery.keep_text:
            return "关"
        return "开(文字先)" if self.config.delivery.text_first else "开(语音先)"

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

    # ---------------------------------------------------------- 手动命令（0.5.5）

    @Command(
        "voice",
        description="MiMo 语音手动命令：/voice on|off|test|status|help|say（仅私聊、仅管理员）",
        pattern=r"^/(?:voice|vc)(?:\s+.*)?$",
    )
    async def handle_voice_command(self, **kwargs: Any) -> Tuple[bool, Optional[str], int]:
        """处理 ``/voice`` 与 ``/vc``。

        权限不交给宿主的 ``operator``：那套白名单在宿主配置里，插件拿不到多管理员列表。
        这里自己判 ``commands.admins``（可写 ``10000002`` 或 ``qq:10000002``），失败也给回执。
        命令只在私聊生效——群里的 ``/voice`` 按普通消息处理，避免在群里刷屏。
        """
        parsed = _commands.parse_command(str(kwargs.get("text") or ""))
        if parsed is None or not self.config.commands.enabled:
            return True, None, 0

        stream_id = str(kwargs.get("stream_id") or "").strip()
        group_id = str(kwargs.get("group_id") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip()
        platform = str(kwargs.get("platform") or "qq").strip() or "qq"

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

        sub, rest = parsed
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
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            self.ctx.logger.warning("命令回执发送失败（%s）", exc)

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
        if not await self._send_voice(stream_id, audio):
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
        if not await self._send_voice(target_stream, audio):
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
