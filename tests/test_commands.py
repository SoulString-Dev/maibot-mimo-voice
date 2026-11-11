"""命令层测试：解析、权限、文案、五个命令的行为（全部走假的 ctx）。"""

from __future__ import annotations

import asyncio
import io
import wave

from __bootstrap import Checker, load_plugin, make_plugin

ADMIN = "10000002"
PEER = "10000003"


def wav_bytes(seconds: float = 1.0, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def call(plugin, text: str, *, group_id: str = "", user_id: str = ADMIN, stream_id: str = "s1"):
    return asyncio.run(
        plugin.handle_voice_command(
            text=text,
            stream_id=stream_id,
            group_id=group_id,
            user_id=user_id,
            platform="qq",
        )
    )


def stub_synthesis(plugin, audio: bytes) -> None:
    async def fake(text: str) -> bytes:
        return audio

    plugin.__dict__["_synthesize_guarded"] = fake
    plugin.__dict__["_postprocess"] = lambda data: data


def make_ready(module, **overrides):
    base = {"admins": [ADMIN]}
    base.update(overrides.pop("commands", {}))
    plugin, config, logger = make_plugin(module, commands=base, **overrides)
    return plugin, config, logger


def test_build(module) -> int:
    check = Checker("命令层：解析 / 权限 / 目标")
    commands = __import__(f"{module.__name__}.core.commands", fromlist=["x"])

    check.equal("on 子命令", commands.parse_command("/voice on"), ("on", ""))
    check.equal("vc 别名 + 参数", commands.parse_command("/vC TEST 你好"), ("test", "你好"))
    check.equal("只有命令名", commands.parse_command("/voice"), ("", ""))
    check.equal("全角斜杠", commands.parse_command("／voice status"), ("status", ""))
    check.equal("多余空格", commands.parse_command("  /vc   say   g123   你好 世界 "), ("say", "g123   你好 世界"))
    check.equal("别的命令", commands.parse_command("/voicex on"), None)
    check.equal("不是命令", commands.parse_command("今天天气不错"), None)
    check.equal("旧别名不支持", commands.parse_command("/语音 on"), None)

    check.check("纯 ID 命中", commands.admin_matches(ADMIN, "qq", [ADMIN]))
    check.check("scoped 命中", commands.admin_matches(ADMIN, "qq", [f"qq:{ADMIN}"]))
    check.check("scoped 平台不对不命中", not commands.admin_matches(ADMIN, "tg", [f"qq:{ADMIN}"]))
    check.check("空列表不命中", not commands.admin_matches(ADMIN, "qq", []))
    check.check("空 user_id 不命中", not commands.admin_matches("", "qq", [ADMIN]))

    check.equal("无前缀目标", commands.parse_target("123456"), (None, "123456"))
    check.equal("g 前缀", commands.parse_target("g123456"), ("group", "123456"))
    check.equal("u 前缀", commands.parse_target("U10000002"), ("private", "10000002"))
    check.check("管理员纯 ID 合法", commands.admin_entry_problem("10000002") is None)
    check.check("管理员 scoped 合法", commands.admin_entry_problem("qq:10000002") is None)
    check.check("管理员写成昵称被拦", commands.admin_entry_problem("麦麦") is not None)
    check.check("管理员带空格被拦", commands.admin_entry_problem("1000 0002") is not None)
    check.check("管理员空条目被拦", commands.admin_entry_problem("  ") is not None)
    check.equal("拆目标与文本", commands.split_target_and_text("g123 你好 世界"), ("g123", "你好 世界"))
    return check.report()


def test_handler(module) -> int:
    check = Checker("命令层：handler 行为")
    plugin, _config, _logger = make_ready(module)

    # 非私聊：群里直接拒绝，且不合成
    _ok, _response, intercept = call(plugin, "/voice status", group_id="123456", stream_id="g1")
    check.equal("群聊 intercept", intercept, 1)
    check.check("群聊提示去私聊", "私聊" in plugin.ctx.send.texts[-1][1])
    check.equal("群聊不发语音", len(plugin.ctx.send.hybrids), 0)

    # 非管理员
    call(plugin, "/voice status", user_id=PEER)
    check.check("非管理员被拒", "没有权限" in plugin.ctx.send.texts[-1][1])

    # 未配置管理员
    bare, _, _ = make_plugin(module)
    call(bare, "/voice help")
    check.check("未配置管理员提示", "还没有配置管理员" in bare.ctx.send.texts[-1][1])

    # help
    call(plugin, "/voice help")
    help_text = plugin.ctx.send.texts[-1][1]
    check.check("help 列出 test", "/voice test" in help_text)
    check.check("help 列出 say", "/voice say" in help_text)

    # 未知子命令 / 空命令
    call(plugin, "/vc nope")
    check.check("未知子命令回 help", "不认识的子命令" in plugin.ctx.send.texts[-1][1])
    call(plugin, "/voice")
    check.check("空子命令回 help", "MiMo 语音命令" in plugin.ctx.send.texts[-1][1])

    # on / off 幂等
    call(plugin, "/voice on")
    check.check("开启回执", "语音已开启" in plugin.ctx.send.texts[-1][1])
    check.equal("开关写入", plugin._session_switch.get("s1"), True)
    call(plugin, "/voice on")
    check.check("重复开启幂等", "本来就是开启" in plugin.ctx.send.texts[-1][1])
    call(plugin, "/voice off")
    check.check("关闭回执", "语音已关闭" in plugin.ctx.send.texts[-1][1])
    converted, reason, _ = plugin._should_convert("你好", "s1", "private", "", ADMIN)
    check.check("关闭后不再转语音", not converted and reason == "off", reason)
    call(plugin, "/voice off")
    check.check("重复关闭幂等", "本来就是关闭" in plugin.ctx.send.texts[-1][1])

    # status
    call(plugin, "/voice status")
    status_text = plugin.ctx.send.texts[-1][1]
    check.check("status 带版本", "v0.6.0" in status_text)
    check.check("status 带超时", "超时 25s" in status_text)
    check.check("status 带会话标签", "QQ 10000002" in status_text)

    plugin._synth_stats["failed"] = 3
    plugin._synth_failures = {"429": 2, "network": 1}
    call(plugin, "/voice status")
    status_text = plugin.ctx.send.texts[-1][1]
    check.check("status 带失败类型分布", "失败 3：429×2、network×1" in status_text, status_text)
    plugin._synth_stats["failed"] = 0
    plugin._synth_failures = {}

    mixed, _, mixed_log = make_ready(module, commands={"admins": ["麦麦", ADMIN]})
    mixed._check_admin_entries()
    check.check(
        "admins 里写错的条目有告警",
        any("麦麦" in text and "不可用" in text for _level, text in mixed_log.records),
        mixed_log.records,
    )
    broken, _, broken_log = make_ready(module, commands={"admins": ["麦麦"]})
    broken._check_admin_entries()
    check.check(
        "admins 全都不合法时给总告警",
        any("没有可用的账号" in text for _level, text in broken_log.records),
        broken_log.records,
    )

    # test：空文本 / 超长 / 成功 / 忙
    call(plugin, "/voice test")
    check.check("test 空文本回用法", "用法" in plugin.ctx.send.texts[-1][1])
    short_limit, _, _ = make_ready(module, commands={"test_max_chars": 5})
    call(short_limit, "/voice test 这是一段明显超过上限的文本内容")
    check.check("test 超长直接拒绝", "文本太长" in short_limit.ctx.send.texts[-1][1])

    stub_synthesis(plugin, wav_bytes(1.0))
    call(plugin, "/voice test 你好呀")
    check.equal("test 发出一条语音", len(plugin.ctx.send.hybrids), 1)
    segments = plugin.ctx.send.hybrids[-1][1]
    check.equal("语音段类型", segments[0].get("type"), "voice")
    check.check("语音段带 base64", bool(segments[0].get("binary_data_base64")))

    busy = module._SynthesisBusy("已有语音正在合成")

    async def busy_synth(text: str) -> bytes:
        raise busy

    plugin.__dict__["_synthesize_guarded"] = busy_synth
    call(plugin, "/voice test 再试一次")
    check.check("test 忙时提示稍后", "正在合成" in plugin.ctx.send.texts[-1][1])

    # say：发给自己 / 白名单 / 群 / 新建会话
    call(plugin, "/voice say " + ADMIN + " 你好")
    check.check("say 发给自己提示用 test", "/voice test" in plugin.ctx.send.texts[-1][1])

    stub_synthesis(plugin, wav_bytes(2.0))
    plugin.ctx.chat.groups["123456"] = "g1"
    call(plugin, "/voice say g123456 你好")
    check.equal("say 发到已有群流", plugin.ctx.send.hybrids[-1][0], "g1")
    check.check("say 成功回执", "已发送到群 123456" in plugin.ctx.send.texts[-1][1])
    check.check("say 回执带时长", "语音 2.0s" in plugin.ctx.send.texts[-1][1])

    call(plugin, "/voice say 777777 你好")
    check.check("say 新建会话", ("group", "777777", "qq") in plugin.ctx.chat.opened)
    check.equal("say 发到新建流", plugin.ctx.send.hybrids[-1][0], "group:777777")

    plugin.ctx.chat.can_open = False
    call(plugin, "/voice say 123 你好")
    check.check("say 目标不存在时报错", "找不到目标" in plugin.ctx.send.texts[-1][1])

    # say 在群里被拒
    before = len(plugin.ctx.send.hybrids)
    call(plugin, "/voice say g123456 你好", group_id="123456", stream_id="g1")
    check.equal("群里用 say 不发语音", len(plugin.ctx.send.hybrids), before)

    # 命令关闭时按普通消息处理
    off_plugin, _off_config, _ = make_ready(module, commands={"enabled": False})
    _ok, _response, intercept = call(off_plugin, "/voice status")
    check.equal("禁用时 intercept 为 0", intercept, 0)
    check.equal("禁用时不回执", len(off_plugin.ctx.send.texts), 0)
    return check.report()


def run(module) -> int:
    return test_build(module) + test_handler(module)


if __name__ == "__main__":
    raise SystemExit(run(load_plugin()))
