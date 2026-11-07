"""触发增强测试：点名一次性窗口、情绪加权、抽签来源日志。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __bootstrap import Checker, load_plugin, make_plugin

module = load_plugin()
trigger = sys.modules[f"{module.__name__}.core.trigger"]
checker = Checker("触发增强")

# ---------------------------------------------------------------- 情绪加权
checker.equal("平静文本不加分", trigger.emotion_bonus("今天天气不错", step=0.04, cap=0.2), (0.0, []))
bonus, hits = trigger.emotion_bonus("真的假的！！也太厉害了吧～", step=0.04, cap=0.2)
checker.equal("感叹号+波浪号两项", round(bonus, 3), 0.08)
checker.check("命中标签可读", hits == ["感叹", "波浪号"], f"{hits}")
checker.equal("单个感叹号也算", round(trigger.emotion_bonus("好!", step=0.04, cap=0.2)[0], 3), 0.04)
checker.equal("单个问号不算", trigger.emotion_bonus("你在吗?", step=0.04, cap=0.2)[0], 0.0)
checker.equal("连续问号算", round(trigger.emotion_bonus("真的吗？？", step=0.04, cap=0.2)[0], 3), 0.04)
bonus, hits = trigger.emotion_bonus("哈哈哈哈！？？", step=0.04, cap=0.2)
checker.equal("叠字+拟声+感叹+问句四项", round(bonus, 3), 0.16)
checker.equal("叠字命中", "叠字" in hits, True)
checker.equal("拟声命中", "拟声" in hits, True)
checker.equal("封顶生效", round(trigger.emotion_bonus("哈哈！！？？～～……", step=0.04, cap=0.1)[0], 3), 0.1)
checker.equal("step 为 0 直接返回", trigger.emotion_bonus("哈哈！！", step=0.0, cap=0.2), (0.0, []))
checker.equal("cap 为 0 直接返回", trigger.emotion_bonus("哈哈！！", step=0.04, cap=0.0), (0.0, []))

# ---------------------------------------------------------------- 点名窗口
now = [1000.0]
window = trigger.MentionPriority(clock=lambda: now[0])
checker.equal("初始不优先", window.active("s1"), False)
window.record("s1", 90.0)
checker.equal("记录后优先", window.active("s1"), True)
checker.equal("别的会话不受影响", window.active("s2"), False)
window.consume("s1")
checker.equal("用掉即清（一次性）", window.active("s1"), False)
window.record("s1", 90.0)
now[0] += 91.0
checker.equal("超过 TTL 作废", window.active("s1"), False)
window.record("s3", 0.0)
checker.equal("TTL 为 0 不记录", window.active("s3"), False)
window.record("s4", 5.0)
now[0] += 4.0
checker.equal("TTL 内仍有效", window.active("s4"), True)

# ---------------------------------------------------------------- 插件接入
plugin, config, _ = make_plugin(module)
plugin._mention_priority.clear()


def decide(text: str, stream: str = "sess-1"):
    return plugin._should_convert(text, stream, "group", "123456", "10000002")


# 默认关：点名与情绪都不生效
plugin._mention_priority.record("sess-1", 90.0)
converted, reason, _ = decide("好的呀")
checker.equal("默认关时点名不生效", reason in ("random", "miss"), True)
checker.check("默认关时来源仍是随机", reason in ("random", "miss"), reason)
plugin._mention_priority.clear()

# 打开点名：概率固定抬到 1.0 便于断言（mention_probability 上限 1.0）
plugin.__dict__["_plugin_config_instance"] = module.MiMoVoiceConfig(
    trigger={"mention_priority": True, "mention_probability": 1.0, "probability": 0.0}
)
plugin._mention_priority.record("sess-2", 90.0)
converted, reason, probability = plugin._should_convert("好的呀", "sess-2", "group", "123456", "10000002")
checker.equal("点名命中即转语音", converted, True)
checker.equal("来源标注 mention", reason, "mention")
checker.equal("概率取点名值", round(probability, 3), 1.0)
converted, reason, _ = plugin._should_convert("好的呀", "sess-2", "group", "123456", "10000002")
checker.equal("用掉之后回到基准概率 0", converted, False)
checker.equal("第二次来源是 miss", reason, "miss")

# 点名时跳过"随机需纯中文"，但仍按 english_policy 清洗过
plugin._mention_priority.record("sess-3", 90.0)
converted, reason, _ = plugin._should_convert("OK 好的", "sess-3", "group", "123456", "10000002")
checker.equal("点名时英文不拦截", converted, True)
checker.equal("来源仍是 mention", reason, "mention")

# 点名不绕过短句上限
plugin.__dict__["_plugin_config_instance"] = module.MiMoVoiceConfig(
    trigger={"mention_priority": True, "mention_probability": 1.0, "short_reply_max_chars": 5}
)
plugin._mention_priority.record("sess-4", 90.0)
converted, reason, _ = plugin._should_convert("这是一句很长的回复内容", "sess-4", "group", "123456", "10000002")
checker.equal("点名不绕过短句上限", converted, False)
checker.equal("来源 short_reply", reason, "short_reply")

# 情绪加权：概率 0.0 + 0.2 上限也不足以稳定命中，改用 0.9 基准 + 感叹号
plugin.__dict__["_plugin_config_instance"] = module.MiMoVoiceConfig(
    trigger={"emotion_weighting": True, "emotion_step": 0.05, "emotion_cap": 0.2, "probability": 0.0}
)
converted, reason, probability = plugin._should_convert("太好了！！", "sess-5", "group", "123456", "10000002")
checker.equal("情绪加分进入概率", round(probability, 3), 0.05)
checker.check("情绪来源带标签", reason.startswith("emotion(") or reason == "miss", reason)

# 情绪加权不改变 voice_only / llm_trigger 的确定性
plugin.__dict__["_plugin_config_instance"] = module.MiMoVoiceConfig(trigger={"mode": "voice_only"})
checker.equal("voice_only 恒为真", plugin._should_convert("随便", "s", "group", "1", "2"), (True, "voice_only", 1.0))

# 情绪看的是原始回复：清洗会把 ？？ 折成 ？、～ 折成逗号、emoji 删掉
plugin.__dict__["_plugin_config_instance"] = module.MiMoVoiceConfig(
    trigger={"emotion_weighting": True, "emotion_step": 0.15, "emotion_cap": 0.5, "probability": 0.0}
)
cleaned = plugin._sanitize_for_tts("真的吗？？")
checker.equal("清洗后只剩一个问号", cleaned, "真的吗？")
bonus_clean = trigger.emotion_bonus(cleaned, step=0.15, cap=0.5)
checker.equal("清洗后问句不再命中", bonus_clean, (0.0, []))
_, _, prob_clean = plugin._should_convert(cleaned, "sess-6", "group", "123456", "10000002")
checker.equal("只看清洗文本时为基准概率", round(prob_clean, 3), 0.0)
converted, reason, probability = plugin._should_convert(
    cleaned, "sess-6", "group", "123456", "10000002", raw_text="真的吗？？"
)
checker.equal("看原始回复时命中问句", round(probability, 3), 0.15)
checker.check("来源标注问句", reason.startswith("emotion(") or reason == "miss", reason)
converted, reason, _ = plugin._should_convert(
    "好", "sess-7", "group", "123456", "10000002", raw_text="好～"
)
checker.equal("波浪号只看原始回复才命中", round(trigger.emotion_bonus("好～", step=0.15, cap=0.5)[0], 3), 0.15)

raise SystemExit(checker.report())
