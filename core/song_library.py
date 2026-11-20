"""曲库：解析、匹配、唯一性与增删改的纯逻辑。

插件侧只负责 IO 与宿主调用；这里的函数只吃字符串和列表，方便
``tests/test_song_library.py`` 用标准库直接断言。

唯一性规则（用户 2026-11-19 定）：

* ``name`` 全局唯一；
* ``aliases`` 全局唯一，且不得与任何 ``name`` 重复——不唯一的概念应放进 ``tags``；
* ``tags`` 只参与匹配，允许重复，不参与唯一性。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 入库只收这三种容器；语音条另走 wav（入站时已经是 wav）
AUDIO_EXTENSIONS = (".mp3", ".wav", ".flac")

# 半角逗号、全角逗号、顿号、空白都算分隔（全角逗号是用户明确要求加上的）
_SEPARATORS = re.compile(r"[,，、\s]+")

# 这些词是子命令关键字，拿来当歌名会让 /voice sing 的命令解析歧义
RESERVED_WORDS = ("list", "add", "addforce", "del", "set", "send", "random")

_LIBRARY_FILE = "songs.json"


def normalize_key(value: Any) -> str:
    """归一化用于比较：NFKC（全角→半角）、去首尾、折叠空白、小写。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


def split_values(raw: str) -> List[str]:
    """按半角/全角逗号、顿号、空白切分多值；保留原样大小写，去空项与重复。"""
    parts = [item.strip() for item in _SEPARATORS.split(str(raw or ""))]
    result: List[str] = []
    seen: set[str] = set()
    for item in parts:
        if not item:
            continue
        key = normalize_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def has_audio_extension(name: str) -> bool:
    return str(name or "").strip().lower().endswith(AUDIO_EXTENSIONS)


@dataclass
class Song:
    """一首歌的元数据。``id`` 创建后不变，改名也不变。"""

    id: str
    name: str
    aliases: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    file: str = ""
    duration_s: float = 0.0
    added_at: float = 0.0
    added_by: str = ""

    def keys(self) -> List[str]:
        """所有可用于匹配的字符串（name + aliases + tags）。"""
        return [self.name, *self.aliases, *self.tags]

    def label(self) -> str:
        """列表里的一行：``名称 · 时长 · 别名/标签``。"""
        minutes = int(self.duration_s // 60)
        seconds = round(self.duration_s % 60)
        parts = [self.name, f"{minutes}:{seconds:02d}"]
        extra = [*self.aliases, *self.tags]
        if extra:
            parts.append(" / ".join(extra))
        return " · ".join(parts)


def song_to_dict(song: Song) -> Dict[str, Any]:
    return {
        "id": song.id,
        "name": song.name,
        "aliases": list(song.aliases),
        "tags": list(song.tags),
        "file": song.file,
        "duration_s": round(float(song.duration_s), 2),
        "added_at": float(song.added_at),
        "added_by": song.added_by,
    }


def song_from_dict(raw: Any) -> Optional[Song]:
    """把一条 JSON 转成 ``Song``；字段类型不对就返回 ``None``（由调用方告警）。"""
    if not isinstance(raw, dict):
        return None
    song_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    file_name = str(raw.get("file") or "").strip()
    if not song_id or not name or not file_name:
        return None
    if file_name.startswith(("/", "\\")) or ".." in Path(file_name).parts:
        return None
    if Path(file_name).name != file_name or "\\" in file_name:
        # 曲库文件永远平铺在 songs/ 下；带路径分隔符的条目一律视为被篡改
        return None
    aliases_raw = raw.get("aliases") or []
    tags_raw = raw.get("tags") or []
    if not isinstance(aliases_raw, list) or not isinstance(tags_raw, list):
        return None
    try:
        duration = float(raw.get("duration_s") or 0.0)
        added_at = float(raw.get("added_at") or 0.0)
    except (TypeError, ValueError):
        return None
    return Song(
        id=song_id,
        name=name,
        aliases=[str(item).strip() for item in aliases_raw if str(item).strip()],
        tags=[str(item).strip() for item in tags_raw if str(item).strip()],
        file=file_name,
        duration_s=max(0.0, duration),
        added_at=added_at,
        added_by=str(raw.get("added_by") or ""),
    )


def load_library(path: Path) -> Tuple[List[Song], List[str]]:
    """读 ``songs.json``：返回 (曲目, 告警)。文件不存在时返回空曲库而不是抛异常。"""
    warnings: List[str] = []
    target = Path(path)
    if not target.exists():
        return [], warnings
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warnings.append(f"曲库读取失败（{exc}），按空曲库处理")
        return [], warnings
    if not isinstance(raw, list):
        warnings.append("曲库应为一个数组，按空曲库处理")
        return [], warnings

    songs: List[Song] = []
    taken: Dict[str, str] = {}
    for index, item in enumerate(raw, start=1):
        song = song_from_dict(item)
        if song is None:
            warnings.append(f"曲库第 {index} 条格式不合法，已跳过")
            continue
        conflict = _taken_by(taken, [song.name, *song.aliases])
        if conflict:
            warnings.append(f"曲库第 {index} 条「{song.name}」与「{conflict}」重名，已跳过")
            continue
        for key in [song.name, *song.aliases]:
            taken[normalize_key(key)] = song.name
        songs.append(song)
    return songs, warnings


def save_library(path: Path, songs: Sequence[Song]) -> None:
    """写回 ``songs.json``；先写临时文件再替换，避免半截文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([song_to_dict(song) for song in songs], ensure_ascii=False, indent=2)
    temp = target.with_suffix(".tmp")
    temp.write_text(payload + "\n", encoding="utf-8")
    temp.replace(target)


def _taken_by(taken: Dict[str, str], keys: Iterable[str], exclude_id: str = "") -> Optional[str]:
    """返回占用了这些 key 的歌名（排除 ``exclude_id`` 自己）。"""
    for key in keys:
        owner = taken.get(normalize_key(key))
        if owner and owner != exclude_id:
            return owner
    return None


def find_conflict(songs: Sequence[Song], keys: Iterable[str], exclude_id: str = "") -> Optional[Song]:
    """找出占用这些 key 的另一首歌（``exclude_id`` 自己不算）。"""
    wanted = {normalize_key(key) for key in keys if normalize_key(key)}
    if not wanted:
        return None
    for song in songs:
        if song.id == exclude_id:
            continue
        if any(normalize_key(key) in wanted for key in [song.name, *song.aliases]):
            return song
    return None


def assign_id(songs: Sequence[Song], name: str) -> str:
    """由名字生成稳定 id；撞了就加序号。改名不会改 id。"""
    base = "s" + hashlib.sha1(normalize_key(name).encode("utf-8")).hexdigest()[:8]
    used = {song.id for song in songs}
    if base not in used:
        return base
    index = 2
    while f"{base}-{index}" in used:
        index += 1
    return f"{base}-{index}"


def match_songs(songs: Sequence[Song], query: str) -> List[Song]:
    """匹配优先级：name 精确 → alias 精确 → tag 精确 → name/alias 子串。

    返回去重后的列表；调用方在多条命中时随机取一（用户 2026-11-19 定）。
    """
    key = normalize_key(query)
    if not key:
        return []
    result: List[Song] = []
    seen: set[str] = set()

    def push(song: Song) -> None:
        if song.id not in seen:
            seen.add(song.id)
            result.append(song)

    for song in songs:
        if normalize_key(song.name) == key:
            push(song)
    for song in songs:
        if any(normalize_key(alias) == key for alias in song.aliases):
            push(song)
    for song in songs:
        if any(normalize_key(tag) == key for tag in song.tags):
            push(song)
    if len(key) >= 2:
        for song in songs:
            if key in normalize_key(song.name) or any(key in normalize_key(alias) for alias in song.aliases):
                push(song)
    return result


def pick_random(songs: Sequence[Song], exclude_ids: Sequence[str] = ()) -> Optional[Song]:
    """随机取一首；优先避开 ``exclude_ids``，可用歌曲不够时退回全库。"""
    skipped = {str(item) for item in exclude_ids}
    pool = [song for song in songs if song.id not in skipped]
    if not pool:
        pool = list(songs)
    if not pool:
        return None
    return random.choice(pool)


def render_library(songs: Sequence[Song]) -> List[str]:
    """``list`` 的每一行；空曲库返回提示行。"""
    if not songs:
        return ["曲库还是空的：先发一个音频文件，再用 /voice sing add <歌名>"]
    return [song.label() for song in songs]


def parse_sing(rest: str) -> Tuple[str, str, str, str]:
    """拆 ``/voice sing`` 后面的参数。

    返回 ``(动作, 歌名, 字段, 值)``，动作取值：
    ``list`` / ``add`` / ``addforce`` / ``del`` / ``set`` / ``send`` / ``random`` / ``sing``。
    """
    body = str(rest or "").strip()
    if not body:
        return "sing", "", "", ""
    head, _, tail = body.partition(" ")
    head = head.strip()
    lowered = head.lower()
    if lowered == "list":
        return "list", "", "", ""
    if lowered in ("add", "addforce", "del"):
        return lowered, tail.strip(), "", ""
    if lowered == "set":
        name, _, remainder = tail.strip().partition(" ")
        field_name, _, value = remainder.strip().partition(" ")
        return "set", name.strip(), field_name.strip().lower(), value.strip()
    if lowered == "send":
        target, _, name = tail.strip().partition(" ")
        return "send", name.strip(), "", target.strip()
    if lowered == "random":
        return "random", "", "", tail.strip()
    return "sing", body, "", ""


def add_song(
    songs: List[Song],
    name: str,
    *,
    file_name: str,
    duration_s: float,
    added_by: str,
    now: float,
    max_songs: int,
    force: bool = False,
) -> Tuple[Optional[Song], str]:
    """新增或（``force``）替换一首歌；返回 (歌曲, 错误文案)。"""
    clean = str(name or "").strip()
    if not clean:
        return None, "要一个歌名：/voice sing add <歌名>"
    if normalize_key(clean) in RESERVED_WORDS:
        return None, f"「{clean}」是命令关键字，换个名字吧"
    if not has_audio_extension(file_name):
        return None, "只收 mp3 / wav / flac"

    existing = find_conflict(songs, [clean])
    if existing is not None and not force:
        return None, f"「{existing.name}」已经用了这个名字，用 /voice sing addforce {clean} 覆盖"

    if existing is not None:
        existing.file = Path(file_name).name
        existing.duration_s = max(0.0, float(duration_s))
        return existing, ""

    if len(songs) >= max(0, int(max_songs)):
        return None, f"曲库满了（上限 {max_songs} 首），先 /voice sing del <歌名>"

    song = Song(
        id=assign_id(songs, clean),
        name=clean,
        file=Path(file_name).name,
        duration_s=max(0.0, float(duration_s)),
        added_at=float(now),
        added_by=str(added_by),
    )
    songs.append(song)
    return song, ""


def delete_song(songs: List[Song], name: str) -> Tuple[Optional[Song], str]:
    """按 **歌名** 删除；别名与 tag 一律不认（用户 2026-11-19 定）。"""
    clean = str(name or "").strip()
    if not clean:
        return None, "要歌名：/voice sing del <歌名>"
    for index, song in enumerate(songs):
        if normalize_key(song.name) == normalize_key(clean):
            return songs.pop(index), ""
    by_alias = find_conflict(songs, [clean])
    if by_alias is not None:
        return None, f"「{clean}」是「{by_alias.name}」的别名或标签，删除请用歌名本身"
    return None, f"没有叫「{clean}」的歌"


def set_song_field(
    songs: List[Song],
    name: str,
    field_name: str,
    value: str,
) -> Tuple[Optional[Song], str]:
    """``name`` / ``alias`` / ``tags`` 的**全量替换**；返回 (歌曲, 错误文案)。"""
    clean = str(name or "").strip()
    song = next((item for item in songs if normalize_key(item.name) == normalize_key(clean)), None)
    if song is None:
        return None, f"没有叫「{clean}」的歌（改名也要用当前歌名）"

    field_key = str(field_name or "").strip().lower()
    if field_key in ("name", "歌名"):
        new_name = str(value or "").strip()
        if not new_name:
            return None, "歌名不能清空"
        if normalize_key(new_name) in RESERVED_WORDS:
            return None, f"「{new_name}」是命令关键字，换个名字吧"
        conflict = find_conflict(songs, [new_name], exclude_id=song.id)
        if conflict is not None:
            return None, f"「{conflict.name}」已经用了这个名字"
        song.name = new_name
        return song, ""

    values = split_values(value)
    if field_key in ("alias", "aliases", "别名"):
        conflict = find_conflict(songs, values, exclude_id=song.id)
        if conflict is not None:
            return None, f"别名「{conflict.name}」已被占用；想共用检索词请放进 tag"
        song.aliases = values
        return song, ""

    if field_key in ("tag", "tags", "标签"):
        song.tags = values
        return song, ""

    return None, "只能改 name / alias / tags"
