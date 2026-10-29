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
