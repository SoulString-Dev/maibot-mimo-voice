"""朗读文本清洗：表情/引号/波浪号/省略号处理，以及英文口癖策略。"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_URL_PATTERN = re.compile(r"https?://\S+")
_CQ_PATTERN = re.compile(r"\[CQ:[^\]]+\]")
# 表情符号与零宽字符：朗读前必须清掉，否则会被念出来或者把音高带跑。
_EMOJI_PATTERN = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF\ufe0f\u200d]"
)
_LEAD_NOISE_PATTERN = re.compile(r"^[\s'\"“”‘’「」『』（）()\[\]【】《》<>~～\-—*_`#>、，,。]+")
_TAIL_NOISE_PATTERN = re.compile(r"[\s'\"“”‘’「」『』（）()\[\]【】《》<>*_`~～\-—、，,]+$")
_SENTENCE_END = "。！？!?"
_LATIN_PATTERN = re.compile(r"[A-Za-z]+")
_VOWELS = "aeiou"
_CONSONANTS = "bcdfghjklmnpqrstvwxyz"


def sanitize_for_tts(text: str, policy: str) -> Optional[str]:
    """把聊天文本整理成适合朗读的句子。

    去表情/零宽字符/首尾引号、按 english_policy 处理英文口癖、
    把 ～ 和省略号折成逗号、补句末标点。
    实测这些符号会让合成音高漂移 2–4 个半音，并带来首尾的爆音与长静音。
    """
    cleaned = _URL_PATTERN.sub("", text)
    cleaned = _CQ_PATTERN.sub("", cleaned)
    cleaned = _EMOJI_PATTERN.sub("", cleaned)
    cleaned = apply_english_policy(cleaned, policy)
    # 零宽字符/方向控制符会让模型把停顿读错；引号括号则会被念成奇怪的语气
    cleaned = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", cleaned)
    cleaned = re.sub(r"[「」『』“”‘’\"'（）()【】\[\]《》〈〉]", "", cleaned)
    cleaned = cleaned.replace("\u3000", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # 去掉 URL/英文口癖后常留下"看这个 吧"这类空格：中文之间不需要空格，朗读时会变成多余停顿
    cleaned = re.sub(r"(?<=[\u4e00-\u9fff\uff0c\u3002\uff01\uff1f])\s+(?=[\u4e00-\u9fff])", "", cleaned)
    cleaned = _LEAD_NOISE_PATTERN.sub("", cleaned)
    cleaned = _TAIL_NOISE_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"[~～]+", "，", cleaned)
    cleaned = re.sub(r"(…+|\.{3,}|。{2,})", "，", cleaned)
    cleaned = re.sub(r"[!！]{2,}", "！", cleaned)
    cleaned = re.sub(r"[?？]{2,}", "？", cleaned)
    cleaned = re.sub(r"[，,、]{2,}", "，", cleaned)
    cleaned = re.sub(r"，+([。！？])", r"\1", cleaned)
    cleaned = cleaned.strip("，,、 ")
    if not cleaned:
        return None
    if cleaned[-1] not in _SENTENCE_END:
        cleaned += "。"
    return cleaned


def is_tic_token(token: str) -> bool:
    """判断拉丁串是不是"口癖"而不是真词。

    命中两类：整词是同一个辅音的重复（ww / mmm / hhhh），
    或一个元音 + 同一辅音重复三次以上（emmm / ohhhhhhh）。
    LLM/SSR/boss/miss/cos/token 这类真词与缩写一律不匹配。
    """
    low = token.lower()
    if len(low) < 2:
        return False
    if len(set(low)) == 1 and low[0] in _CONSONANTS:
        return True
    # 一个元音 + 同一辅音重复三次以上（emmm / ohhhhhhh）
    return (
        low[0] in _VOWELS
        and len(low) >= 4
        and len(set(low[1:])) == 1
        and low[1] in _CONSONANTS
    )


def touches_digit(text: str, match: Any) -> bool:
    """拉丁串紧挨着数字时不当作口癖（ff14、mp3、b站25 这类是型号不是语气）。"""
    start, end = match.span()
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return before.isdigit() or after.isdigit()


def apply_english_policy(text: str, policy: str) -> str:
    if policy == "keep":
        return text
    if policy == "strip":
        return _LATIN_PATTERN.sub("", text)
    return _LATIN_PATTERN.sub(
        lambda m: "" if is_tic_token(m.group(0)) and not touches_digit(text, m) else m.group(0),
        text,
    )

# ------------------------------------------------------------------- 过滤


def plain_text_message(message: Dict[str, Any]) -> Optional[str]:
    raw = message.get("raw_message")
    if not isinstance(raw, list) or not raw:
        return None
    segments = [item for item in raw if isinstance(item, dict)]
    # 引用回复会带一个 reply 段，不能因为它就把整条消息当成“非纯文本”跳过：
    # 转语音时引用段本来就会被移除。这里只接受 text(+reply) 组合，且必须恰好一个 text。
    types = [str(segment.get("type") or "") for segment in segments]
    if any(kind not in {"text", "reply"} for kind in types) or types.count("text") != 1:
        return None
    segment = segments[types.index("text")]
    data = segment.get("data")
    text = data if isinstance(data, str) else (data or {}).get("text") if isinstance(data, dict) else None
    if not isinstance(text, str):
        return None
    text = _CQ_PATTERN.sub("", text).strip()
    return text or None


def contains_latin(text: str) -> bool:
    """文本里是否还有连续拉丁字母（随机触发语音时用来跳过英文句子）。"""
    return bool(_LATIN_PATTERN.search(text))
