"""待入库音频的暂存池。

不拦截消息，只在 ``chat.receive.after_process``（OBSERVE）里旁路下载；这里
只负责把字节落到 ``<data_dir>/pending/`` 并维护 ``index.json``：

* ``/voice sing add <歌名>`` 在 ``add_window_seconds`` 内从池里认领最近一条；
* ``index.json`` 同时是给其他插件取用音频的共享入口（用户 2026-11-19 的要求）；
* 容量与 TTL 由 ``[songs]`` 配置控制，超了就按最旧的删。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

INDEX_NAME = "index.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(bytes(data or b"")).hexdigest()


def _safe_name(value: Any) -> str:
    text = str(value or "").strip()
    return text.replace("/", "_").replace("\\", "_")[:64]


@dataclass
class PendingItem:
    """池里的一条；``file`` 是相对 ``pending/`` 的文件名。"""

    sha256: str
    file: str
    ext: str
    duration_s: float
    size: int
    session_id: str = ""
    user_id: str = ""
    sender: str = ""
    message_id: str = ""
    source: str = "voice"  # voice / file
    created_at: float = 0.0

    def path_in(self, root: Path) -> Path:
        return Path(root) / self.file

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sha256": self.sha256,
            "file": self.file,
            "ext": self.ext,
            "duration_s": round(float(self.duration_s), 2),
            "size": int(self.size),
            "session_id": self.session_id,
            "user_id": self.user_id,
            "sender": self.sender,
            "message_id": self.message_id,
            "source": self.source,
            "created_at": float(self.created_at),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["PendingItem"]:
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("file") or "").strip()
        sha = str(raw.get("sha256") or "").strip()
        if not name or not sha or name.startswith(("/", "\\")) or ".." in Path(name).parts:
            return None
        try:
            duration = float(raw.get("duration_s") or 0.0)
            size = int(raw.get("size") or 0)
            created = float(raw.get("created_at") or 0.0)
        except (TypeError, ValueError):
            return None
        return cls(
            sha256=sha,
            file=name,
            ext=str(raw.get("ext") or "").lower(),
            duration_s=max(0.0, duration),
            size=max(0, size),
            session_id=str(raw.get("session_id") or ""),
            user_id=str(raw.get("user_id") or ""),
            sender=str(raw.get("sender") or ""),
            message_id=str(raw.get("message_id") or ""),
            source=str(raw.get("source") or "voice"),
            created_at=created,
        )


class PendingPool:
    """``pending/`` 目录 + ``index.json`` 的读写；不做下载，只管落盘与认领。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.index_path = self.root / INDEX_NAME

    def items(self) -> List[PendingItem]:
        return self._load()

    def _load(self) -> List[PendingItem]:
        if not self.index_path.exists():
            return []
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return []
        items: List[PendingItem] = []
        for row in rows:
            item = PendingItem.from_dict(row)
            if item is not None:
                items.append(item)
        return items

    def _save(self, items: List[PendingItem]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "items": [item.to_dict() for item in items]},
            ensure_ascii=False,
            indent=2,
        )
        temp = self.index_path.with_suffix(".tmp")
        temp.write_text(payload + "\n", encoding="utf-8")
        temp.replace(self.index_path)

    def put(
        self,
        data: bytes,
        *,
        suffix: str,
        duration_s: float,
        session_id: str,
        user_id: str,
        sender: str = "",
        message_id: str = "",
        source: str = "voice",
        now: Optional[float] = None,
    ) -> PendingItem:
        """写入一份音频；同 sha256 已存在时只刷新元数据，不重复占盘。"""
        stamp = time.time() if now is None else float(now)
        sha = digest(data)
        ext = suffix.lower() if suffix else ""
        if ext and not ext.startswith("."):
            ext = "." + ext
        file_name = f"{sha}{ext}"
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / file_name
        if not target.exists() or target.stat().st_size != len(data):
            target.write_bytes(data)

        items = [item for item in self._load() if item.sha256 != sha]
        item = PendingItem(
            sha256=sha,
            file=file_name,
            ext=ext,
            duration_s=max(0.0, float(duration_s)),
            size=len(data),
            session_id=str(session_id or ""),
            user_id=str(user_id or ""),
            sender=_safe_name(sender),
            message_id=str(message_id or ""),
            source=str(source or "voice"),
            created_at=stamp,
        )
        items.append(item)
        self._save(items)
        return item

    def claim(
        self,
        *,
        session_id: str,
        user_id: str = "",
        window_seconds: float,
        now: Optional[float] = None,
    ) -> Optional[PendingItem]:
        """在窗口内找**本会话**最近的一条。

        跨会话不认领（写死）：同一个人先发到群里、再去私聊 ``add`` 不行，必须在这个会话里
        把音频再发一次。这样「配置/入库只发生在私聊」这条边界在代码层面成立，而不是靠提示。
        """
        stamp = time.time() if now is None else float(now)
        window = max(0.0, float(window_seconds))
        wanted_session = str(session_id or "")
        wanted_user = str(user_id or "")

        def usable(item: PendingItem) -> bool:
            return (
                item.created_at <= stamp
                and stamp - item.created_at <= window
                and item.path_in(self.root).exists()
            )

        items = [item for item in self._load() if usable(item)]
        candidates = [item for item in items if item.session_id == wanted_session]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.created_at, reverse=True)
        for item in candidates:
            if wanted_user and item.user_id == wanted_user:
                return item
        return candidates[0]

    def discard(self, item: PendingItem) -> None:
        """认领完成或删除后把记录与文件一起清掉。"""
        items = [row for row in self._load() if row.sha256 != item.sha256]
        self._save(items)
        path = item.path_in(self.root)
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def prune(
        self,
        *,
        ttl_seconds: float,
        max_bytes: int,
        max_entries: int,
        now: Optional[float] = None,
    ) -> int:
        """按 TTL / 总量 / 条数清理，返回删除条数（先删最旧）。"""
        stamp = time.time() if now is None else float(now)
        items = self._load()
        ttl = max(0.0, float(ttl_seconds))
        kept: List[PendingItem] = []
        removed = 0

        for item in sorted(items, key=lambda row: row.created_at):
            path = item.path_in(self.root)
            if not path.exists():
                removed += 1
                continue
            if ttl and stamp - item.created_at > ttl:
                self._unlink(path)
                removed += 1
                continue
            kept.append(item)

        limit_entries = max(0, int(max_entries))
        limit_bytes = max(0, int(max_bytes))
        total = sum(item.size for item in kept)
        while kept and (len(kept) > limit_entries or (limit_bytes and total > limit_bytes)):
            oldest = kept.pop(0)
            self._unlink(oldest.path_in(self.root))
            total -= oldest.size
            removed += 1

        # 索引里没有的孤儿文件（例如入库后钩子又把文件写回来、或索引被外部清掉）也要清，
        # 否则 pending/ 会一直涨；只删超过 TTL 的，避免误删刚写盘、索引还没落地的文件。
        if ttl:
            referenced = {item.file for item in kept}
            try:
                entries = list(self.root.iterdir())
            except OSError:
                entries = []
            for path in entries:
                if not path.is_file() or path.name == INDEX_NAME or path.name in referenced:
                    continue
                try:
                    if stamp - path.stat().st_mtime > ttl:
                        path.unlink()
                        removed += 1
                except OSError:
                    continue

        if removed or kept != items:
            self._save(kept)
        return removed

    def total_bytes(self) -> int:
        return sum(item.size for item in self._load())

    def stats(self) -> Dict[str, Any]:
        items = self._load()
        return {
            "count": len(items),
            "bytes": sum(item.size for item in items),
            "oldest": min((item.created_at for item in items), default=0.0),
        }

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


__all__: List[str] = ["INDEX_NAME", "PendingItem", "PendingPool", "digest"]
