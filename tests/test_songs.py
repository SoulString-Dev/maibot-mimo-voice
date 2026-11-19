"""唱歌点播：入站旁路收集、入库、点歌、限流、删除与权限。"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __bootstrap import Checker, FakeCtx, FakeLogger, load_plugin, make_plugin, temp_dir

module = load_plugin()
lib = sys.modules[f"{module.__name__}.core.song_library"]
pending_mod = sys.modules[f"{module.__name__}.core.pending"]
checker = Checker("唱歌点播")


def wav_bytes(seconds: float = 1.5, rate: int = 48000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x10" * int(rate * seconds))
    return buffer.getvalue()


def make(root: Path, **songs: object):
    plugin, config, logger = make_plugin(
        module,
        songs={"enabled": True, **songs},
        commands={"admins": ["456"]},
    )
    plugin.__dict__["_ctx"] = FakeCtx(FakeLogger(), data_dir=root)
    return plugin, config, logger


def voice_payload(audio: bytes, *, session_id: str = "s1", group_id: str = "123", user_id: str = "456") -> dict:
    return {
        "session_id": session_id,
        "scope": "group",
        "message_id": "m1",
        "raw_message": [
            {"type": "voice", "data": "", "hash": "h", "binary_data_base64": base64.b64encode(audio).decode()}
        ],
        "message_info": {
            "group_info": {"group_id": group_id},
            "user_info": {"user_id": user_id, "nickname": "小王"},
        },
    }


def collect(plugin, payload: dict) -> None:
    asyncio.run(plugin.collect_song_media(message=payload))


def command(plugin, text: str, *, user_id: str = "456", group_id: str = "123", stream_id: str = "s1") -> str:
    asyncio.run(
        plugin.handle_voice_command(
            text=text, stream_id=stream_id, group_id=group_id, user_id=user_id, platform="qq"
        )
    )
    return plugin.ctx.send.texts[-1][1] if plugin.ctx.send.texts else ""


def private(plugin, text: str, *, user_id: str = "456", stream_id: str = "p1") -> str:
    """私聊里的命令；入库 / 删除 / 改名只有私聊能用，音频可能是刚在群里发的。"""
    return command(plugin, text, user_id=user_id, group_id="", stream_id=stream_id)


# ---------------------------------------------------------------- 入站收集
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    collect(plugin, voice_payload(wav_bytes(1.5)))
    index_path = Path(root) / "pending" / "index.json"
    checker.check("暂存索引已写", index_path.exists())
    items = pending_mod.PendingPool(Path(root) / "pending").items()
    checker.equal("暂存 1 条", len(items), 1)
    checker.check("原始 48k 已降采样到 16k", items[0].size < len(wav_bytes(1.5)) // 2, str(items[0].size))
    checker.check("时长记录", abs(items[0].duration_s - 1.5) < 0.05, f"{items[0].duration_s:.2f}s")

    # 文本消息里的音频文件反而应该在下载前被内网守卫挡住（不做网络请求）
    before = len(pending_mod.PendingPool(Path(root) / "pending").items())
    collect(
        plugin,
        {
            "session_id": "s1",
            "scope": "group",
            "raw_message": [{"type": "text", "data": "[文件] a.mp3，大小: 1 KB，链接: https://10.0.0.1/a.mp3"}],
            "message_info": {"group_info": {"group_id": "123"}},
        },
    )
    checker.equal("内网地址不下载", len(pending_mod.PendingPool(Path(root) / "pending").items()), before)

# ---------------------------------------------------------------- 入库与点歌
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    collect(plugin, voice_payload(wav_bytes(1.5)))

    reply = private(plugin, "/voice sing add 雨爱")
    checker.check("入库回执", "已入库" in reply, reply)
    songs_path = Path(root) / "songs" / "songs.json"
    checker.check("曲库文件已写", songs_path.exists())
    songs, _warnings = lib.load_library(songs_path)
    checker.equal("曲库 1 首", len(songs), 1)
    checker.equal("歌名", songs[0].name, "雨爱")
    checker.check("音频落在 songs/", (Path(root) / "songs" / songs[0].file).exists())
    checker.equal("认领后暂存清空", len(pending_mod.PendingPool(Path(root) / "pending").items()), 0)

    reply = command(plugin, "/voice sing list")
    checker.check("列表包含歌名", "雨爱" in reply and "1 首" in reply, reply)

    reply = command(plugin, "/voice sing 雨爱")
    checker.equal("点歌不回执错误", plugin.ctx.send.texts[-1][1] if reply else "", reply)
    checker.equal("发出 1 条语音", len(plugin.ctx.send.hybrids), 1)
    segment = plugin.ctx.send.hybrids[-1][1][0]
    checker.equal("段类型", segment["type"], "voice")
    audio = base64.b64decode(segment["binary_data_base64"])
    checker.check("发的是入库的 wav", audio[:4] == b"RIFF")

    reply = command(plugin, "/voice sing 雨爱")
    checker.check("冷却生效", "刚点过" in reply, reply)
    plugin.__dict__["_sing_group_last"].clear()
    plugin.__dict__["_sing_user_last"].clear()

    reply = command(plugin, "/voice sing 不存在的歌")
    checker.check("找不到提示", "没有找到" in reply, reply)

    # 别名与全量替换
    reply = private(plugin, "/voice sing set 雨爱 alias 雨天,下雨")
    checker.check("改别名回执", "已更新" in reply, reply)
    plugin.__dict__["_sing_group_last"].clear()
    plugin.__dict__["_sing_user_last"].clear()
    command(plugin, "/voice sing 下雨")
    checker.equal("按别名点歌", len(plugin.ctx.send.hybrids), 2)

    plugin.__dict__["_sing_group_last"].clear()
    plugin.__dict__["_sing_user_last"].clear()
    reply = private(plugin, "/voice sing set 雨爱 tags 杨丞琳,伤感情歌")
    command(plugin, "/voice sing 杨丞琳")
    checker.equal("按标签点歌", len(plugin.ctx.send.hybrids), 3)

    # 删除只认歌名
    reply = private(plugin, "/voice sing del 下雨")
    checker.check("别名不能删", "别名" in reply, reply)
    reply = private(plugin, "/voice sing del 雨爱")
    checker.check("按歌名删除", "已删除" in reply, reply)
    songs, _warnings = lib.load_library(songs_path)
    checker.equal("删除后空库", len(songs), 0)

# ---------------------------------------------------------------- 权限与开关
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    reply = private(plugin, "/voice sing add 雨爱", user_id="999")
    checker.check("非管理员不能入库", "管理员" in reply, reply)
    reply = command(plugin, "/voice sing list", user_id="999")
    checker.check("非管理员能看列表", "曲库" in reply, reply)

    reply = command(plugin, "/voice sing 雨爱", user_id="999")
    checker.check("非管理员点歌走找不到", "没有找到" in reply, reply)

    # 入库与增删改只走私聊：群里发就跟 test / say 一样被「只在私聊」挡掉
    for text in ("/voice sing add 雨爱", "/voice sing addforce 雨爱", "/voice sing del 雨爱", "/voice sing set 雨爱 alias 雨"):
        reply = command(plugin, text)
        checker.check(f"群里拒绝 {text.split()[-1] or text}", "只在私聊里可用" in reply, reply)

with temp_dir() as root:
    plugin, config, _logger = make(Path(root), enabled=False)
    reply = command(plugin, "/voice sing list")
    checker.check("功能关闭", "没开" in reply, reply)

with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    config.filters.group_mode = "whitelist"
    config.filters.group_ids = ["999999"]
    reply = command(plugin, "/voice sing list")
    checker.check("白名单外拦截", "名单" in reply, reply)

with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    collect(plugin, voice_payload(wav_bytes(1.5)))
    private(plugin, "/voice sing add 雨爱")
    plugin.__dict__["_session_switch"]["s1"] = False
    reply = command(plugin, "/voice sing 雨爱")
    checker.check("/voice off 挡点歌", "已关闭" in reply, reply)

with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    collect(plugin, voice_payload(wav_bytes(1.5)))
    stale = Path(root) / "pending" / "index.json"
    raw = json.loads(stale.read_text(encoding="utf-8"))
    for row in raw["items"]:
        row["created_at"] -= 3600
    stale.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    reply = private(plugin, "/voice sing add 雨爱")
    checker.check("窗口过期认领不到", "没收到音频" in reply, reply)

with temp_dir() as root:
    plugin, config, _logger = make(Path(root), max_duration_seconds=5.0)
    collect(plugin, voice_payload(wav_bytes(7.0)))
    checker.equal("超长不入池", len(pending_mod.PendingPool(Path(root) / "pending").items()), 0)

# ---------------------------------------------------------------- 暂存池清理
with temp_dir() as root:
    pool = pending_mod.PendingPool(Path(root))
    pool.put(b"a" * 100, suffix=".mp3", duration_s=1.0, session_id="s1", user_id="456")
    pool.put(b"b" * 100, suffix=".mp3", duration_s=1.0, session_id="s1", user_id="456")
    removed = pool.prune(ttl_seconds=3600, max_bytes=1024 * 1024, max_entries=1)
    checker.equal("按条数清理 1 条", removed, 1)
    checker.equal("剩 1 条", len(pool.items()), 1)
    removed = pool.prune(ttl_seconds=0.0, max_bytes=1024, max_entries=10)
    checker.equal("TTL 为 0 不清理", removed, 0)
    checker.equal("总字节可查", pool.stats()["count"], 1)
    only = pool.items()[0]
    pool.discard(only)
    checker.equal("丢弃后清空", len(pool.items()), 0)
    checker.check("丢弃后文件删除", not only.path_in(Path(root)).exists())

# ---------------------------------------------------------------- 文件消息补链接
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    history = {
        "messages": [
            {"message": [{"type": "text", "data": "随便聊聊"}]},
            {
                "message": [
                    {
                        "type": "file",
                        "data": {"file": "雨爱.mp3", "file_size": "5594426", "file_id": "FID-1"},
                    }
                ]
            },
        ]
    }
    calls = []

    async def fake_call(api_name: str, params):
        calls.append((api_name, params))
        if api_name.endswith("get_friend_msg_history"):
            return {"messages": history["messages"]}
        if api_name.endswith("get_private_file_url"):
            return {"url": "http://1.2.3.4:80/qqdownloadftnv5?rkey=abc"}
        return None

    plugin._call_adapter_api = fake_call
    url = asyncio.run(
        plugin._resolve_file_url("[文件] 雨爱.mp3，大小: 5594426", group_id="", user_id="456")
    )
    checker.equal("补链接成功", url, "http://1.2.3.4:80/qqdownloadftnv5?rkey=abc")
    checker.equal("先查历史再换链", [name.split(".")[-1] for name, _args in calls], ["get_friend_msg_history", "get_private_file_url"])
    checker.equal("历史查询带 user_id", str(calls[0][1].get("user_id")), "456")

    # 群聊走群历史 + 群文件链接
    calls.clear()
    group_history = {"messages": [{"message": [{"type": "file", "data": {"file": "雨爱.mp3", "file_size": "5594426", "file_id": "FID-2"}}]}]}

    async def fake_group_call(api_name: str, params):
        calls.append((api_name, params))
        if api_name.endswith("get_group_msg_history"):
            return group_history
        if api_name.endswith("get_group_file_url"):
            return {"url": "http://5.6.7.8:80/file?rkey=xyz"}
        return None

    plugin._call_adapter_api = fake_group_call
    url = asyncio.run(
        plugin._resolve_file_url("[文件] 雨爱.mp3，大小: 5594426", group_id="123", user_id="456")
    )
    checker.equal("群文件补链接", url, "http://5.6.7.8:80/file?rkey=xyz")
    checker.check("群历史带群号", str(calls[0][1].get("group_id")) == "123", str(calls[0][1]))
    checker.check("群文件地址带群号", str(calls[1][1].get("group_id")) == "123", str(calls[1][1]))

    # 名字/大小对不上就不认领
    checker.equal("名字不符不回退", plugin._pick_file_id(history, name="别的.mp3", size=0), "")
    checker.equal("大小不符不认领", plugin._pick_file_id(history, name="雨爱.mp3", size=1), "")

    # 开关关掉时不再问适配器
    calls.clear()
    config.songs.lookup_file_url = False
    url = asyncio.run(
        plugin._resolve_file_url("[文件] 雨爱.mp3，大小: 5594426", group_id="", user_id="456")
    )
    checker.equal("关掉补链接开关", url, "")
    checker.equal("关掉后不调 API", calls, [])

    # 非音频文件名不去查历史
    calls.clear()
    config.songs.lookup_file_url = True
    url = asyncio.run(plugin._resolve_file_url("[文件] 文档.pdf，大小: 100", group_id="", user_id="456"))
    checker.equal("非音频不查历史", url, "")
    checker.equal("非音频零调用", len(calls), 0)

sys.exit(checker.report())
