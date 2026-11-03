"""触发增强：点名优先（一次性窗口）与情绪加权。

两者都默认关闭，只在 ``random`` / ``random_llm`` 的抽签环节生效；改的只是"这条回复要不要
转语音"的概率，不碰朗读清洗、长度上限与超时链路。
"""
from __future__ import annotations

import re
import time
from typing import Callable, Dict, Iterable, List, Tuple

# 情绪标记：值越小越先匹配（叠字/多标点要排在单字符前面，避免被拆成多个命中）。
_EMOTION_RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("感叹", re.compile(r"[!！]+")),
    ("问句", re.compile(r"[?？]{2,}")),
    ("波浪号", re.compile(r"[~～]+")),
    ("省略号", re.compile(r"(?:…{2,}|\.{3,}|。{3,})")),
    ("叠字", re.compile(r"(哈|嘿|嘻|呜|哭|嗷|啊|呀|哇|嗯|诶|欸|呵|啧){2,}")),
    ("拟声", re.compile(r"(哈哈|嘿嘿|嘻嘻|呜呜|哇哦|哎呀|哎哟|咦|哼|诶嘿|呜哇|咕|噜)")),
    ("emoji", re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")),
)


def emotion_bonus(text: str, *, step: float, cap: float) -> Tuple[float, List[str]]:
    """按情绪标记给抽签概率加分，返回 ``(加分, 命中标签)``。

    每个标签最多计一次，命中数 × ``step``，封顶 ``cap``；``step`` 或 ``cap`` 为 0 时直接返回 0。
    只增不减：文字平静时不会因为"没情绪"被扣分。
    """
    if step <= 0.0 or cap <= 0.0:
        return 0.0, []
    hits: List[str] = []
    for label, pattern in _EMOTION_RULES:
        if pattern.search(text):
            hits.append(label)
    if not hits:
        return 0.0, []
    return min(cap, len(hits) * step), hits


class MentionPriority:
    """点名优先的"一次性窗口"。

    ``record`` 记下一次点名，``active`` 只查看不消费，``consume`` 在真正用到时清掉；
    没被用掉的记录按 TTL 过期，避免"被 @ 之后十分钟内的每条回复都当点名"。
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._expires: Dict[str, float] = {}

    def record(self, key: str, ttl: float) -> None:
        if not key or ttl <= 0.0:
            return
        self._expires[key] = self._clock() + ttl

    def active(self, key: str) -> bool:
        expires_at = self._expires.get(key)
        if expires_at is None:
            return False
        if expires_at <= self._clock():
            self._expires.pop(key, None)
            return False
        return True

    def consume(self, key: str) -> None:
        self._expires.pop(key, None)

    def clear(self) -> None:
        self._expires.clear()

    def prune(self, limit: int = 1024) -> None:
        """窗口数量兜底：超过上限先清过期项，仍超上限直接清空（丢了只是少一次优先）。"""
        if len(self._expires) <= limit:
            return
        now = self._clock()
        for key, expires_at in list(self._expires.items()):
            if expires_at <= now:
                self._expires.pop(key, None)
        if len(self._expires) > limit:
            self._expires.clear()

    def keys(self) -> Iterable[str]:
        return tuple(self._expires)
