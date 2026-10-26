from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
 path TEXT PRIMARY KEY, scanned REAL, complete INTEGER NOT NULL DEFAULT 0,
 errors TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS assets (
 id INTEGER PRIMARY KEY, album TEXT NOT NULL, path TEXT UNIQUE NOT NULL,
 stem TEXT NOT NULL, role TEXT NOT NULL, logical_key TEXT NOT NULL,
 size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
 protected INTEGER NOT NULL DEFAULT 0, present INTEGER NOT NULL DEFAULT 1,
 decision TEXT NOT NULL DEFAULT 'pending', metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS assets_album ON assets(album,present);
CREATE TABLE IF NOT EXISTS aliases (path TEXT PRIMARY KEY, logical_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS features (
 path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, version TEXT, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digests (
 path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
 asset_id INTEGER PRIMARY KEY, context TEXT NOT NULL, decision TEXT NOT NULL,
 updated REAL NOT NULL, FOREIGN KEY(asset_id) REFERENCES assets(id)
);
CREATE TABLE IF NOT EXISTS feedback (
 asset_id INTEGER PRIMARY KEY, context TEXT NOT NULL, decision TEXT NOT NULL,
 source TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
 updated REAL NOT NULL, FOREIGN KEY(asset_id) REFERENCES assets(id)
);
CREATE TABLE IF NOT EXISTS feedback_events (
 id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, context TEXT NOT NULL,
 previous_decision TEXT NOT NULL, decision TEXT NOT NULL, source TEXT NOT NULL,
 size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS feedback_events_source ON feedback_events(source,id);
CREATE TABLE IF NOT EXISTS learning_state (key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS embeddings (
 cache_key TEXT PRIMARY KEY, version TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
 id TEXT PRIMARY KEY, album TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_runs (album TEXT PRIMARY KEY, signature TEXT NOT NULL, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pair_matches (
 album TEXT NOT NULL, cache_key TEXT PRIMARY KEY, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_cache (
 path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
 version TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_runs (
 album TEXT PRIMARY KEY, signature TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
 id TEXT PRIMARY KEY, album TEXT NOT NULL, kind TEXT NOT NULL,
 created REAL NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
 id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL, seq INTEGER NOT NULL,
 src TEXT NOT NULL, dst TEXT NOT NULL, action TEXT NOT NULL,
 sha256 TEXT NOT NULL, state TEXT NOT NULL, error TEXT,
 UNIQUE(plan_id,seq)
);
CREATE TABLE IF NOT EXISTS photo_sets (
 id TEXT PRIMARY KEY, album TEXT NOT NULL, name TEXT NOT NULL,
 created REAL NOT NULL, status TEXT NOT NULL DEFAULT '草稿'
);
CREATE TABLE IF NOT EXISTS set_items (
 set_id TEXT NOT NULL, slot INTEGER NOT NULL, asset_id INTEGER NOT NULL,
 logical_key TEXT NOT NULL UNIQUE, locked INTEGER NOT NULL DEFAULT 0,
 content_hash TEXT UNIQUE,
 PRIMARY KEY(set_id,slot), FOREIGN KEY(set_id) REFERENCES photo_sets(id) ON DELETE CASCADE,
 FOREIGN KEY(asset_id) REFERENCES assets(id)
);
CREATE TABLE IF NOT EXISTS set_options (
 set_id TEXT PRIMARY KEY, requested INTEGER NOT NULL DEFAULT 9,
 second_cover INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(set_id) REFERENCES photo_sets(id) ON DELETE CASCADE
);
"""


class Store:
    def __init__(self, path: Path, *, regenerable=False):
        self.path = Path(path)
        self.regenerable=regenerable
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if self.regenerable:
            # Only the isolated learner uses this connection policy for caches
            # and derived models. User decisions and move journals retain FULL.
            db.execute('PRAGMA synchronous=NORMAL')
            db.execute('PRAGMA busy_timeout=100')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, params=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.connect() as db:
            return db.execute(sql, params).lastrowid

    def assets(self, album):
        return self.rows("SELECT * FROM assets WHERE album=? AND present=1 ORDER BY path", (album,))

    def asset(self, asset_id):
        return self.one("SELECT * FROM assets WHERE id=?", (asset_id,))

    def decide(self, ids, decision, context, source="manual"):
        return self.decide_many({asset_id: decision for asset_id in ids}, context, source=source)

    def review_stamps(self, album):
        rows = self.rows('''SELECT a.id,a.size,a.mtime_ns,a.decision,r.updated
            FROM assets a LEFT JOIN reviews r ON r.asset_id=a.id
            WHERE a.album=? AND a.present=1''', (album,))
        return {str(a['id']): [a['size'],a['mtime_ns'],a['decision'],a['updated']] for a in rows}

    def decide_many(self, decisions, context, source="manual"):
        if source not in {"manual", "adopt"}: raise ValueError("无效操作来源")
        with self.connect() as db:
            for asset_id, decision in decisions.items():
                if decision not in {"pending", "keep", "delete"}:
                    raise ValueError("无效状态")
                a = db.execute("SELECT * FROM assets WHERE id=? AND present=1", (asset_id,)).fetchone()
                if a is None:
                    raise ValueError("照片已不在当前目录")
                if decision == "delete" and (a["protected"] or a["role"] != "ordinary_jpeg"):
                    raise ValueError("受保护或非普通 JPG，不能加入待删")
                review_context=context.get(asset_id,'manual') if isinstance(context,dict) else context
                if not isinstance(review_context,str): raise ValueError('无效复核上下文')
                prior=db.execute('SELECT * FROM feedback WHERE asset_id=?',(asset_id,)).fetchone()
                # A repeated adoption cannot turn an explicit choice into an AI label.
                if a['decision']==decision and prior and (prior['source']=='manual' or source=='adopt'):
                    if source == 'manual':
                        # Reaffirming a keep invalidates older secondary suggestions,
                        # without creating duplicate learning samples or training events.
                        db.execute('UPDATE reviews SET updated=MAX(updated+0.000001,?) WHERE asset_id=?', (time.time(),asset_id))
                    continue
                db.execute("UPDATE assets SET decision=? WHERE id=?", (decision, asset_id))
                if decision == "pending":
                    db.execute("DELETE FROM reviews WHERE asset_id=?", (asset_id,))
                else:
                    db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?)", (asset_id, review_context, decision, time.time()))
                # Capture provenance and file identity in this same short transaction.
                # Folder protection alone is never a label. A deliberate manual
                # keep of its ordinary original is still an explicit preference.
                if a['role']=='ordinary_jpeg' and (not a['protected'] or (source=='manual' and decision=='keep')):
                    now=time.time()
                    db.execute('INSERT INTO feedback_events(asset_id,context,previous_decision,decision,source,size,mtime_ns,updated) VALUES (?,?,?,?,?,?,?,?)',
                               (asset_id,review_context,a['decision'],decision,source,a['size'],a['mtime_ns'],now))
                    if decision=='pending': db.execute('DELETE FROM feedback WHERE asset_id=?',(asset_id,))
                    else:
                        db.execute('INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?,?,?)',
                                   (asset_id,review_context,decision,source,a['size'],a['mtime_ns'],now))

    def feedback_revision(self):
        return self.one("SELECT COALESCE(MAX(id),0) AS revision FROM feedback_events WHERE source='manual'")['revision']

    def learning_state(self):
        row=self.one("SELECT data FROM learning_state WHERE key='automatic'")
        return json.loads(row['data']) if row else {}

    def feature(self, asset, version=None):
        row = self.one("SELECT * FROM features WHERE path=? AND size=? AND mtime_ns=?",
                       (asset["path"], asset["size"], asset["mtime_ns"]))
        if not row or (version is not None and row["version"] != version):
            return None
        return json.loads(row["data"])

    def save_feature(self, asset, version, data):
        self.execute("INSERT OR REPLACE INTO features VALUES (?,?,?,?,?)",
                     (asset["path"], asset["size"], asset["mtime_ns"], version, json.dumps(data)))

    def alias(self, asset_id, source_id):
        a, source = self.asset(asset_id), self.asset(source_id)
        if not a or not source or a["album"] != source["album"]:
            raise ValueError("只能关联同一拍摄组内的照片")
        if self.one("SELECT * FROM set_items WHERE asset_id=?", (asset_id,)):
            raise ValueError("请先从图集中移出这张图，再关联原片")
        self.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (a["path"], source["logical_key"]))

    def feature_snapshot(self, album, version=None):
        rows = self.rows("""SELECT f.* FROM features f JOIN assets a ON a.path=f.path
                            WHERE a.album=? AND f.size=a.size AND f.mtime_ns=a.mtime_ns""", (album,))
        return {r["path"]: json.loads(r["data"]) for r in rows if version is None or r["version"] == version}

    def digest_snapshot(self, album):
        prefix = album + "/"
        return {r["path"]: r for r in self.rows("SELECT * FROM digests WHERE path>=? AND path<?", (prefix, prefix+"\U0010ffff"))}

    def save_features(self, entries, version):
        if not entries: return
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO features VALUES (?,?,?,?,?)", [
                (a["path"], a["size"], a["mtime_ns"], version, json.dumps(data)) for a, data in entries])
            db.executemany("INSERT OR REPLACE INTO digests VALUES (?,?,?,?)", [
                (a["path"], a["size"], a["mtime_ns"], data["sha256"]) for a, data in entries if not data.get("error")])

    def groups(self, album):
        return [json.loads(r["data"]) for r in self.rows("SELECT data FROM groups WHERE album=? ORDER BY id", (album,))]

    def quality_report(self,album):
        row=self.one('SELECT data FROM quality_runs WHERE album=?',(album,))
        return json.loads(row['data']) if row else None
