"""文本侧测试：朗读清洗、英文口癖策略、纯文本判定、规划器工具显隐。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __bootstrap import Checker, load_plugin, make_plugin  # noqa: E402

module = load_plugin()
checker = Checker("文本侧")


def plugin_with(**trigger: object):
    return make_plugin(module, trigger=dict(trigger))[0]


# ---------------------------------------------------------------- 朗读清洗
p = plugin_with(english_policy="strip_tics")
cases = {
    "早啊😊今天天气不错～": "早啊今天天气不错。",
    "「早啊」，今天天气不错！！": "早啊，今天天气不错！",
    "（小声）那个，我想说……你听得到吗？": "小声那个，我想说，你听得到吗？",
    "看这个 https://example.com/a?b=1 吧。": "看这个吧。",
}
for source, expected in cases.items():
    checker.equal(f"清洗 {source[:14]}", p._sanitize_for_tts(source), expected)

checker.equal("只有表情时返回 None", p._sanitize_for_tts("😊😊"), None)
checker.equal("空字符串返回 None", p._sanitize_for_tts("   "), None)

# ---------------------------------------------------------------- 英文策略
keep = plugin_with(english_policy="keep")
tics = plugin_with(english_policy="strip_tics")
strip = plugin_with(english_policy="strip")

checker.equal("keep 保留 ww", keep._sanitize_for_tts("真成小丑ww。"), "真成小丑ww。")
checker.equal("strip_tics 去掉 ww", tics._sanitize_for_tts("真成小丑ww。"), "真成小丑。")
checker.equal("strip_tics 去掉 ohhhhhhh", tics._sanitize_for_tts("啊啊啊ohhhhhhh"), "啊啊啊。")
for word in ("LLM", "token", "cos", "DeepSeek", "boss", "miss", "all", "ff14"):
    checker.equal(f"strip_tics 保留 {word}", tics._apply_english_policy(f"抽到{word}了"), f"抽到{word}了")
checker.equal("strip 全删 token", strip._sanitize_for_tts("本鲸鱼自己烧的token。"), "本鲸鱼自己烧的。")

# ---------------------------------------------------------------- 纯文本判定
plain = p._plain_text_message({"raw_message": [{"type": "text", "data": "早啊～"}]})
checker.equal("单段文本可转", plain, "早啊～")
mixed = p._plain_text_message(
    {
        "processed_plain_text": "早啊",
        "raw_message": [{"type": "reply", "data": "1"}, {"type": "text", "data": "早啊"}],
    }
)
checker.equal("引用+文本可转", mixed, "早啊")
checker.equal(
    "图片消息不可转",
    p._plain_text_message({"raw_message": [{"type": "image", "data": "x"}]}),
    None,
)
checker.equal(
    "文本+表情不可转",
    p._plain_text_message(
        {"raw_message": [{"type": "text", "data": "早"}, {"type": "emoji", "data": "x"}]}
    ),
    None,
)

# ---------------------------------------------------------------- 工具显隐
TOOLS = [
    {"type": "function", "function": {"name": module.MiMoVoicePlugin._TOOL_TEXT_REPLY}},
    {"type": "function", "function": {"name": module.MiMoVoicePlugin._TOOL_VOICE_SAY}},
    {"type": "function", "function": {"name": "reply"}},
]


def visible(mode: str) -> list[str]:
    inst = plugin_with(mode=mode)
    result = asyncio.run(inst.configure_planner_tools(tool_definitions=list(TOOLS)))
    definitions = TOOLS if result is None else result["modified_kwargs"]["tool_definitions"]
    return [inst._tool_definition_name(item) for item in definitions]


checker.equal("random 模式两只都隐藏", visible("random"), ["reply"])
checker.equal("random_llm 只留 say", visible("random_llm"), [module.MiMoVoicePlugin._TOOL_VOICE_SAY, "reply"])
checker.equal("llm_trigger 只留 text_reply", visible("llm_trigger"), [module.MiMoVoicePlugin._TOOL_TEXT_REPLY, "reply"])

sys.exit(checker.report())
