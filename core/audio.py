"""音频工具：WAV 读写、频谱曲线与音色对齐矩阵。

只做无状态计算，不碰配置、不碰文件系统，便于单元测试。
"""

from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Any, Dict, Tuple

_NUMPY_MODULE: Any = None
_NUMPY_TRIED = False


def _numpy() -> Any:
    """按需导入 numpy（可选依赖），失败返回 None 并记住结果。"""
    global _NUMPY_MODULE, _NUMPY_TRIED
    if not _NUMPY_TRIED:
        _NUMPY_TRIED = True
        try:
            import numpy

            _NUMPY_MODULE = numpy
        except ImportError:  # pragma: no cover - 正常安装都有 numpy
            _NUMPY_MODULE = None
    return _NUMPY_MODULE


def _stderr_excerpt(exc: BaseException, limit: int = 160) -> str:
    """把 ffmpeg 的报错压成一行：完整命令行里有用户路径，不该整个进日志。"""
    stderr = getattr(exc, "stderr", None) or b""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr)
    text = " ".join(text.split())
    return (text[:limit] or type(exc).__name__)


def _concat_path(path: Path) -> str:
    """ffmpeg concat demuxer 的 file 行：单引号内的单引号需要转义，否则带引号的文件名会解析错。"""
    return path.as_posix().replace("'", r"'\''")


def _timbre_band_edges() -> Any:
    np = _numpy()
    if np is None:  # pragma: no cover
        raise RuntimeError("音色对齐需要 numpy")
    return np.logspace(np.log10(60.0), np.log10(12000.0), 31)


_BAND_MATRIX_CACHE: Dict[Tuple[int, int], Tuple[Any, Any]] = {}


def _band_matrix(frame: int, rate: int) -> Tuple[Any, Any]:
    """(weights, counts)：把 FFT 频点归并到 30 个频带的矩阵，按帧长/采样率缓存。

    以前每个音频要重建两次（算曲线一次、应用增益一次），矩阵本身与音频无关，
    缓存后单条语音能省掉两次 30×1025 的构造。
    """
    np = _numpy()
    if np is None:  # pragma: no cover
        raise RuntimeError("音色对齐需要 numpy")
    key = (int(frame), int(rate))
    cached = _BAND_MATRIX_CACHE.get(key)
    if cached is not None:
        return cached
    freqs = np.fft.rfftfreq(frame, d=1.0 / rate)
    edges = _timbre_band_edges()
    band_of_bin = np.clip(np.searchsorted(edges, freqs, side="right") - 1, 0, len(edges) - 2)
    weights = np.zeros((len(edges) - 1, len(freqs)), dtype=np.float32)
    weights[band_of_bin, np.arange(len(freqs))] = 1.0
    counts = weights.sum(axis=1)
    counts[counts == 0] = 1.0
    if len(_BAND_MATRIX_CACHE) > 8:
        _BAND_MATRIX_CACHE.clear()
    _BAND_MATRIX_CACHE[key] = (weights, counts)
    return weights, counts


def _spectral_curve(pcm: Any, rate: int) -> Any:
    """返回长度 30 的相对频谱曲线（dB，已按 600–2500Hz 中位数归零）。"""
    np = _numpy()
    if np is None:  # pragma: no cover
        raise RuntimeError("音色对齐需要 numpy")

    frame = 2048
    if len(pcm) < frame * 2:
        frame = max(256, (len(pcm) // 4) // 2 * 2)
    hop = max(1, frame // 2)
    window = np.hanning(frame).astype(np.float32)
    edges = _timbre_band_edges()
    weights, counts = _band_matrix(frame, rate)
    acc = np.zeros(len(edges) - 1, dtype=np.float64)
    frames = 0
    for start in range(0, len(pcm) - frame + 1, hop):
        spec = np.abs(np.fft.rfft(pcm[start : start + frame] * window))
        acc += weights @ spec
        frames += 1
    if frames == 0:
        return np.zeros(len(edges) - 1, dtype=np.float32)
    curve = 20.0 * np.log10(np.maximum(acc / frames / counts, 1e-9))
    mask = (edges[:-1] >= 600.0) & (edges[1:] <= 2500.0)
    if not mask.any():
        mask = np.ones_like(curve, dtype=bool)
    return (curve - float(np.median(curve[mask]))).astype(np.float32)


def _apply_gain_curve(pcm: Any, rate: int, gains_db: Any) -> Any:
    """用 STFT 把频带增益应用到时域，返回与输入等长、音量不增的信号。"""
    np = _numpy()
    if np is None:  # pragma: no cover
        raise RuntimeError("音色对齐需要 numpy")

    frame = 2048
    if len(pcm) < frame * 2:
        frame = max(256, (len(pcm) // 4) // 2 * 2)
    hop = max(1, frame // 2)
    window = np.hanning(frame).astype(np.float32)
    freqs = np.maximum(np.fft.rfftfreq(frame, d=1.0 / rate), 1.0)
    edges = _timbre_band_edges()
    centers = np.sqrt(edges[:-1] * edges[1:])
    gain = np.interp(np.log10(freqs), np.log10(centers), np.asarray(gains_db, dtype=np.float64))
    linear = (10.0 ** (gain / 20.0)).astype(np.float32)
    out = np.zeros(len(pcm), dtype=np.float32)
    norm = np.zeros(len(pcm), dtype=np.float32)
    for start in range(0, len(pcm) - frame + 1, hop):
        spec = np.fft.rfft(pcm[start : start + frame] * window) * linear
        out[start : start + frame] += np.fft.irfft(spec).astype(np.float32) * window
        norm[start : start + frame] += window * window
    mask = norm > 1e-6
    out[mask] /= norm[mask]
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 0.99:
        out *= 0.99 / peak
    return out


def _frame_rms(pcm: Any, rate: int, frame_ms: float = 20.0) -> Any:
    """按 20ms 分帧算 RMS，用于参考音频的静音分析（够用且不受单点毛刺影响）。"""
    np = _numpy()
    if np is None:  # pragma: no cover
        raise RuntimeError("音频处理需要 numpy")
    frame = max(1, int(rate * frame_ms / 1000.0))
    count = len(pcm) // frame
    if count == 0:
        return np.asarray([float(np.sqrt(np.mean(np.square(pcm))))] if len(pcm) else [])
    blocks = np.asarray(pcm[: count * frame], dtype=np.float32).reshape(count, frame)
    return np.sqrt(np.mean(np.square(blocks), axis=1))


def analyze_reference(pcm: Any, rate: int, silence_dbfs: float = -45.0) -> Dict[str, float]:
    """参考音频体检：时长、静音占比、最长静音、首尾静音。

    只看"像不像一段干净的单句素材"：时长 2–5s、静音少、句间没有长停顿。
    """
    np = _numpy()
    empty = {"duration": 0.0, "silence_ratio": 0.0, "longest_silence": 0.0,
             "head_silence": 0.0, "tail_silence": 0.0}
    if np is None or rate <= 0 or len(pcm) == 0:
        return empty
    rms = _frame_rms(pcm, rate)
    if rms.size == 0:
        return empty
    silent = rms < float(10 ** (silence_dbfs / 20.0))
    duration = len(pcm) / float(rate)
    ratio = float(silent.mean())
    longest = 0
    run = 0
    for flag in silent.tolist():
        run = run + 1 if flag else 0
        longest = max(longest, run)
    head = 0
    for flag in silent.tolist():
        if not flag:
            break
        head += 1
    tail = 0
    for flag in reversed(silent.tolist()):
        if not flag:
            break
        tail += 1
    frame_seconds = 0.02
    return {
        "duration": duration,
        "silence_ratio": ratio,
        "longest_silence": longest * frame_seconds,
        "head_silence": head * frame_seconds,
        "tail_silence": tail * frame_seconds,
    }


def _voiced_segments(
    pcm: Any,
    rate: int,
    silence_dbfs: float = -45.0,
    min_seconds: float = 0.25,
    gap_seconds: float = 0.15,
) -> list:
    """把参考音频切成"有声音的句子"：短静音并进句子，过短的碎片丢掉。"""
    np = _numpy()
    if np is None:  # pragma: no cover
        raise RuntimeError("音频处理需要 numpy")
    frame = max(1, int(rate * 0.02))
    rms = _frame_rms(pcm, rate)
    if rms.size == 0:
        return []
    silent = rms < float(10 ** (silence_dbfs / 20.0))
    # 先合并太短的静音（气口），再按静音切开
    max_gap_frames = max(1, int(gap_seconds / 0.02))
    flags = silent.tolist()
    index = 0
    while index < len(flags):
        if flags[index]:
            end = index
            while end < len(flags) and flags[end]:
                end += 1
            if (end - index) < max_gap_frames:
                for pos in range(index, end):
                    flags[pos] = False
            index = end
        else:
            index += 1
    segments = []
    start = None
    for pos, flag in enumerate([*flags, True]):
        if not flag and start is None:
            start = pos
        elif flag and start is not None:
            begin = start * frame
            finish = min(len(pcm), pos * frame)
            if (finish - begin) >= int(min_seconds * rate):
                segments.append((begin, finish))
            start = None
    return segments


def prepare_reference(
    pcm: Any,
    rate: int,
    target_seconds: float = 3.7,
    max_seconds: float = 5.0,
    silence_dbfs: float = -45.0,
    target_peak_dbfs: float = -3.0,
    gap_seconds: float = 0.15,
    min_segment_seconds: float = 0.25,
) -> Tuple[Any, Dict[str, Any]]:
    """把长参考音频瘦身成"单句拼接"版本：按原顺序取句，去句间长静音，峰值归一。

    返回 (pcm, meta)。已经是短且干净的素材时同样会走一遍（幂等），调用方可以先用
    ``analyze_reference`` 判断值不值得处理。
    """
    np = _numpy()
    if np is None:  # pragma: no cover
        raise RuntimeError("音频处理需要 numpy")
    segments = _voiced_segments(pcm, rate, silence_dbfs=silence_dbfs, min_seconds=min_segment_seconds)
    source_seconds = len(pcm) / float(rate)
    if not segments:
        return pcm, {"duration": source_seconds, "segments": 0, "source_seconds": source_seconds}
    gap = np.zeros(max(0, int(rate * gap_seconds)), dtype=np.float32)
    budget = int(max_seconds * rate)
    pieces = []
    used = 0
    picked = 0
    for begin, finish in segments:
        piece = pcm[begin:finish]
        room = budget - used
        if room <= 0:
            break
        take = min(piece.size, room)
        pieces.append(piece[:take])
        picked += 1
        used += take
        if used >= int(target_seconds * rate):
            break
        if gap.size and used + gap.size <= budget:
            pieces.append(gap)
            used += gap.size
    out = np.concatenate(pieces)[:budget] if pieces else pcm
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 1e-6:
        out = (out * (10 ** (target_peak_dbfs / 20.0) / peak)).astype(np.float32)
    meta = {
        "duration": len(out) / float(rate),
        "segments": picked,
        "source_seconds": source_seconds,
    }
    return out, meta


def smooth_gain_bands(gains_db: Any, radius: int = 1) -> Any:
    """把相邻频带的增益做一次移动平均，避免某个频带单独顶到上限造成"闷/金属感"。"""
    np = _numpy()
    if np is None:  # pragma: no cover
        raise RuntimeError("音频处理需要 numpy")
    arr = np.asarray(gains_db, dtype=np.float64)
    if arr.size < 3 or radius <= 0:
        return arr.astype(np.float32)
    padded = np.pad(arr, (radius, radius), mode="edge")
    kernel = np.ones(2 * radius + 1, dtype=np.float64) / float(2 * radius + 1)
    smoothed = np.convolve(padded, kernel, mode="same")[radius:-radius]
    return smoothed.astype(np.float32)


def soft_limit(pcm: Any, ceiling: float = 0.97) -> Any:
    """软限幅：超过拐点（0.85×天花板）的峰值用 tanh 压回，不引入硬削顶。

    实测后处理会把原始削顶放大成明显破音，这一步在峰值归一之前兜底。
    """
    np = _numpy()
    if np is None:  # pragma: no cover
        raise RuntimeError("音频处理需要 numpy")
    data = np.asarray(pcm, dtype=np.float32)
    if data.size == 0:
        return data
    limit = float(ceiling)
    knee = limit * 0.85
    magnitude = np.abs(data)
    if magnitude.size == 0 or float(np.max(magnitude)) <= knee:
        return data
    scaled = knee + (limit - knee) * np.tanh(np.maximum(magnitude - knee, 0.0) / max(limit - knee, 1e-6))
    result = np.where(magnitude <= knee, data, np.sign(data) * scaled)
    return result.astype(np.float32)


def _read_wav_pcm(audio: bytes) -> Tuple[Any, int]:
    np = _numpy()
    if np is None:  # pragma: no cover
        raise RuntimeError("音频处理需要 numpy")

    with wave.open(io.BytesIO(audio), "rb") as fh:
        rate = fh.getframerate()
        width = fh.getsampwidth()
        channels = fh.getnchannels()
        raw = fh.readframes(fh.getnframes())
    if width != 2:
        raise ValueError(f"只处理 16-bit PCM（实际 {width * 8} bit）")
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return pcm, rate


def _write_wav_pcm(pcm: Any, rate: int) -> bytes:
    np = _numpy()
    if np is None:  # pragma: no cover
        raise RuntimeError("音频处理需要 numpy")

    data = np.clip(np.asarray(pcm, dtype=np.float32), -1.0, 1.0)
    raw = (data * 32767.0).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(raw)
    return buffer.getvalue()
