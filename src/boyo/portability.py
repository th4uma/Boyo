"""Stable library identity and bounded, offline workspace relocation checks."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .settings_io import atomic_json


def binding_path(runtime):
    return runtime.home / 'settings' / 'workspace-binding.json'


def normalized(path):
    return os.path.normcase(str(Path(path).resolve())).casefold()


def read_binding(runtime):
    try:
        data = json.loads(binding_path(runtime).read_text(encoding='utf-8'))
        name = data['database']
        if (data.get('version') != 1 or Path(name).name != name
                or not name.startswith('library-') or not name.endswith('.sqlite3')
                or not all(isinstance(data.get(key), str) for key in ('root', 'runtime_home'))
                or not isinstance(data.get('probes'), list) or len(data['probes']) > 12):
            return None
        for probe in data['probes']:
            if (not isinstance(probe, dict) or not isinstance(probe.get('path'), str)
                    or not isinstance(probe.get('size'), int) or probe['size'] < 0
                    or not isinstance(probe.get('sha256'), str) or len(probe['sha256']) != 64):
                return None
        if not isinstance(data.get('history', []), list):
            return None
        if data.get('pending_settings_root') is not None and not isinstance(data['pending_settings_root'], str):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def bound_database(runtime, workspace):
    binding = read_binding(runtime)
    if binding and normalized(binding['root']) == normalized(workspace):
        path = runtime.home / 'data' / binding['database']
        if not path.is_file():
            raise ValueError('已绑定状态库缺失，请恢复 local/data；不会创建空库替代原记录')
        return path
    return None


def _digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _probe_path(root, relative):
    # Do not accept absolute paths, traversal or reparse points in a copied binding.
    from .fs import checked
    return checked(root, relative, True)


def bind_workspace(runtime, workspace, database):
    root, database = Path(workspace).resolve(strict=True), Path(database).resolve(strict=True)
    if database.parent != (runtime.home / 'data').resolve():
        raise ValueError('状态库必须位于工具 local/data 内')
    probes, seen = [], {}
    with closing(sqlite3.connect(database)) as db:
        rows = db.execute("SELECT album,path,size,mtime_ns FROM assets WHERE present=1 "
                          "AND role='ordinary_jpeg' ORDER BY album,path").fetchall()
        for album, relative, size, mtime in rows:
            if seen.get(album, 0) >= 3:
                continue
            try:
                path = _probe_path(root, relative)
                before = path.stat()
                if before.st_size != size:
                    continue
                digest = _digest(path)
                if path.stat().st_mtime_ns != before.st_mtime_ns:
                    raise ValueError('工作区验证期间照片发生变化')
                probes.append({'path': relative, 'size': size, 'sha256': digest})
                seen[album] = seen.get(album, 0) + 1
            except (OSError, ValueError):
                continue
            if len(probes) >= 12:
                break
    try:
        relative_root = os.path.relpath(root, runtime.home.resolve())
    except ValueError:
        relative_root = None
    previous = read_binding(runtime)
    value = {'version': 1, 'root': str(root), 'runtime_home': str(runtime.home.resolve()),
             'relative_root': relative_root, 'database': database.name, 'probes': probes,
             'history': (previous or {}).get('history', [])[-19:]}
    if previous and normalized(previous['root']) != normalized(root):
        value['history'].append({'root': previous['root'], 'time': time.time()})
    atomic_json(binding_path(runtime), value)
    return value


def _relocation_probes(runtime, binding):
    probes = list(binding['probes'])
    # Static examples can have been culled since the last move. Reuse current
    # cached content identities, without decoding images or scanning the disk.
    try:
        path = runtime.home / 'data' / binding['database']
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
            rows = db.execute("SELECT a.path,a.size,d.sha256 FROM assets a JOIN digests d ON a.path=d.path "
                              "AND a.size=d.size AND a.mtime_ns=d.mtime_ns WHERE a.present=1 "
                              "AND a.role='ordinary_jpeg' ORDER BY a.album,a.path").fetchall()
        if rows:
            step = max(1, len(rows)//12)
            probes = [{'path':p,'size':s,'sha256':h} for p,s,h in rows[::step][:12]] + probes
    except (OSError, sqlite3.Error):
        pass
    return list({p['path']:p for p in probes}.values())[:24]


def _matches_workspace(runtime, binding, candidate):
    probes = _relocation_probes(runtime, binding)
    required, matched = min(2, len(probes)), 0
    for probe in probes:
        try:
            path = _probe_path(candidate, probe['path'])
            if path.stat().st_size == probe['size'] and _digest(path) == probe['sha256']:
                matched += 1
                if matched >= required:
                    return True
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False


def relocated_settings(runtime, settings):
    binding = read_binding(runtime)
    if not binding:
        return settings
    pending = binding.get('pending_settings_root')
    if pending:
        if normalized(settings.get('workspace', pending)) == normalized(pending):
            settings = {**settings, 'workspace': binding['root']}
            runtime.save_settings(settings)
        binding.pop('pending_settings_root', None)
        atomic_json(binding_path(runtime), binding)
    if normalized(binding['runtime_home']) == normalized(runtime.home):
        return settings
    relative = binding.get('relative_root')
    if not isinstance(relative, str) or not relative or not binding.get('probes'):
        return settings
    candidate = (runtime.home / relative).resolve()
    if not candidate.is_dir():
        return settings
    if not _matches_workspace(runtime, binding, candidate):
        return settings
    # Existing persisted photo, plan and journal paths are all workspace-relative.
    # Keep the database name stable so personal models and caches stay associated.
    old = binding['root']
    binding['pending_settings_root'] = old
    binding['root'], binding['runtime_home'] = str(candidate), str(runtime.home.resolve())
    binding.setdefault('history', []).append({'root': old, 'time': time.time()})
    binding['history'] = binding['history'][-20:]
    atomic_json(binding_path(runtime), binding)
    if normalized(settings.get('workspace', old)) == normalized(old):
        settings = {**settings, 'workspace': str(candidate)}
        runtime.save_settings(settings)
    binding.pop('pending_settings_root', None)
    atomic_json(binding_path(runtime), binding)
    return settings


def repair_workspace(runtime, workspace):
    settings = runtime.settings()
    old_root = Path(settings['workspace'])
    database = bound_database(runtime, old_root) or runtime.database(old_root)
    if not database.is_file():
        raise ValueError('原状态库不存在，拒绝新建空库代替迁移')
    old_binding = read_binding(runtime)
    if old_binding and not _matches_workspace(runtime, old_binding, Path(workspace)):
        raise ValueError('目标照片内容不匹配，拒绝关联原状态库')
    binding = bind_workspace(runtime, workspace, database)
    binding['pending_settings_root'] = str(old_root)
    atomic_json(binding_path(runtime), binding)
    runtime.save_settings({**settings, 'workspace': str(Path(workspace).resolve())})
    binding.pop('pending_settings_root', None)
    atomic_json(binding_path(runtime), binding)
    return binding
