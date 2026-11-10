"""触发词匹配（0.5.6）。

用户只填「词 + 位置档（头 / 尾 / 任意）」，不写正则：

- 归一化：去掉全半角空白、``casefold``、剥掉首尾标点；
- ``head`` 额外容忍开头的 ``@昵称``（群里 @ 机器人时，文本前面常带这一段）；
- 匹配就是 ``startswith`` / ``endswith`` / ``in``，没有任何回溯风险；
- 比对前先把文本截到 2000 字，避免超长回复逐条做全串扫描。
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence, Tuple

MODE_ANY = "any"
MODE_HEAD = "head"
MODE_TAIL = "tail"

# 配置里允许的写法（大小写不敏感）；中文是 WebUI 下拉框里的值
MODE_ALIASES = {
    "任意": MODE_ANY,
    "any": MODE_ANY,
    "头": MODE_HEAD,
    "头部": MODE_HEAD,
    "head": MODE_HEAD,
    "尾": MODE_TAIL,
    "尾部": MODE_TAIL,
    "tail": MODE_TAIL,
}

MODE_LABELS = {MODE_ANY: "任意", MODE_HEAD: "头", MODE_TAIL: "尾"}

_MAX_SCAN_CHARS = 2000

# 首尾常见的标点与空白：为了「说说看？」能命中「说说看」
_EDGE_CHARS = "。．.！!？?，,、；;：:…~～—–－-‐\"'“”‘’（）()【】[]{}<>《》〈〉 \t\r\n\u3000"

# 开头的 @昵称（可能连续几个）；只用于 head 档
_LEADING_MENTIONS = re.compile(r"^(?:@[^\s@]+\s*)+")


def normalize_mode(value: object) -> Optional[str]:
    """把配置里的位置写法归一成 ``any`` / ``head`` / ``tail``，认不出返回 None。"""

    raw = str(value or "").strip()
    if not raw:
        return MODE_ANY
    return MODE_ALIASES.get(raw) or MODE_ALIASES.get(raw.casefold())


def mode_label(mode: str) -> str:
    """给日志与 ``/voice status`` 用的人类可读名称。"""

    return MODE_LABELS.get(mode, mode)


def normalize_word(word: object) -> str:
    """归一化触发词：去空白、``casefold``、剥首尾标点。空词返回空串（调用方应跳过）。"""

    raw = str(word or "")
    if not raw:
        return ""
    return _clean(raw)


def _clean(text: str) -> str:
    """去空白 + ``casefold`` + 剥首尾标点。"""

    compact = re.sub(r"\s+", "", text)
    return compact.casefold().strip(_EDGE_CHARS)


def matches(text: str, word: str, mode: str = MODE_ANY) -> bool:
    """``text`` 是否按 ``mode`` 命中 ``word``。

    空词一律不命中；``text`` 先截到 2000 字。
    """

    target = normalize_word(word)
    if not target:
        return False
    body = str(text or "")[:_MAX_SCAN_CHARS]
    if not body:
        return False
    mode = normalize_mode(mode) or MODE_ANY
    if mode == MODE_HEAD:
        body = _LEADING_MENTIONS.sub("", body)
        return _clean(body).startswith(target)
    if mode == MODE_TAIL:
        return _clean(body).endswith(target)
    return target in _clean(body)


def match_rules(text: str, rules: Iterable[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    """按配置顺序找第一条命中的规则，返回 ``(word, mode)``；没有命中返回 None。"""

    for word, mode in rules:
        if matches(text, word, mode):
            return str(word).strip(), normalize_mode(mode) or MODE_ANY
    return None


def describe_rules(rules: Sequence[Tuple[str, str]]) -> str:
    """把规则列表压成一行，给 ``/voice status`` 用（最多展示 5 条）。"""

    shown = [f"{word}({mode_label(normalize_mode(mode) or MODE_ANY)})" for word, mode in rules[:5]]
    if len(rules) > 5:
        shown.append(f"…共 {len(rules)} 条")
    return "、".join(shown)
