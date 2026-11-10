"""触发词测试（0.5.6）：三档匹配、归一化、插件接入与一次性窗口。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __bootstrap import Checker, load_plugin, make_plugin

module = load_plugin()
triggers = sys.modules[f"{module.__name__}.core.triggers"]
config_module = sys.modules[f"{module.__name__}.core.config"]
checker = Checker("触发词")

# ---------------------------------------------------------------- 匹配
checker.equal("任意位置命中", triggers.matches("你能不能说得清楚一点", "说得清楚"), True)
checker.equal("任意位置不命中", triggers.matches("今天天气不错", "说得清楚"), False)
checker.equal("头部命中", triggers.matches("请用语音回答这个问题", "请用语音回答", "head"), True)
checker.equal("头部不命中（词在中间）", triggers.matches("这个问题请用语音回答", "请用语音回答", "head"), False)
checker.equal("尾部命中", triggers.matches("你好，说说看？", "说说看", "tail"), True)
checker.equal("尾部不容忍语气词", triggers.matches("你好，说说看吧？", "说说看", "tail"), False)
checker.equal("尾部忽略尾部标点", triggers.matches("今天就到这里。", "到这里", "tail"), True)
checker.equal("尾部不命中（词在开头）", triggers.matches("到这里就好了", "到这里", "tail"), False)
checker.equal("头部容忍开头 @昵称", triggers.matches("@麦麦 请用语音回答", "请用语音回答", "head"), True)
checker.equal("头部容忍连续 @", triggers.matches("@麦麦 @另一个 请用语音回答", "请用语音回答", "head"), True)
checker.equal("忽略首尾空格与全角空格", triggers.matches("　你好　说说看　", "说说看", "tail"), True)
checker.equal("英文大小写不敏感", triggers.matches("Please use VOICE", "use voice"), True)
checker.equal("空词不命中", triggers.matches("用语音说吧", ""), False)
checker.equal("空文本不命中", triggers.matches("", "用语音"), False)
checker.equal("超长文本只扫前 2000 字", triggers.matches("啊" * 2100 + "用语音", "用语音"), False)
checker.equal("位置别名 head", triggers.normalize_mode("head"), "head")
checker.equal("位置别名 头部", triggers.normalize_mode("头部"), "head")
checker.equal("位置别名 尾", triggers.normalize_mode("尾"), "tail")
checker.equal("位置留空默认任意", triggers.normalize_mode(""), "any")
checker.equal("位置乱写返回 None", triggers.normalize_mode("中间"), None)

rules = [("请用语音", "head"), ("说说看", "tail"), ("用语音", "any")]
checker.equal("按顺序取第一条", triggers.match_rules("请用语音说说看", rules), ("请用语音", "head"))
checker.equal("第一条不中取第二条", triggers.match_rules("你说说看", rules), ("说说看", "tail"))
checker.equal("哥哥都不中", triggers.match_rules("今天天气不错", rules), None)

# ---------------------------------------------------------------- 配置模型
config = config_module.MiMoVoiceConfig()
checker.equal("默认开启", config.triggers.enabled, True)
checker.equal("默认没有规则", list(config.triggers.rules), [])
rule = config_module.TriggerWordRule(word="  用语音 ", mode="head")
checker.equal("词去掉首尾空格", rule.word, "用语音")
checker.equal("位置归一成中文", rule.mode, "头")
rule2 = config_module.TriggerWordRule(word="说说看", mode="tail")
checker.equal("tail 归一成尾", rule2.mode, "尾")
rule3 = config_module.TriggerWordRule(word="随便", mode="任意")
checker.equal("中文位置保持", rule3.mode, "任意")

# ---------------------------------------------------------------- 插件接入
plugin, cfg, _ = make_plugin(module)
plugin.__dict__["_plugin_config_instance"] = config_module.MiMoVoiceConfig(
    triggers={"enabled": True, "rules": [{"word": "用语音", "mode": "任意"}]},
    trigger={"probability": 0.0},
)
checker.equal("窗口 = 超时 + 10 秒", plugin._trigger_ttl(), 35.0)


def inbound(text: str, *, stream: str = "sess-t1", scope: str = "group", group: str = "123456", at: bool = True):
    payload = {
        "session_id": stream,
        "scope": scope,
        "processed_plain_text": text,
        "is_at": at,
        "is_mentioned": False,
        "message_info": {"group_info": {"group_id": group}},
    }
    asyncio.run(plugin.remember_trigger(payload))


plugin._trigger_last_hit.clear()
inbound("麦麦你用语音回答我")
checker.equal("群里 @ + 命中就记下", plugin._active_trigger("sess-t1"), ("用语音", "any"))
converted, reason, probability = plugin._should_convert("好的呀", "sess-t1", "group", "123456", "10000002")
checker.equal("触发词强制转语音", converted, True)
checker.equal("来源带触发词", reason, "trigger:用语音")
checker.equal("概率固定 1.0", probability, 1.0)
converted, reason, _ = plugin._should_convert("随便什么", "sess-t1", "group", "123456", "10000002")
checker.equal("未消费前仍然有效", converted, True)

plugin._trigger_requests.clear()
plugin._trigger_last_hit.clear()
inbound("你用语音回答我", at=False)
checker.equal("群聊没 @ 不记", plugin._active_trigger("sess-t2"), None)
inbound("你用语音回答我", stream="sess-t3", scope="private", group="", at=False)
checker.equal("私聊不需要 @", plugin._active_trigger("sess-t3"), ("用语音", "any"))

plugin._trigger_last_hit.clear()
inbound("/voice test 用语音", stream="sess-t4")
checker.equal("命令消息不参与", plugin._active_trigger("sess-t4"), None)
inbound("  ／vc status 用语音", stream="sess-t5")
checker.equal("全角斜杠命令也不参与", plugin._active_trigger("sess-t5"), None)

# 限流：同会话 5 秒内只记一次（窗口未被覆盖）
plugin._trigger_last_hit.clear()
inbound("用语音第一句", stream="sess-t6")
first = plugin._trigger_requests["sess-t6"]
inbound("用语音第二句", stream="sess-t6")
checker.equal("5 秒内第二次命中被忽略", plugin._trigger_requests["sess-t6"], first)

# 过期即作废
plugin._trigger_last_hit.clear()
plugin.__dict__["_plugin_config_instance"] = config_module.MiMoVoiceConfig(
    triggers={"enabled": True, "rules": [{"word": "用语音", "mode": "任意"}]},
    trigger={"probability": 0.0},
)
inbound("用语音", stream="sess-t7")
word, mode, expires = plugin._trigger_requests["sess-t7"]
plugin._trigger_requests["sess-t7"] = (word, mode, expires - 3600.0)
checker.equal("过期后不再生效", plugin._active_trigger("sess-t7"), None)

# 关闭开关后不记录
plugin._trigger_last_hit.clear()
plugin.__dict__["_plugin_config_instance"] = config_module.MiMoVoiceConfig(
    triggers={"enabled": False, "rules": [{"word": "用语音", "mode": "任意"}]}
)
inbound("用语音", stream="sess-t8")
checker.equal("关掉后不记", plugin._active_trigger("sess-t8"), None)
# 没有规则时也不记录
plugin.__dict__["_plugin_config_instance"] = config_module.MiMoVoiceConfig(triggers={"enabled": True, "rules": []})
inbound("用语音", stream="sess-t9")
checker.equal("没有规则不记", plugin._active_trigger("sess-t9"), None)

# 触发词不受短句上限与英文策略约束，但仍受会话开关约束
plugin._trigger_last_hit.clear()
plugin.__dict__["_plugin_config_instance"] = config_module.MiMoVoiceConfig(
    triggers={"enabled": True, "rules": [{"word": "用语音", "mode": "任意"}]},
    trigger={
        "probability": 0.0,
        "short_reply_max_chars": 3,
        "random_requires_chinese": True,
        "mode": "random_llm",
    },
)
inbound("用语音读这一句很长的中英混排 content 好吗", stream="sess-t10")
converted, reason, _ = plugin._should_convert(
    "这一句很长而且含 content", "sess-t10", "group", "123456", "10000002"
)
checker.equal("绕过短句与英文限制", converted, True)
checker.equal("来源仍是触发词", reason, "trigger:用语音")
plugin._session_switch["sess-t10"] = False
converted, reason, _ = plugin._should_convert(
    "这一句很长而且含 content", "sess-t10", "group", "123456", "10000002"
)
checker.equal("会话开关优先于触发词", converted, False)
checker.equal("来源 off", reason, "off")

sys.exit(checker.report())
