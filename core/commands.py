"""命令层的纯逻辑：解析 ``/voice``、判定管理员、解析发送目标、拼回执文案。

这里不放任何宿主调用，插件侧的 handler 只负责把结果发出去；这样绝大部分行为
都能在 ``tests/test_commands.py`` 里用标准库直接断言。

设计要点（与 ``docs/commands-0.5.5.md`` 对齐）：

* 命令名只收 ``voice`` 与 ``vc``，大小写不敏感，兼容全角斜杠 ``／``；
* 管理员按 ``user_id`` 或 ``平台:user_id`` 匹配，先精确比较 ID、再比较平台前缀；
* 目标写法 ``g<群号>`` / ``u<QQ号>`` 是逃生门，无前缀时交给插件按"活跃群 → 活跃私聊 →
  新建会话"依次尝试（那部分需要宿主能力，不在这里）。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

# 只认这两个名字；不加 /tts、/say 之类容易和同类插件撞车的短名
VOICE_COMMAND_NAMES = ("voice", "vc")

_USAGE = {
    "on": "/voice on",
    "off": "/voice off",
    "test": "/voice test <文本>",
    "status": "/voice status",
    "help": "/voice help",
    "say": "/voice say <群号或QQ号> <文本>",
}


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    """把 ``/voice test 你好`` 拆成 ``("test", "你好")``。

    返回 ``None`` 表示这不是本插件的命令；返回 ``("", "")`` 表示只敲了 ``/voice``。
    子命令统一转小写，参数（含空格）原样保留。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    # 全角斜杠与全角空格都要兼容：手机输入法经常打出来
    normalized = raw.replace("／", "/").replace("\u3000", " ")
    if not normalized.startswith("/"):
        return None
    body = normalized[1:].strip()
    if not body:
        return None
    parts = body.split(None, 1)
    name = parts[0].lower()
    if name not in VOICE_COMMAND_NAMES:
        return None
    rest = (parts[1] if len(parts) > 1 else "").strip()
    if not rest:
        return "", ""
    sub = rest.split(None, 1)
    return sub[0].lower(), (sub[1] if len(sub) > 1 else "").strip()


def admin_matches(user_id: str, platform: str, admins: Iterable[str]) -> bool:
    """判断发送者是否在管理员列表里。

    条目可以写纯 ID（``"10000002"``）或带平台的 scoped ID（``"qq:10000002"``）。
    scoped 比较时平台前缀不区分大小写，ID 部分保持原样（openid 之类是大小写敏感的）。
    """
    uid = (user_id or "").strip()
    if not uid:
        return False
    plat = (platform or "").strip().lower()
    for entry in admins:
        item = str(entry or "").strip()
        if not item:
            continue
        if item == uid:
            return True
        prefix, sep, rest = item.partition(":")
        if sep and rest.strip() == uid and prefix.strip().lower() == plat:
            return True
    return False


def parse_target(raw: str) -> Tuple[Optional[str], str]:
    """解析 ``say`` 的目标：返回 ``("group"|"private"|None, 标识)``。"""
    text = (raw or "").strip()
    if not text:
        return None, ""
    lowered = text.lower()
    if lowered.startswith("g") and len(text) > 1:
        return "group", text[1:].strip()
    if lowered.startswith("u") and len(text) > 1:
        return "private", text[1:].strip()
    return None, text


_ADMIN_ENTRY = re.compile(r"^(?:[A-Za-z][A-Za-z0-9_]*:)?[0-9]+$")


def admin_entry_problem(entry: str) -> Optional[str]:
    """检查 ``commands.admins`` 的一条；没问题返回 ``None``。

    合法写法只有两种：纯数字账号，或 ``平台:账号``（``qq:10000002``）。写成昵称、
    带空格、混中文都匹配不到人，启动时给一条告警比让人对着"没有权限"猜要好。
    """
    item = str(entry or "").strip()
    if not item:
        return "空条目"
    if _ADMIN_ENTRY.fullmatch(item):
        return None
    return "应为数字账号，可以带平台前缀（如 qq:10000002）"


def split_target_and_text(rest: str) -> Tuple[str, str]:
    """把 ``say`` 的参数拆成（目标, 文本）；目标里不能有空格。"""
    body = (rest or "").strip()
    if not body:
        return "", ""
    parts = body.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "").strip()


def usage_of(sub: str) -> str:
    """某个子命令的用法提示。"""
    return _USAGE.get(sub, "/voice help")


def render_help() -> str:
    """``/voice help`` 的正文；命令都只对管理员开放，所以这里不区分身份。"""
    return "\n".join(
        [
            "MiMo 语音命令（仅私聊、仅管理员）",
            "/voice on | off          开启/关闭当前会话的自动语音",
            "/voice test <文本>        立刻朗读这段文本（当前会话）",
            "/voice say <目标> <文本>  把语音发到指定群/QQ，目标可写 g群号 或 uQQ号",
            "/voice status            查看版本、音色、触发参数与最近合成情况",
            "/voice help              显示本帮助",
            "也可以把 /voice 换成 /vc。",
        ]
    )


@dataclass(frozen=True)
class StatusSnapshot:
    """``/voice status`` 需要的全部数据，由插件填好。"""

    version: str
    preset: str
    reference: str
    mode: str
    probability: float
    short_reply_max_chars: int
    english_policy: str
    timeout: float
    max_concurrent: int
    mention_enabled: bool
    mention_probability: float
    emotion_enabled: bool
    emotion_step: float
    emotion_cap: float
    trigger_enabled: bool
    trigger_ttl: float
    trigger_rules: Sequence[str]
    trigger_active: bool
    session_on: bool
    session_label: str
    synth_ok: int = 0
    synth_timeout: int = 0
    synth_failed: int = 0
    synth_failures: Sequence[Tuple[str, int]] = field(default_factory=tuple)
    durations: Sequence[float] = field(default_factory=tuple)


def _percentile(samples: Sequence[float], ratio: float) -> Optional[float]:
    """线性插值分位数；样本为空返回 ``None``。"""
    values = sorted(float(item) for item in samples)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * max(0.0, min(1.0, ratio))
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def render_status(snapshot: StatusSnapshot) -> str:
    """四块状态：版本音色 / 触发 / 超时并发 / 本会话与近期合成。"""
    lines: List[str] = [
        f"MiMo TTS 语音回复 v{snapshot.version} · 已加载",
        f"音色 {snapshot.preset} · {snapshot.reference}",
        f"触发 模式={snapshot.mode} 概率={snapshot.probability:.2f} "
        f"短句≤{snapshot.short_reply_max_chars}字 英文={snapshot.english_policy}",
        "加强 "
        + ("点名开" if snapshot.mention_enabled else "点名关")
        + f"({snapshot.mention_probability:.2f}) "
        + (
            f"情绪开(+{snapshot.emotion_step:.2f}/上限{snapshot.emotion_cap:.2f})"
            if snapshot.emotion_enabled
            else "情绪关"
        )
        + f" 触发词{'开' if snapshot.trigger_enabled else '关'}"
        + (f"（{len(snapshot.trigger_rules)} 条）" if snapshot.trigger_rules else "（未配置）"),
        f"超时 {snapshot.timeout:.0f}s · 并发 {snapshot.max_concurrent} · 一条语音只发一次请求",
    ]

    stats = f"本会话{'已开启' if snapshot.session_on else '已关闭'}（{snapshot.session_label}）"
    stats += f" · 进程内合成 {snapshot.synth_ok} 次"
    if snapshot.synth_timeout or snapshot.synth_failed:
        stats += f"（超时 {snapshot.synth_timeout}，失败 {snapshot.synth_failed}"
        if snapshot.synth_failures:
            stats += "：" + "、".join(f"{kind}×{count}" for kind, count in snapshot.synth_failures)
        stats += "）"
    if snapshot.trigger_active:
        stats += f" · 触发词已就绪（{snapshot.trigger_ttl:.0f}s 内有效）"
    median = _percentile(snapshot.durations, 0.5)
    p95 = _percentile(snapshot.durations, 0.95)
    if median is not None and p95 is not None:
        stats += f" · 中位 {median:.1f}s / p95 {p95:.1f}s"
    lines.append(stats)
    return "\n".join(lines)


def median_of(samples: Sequence[float]) -> Optional[float]:
    """给插件侧复用的小工具（测试也用它）。"""
    values = [float(item) for item in samples]
    if not values:
        return None
    return statistics.median(values)
