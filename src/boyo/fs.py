from __future__ import annotations

import hashlib
import os
import stat as statmod
from pathlib import Path, PurePosixPath


RAW_EXTS = {".arw", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".dng", ".orf", ".rw2", ".pef", ".srw"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
EXCLUDED = {"deleteraw", "已删除备份", "图集", "tools", "avf_info", "system volume information"}


def is_link(path: Path):
    st = path.lstat()
    return statmod.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def checked(root: Path, relative: str, exists=False):
    """Reject traversal and reparse points, including junctions on Windows."""
    root = root.resolve(strict=True)
    rel = PurePosixPath(relative.replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(p in {".", ".."} or ":" in p for p in rel.parts):
        raise ValueError("路径必须在工作区内")
    path = root.joinpath(*rel.parts)
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.exists() and is_link(cursor):
            raise ValueError(f"拒绝链接或目录联接：{cursor}")
    if not path.resolve(strict=exists).is_relative_to(root):
        raise ValueError("路径越过工作区边界")
    if exists and not path.is_file():
        raise ValueError(f"源文件不存在：{relative}")
    return path


def sha256(path: Path, control=None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if control and not control.checkpoint():
                raise ValueError("任务已取消")
            digest.update(block)
    return digest.hexdigest()


def cached_digest(store, root, relative):
    path = checked(root, relative, True)
    stat = path.stat()
    row = store.one("SELECT sha256 FROM digests WHERE path=? AND size=? AND mtime_ns=?",
                    (relative, stat.st_size, stat.st_mtime_ns))
    if row:
        return row["sha256"]
    digest = sha256(path)
    after = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("文件在读取期间发生变化")
    store.execute("INSERT OR REPLACE INTO digests VALUES (?,?,?,?)",
                  (relative, stat.st_size, stat.st_mtime_ns, digest))
    return digest


def rename_no_replace(src: Path, dst: Path):
    """Windows rename never replaces. POSIX uses an exclusive hard-link step."""
    if os.name == "nt":
        os.rename(src, dst)
    else:
        os.link(src, dst)
        src.unlink()
