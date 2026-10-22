"""原文归档:按内容哈希存放,写入后不可变,读取时复核哈希。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class ArchiveIntegrityError(Exception):
    pass


class ArchiveStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def put(self, content: bytes) -> tuple[str, str]:
        """写入原文。返回 (sha256, 相对路径)。相同内容只存一份。"""
        digest = self.sha256(content)
        rel = f"{digest[:2]}/{digest}"
        target = self._root / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(content)
                os.replace(tmp, target)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return digest, rel

    def read(self, rel: str) -> bytes:
        """读取并复核哈希;文件名即期望哈希,不一致说明归档被篡改或损坏。"""
        path = (self._root / rel).resolve()
        if self._root not in path.parents:
            raise ArchiveIntegrityError(f"路径越界: {rel}")
        content = path.read_bytes()
        if self.sha256(content) != path.name:
            raise ArchiveIntegrityError(f"归档文件哈希不一致: {rel}")
        return content
