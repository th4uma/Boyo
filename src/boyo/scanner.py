from __future__ import annotations

import json
import logging
import os
import time
import stat as statmod
from types import SimpleNamespace
from collections import defaultdict
from pathlib import Path


from .fs import EXCLUDED, IMAGE_EXTS, RAW_EXTS, checked, sha256, is_link


def metadata(path):
    result = {"reader_version": 2}
    if path.suffix.lower() in RAW_EXTS:
        try:
            import exifread
            logging.getLogger("exifread").setLevel(logging.ERROR)
            with path.open("rb") as stream:
                tags = exifread.process_file(stream, details=False, stop_tag="DateTimeOriginal")
            return {"reader_version": 2, "camera": str(tags.get("Image Model", "")),
                    "capture": str(tags.get("EXIF DateTimeOriginal", tags.get("Image DateTime", "")))}
        except Exception as exc:
            return {"reader_version": 2, "read_error": str(exc)}
    if path.suffix.lower() not in IMAGE_EXTS:
        return result
    from PIL import Image
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            result = {"reader_version": 2, "width": im.width, "height": im.height,
                      "camera": str(exif.get(272, "")), "capture": str(exif.get(306, ""))}
            try:
                sub = exif.get_ifd(34665)
                result["capture"] = str(sub.get(36867, result["capture"]))
            except (KeyError, TypeError, ValueError):
                pass
    except Exception as exc:
        result["read_error"] = str(exc)
    return result


def role_for(parts, ext):
    lower = [p.lower() for p in parts[:-1]]
    if "精选" in parts[:-1]:
        return "selected_raw" if ext in RAW_EXTS else "selected_jpeg" if ext in IMAGE_EXTS else "sidecar"
    if "成片" in parts[:-1] and ext in IMAGE_EXTS:
        return "finished"
    if ext in RAW_EXTS:
        return "ordinary_raw"
    if ext in {".jpg", ".jpeg"}:
        return "ordinary_jpeg" if len(parts) == 1 or (lower and lower[0] in {"jpg", "raw"}) else "extra_image"
    if ext in IMAGE_EXTS:
        return "extra_image"
    return "sidecar" if ext in {".xmp", ".acr"} else "other"


class Scanner:
    def __init__(self, store, workspace):
        self.store, self.root = store, Path(workspace).resolve(strict=True)

    def scan(self, album_path: Path, progress=None, cancel=None, control=None):
        album_path = Path(album_path).resolve(strict=True)
        if not album_path.is_relative_to(self.root) or album_path == self.root:
            raise ValueError("请选择工作区内的一个拍摄组，而非工作区根目录")
        album = album_path.relative_to(self.root).as_posix()
        if len(Path(album).parts) != 1 or album.lower() in EXCLUDED or album.startswith("."):
            raise ValueError("请选择工作区一级拍摄目录")
        if is_link(self.root / album):
            raise ValueError("不能扫描目录链接")
        self.store.execute("INSERT INTO albums(path,complete) VALUES (?,0) ON CONFLICT(path) DO UPDATE SET complete=0", (album,))
        entries, errors = [], []
        previous = {a["path"]: a for a in self.store.assets(album)}
        directories = [album_path]
        while directories:
            directory = directories.pop()
            try:
                with os.scandir(directory) as iterator:
                    found = sorted(iterator, key=lambda entry: entry.name)
            except OSError as exc:
                errors.append(str(exc)); continue
            for item in found:
                if (cancel and cancel.is_set()) or (control and not control.checkpoint()):
                    errors.append("扫描已取消")
                    break
                p = Path(item.path)
                try:
                    st = item.stat(follow_symlinks=False)
                    directory_entry = statmod.S_ISDIR(st.st_mode)
                    if directory_entry and (item.name.lower() in EXCLUDED or item.name.startswith(".")):
                        continue
                    if statmod.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400):
                        errors.append(f"链接未扫描：{p}"); continue
                    if directory_entry:
                        directories.append(p); continue
                    relative = p.relative_to(self.root).as_posix()
                    parts = p.relative_to(album_path).parts
                    role = role_for(parts, p.suffix.lower())
                    old = previous.get(relative)
                    old_meta = json.loads(old["metadata"]) if old else {}
                    meta = old_meta if old and old_meta.get("reader_version") == 2 and (old["size"], old["mtime_ns"]) == (st.st_size, st.st_mtime_ns) else metadata(p)
                    entries.append({"album": album, "path": relative, "stem": p.stem.casefold(),
                                    "role": role, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                                    "logical_key": f"{album}|{p.stem.casefold()}", "metadata": meta})
                    if progress and len(entries) % 25 == 0:
                        progress(0, 1, f"正在只读扫描，已登记 {len(entries)} 个文件")
                except OSError as exc:
                    errors.append(str(exc))
            if (cancel and cancel.is_set()) or (control and control.cancel.is_set()):
                break
        if errors:
            self.store.execute("UPDATE albums SET errors=?,complete=0 WHERE path=?", (json.dumps(errors, ensure_ascii=False), album))
            return {"album": album, "complete": False, "errors": errors, "count": len(entries)}
        # Only size-matched candidates need hashing to bind renamed, byte-identical selected copies.
        digests = self.store.digest_snapshot(album)
        digest_writes = []
        digest_control = control or (SimpleNamespace(checkpoint=lambda: not cancel.is_set()) if cancel else None)
        def selected_digest(entry):
            if digest_control and not digest_control.checkpoint(): raise ValueError("扫描已取消")
            path = checked(self.root, entry["path"], True)
            before = path.stat()
            if (before.st_size, before.st_mtime_ns) != (entry["size"], entry["mtime_ns"]):
                raise ValueError("照片在扫描期间发生变化，请重新扫描")
            cached = digests.get(entry["path"])
            if cached and (cached["size"], cached["mtime_ns"]) == (entry["size"], entry["mtime_ns"]):
                return cached["sha256"]
            digest = sha256(path, digest_control)
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (entry["size"], entry["mtime_ns"]):
                raise ValueError("照片在扫描期间发生变化，请重新扫描")
            digest_writes.append((entry["path"], entry["size"], entry["mtime_ns"], digest))
            return digest
        selected_sizes = {e["size"] for e in entries if e["role"] == "selected_jpeg"}
        by_digest = {}
        try:
            for e in entries:
                if e["role"] == "ordinary_jpeg" and e["size"] in selected_sizes:
                    digest = selected_digest(e)
                    by_digest.setdefault(digest, e["logical_key"])
            for e in entries:
                if e["role"] == "selected_jpeg":
                    digest = selected_digest(e)
                    e["logical_key"] = by_digest.get(digest, e["logical_key"])
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
        if errors:
            self.store.execute("UPDATE albums SET errors=?,complete=0 WHERE path=?", (json.dumps(errors, ensure_ascii=False), album))
            return {"album": album, "complete": False, "errors": errors, "count": len(entries)}
        selected_keys = {e["stem"]: e["logical_key"] for e in entries if e["role"] == "selected_jpeg"}
        for e in entries:
            if "精选" in Path(e["path"]).parts and e["stem"] in selected_keys:
                e["logical_key"] = selected_keys[e["stem"]]
        aliases = {r["path"]: r["logical_key"] for r in self.store.rows("SELECT * FROM aliases")}
        for e in entries:
            e["logical_key"] = aliases.get(e["path"], e["logical_key"])
        protected = {e["logical_key"] for e in entries if e["role"] in {"selected_jpeg", "selected_raw", "finished", "extra_image"}}
        # Content binding may find another identical ordinary copy. Never lose the
        # conservative filename-based protection of the selected original's RAW.
        protected.update(f"{album}|{e['stem']}" for e in entries if e["role"] in {"selected_jpeg", "selected_raw", "finished", "extra_image"})
        with self.store.connect() as db:
            if digest_writes:
                db.executemany("INSERT OR REPLACE INTO digests VALUES (?,?,?,?)", digest_writes)
            db.execute("UPDATE assets SET present=0 WHERE album=?", (album,))
            for e in entries:
                lock = int(e["logical_key"] in protected or e["role"] in {"selected_jpeg", "selected_raw", "finished", "extra_image"})
                db.execute("""INSERT INTO assets(album,path,stem,role,logical_key,size,mtime_ns,protected,metadata)
                           VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                           album=excluded.album,stem=excluded.stem,role=excluded.role,logical_key=excluded.logical_key,
                           size=excluded.size,mtime_ns=excluded.mtime_ns,protected=excluded.protected,present=1,
                           metadata=excluded.metadata,
                           decision=CASE WHEN excluded.protected=1 THEN 'keep'
                           WHEN assets.size<>excluded.size OR assets.mtime_ns<>excluded.mtime_ns THEN 'pending'
                           ELSE assets.decision END""",
                           tuple(e[k] for k in ("album", "path", "stem", "role", "logical_key", "size", "mtime_ns")) + (lock, json.dumps(e["metadata"], ensure_ascii=False)))
            db.execute("UPDATE assets SET decision='keep' WHERE album=? AND protected=1", (album,))
            db.execute("UPDATE albums SET complete=1,scanned=?,errors='[]' WHERE path=?", (time.time(), album))
        return {"album": album, "complete": True, "errors": [], "count": len(entries), "protected": sum(e["logical_key"] in protected for e in entries)}
