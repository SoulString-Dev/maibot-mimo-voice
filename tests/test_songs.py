"""唱歌点播：入站旁路收集、入库、点歌、限流、删除与权限。"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import time
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


def voice_segments(plugin) -> list:
    return [seg for _sid, segs in plugin.ctx.send.hybrids for seg in segs if seg.get("type") == "voice"]


def collect(plugin, payload: dict) -> None:
    asyncio.run(plugin.collect_song_media(message=payload))


def command(plugin, text: str, *, user_id: str = "456", group_id: str = "123", stream_id: str = "s1") -> str:
    asyncio.run(
        plugin.handle_voice_command(
            text=text, stream_id=stream_id, group_id=group_id, user_id=user_id, platform="qq"
        )
    )
    return plugin.ctx.send.texts[-1][1] if plugin.ctx.send.texts else ""


def private(plugin, text: str, *, user_id: str = "456", stream_id: str = "s1") -> str:
    """私聊里的命令；入库 / 删除 / 改名只有私聊能用，音频必须在这个会话里发过。"""
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

    texts_before_list = len(plugin.ctx.send.texts)
    command(plugin, "/voice sing list")
    forward = plugin.ctx.send.hybrids[-1][1]
    checker.equal("清单走合并转发", forward[0]["type"], "forward")
    nodes = forward[0]["data"]
    joined = "\n".join(node["content"][0]["data"] for node in nodes)
    checker.check("转发里有歌名与总数", "雨爱" in joined and "1 首" in joined, joined)
    checker.equal("清单不额外发文字", len(plugin.ctx.send.texts), texts_before_list)

    reply = command(plugin, "/voice sing 雨爱")
    checker.equal("点歌不回执错误", plugin.ctx.send.texts[-1][1] if reply else "", reply)
    checker.equal("发出 1 条语音", len(voice_segments(plugin)), 1)
    segment = voice_segments(plugin)[-1]
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
    checker.equal("按别名点歌", len(voice_segments(plugin)), 2)

    plugin.__dict__["_sing_group_last"].clear()
    plugin.__dict__["_sing_user_last"].clear()
    reply = private(plugin, "/voice sing set 雨爱 tags 杨丞琳,伤感情歌")
    command(plugin, "/voice sing 杨丞琳")
    checker.equal("按标签点歌", len(voice_segments(plugin)), 3)

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

# ---------------------------------------------------------------- api.call 返回拆包
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    envelope = {"status": "ok", "retcode": 0, "data": {"messages": [{"message": []}]}}
    checker.equal("拆 NapCat 信封", plugin._unwrap_adapter_response(envelope, "x"), {"messages": [{"message": []}]})
    wrapped = {"success": True, "result": envelope}
    checker.equal("拆宿主 + 信封两层", plugin._unwrap_adapter_response(wrapped, "y"), {"messages": [{"message": []}]})
    denied = {"success": False, "error": "插件 xxx 未获授权能力: api.call"}
    checker.equal("拒绝时返回 None", plugin._unwrap_adapter_response(denied, "z"), None)
    checker.equal("无法识别返回 None", plugin._unwrap_adapter_response("oops", "w"), None)

# ---------------------------------------------------------------- add 的历史兜底
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    history = {
        "messages": [
            {
                "user_id": "456",
                "sender": {"nickname": "Miu"},
                "message_id": "m1",
                "time": time.time(),
                "message": [
                    {"type": "file", "data": {"file": "雨爱.mp3", "file_size": "1234", "file_id": "FID-9"}}
                ],
            }
        ]
    }

    async def fake_call(api_name: str, params):
        if api_name.endswith("get_friend_msg_history"):
            return history
        if api_name.endswith("get_private_file_url"):
            return {"url": "http://1.2.3.4:80/a.mp3"}
        return None

    async def fake_download(url: str, *, text_source: bool = False) -> bytes:
        return wav_bytes(1.0)

    plugin._call_adapter_api = fake_call
    plugin._download_media = fake_download
    reply = private(plugin, "/voice sing add 雨爱")
    checker.check("历史兜底入库", "已入库" in reply, reply)
    songs, _warnings = lib.load_library(Path(root) / "songs" / "songs.json")
    checker.equal("兜底后曲库 1 首", len(songs), 1)

# ---------------------------------------------------------------- 认领后的重复收集
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    collect(plugin, voice_payload(wav_bytes(1.5)))
    private(plugin, "/voice sing add 雨爱")
    checker.equal("入库后暂存清空", len(pending_mod.PendingPool(Path(root) / "pending").items()), 0)
    collect(plugin, voice_payload(wav_bytes(1.5)))  # 同一条消息的第二次钩子
    checker.equal("认领过的音频不再暂存", len(pending_mod.PendingPool(Path(root) / "pending").items()), 0)

# ---------------------------------------------------------------- 跨会话不认领（写死）
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    collect(plugin, voice_payload(wav_bytes(1.5), session_id="s1"))  # 群里发的
    reply = private(plugin, "/voice sing add 雨爱", stream_id="p2")  # 去私聊入库
    checker.check("跨会话认领被拒", "没收到音频" in reply, reply)
    songs_path = Path(root) / "songs" / "songs.json"
    checker.check("跨会话没入库", not songs_path.exists())

# ---------------------------------------------------------------- P0-A 去重键带 message_id
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    collect(plugin, voice_payload(wav_bytes(1.5)))  # message_id=m1
    private(plugin, "/voice sing add 雨爱")
    collect(plugin, voice_payload(wav_bytes(1.5)))  # 同一条消息的重复投递，仍挡
    checker.equal("同一条消息重复投递不再暂存", len(pending_mod.PendingPool(Path(root) / "pending").items()), 0)

    resent = voice_payload(wav_bytes(1.5))
    resent["message_id"] = "m2"  # 10 分钟内真的又发了一次同一首
    collect(plugin, resent)
    checker.equal("不同消息的同一首可以重新暂存", len(pending_mod.PendingPool(Path(root) / "pending").items()), 1)
    reply = private(plugin, "/voice sing addforce 雨爱")
    checker.check("重发后 addforce 成功", "已替换" in reply, reply)

# ---------------------------------------------------------------- P0-B 文本链接只认 QQ 域名
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    media = sys.modules[f"{module.__name__}.core.song_media"]
    checker.check("QQ CDN 域名放行", media.is_qq_media_url("https://multimedia.nt.qq.com.cn/x/a.mp3"))
    checker.check("QQ 文件 CDN 放行", media.is_qq_media_url("http://113.240.75.246:80/qqdownloadftnv5?fileid=1"))
    checker.check("普通公网域名拒绝", not media.is_qq_media_url("https://evil.example.com/a.mp3"))
    checker.check("非 /qqdownload 的 IP 链接拒绝", not media.is_qq_media_url("http://113.240.75.246:80/other/a.mp3"))

    collect(
        plugin,
        {
            "session_id": "s1",
            "scope": "group",
            "raw_message": [{"type": "text", "data": "[文件] a.mp3，大小: 1 KB，链接: https://evil.example.com/a.mp3"}],
            "message_info": {"group_info": {"group_id": "123"}},
        },
    )
    checker.equal("伪造的非 QQ 链接不暂存", len(pending_mod.PendingPool(Path(root) / "pending").items()), 0)

# ---------------------------------------------------------------- P1-A 曲库总量上限
with temp_dir() as root:
    plugin, config, _logger = make(Path(root), max_total_mb=10.0)
    songs_root = Path(root) / "songs"
    songs_root.mkdir(parents=True, exist_ok=True)
    (songs_root / "placeholder.mp3").write_bytes(b"\0" * (10 * 1024 * 1024 + 1024))
    collect(plugin, voice_payload(wav_bytes(1.5)))
    reply = private(plugin, "/voice sing add 雨爱")
    checker.check("曲库满了拒绝入库", "曲库已占" in reply, reply)
    checker.check("拒绝后曲库为空", not (songs_root / "songs.json").exists())

# ---------------------------------------------------------------- P1-B 曲库锁
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    checker.check("曲库锁已创建", isinstance(plugin._songs_lock, asyncio.Lock))

# ---------------------------------------------------------------- P1-C 后台维护
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    songs_root = Path(root) / "songs"
    trash = songs_root / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    old = trash / "old.mp3"
    old.write_bytes(b"x")
    stamp = time.time() - 8 * 86400
    import os

    os.utime(old, (stamp, stamp))
    fresh = trash / "fresh.mp3"
    fresh.write_bytes(b"x")
    removed = plugin._purge_trash(songs_root)
    checker.equal("回收站删掉 1 个旧文件", removed, 1)
    checker.check("旧文件已删", not old.exists())
    checker.check("新文件保留", fresh.exists())

    checker.equal(
        "曲库占用只算音频", plugin._songs_total_bytes(songs_root), 0
    )  # 只有 .trash 里的文件

    collect(plugin, voice_payload(wav_bytes(1.5)))
    index = Path(root) / "pending" / "index.json"
    data = json.loads(index.read_text(encoding="utf-8"))
    data["items"][0]["created_at"] = time.time() - 7200
    index.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    asyncio.run(plugin._song_housekeeping_once())
    checker.equal("维护任务清掉过期暂存", len(pending_mod.PendingPool(Path(root) / "pending").items()), 0)

# ---------------------------------------------------------------- 清单转发失败回退文本
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    collect(plugin, voice_payload(wav_bytes(1.5)))
    private(plugin, "/voice sing add 雨爱")

    async def boom(segments, stream_id, **kwargs):
        raise RuntimeError("适配器不支持合并转发")

    plugin.ctx.send.hybrid = boom
    before = len(plugin.ctx.send.texts)
    command(plugin, "/voice sing list")
    checker.equal("转发失败补一条文字", len(plugin.ctx.send.texts), before + 1)
    checker.check("回退清单含歌名", "雨爱" in plugin.ctx.send.texts[-1][1], plugin.ctx.send.texts[-1][1])

# ---------------------------------------------------------------- 下载链接校验
with temp_dir() as root:
    plugin, config, _logger = make(Path(root))
    checker.equal(
        "公网 IP 链接通过",
        asyncio.run(plugin._media_url_problem("http://1.2.3.4:80/a.mp3", text_source=False)),
        "",
    )
    checker.equal(
        "内网 IP 链接拒绝",
        asyncio.run(plugin._media_url_problem("http://10.0.0.1/a.mp3", text_source=False)),
        "private-host",
    )
    checker.equal(
        "非 QQ 文本链接拒绝",
        asyncio.run(plugin._media_url_problem("http://1.2.3.4:80/other/a.mp3", text_source=True)),
        "text-url",
    )
    plugin._host_resolves_public = staticmethod(lambda host: False)
    checker.equal(
        "域名解析到内网拒绝",
        asyncio.run(plugin._media_url_problem("http://example.com/a.mp3", text_source=False)),
        "private-dns",
    )

sys.exit(checker.report())
