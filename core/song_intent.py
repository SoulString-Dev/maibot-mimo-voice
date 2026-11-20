"""意图层：从「唱一首X」「来一个Y」这类话里认出点歌意图，并生成给模型看的候选提示。

只做文本处理，不碰网络、存储和宿主 API，方便单独测试。
触发正则由用户在配置页里写，写错了跳过并告警，不会拖垮插件。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

# 用户可配的正则上限：匹配每条消息都要跑，太长/太多的正则容易写出灾难性回溯
MAX_PATTERN_LENGTH = 200
MAX_PATTERNS = 20

# 默认只认「唱/来/放/点 + 一? + 首/个」这一种说法（用户 2026-11-20 指定）。
# 「唱首」「来一个」「放一首」都命中；配置页整行替换即可换成任意正则。
DEFAULT_PATTERNS: Tuple[str, ...] = ("(?:唱|来|放|点)一?(?:首|个)",)

# 随机点歌的说法（用户 2026-11-20 指定，暂不支持按标签筛）
DEFAULT_RANDOM_PATTERNS: Tuple[str, ...] = ("(?:随机|随便|任意)(?:来|放)?(?:一)?(?:首|个)",)

# 注入提示的固定前缀：下一轮 planner 请求时按它把上一轮留下的提示清掉
HINT_MARK = "【点歌提示】"

# 歌名两侧常带的空白、标点、引号与书名号
_NOISE = " \t\r\n\u3000，,。.、!！?？~～:：;；\"'“”‘’《》<>()（）[]{}【】"
# 句尾语气词：「唱一首雨爱嘛」不该把「嘛」带进歌名
_TAIL_PARTICLES = "嘛吧呢啊呀哦啦咯喔哩呐呗哈"
# 开头的 @昵称（含 CQ 码风格与全角空格分隔）
_MENTION_PREFIX = re.compile(r"^(?:\s*@[^\s@]+|\s*\[@[^\]]+\])+")
_MAX_KEYWORD = 40


@dataclass(frozen=True)
class IntentHit:
    """一次命中：命中的是哪条正则、猜出来的歌名（可能为空串）。

    ``after`` 表示歌名取自匹配之后（True）还是匹配之前的兜底（False）——
    「随机来一首」这里前面的字只是残句，不能当歌名。
    """

    pattern: str
    keyword: str
    after: bool = True


def compile_patterns(raw: Any) -> Tuple[List[re.Pattern[str]], List[str]]:
    """编译配置里的正则行。

    Args:
        raw: 配置值，通常是 ``list[str]``；也容忍单个字符串（按行拆）。

    Returns:
        ``(patterns, warnings)``：可用的编译结果与给人看的告警。全写错时退回默认正则。
    """
    if isinstance(raw, str):
        lines: Sequence[Any] = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        lines = list(raw)
    else:
        lines = []

    patterns: List[re.Pattern[str]] = []
    warnings: List[str] = []
    if len(lines) > MAX_PATTERNS:
        warnings.append(f"intent_patterns 只取前 {MAX_PATTERNS} 条，其余已忽略")
    for line in lines[:MAX_PATTERNS]:
        text = str(line or "").strip()
        if not text:
            continue
        if len(text) > MAX_PATTERN_LENGTH:
            warnings.append(
                f"intent_patterns 里的正则太长（{len(text)} 字符，上限 {MAX_PATTERN_LENGTH}），已跳过"
            )
            continue
        try:
            patterns.append(re.compile(text, re.IGNORECASE))
        except re.error as exc:
            warnings.append(f"intent_patterns 里的正则 {text!r} 编译失败（{exc}），已跳过")
    if not patterns:
        patterns = [re.compile(item, re.IGNORECASE) for item in DEFAULT_PATTERNS]
        if lines:
            warnings.append("intent_patterns 没有一条能用，已退回默认正则")
    return patterns, warnings


def strip_mentions(text: str) -> str:
    """去掉开头的 @某人，剩下的才是「唱一首雨爱」这样的正文。"""
    cleaned = _MENTION_PREFIX.sub("", str(text or ""))
    return cleaned.strip()


def _clean_keyword(text: str) -> str:
    keyword = text.strip(_NOISE)
    while len(keyword) > 1 and keyword[-1] in _TAIL_PARTICLES:
        keyword = keyword[:-1].strip(_NOISE)
    if len(keyword) > _MAX_KEYWORD:
        keyword = keyword[:_MAX_KEYWORD]
    return keyword


def match_intent(text: str, patterns: Sequence[re.Pattern[str]]) -> Optional[IntentHit]:
    """在文本里找第一条命中的点歌正则。

    Returns:
        命中返回 :class:`IntentHit`（``keyword`` 是匹配之后剩下的部分，按歌名用）；
        没有命中返回 ``None``。
    """
    body = strip_mentions(text)
    if not body:
        return None
    for pattern in patterns:
        match = pattern.search(body)
        if match is None:
            continue
        # 取匹配之后的部分当歌名猜测：「唱一首雨爱」→「雨爱」；「来一首」→ 空串
        keyword = _clean_keyword(body[match.end() :])
        after = True
        if not keyword and match.start() > 0:
            # 歌名写在前面（「雨爱来一个」）也认一下
            keyword = _clean_keyword(body[: match.start()])
            after = False
        return IntentHit(pattern=pattern.pattern, keyword=keyword, after=after)
    return None


def matches_any(text: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    """文本里是否出现任意一条正则（先剥掉开头的 @昵称）。"""
    body = strip_mentions(text)
    if not body:
        return False
    return any(pattern.search(body) is not None for pattern in patterns)


def build_random_hint() -> str:
    """拼「用户想随机听一首」的注入提示。"""
    return (
        HINT_MARK
        + "用户想随机听一首。\n"
        + "调用 mimo_voice_play_random 工具即可，系统会从曲库里挑一首（优先避开最近播过的）直接发语音条；"
        "不必报歌名，也不要说自己会唱。"
    )


def build_hint(candidates: Sequence[str], keyword: str, limit: int = 5) -> str:
    """拼一段注入给模型的提示：候选清单 + 该调用哪个工具。"""
    size = max(1, min(int(limit), len(candidates) or 1))
    head = HINT_MARK + "用户这句话像是在点歌"
    head += f"（想要的可能是「{keyword}」）。" if keyword else "。"
    lines = [head, "曲库里现有这些："]
    for index, label in enumerate(candidates[:size], 1):
        lines.append(f"{index}. {label}")
    lines.append(
        "如果表里有用户想听的歌，调用 mimo_voice_play_song 工具，name 原样填表里的歌名；"
        "系统会直接把语音条发出去，你不必再念一遍歌名，也不要说自己会唱。"
    )
    lines.append("表里没有合适的就直说曲库里没有这首，不要编歌名。")
    return "\n".join(lines)


def build_note_item(note: str, *, item_id: str, timestamp: str) -> dict:
    """把提示包成宿主认识的 SystemMessageItem 快照。

    ``item_id`` 必须唯一、``timestamp`` 必须是 ISO 时间，否则宿主反序列化会丢整份 items。
    """
    return {
        "item_type": "SystemMessageItem",
        "meta": {
            "item_id": item_id,
            "logical_turn_id": None,
            "timestamp": timestamp,
        },
        "parts": [{"type": "text", "text": note}],
    }


def append_hint(items: List[dict], note: str, *, item_id: str, timestamp: str) -> bool:
    """把提示追加到已有 system 消息末尾；没有就插一条新的。

    Returns:
        是否写入成功（``items`` 不是列表时返回 ``False``）。
    """
    if not isinstance(items, list):
        return False
    for item in reversed(items):
        if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem":
            parts = item.get("parts")
            if not isinstance(parts, list):
                parts = []
                item["parts"] = parts
            parts.append({"type": "text", "text": note})
            return True
    items.insert(0, build_note_item(note, item_id=item_id, timestamp=timestamp))
    return True


def strip_stale_notes(items: Any) -> int:
    """清掉上一轮注入的点歌提示。

    这些提示可能被宿主持久化进对话历史，不清的话模型下一轮还会看见「用户想点歌」，
    跟着再调一次播放工具。返回清掉的段数。
    """
    if not isinstance(items, list):
        return 0
    removed = 0
    for item in list(items):
        if not isinstance(item, dict):
            continue
        parts = item.get("parts")
        if isinstance(parts, list):
            kept = [
                part
                for part in parts
                if not (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and str(part.get("text") or "").startswith(HINT_MARK)
                )
            ]
            removed += len(parts) - len(kept)
            if len(kept) != len(parts):
                item["parts"] = kept
        meta = item.get("meta")
        item_id = str(meta.get("item_id") or "") if isinstance(meta, dict) else ""
        if item_id.startswith("mimo-voice-song-"):
            current = item.get("parts")
            if not isinstance(current, list) or not current:
                items.remove(item)
                removed += 1
    return removed
