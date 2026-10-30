"""音频侧测试：合成结果校验、后处理（尾静音/补尾/峰值）、缓存清理。"""
from __future__ import annotations

import math
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __bootstrap import Checker, load_plugin, make_plugin

module = load_plugin()
checker = Checker("音频侧")
np = module._numpy()
checker.check("容器 / 本机有 numpy", np is not None)


def _catch(fn) -> str:
    """执行并把异常压成稳定标签，便于断言。"""
    try:
        fn()
    except module._RetryableSynthesisError as exc:
        text = str(exc)
        return "过短" if "过短" in text else ("几乎无声" if "无声" in text else text)
    except RuntimeError as exc:
        return "过长:RuntimeError" if "过长" in str(exc) else "RuntimeError"
    return "ok"


def make_wav(seconds: float, rate: int = 24000, amplitude: float = 0.25, freq: float = 220.0) -> bytes:
    """生成一段正弦波 WAV（16-bit 单声道）。"""
    frames = int(seconds * rate)
    samples = np.sin(2 * math.pi * freq * np.arange(frames) / rate).astype(np.float32) * amplitude
    return module._write_wav_pcm(samples, rate)


def parse(wav_bytes: bytes):
    pcm, rate = module._read_wav_pcm(wav_bytes)
    return pcm, rate


def with_silence_tail(seconds: float = 0.6, body: float = 1.0) -> bytes:
    body_pcm, rate = parse(make_wav(body))
    silence = np.zeros(int(rate * seconds), dtype=np.float32)
    return module._write_wav_pcm(np.concatenate([body_pcm, silence]), rate)


# ---------------------------------------------------------------- 结果校验
plugin, _, _ = make_plugin(module)

checker.equal("短字节直接判失败", _catch(lambda: plugin._validate_audio(b"RIFF", "wav")), "过短")
checker.equal("0.1 秒判失败", _catch(lambda: plugin._validate_audio(make_wav(0.1), "wav", "早")), "过短")
checker.equal("静音判失败", _catch(lambda: plugin._validate_audio(make_wav(1.0, amplitude=0.00001), "wav", "早")), "几乎无声")
checker.equal("正常音频通过", _catch(lambda: plugin._validate_audio(make_wav(1.0), "wav", "早")), "ok")
checker.equal("mp3 直通不解析", _catch(lambda: plugin._validate_audio(b"ID3" + b"x" * 900, "mp3", "早")), "ok")
checker.equal(
    "超长音频按不可重试处理",
    _catch(lambda: plugin._validate_audio(make_wav(30.0), "wav", "早")),
    "过长:RuntimeError",
)

checker.equal("10 字下限 20s", plugin._max_audio_seconds("早"), 20.0)
checker.equal("100 字约 70s", plugin._max_audio_seconds("啊" * 100), 70.0)
checker.equal("300 字封顶 90s", plugin._max_audio_seconds("啊" * 300), 90.0)

# ---------------------------------------------------------------- 后处理
inst, _, logger = make_plugin(module)
inst.config.delivery.trim_tail_silence_ms = 250
inst.config.delivery.tail_pad_ms = 120
inst.config.delivery.peak_normalize = True
inst.config.delivery.timbre_match = False

raw = with_silence_tail()
processed = inst._postprocess(raw)
before, rate = parse(raw)
after, rate_after = parse(processed)
checker.equal("采样率不变", rate_after, rate)
checker.check("尾静音被裁掉", after.size < before.size, f"{before.size} -> {after.size}")
tail = after[-int(rate * 0.10) :]
tail_db = 20 * math.log10(max(float(np.sqrt(np.mean(np.square(tail)))), 1e-9))
checker.check("尾巴补的是静音而不是长拖尾", tail_db < -60, f"尾 0.1s {tail_db:.1f} dBFS")
peak_db = 20 * math.log10(max(float(np.max(np.abs(after))), 1e-9))
# 输入只有 -12 dBFS，旧上限 +12 dB 会直接拉到 -1.5 dBFS；现在最多 +6 dB（防破音），落在 -6 dBFS。
checker.check("安静条最多提 +6 dB", abs(peak_db - (-6.0)) < 0.4, f"{peak_db:.2f} dBFS")
info_lines = [text for text in logger.messages("info") if text.startswith("语音后处理")]
checker.equal("只打一行 info 日志", len(info_lines), 1)

# 输入已经接近目标响度时仍归一到 -1.5 dBFS（增益在上限内）
near = make_wav(1.0, amplitude=0.63)
inst_near, _, _ = make_plugin(module)
inst_near.config.delivery.trim_tail_silence_ms = 0
inst_near.config.delivery.tail_pad_ms = 0
inst_near.config.delivery.timbre_match = False
near_after = parse(inst_near._postprocess(near))[0]
near_db = 20 * math.log10(max(float(np.max(np.abs(near_after))), 1e-9))
checker.check("上限内归一到 -1.5 dBFS", abs(near_db - (-1.5)) < 0.4, f"{near_db:.2f} dBFS")

# 无需调整时只打 debug
clean = make_wav(1.0, amplitude=0.85)
inst2, _, logger2 = make_plugin(module)
inst2.config.delivery.trim_tail_silence_ms = 0
inst2.config.delivery.tail_pad_ms = 0
inst2.config.delivery.peak_normalize = False
inst2.config.delivery.timbre_match = False
inst2._postprocess(clean)
checker.equal("无改动时不打 info", len([m for m in logger2.messages("info") if m.startswith("语音后处理")]), 0)

# ---------------------------------------------------------------- 缓存清理
with tempfile.TemporaryDirectory() as tmp:
    cache_root = Path(tmp)
    cache = cache_root / "reference-cache"
    cache.mkdir()
    for index in range(9):
        target = cache / f"reference-{index}.wav"
        target.write_bytes(make_wav(0.2))
        os.utime(target, (1_700_000_000 + index, 1_700_000_000 + index))
    inst3, _, _ = make_plugin(module)
    inst3.ctx.paths.runtime_dir = cache_root
    inst3._prune_reference_cache(keep=6)
    kept = sorted(item.name for item in cache.iterdir())
    checker.equal("缓存只留最近 6 个", kept, [f"reference-{i}.wav" for i in range(3, 9)])


# ---------------------------------------------------------------- 音色对齐失败要降级
# P0 回归：曾经在这里调用已删除的 _warn_timbre_once，异常分支直接 AttributeError
inst4, _, logger4 = make_plugin(module)
inst4.config.delivery.timbre_match = True
inst4.config.delivery.trim_tail_silence_ms = 0
inst4.config.delivery.tail_pad_ms = 0
inst4.config.delivery.peak_normalize = False
inst4._timbre_target = lambda: [0.0] * 30  # type: ignore[method-assign]
_original_gain = module._apply_gain_curve


def _boom(*_args, **_kwargs):
    raise RuntimeError("模拟频谱处理失败")


module._apply_gain_curve = _boom
try:
    survived = inst4._postprocess(make_wav(1.0))
finally:
    module._apply_gain_curve = _original_gain
checker.check("对齐失败时不抛异常", isinstance(survived, bytes) and len(survived) > 0)
warnings4 = [text for text in logger4.messages("warning") if "音色对齐失败" in text]
checker.equal("对齐失败留一条告警", len(warnings4), 1)

# ---------------------------------------------------------------- 参考音频换了要重算目标曲线
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    ref = root / "probe.wav"
    ref.write_bytes(make_wav(0.8, freq=220.0))
    inst5, _, _ = make_plugin(module)
    inst5.ctx.paths.data_dir = root
    inst5.config.voice.preset = "custom"
    inst5.config.voice.reference_path = "probe.wav"
    first = inst5._timbre_target()
    ref.write_bytes(make_wav(0.8, freq=880.0))
    os.utime(ref, (1_700_000_500, 1_700_000_500))
    second = inst5._timbre_target()
    checker.check("参考音频变化后目标曲线重算", first is not None and second is not None and first != second)

# ---------------------------------------------------------------- 缓存清理带上分片目录
with tempfile.TemporaryDirectory() as tmp2:
    root2 = Path(tmp2)
    cache2 = root2 / "reference-cache"
    cache2.mkdir()
    for index in range(8):
        item = cache2 / f"reference-{index}.wav"
        item.write_bytes(make_wav(0.2))
        os.utime(item, (1_700_000_000 + index, 1_700_000_000 + index))
    (cache2 / "parts-abc").mkdir()
    (cache2 / "parts-abc" / "part-0.wav").write_bytes(b"x")
    (cache2 / "merge-abc.txt").write_text("file 'x'\n", encoding="utf-8")
    inst6, _, _ = make_plugin(module)
    inst6.ctx.paths.runtime_dir = root2
    inst6._prune_reference_cache(keep=6)
    names = sorted(item.name for item in cache2.iterdir())
    checker.check("分片目录被清理", "parts-abc" not in names, str(names))
    checker.check("合并列表被清理", "merge-abc.txt" not in names, str(names))

# ---------------------------------------------------------------- 参考音频体检与瘦身
utils_rate = 24000


def speech_like(seconds: float, amplitude: float = 0.4, freq: float = 220.0) -> bytes:
    return make_wav(seconds, rate=utils_rate, amplitude=amplitude, freq=freq)


def silence(seconds: float) -> bytes:
    return module._write_wav_pcm(np.zeros(int(utils_rate * seconds), dtype=np.float32), utils_rate)


def join(*chunks: bytes) -> bytes:
    pcm = np.concatenate([parse(chunk)[0] for chunk in chunks])
    return module._write_wav_pcm(pcm, utils_rate)


long_reference = join(speech_like(1.4), silence(0.5), speech_like(1.4), silence(0.5), speech_like(1.4))
health = module.analyze_reference(*parse(long_reference))
checker.check("体检时长正确", abs(health["duration"] - 5.2) < 0.05, f"{health['duration']:.2f}s")
checker.check("静音占比正确", abs(health["silence_ratio"] - 1.0 / 5.2) < 0.03, f"{health['silence_ratio']:.2f}")
checker.check("最长静音正确", abs(health["longest_silence"] - 0.5) < 0.05, f"{health['longest_silence']:.2f}s")

prepared, meta = module.prepare_reference(*parse(long_reference), target_seconds=3.7, max_seconds=5.0)
prepared_pcm = prepared
prepared_peak = 20 * math.log10(max(float(np.max(np.abs(prepared_pcm))), 1e-9))
checker.check("瘦身后落在 3–5 秒", 3.0 <= meta["duration"] <= 5.0, f"{meta['duration']:.2f}s")
checker.check("瘦身后峰值 -3 dBFS", abs(prepared_peak - (-3.0)) < 0.2, f"{prepared_peak:.2f} dBFS")
head_pcm = prepared_pcm[: int(utils_rate * 0.02)]
head_rms = float(np.sqrt(np.mean(np.square(head_pcm))))
checker.check("瘦身后无开头静音", head_rms > 0.01, f"{head_rms:.3f}")
tail_pcm = prepared_pcm[-int(utils_rate * 0.02) :]
tail_rms = float(np.sqrt(np.mean(np.square(tail_pcm))))
checker.check("瘦身后无结尾静音", tail_rms > 0.01, f"{tail_rms:.3f}")

# 软限幅：超过天花板的峰值被 tanh 压回，正常音量不动
loud = (np.sin(2 * math.pi * 220.0 * np.arange(utils_rate) / utils_rate) * 2.0).astype(np.float32)
limited = module.soft_limit(loud, 0.97)
checker.check("软限幅封顶", float(np.max(np.abs(limited))) <= 0.9701, f"{float(np.max(np.abs(limited))):.3f}")
quiet = (loud * 0.15).astype(np.float32)
unchanged = module.soft_limit(quiet, 0.97)
checker.check("软限幅不碰正常音量", np.array_equal(unchanged, quiet))

smoothed = module.smooth_gain_bands([0.0, 0.0, 6.0, 0.0, 0.0])
checker.check("频带增益被平滑", float(smoothed[2]) < 6.0 and float(smoothed[2]) > 0.0, f"{float(smoothed[2]):.2f}")

# 参考瘦身走插件链路：长参考自动缩短，缓存命中后不再重算
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    ref = root / "long.wav"
    ref.write_bytes(long_reference)
    inst7, _, _ = make_plugin(module)
    inst7.ctx.paths.data_dir = root
    inst7.ctx.paths.runtime_dir = root / "runtime"
    inst7.config.voice.preset = "custom"
    inst7.config.voice.reference_path = "long.wav"
    inst7.config.voice.reference_strategy = "single_file"
    first_bytes = inst7._reference_bytes()
    second_bytes = inst7._reference_bytes()
    payload, payload_rate = parse(first_bytes)
    check = module.analyze_reference(payload, payload_rate)
    checker.check("插件把长参考缩到 3–5 秒", 3.0 <= check["duration"] <= 5.0, f"{check['duration']:.2f}s")
    checker.check("缓存后结果一致", first_bytes == second_bytes)
    cache_files = list((root / "runtime" / "reference-cache").glob("prepared-*.wav"))
    checker.check("瘦身结果落盘缓存", len(cache_files) == 1, str(cache_files))

sys.exit(checker.report())
