"""意图层：正则编译、命中判定、候选注入与点歌工具。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __bootstrap import Checker, FakeCtx, FakeLogger, load_plugin, make_plugin, temp_dir

module = load_plugin()
intent = sys.modules[f"{module.__name__}.core.song_intent"]
lib = sys.modules[f"{module.__name__}.core.song_library"]
checker = Checker("意图层")


def seed_library(root: Path, songs: list[tuple[str, list[str], list[str]]]) -> Path:
    """在 root/songs 下造几首歌：文件名用歌名拼音化，内容随便（只验证路径拼装）。"""
    songs_dir = root / "songs"
    songs_dir.mkdir(parents=True, exist_ok=True)
    library = []
    for index, (name, aliases, tags) in enumerate(songs):
        file_name = f"{index:02d}.wav"
        (songs_dir / file_name).write_bytes(b"RIFF" + b"\x00" * 32)
        song, error = lib.add_song(
            library,
            name,
            file_name=file_name,
            duration_s=120.0,
            added_by="456",
            now=1732000000.0 + index,
            max_songs=50,
            force=False,
        )
        assert song is not None, error
        song.aliases = aliases
        song.tags = tags
    lib.save_library(songs_dir / "songs.json", library)
    return songs_dir / "songs.json"


def make(root: Path, **songs: object):
    plugin, config, logger = make_plugin(
        module,
        songs={"enabled": True, **songs},
        commands={"admins": ["456"]},
    )
    plugin.__dict__["_ctx"] = FakeCtx(FakeLogger(), data_dir=root)
    return plugin, config, logger


def cue(plugin, text: str, *, session_id: str = "s1", scope: str = "group", group_id: str = "123",
        user_id: str = "456", is_at: bool = False, is_mentioned: bool = False) -> None:
    payload = {
        "session_id": session_id,
        "scope": scope,
        "processed_plain_text": text,
        "is_at": is_at,
        "is_mentioned": is_mentioned,
        "message_info": {
            "group_info": {"group_id": group_id},
            "user_info": {"user_id": user_id},
        },
    }
    asyncio.run(plugin.remember_song_cue(message=payload))


def inject(plugin, *, session_id: str = "s1", items: list | None = None) -> dict | None:
    kwargs = {"session_id": session_id, "items": items if items is not None else []}
    result = asyncio.run(plugin.inject_song_intent(**kwargs))
    return result


def note_of(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    items = result.get("modified_kwargs", {}).get("items") or []
    texts = [
        str(part.get("text") or "")
        for item in items
        for part in item.get("parts") or []
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "\n".join(texts)


# ------------------------------------------------------------ 纯文本逻辑
patterns, warnings = intent.compile_patterns(["(?:唱|来|放|点)一?(?:首|个)"])
checker.equal("默认正则编译成功", len(patterns), 1)
checker.equal("默认正则无告警", warnings, [])

patterns, warnings = intent.compile_patterns(["唱([一]?)首", "([", ""])
checker.equal("坏正则跳过", len(patterns), 1)
checker.check("坏正则告警", len(warnings) == 1 and "编译失败" in warnings[0], str(warnings))

patterns, warnings = intent.compile_patterns(["([", "a{2,1}"])
checker.equal("全坏时退回默认", len(patterns), 1)
checker.check("退回默认有告警", any("默认正则" in item for item in warnings), str(warnings))

checker.equal("去掉开头 @", intent.strip_mentions("@麦麦 唱一首雨爱"), "唱一首雨爱")
checker.equal("去掉多个 @", intent.strip_mentions("@a @b 来一个"), "来一个")

defaults, _ = intent.compile_patterns(intent.DEFAULT_PATTERNS)
hit = intent.match_intent("唱一首雨爱", defaults)
checker.check("命中且取出歌名", hit is not None and hit.keyword == "雨爱", str(hit))
hit = intent.match_intent("@麦麦 来一个", defaults)
checker.check("签到语也算命中、歌名为空", hit is not None and hit.keyword == "", str(hit))
hit = intent.match_intent("雨爱来一个", defaults)
checker.check("歌名在前也能认", hit is not None and hit.keyword == "雨爱", str(hit))
hit = intent.match_intent("放首《雨爱》", defaults)
checker.check("书名号剥掉", hit is not None and hit.keyword == "雨爱", str(hit))
checker.check("普通聊天不命中", intent.match_intent("今天天气不错", defaults) is None)

note = intent.build_hint(["雨爱 · 2:20 · 别名 司凤"], "雨爱", 5)
checker.check("提示里有候选", "1. 雨爱" in note, note)
checker.check("提示里点名工具", "mimo_voice_play_song" in note, note)

items = [{"item_type": "SystemMessageItem", "meta": {}, "parts": []}]
checker.check("追加到已有 system 消息", intent.append_hint(items, "提示", item_id="x", timestamp="t"))
checker.equal("system 只有一条", len(items), 1)
checker.equal("parts 已追加", items[0]["parts"][0]["text"], "提示")

items = [{"item_type": "UserMessageItem", "meta": {}, "parts": []}]
checker.check("没有 system 消息时新建", intent.append_hint(items, "提示", item_id="x", timestamp="t"))
checker.equal("新条目插在最前", items[0]["item_type"], "SystemMessageItem")

# ------------------------------------------------------------ 注入门控
with temp_dir() as root:
    root = Path(root)
    seed_library(root, [("雨爱", ["司凤"], []), ("晴天", [], ["周杰伦"])])
    plugin, config, _logger = make(root)

    cue(plugin, "唱一首雨爱", is_at=False, is_mentioned=False)
    checker.check("群里没 @ 不注入", inject(plugin) is None)

    cue(plugin, "唱一首雨爱", is_at=True)
    result = inject(plugin)
    note = note_of(result)
    checker.check("群里 @ 后注入", "点歌提示" in note, note)
    checker.check("候选里点名到的那首", "雨爱" in note, note)
    checker.check("提示里有关键词", "雨爱" in note, note)
    checker.check("线索消费后不再注入", inject(plugin) is None)

    cue(plugin, "来一个", session_id="s1", is_mentioned=True)
    note = note_of(inject(plugin, session_id="s2"))
    checker.check("会话不匹配不注入", note == "", note)

    cue(plugin, "今天天气不错", is_at=True)
    checker.check("命中不了正则不注入", inject(plugin) is None)

    cue(plugin, "放一首晴天", session_id="p1", scope="private", group_id="")
    note = note_of(inject(plugin, session_id="p1"))
    checker.check("私聊不用 @ 也能注入", "晴天" in note, note)

    cue(plugin, "唱一首晴天", is_at=True)
    checker.check("30 秒内不重复注入", inject(plugin) is None)

with temp_dir() as root:
    root = Path(root)
    plugin, config, _logger = make(root)  # 曲库空
    cue(plugin, "唱一首雨爱", is_at=True)
    checker.check("曲库为空不注入", inject(plugin) is None)

with temp_dir() as root:
    root = Path(root)
    seed_library(root, [("雨爱", [], [])])
    plugin, config, _logger = make(root, require_mention=False)
    cue(plugin, "唱一首雨爱", is_at=False, is_mentioned=False)
    note = note_of(inject(plugin))
    checker.check("关掉 require_mention 后不 @ 也注入", "雨爱" in note, note)

# ------------------------------------------------------------ 点歌工具
with temp_dir() as root:
    root = Path(root)
    seed_library(root, [("雨爱", ["司凤"], []), ("晴天", [], [])])
    plugin, config, _logger = make(root)
    result = asyncio.run(plugin.play_song(stream_id="s1", name="雨爱"))
    checker.check("工具点歌成功", result.get("success") is True, str(result))
    checker.equal("发出 1 条语音", len(plugin.ctx.send.hybrids), 1)
    segment = plugin.ctx.send.hybrids[-1][1][0]
    checker.equal("发的是 voice 段", segment.get("type"), "voice")

    plugin.config.songs.sing_group_cooldown_seconds = 0.0
    result = asyncio.run(plugin.play_song(stream_id="s1", name="司凤"))
    checker.check("按别名也能点", result.get("success") is True, str(result))

    result = asyncio.run(plugin.play_song(stream_id="s1", name="不存在的歌"))
    checker.check("曲库没有时不发语音", result.get("success") is False, str(result))
    checker.equal("语音条数量没变", len(plugin.ctx.send.hybrids), 2)

# 工具可见性：曲库空时隐藏
with temp_dir() as root:
    root = Path(root)
    plugin, config, _logger = make(root)
    definitions = [
        {"function": {"name": "mimo_voice_play_song"}},
        {"function": {"name": "mimo_voice_text_reply"}},
    ]
    result = asyncio.run(plugin.configure_planner_tools(tool_definitions=definitions))
    names = [item["function"]["name"] for item in result["modified_kwargs"]["tool_definitions"]]
    checker.check("空曲库隐藏播放工具", "mimo_voice_play_song" not in names, str(names))

with temp_dir() as root:
    root = Path(root)
    seed_library(root, [("雨爱", [], [])])
    plugin, config, _logger = make(root)
    definitions = [
        {"function": {"name": "mimo_voice_play_song"}},
        {"function": {"name": "mimo_voice_text_reply"}},
    ]
    result = asyncio.run(plugin.configure_planner_tools(tool_definitions=definitions))
    names = [item["function"]["name"] for item in result["modified_kwargs"]["tool_definitions"]]
    checker.check("有歌时保留播放工具", "mimo_voice_play_song" in names, str(names))

sys.exit(checker.report())
