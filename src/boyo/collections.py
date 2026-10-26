from __future__ import annotations

import time
import uuid
from pathlib import Path


from .imaging import hash_distance
from .fs import cached_digest


class Collections:
    """Collection membership never writes reviews or preference training data."""
    def __init__(self, store, root):
        self.store, self.root = store, Path(root)

    def create(self, album, name=None):
        count = self.store.one("SELECT COUNT(*) AS n FROM photo_sets WHERE album=?", (album,))["n"]
        set_id = uuid.uuid4().hex
        self.store.execute("INSERT INTO photo_sets(id,album,name,created) VALUES (?,?,?,?)",
                           (set_id, album, name or f"图集{count+1:03d}", time.time()))
        return set_id

    def items(self, set_id):
        return self.store.rows("""SELECT a.*,s.slot,s.locked FROM set_items s JOIN assets a ON a.id=s.asset_id
                                  WHERE s.set_id=? ORDER BY slot""", (set_id,))

    def list(self, album):
        return self.store.rows("SELECT * FROM photo_sets WHERE album=? ORDER BY created", (album,))

    def options(self,set_id):
        return self.store.one('SELECT * FROM set_options WHERE set_id=?',(set_id,)) or {'requested':9,'second_cover':0}

    def save_options(self,set_id,count,second_cover):
        self.store.execute('INSERT OR REPLACE INTO set_options VALUES (?,?,?)',(set_id,count,int(second_cover)))

    def _eligible(self, set_id, asset, replacing_slot=None):
        photo_set = self.store.one("SELECT * FROM photo_sets WHERE id=?", (set_id,))
        if not photo_set or not asset or asset["album"] != photo_set["album"] or not asset["present"]:
            raise ValueError("照片必须属于当前拍摄组")
        if asset["role"] not in {"ordinary_jpeg", "selected_jpeg", "finished", "extra_image"} or asset["decision"] == "delete":
            raise ValueError("待删或非图片文件不能加入图集")
        used = self.store.one("SELECT * FROM set_items WHERE logical_key=?", (asset["logical_key"],))
        if used and (used['set_id'],used['slot'])!=(set_id,replacing_slot):
            raise ValueError("同一照片（含已关联的原片/成片版本）已经属于一个图集")
        digest = cached_digest(self.store, self.root, asset["path"])
        used_content=self.store.one("SELECT * FROM set_items WHERE content_hash=?", (digest,))
        if used_content and (used_content['set_id'],used_content['slot'])!=(set_id,replacing_slot):
            raise ValueError("同一图片内容已被图集使用（即使文件名不同）")
        return digest

    def add(self, set_id, asset_id, slot=None, locked=True):
        asset = self.store.asset(asset_id)
        digest = self._eligible(set_id, asset)
        current = self.items(set_id)
        occupied = {i["slot"] for i in current}
        if slot is None:
            slot = next(i for i in range(1000) if i not in occupied)
        if slot in occupied:
            raise ValueError("位置已占用，请先移出原照片")
        if slot == 0 and asset["role"] != "finished":
            raise ValueError("第 1 张必须来自成片；请先选封面或自动补齐")
        self.store.execute("INSERT INTO set_items(set_id,slot,asset_id,logical_key,locked,content_hash) VALUES (?,?,?,?,?,?)",
                           (set_id, slot, asset_id, asset["logical_key"], int(locked), digest))

    def remove(self, set_id, slot):
        # Preserve slot numbers, including the vacant cover slot, until explicit reordering.
        self.store.execute("DELETE FROM set_items WHERE set_id=? AND slot=?", (set_id, slot))

    def replace(self,set_id,slot,asset_id):
        asset=self.store.asset(asset_id)
        digest=self._eligible(set_id,asset,replacing_slot=slot)
        if slot==0 and asset['role']!='finished': raise ValueError('第 1 张必须来自成片')
        with self.store.connect() as db:
            fresh=db.execute('SELECT * FROM assets WHERE id=?',(asset_id,)).fetchone()
            if not fresh or not fresh['present'] or fresh['decision']=='delete' or any(fresh[k]!=asset[k] for k in ('logical_key','size','mtime_ns')):
                raise ValueError('照片状态已变化')
            db.execute('DELETE FROM set_items WHERE set_id=? AND slot=?',(set_id,slot))
            db.execute('INSERT INTO set_items VALUES (?,?,?,?,1,?)',(set_id,slot,asset_id,asset['logical_key'],digest))

    def create_and_generate(self,album,count=9,second_cover=False,control=None):
        set_id=self.create(album)
        try: return {**self.generate(set_id,count,second_cover,control=control),'set_id':set_id}
        except Exception:
            self.drop(set_id)
            raise

    def lock(self, set_id, slot, value):
        self.store.execute("UPDATE set_items SET locked=? WHERE set_id=? AND slot=?", (int(value), set_id, slot))

    def reorder(self, set_id, slot, delta):
        target = slot + delta
        if target < 0:
            return
        items = {i["slot"]: i for i in self.items(set_id)}
        if target not in items or slot not in items:
            return
        if (slot == 0 and items[target]["role"] != "finished") or (target == 0 and items[slot]["role"] != "finished"):
            raise ValueError("首图必须来自成片")
        with self.store.connect() as db:
            db.execute("UPDATE set_items SET slot=-1 WHERE set_id=? AND slot=?", (set_id, slot))
            db.execute("UPDATE set_items SET slot=? WHERE set_id=? AND slot=?", (slot, set_id, target))
            db.execute("UPDATE set_items SET slot=? WHERE set_id=? AND slot=-1", (target, set_id))

    def drop(self, set_id):
        self.store.execute("DELETE FROM photo_sets WHERE id=?", (set_id,))

    def generate(self, set_id, count=9, second_cover=False, *, replace_unlocked=True, control=None):
        import numpy as np
        if not 1 <= count <= 30:
            raise ValueError("图集数量范围为 1—30")
        photo_set = self.store.one("SELECT * FROM photo_sets WHERE id=?", (set_id,))
        if not photo_set:
            raise ValueError("图集不存在")
        existing = self.items(set_id)
        locked = [i for i in existing if i["locked"] or not replace_unlocked]
        if any(i["slot"] >= count for i in locked):
            raise ValueError("锁定位置超出新数量，请先解锁或增大数量")
        occupied_rows=self.store.rows('SELECT * FROM set_items')
        occupied_rows=[r for r in occupied_rows if r['set_id']!=set_id or r['locked'] or not replace_unlocked]
        used = {r['logical_key'] for r in occupied_rows}
        used_hashes = {r['content_hash'] for r in occupied_rows}
        assets = [a for a in self.store.assets(photo_set["album"]) if a["logical_key"] not in used
                  and a["decision"] != "delete" and a["role"] in {"ordinary_jpeg", "finished"}]
        snapshot=self.store.feature_snapshot(photo_set['album'])
        features = {a['id']:snapshot.get(a['path']) for a in assets+locked}
        assets = [a for a in assets if features[a["id"]] and not features[a["id"]].get("error")
                  and features[a["id"]].get("sha256") not in used_hashes]
        covers = [a for a in assets if a["role"] == "finished"]
        ordinary = [a for a in assets if a["role"] == "ordinary_jpeg"]
        chosen = {i["slot"]: i for i in locked}
        if 0 not in chosen:
            if not covers:
                raise ValueError("没有未使用且已分析的成片可作封面。请先分析，或手动关联/释放已用图片。")
            chosen[0] = max(covers, key=lambda a: features[a["id"]]["quality"])
        if chosen[0]["role"] != "finished":
            raise ValueError("锁定的首图必须来自成片")

        def difference(a, b):
            fa, fb = features.get(a["id"]), features.get(b["id"])
            if not fa or not fb:
                return 0.0
            structure = hash_distance(fa["phash"], fb["phash"]) / 64
            color = float(np.linalg.norm(np.asarray(fa["vector"][-3:]) - np.asarray(fb["vector"][-3:]))) / 1.733
            return 0.8 * structure + 0.2 * color

        for slot in range(count):
            if control and not control.checkpoint(): raise ValueError('已取消组图，已有位置保留')
            if slot in chosen:
                continue
            keys = {i["logical_key"] for i in chosen.values()}
            hashes = {features[i["id"]].get("sha256") for i in chosen.values() if features.get(i["id"])}
            pool = covers if slot == 1 and second_cover else ordinary
            pool = [a for a in pool if a["logical_key"] not in keys and features[a["id"]].get("sha256") not in hashes]
            if not pool and slot == 1 and second_cover:
                pool = [a for a in ordinary if a["logical_key"] not in keys and features[a["id"]].get("sha256") not in hashes]
            if not pool:
                break  # Never repeat a photo to meet a quota.
            # Diversity/coverage policy, independent from the personal preference model.
            chosen[slot] = max(pool, key=lambda a: 0.8 * min(difference(a, b) for b in chosen.values()) + 0.2 * features[a["id"]]["quality"])
        with self.store.connect() as db:
            for asset in chosen.values():
                fresh=db.execute('SELECT * FROM assets WHERE id=?',(asset['id'],)).fetchone()
                if not fresh or not fresh['present'] or fresh['decision']=='delete' or any(fresh[k]!=asset[k] for k in ('logical_key','size','mtime_ns')):
                    raise ValueError('组图期间照片状态已变化，原图集保留，请重试')
            if replace_unlocked: db.execute("DELETE FROM set_items WHERE set_id=? AND locked=0", (set_id,))
            occupied = {i["slot"] for i in locked}
            for slot, a in chosen.items():
                if slot not in occupied:
                    db.execute("INSERT INTO set_items(set_id,slot,asset_id,logical_key,locked,content_hash) VALUES (?,?,?,?,0,?)",
                               (set_id, slot, a["id"], a["logical_key"], features[a["id"]]["sha256"]))
            db.execute('INSERT OR REPLACE INTO set_options VALUES (?,?,?)',(set_id,count,int(second_cover)))
        return {"count": len(chosen), "requested": count,
                "message": "按结构与色彩差异组图；当前不理解具体房间/铭牌等语义，也不使用个人偏好。"}
