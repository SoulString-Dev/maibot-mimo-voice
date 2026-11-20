"""曲库纯逻辑：归一化、切分、命令解析、唯一性、匹配与增删改。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __bootstrap import Checker, load_plugin, temp_dir

module = load_plugin()
lib = sys.modules[f"{module.__name__}.core.song_library"]
checker = Checker("曲库纯逻辑")
songs: list = []


def add(name: str, *, file_name: str = "a.mp3", duration: float = 90.0, force: bool = False, max_songs: int = 50):
    return lib.add_song(
        songs,
        name,
        file_name=file_name,
        duration_s=duration,
        added_by="456",
        now=time.time(),
        max_songs=max_songs,
        force=force,
    )


# ---------------------------------------------------------------- 归一化与切分
checker.equal("全角转半角+折叠空白+小写", lib.normalize_key("　ＹＵ　Ａｉ "), "yu ai")
checker.equal("空值归一", lib.normalize_key(None), "")
checker.equal("全角逗号切分", lib.split_values("雨天，雨爱"), ["雨天", "雨爱"])
checker.equal("顿号切分", lib.split_values("雨天、雨爱"), ["雨天", "雨爱"])
checker.equal("空白切分", lib.split_values(" 雨天   雨爱 "), ["雨天", "雨爱"])
checker.equal("去重保序", lib.split_values("雨爱,雨爱，雨天"), ["雨爱", "雨天"])
checker.check("扩展名判断", lib.has_audio_extension("x.MP3") and not lib.has_audio_extension("x.txt"))

# ------------------------------------------------------------------ 命令解析
checker.equal("list", lib.parse_sing("list"), ("list", "", "", ""))
checker.equal("点歌", lib.parse_sing("雨爱"), ("sing", "雨爱", "", ""))
checker.equal("add", lib.parse_sing("add 雨爱"), ("add", "雨爱", "", ""))
checker.equal("addforce", lib.parse_sing("addforce 雨爱"), ("addforce", "雨爱", "", ""))
checker.equal("del", lib.parse_sing("del 雨爱"), ("del", "雨爱", "", ""))
checker.equal("set", lib.parse_sing("set 雨爱 alias 雨天,下雨"), ("set", "雨爱", "alias", "雨天,下雨"))
checker.equal("空参数", lib.parse_sing("  "), ("sing", "", "", ""))

# -------------------------------------------------------------- 新增与唯一性
song, error = add("雨爱")
checker.check("新增成功", song is not None and not error, error)
checker.equal("入列", len(songs), 1)
checker.equal("id 由名字生成", song.id.startswith("s"), True)
again, error = add("雨爱")
checker.check("同名被拒", again is None and "已经用了" in error, error)
forced, error = add("雨爱", file_name="b.mp3", duration=100.0, force=True)
checker.check("addforce 覆盖", forced is not None and not error and forced.file == "b.mp3", error)
checker.equal("覆盖不新增", len(songs), 1)
checker.equal("覆盖换时长", round(songs[0].duration_s, 1), 100.0)
reserved, error = add("list")
checker.check("保留字被拒", reserved is None and "关键字" in error)
bad, error = add("文本歌", file_name="a.txt")
checker.check("非音频被拒", bad is None and "mp3" in error)
full, error = add("第二首", max_songs=1)
checker.check("满库被拒", full is None and "上限" in error, error)

# ------------------------------------------------------ 别名 / 标签 / 匹配
target = songs[0]
target.aliases = ["雨天", "杨丞琳经典"]
target.tags = ["杨丞琳", "伤感情歌"]
checker.equal("按别名匹配", [item.name for item in lib.match_songs(songs, "雨天")], ["雨爱"])
checker.equal("按标签匹配", [item.name for item in lib.match_songs(songs, "杨丞琳")], ["雨爱"])
checker.equal("子串匹配", [item.name for item in lib.match_songs(songs, "杨丞")], ["雨爱"])
checker.equal("无命中", lib.match_songs(songs, "不存在"), [])
checker.equal("空查询", lib.match_songs(songs, "  "), [])
other, _error = add("雨天", file_name="c.mp3")
checker.check("别名不唯一被拒", other is None)

# ------------------------------------------------------------ 改名与全量替换
ok, error = lib.set_song_field(songs, "雨爱", "alias", "雨天,下雨")
checker.check("别名全量替换", ok is not None and not error, error)
checker.equal("别名结果", songs[0].aliases, ["雨天", "下雨"])
lib.set_song_field(songs, "雨爱", "tags", "杨丞琳,翻唱")
checker.equal("标签替换", songs[0].tags, ["杨丞琳", "翻唱"])
bad, error = lib.set_song_field(songs, "雨爱", "alias", "   ")
checker.check("清空别名", bad is not None and bad.aliases == [], error)
renamed, error = lib.set_song_field(songs, "雨爱", "name", "雨爱2")
checker.check("改名成功", renamed is not None and renamed.name == "雨爱2", error)
checker.equal("改名不改 id", renamed.id, song.id)
dup, error = add("雨爱2")
checker.check("改名后占用新名字", dup is None and "已经用了" in error, error)
bad, error = lib.set_song_field(songs, "雨爱2", "pitch", "3")
checker.check("未知字段被拒", bad is None and "name" in error, error)
bad, error = lib.set_song_field(songs, "不存在", "alias", "x")
checker.check("改不存在的歌", bad is None and "没有叫" in error, error)

# ---------------------------------------------------------------- 删除规则
lib.set_song_field(songs, "雨爱2", "alias", "雨天")
gone, error = lib.delete_song(songs, "雨天")
checker.check("别名不能删", gone is None and "别名" in error, error)
gone, error = lib.delete_song(songs, "雨爱2")
checker.check("按歌名删除", gone is not None and not error, error)
checker.equal("删除后为空", len(songs), 0)
gone, error = lib.delete_song(songs, "雨爱2")
checker.check("重复删除报错", gone is None and "没有叫" in error)

# ------------------------------------------------------------------ 落盘读回
with temp_dir() as root:
    path = Path(root) / "songs.json"
    add("第一首", file_name="a.mp3", duration=61.0)
    add("第二首", file_name="b.mp3", duration=125.0)
    lib.save_library(path, songs)
    loaded, warnings = lib.load_library(path)
    checker.equal("读回数量", len(loaded), 2)
    checker.equal("无告警", warnings, [])
    checker.equal("时长保留", round(loaded[1].duration_s, 1), 125.0)
    checker.equal("列表行格式", loaded[0].label().split(" · ")[1], "1:01")

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.append({"id": "bad", "name": "", "file": "x.mp3"})
    raw.append(dict(raw[0], id="dup", aliases=[]))
    raw.append(dict(raw[0], id="trav", file="../etc/passwd"))
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    loaded, warnings = lib.load_library(path)
    checker.equal("坏条目被跳过", len(loaded), 2)
    checker.check("有告警", len(warnings) >= 2, str(warnings))

    path.write_text("not json", encoding="utf-8")
    loaded, warnings = lib.load_library(path)
    checker.equal("坏 JSON 退空库", loaded, [])
    checker.check("坏 JSON 有告警", bool(warnings))

    loaded, warnings = lib.load_library(Path(root) / "missing.json")
    checker.equal("缺文件退空库", (loaded, warnings), ([], []))

checker.check("空库提示", "空" in lib.render_library([])[0])

# ---------------------------------------------------------------- 随机与命令解析（0.7.0 增量）
samples = [
    lib.Song(id="a", name="甲", file="a.mp3"),
    lib.Song(id="b", name="乙", file="b.mp3"),
    lib.Song(id="c", name="丙", file="c.mp3"),
    lib.Song(id="d", name="丁", file="d.mp3"),
]
picked = {lib.pick_random(samples, ["a", "b", "c"]).id for _ in range(20)}
checker.equal("pick_random 只从没播过的里挑", picked, {"d"})
checker.check("pick_random 排除后可能为空时退化", lib.pick_random(samples, ["a", "b", "c", "d"]).id in {"a", "b", "c", "d"})
checker.equal("空库返回 None", lib.pick_random([], []), None)

checker.equal("send 解析", lib.parse_sing("send g123 雨爱"), ("send", "雨爱", "", "g123"))
checker.equal("random 解析（无目标）", lib.parse_sing("random"), ("random", "", "", ""))
checker.equal("random 解析（带目标）", lib.parse_sing("random u456"), ("random", "", "", "u456"))
checker.equal("send 缺歌名", lib.parse_sing("send g123"), ("send", "", "", "g123"))
checker.check("send/random 是保留字", "send" in lib.RESERVED_WORDS and "random" in lib.RESERVED_WORDS)

sys.exit(checker.report())
