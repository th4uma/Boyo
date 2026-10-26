from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path

from .fs import IMAGE_EXTS, RAW_EXTS, checked, rename_no_replace, sha256
from .scanner import Scanner


class Operations:
    """The only application service allowed to move/copy user photos.

    Planning never writes into an album. Execution re-inventories before touching it.
    Every operation is journalled; destinations use non-overwriting primitives.
    """
    def __init__(self, store, root, *, render_cache_root=None):
        self.store, self.root = store, Path(root).resolve(strict=True)
        self.control = None
        self.progress = None
        self.digest_rows = {}; self.digest_writes = []
        self.timings = {'inventory_ms': 0, 'hash_ms': 0, 'digest_cache_hits': 0}
        self.checks = []
        self.execution_timings = None
        self.render_cache_root = Path(render_cache_root) if render_cache_root else None

    def inventory(self, album):
        started = time.perf_counter()
        result = Scanner(self.store, self.root).scan(self.root / album, self.progress, control=self.control)
        if not result["complete"]:
            raise ValueError("扫描未完整完成，文件操作已停止：" + "; ".join(result["errors"][:3]))
        self.digest_rows = self.store.digest_snapshot(album)
        self.digest_writes = []
        self.timings['inventory_ms'] = round((time.perf_counter()-started)*1000, 2)
        if self.execution_timings is not None:
            self.execution_timings['inventory_ms'] += self.timings['inventory_ms']
        assets=self.store.assets(album)
        self.selected_by_stem=defaultdict(list)
        self.selected_keys=set();self.selected_raw_by_stem=defaultdict(list)
        self.sidecars_by_name=defaultdict(list)
        for asset in assets:
            if asset['role']=='selected_jpeg':
                self.selected_by_stem[Path(asset['path']).stem.casefold()].append(asset);self.selected_keys.add(asset['logical_key'])
            if asset['role']=='selected_raw' and Path(asset['path']).parent==Path(album)/'精选'/'raw':
                self.selected_raw_by_stem[asset['stem']].append(asset)
            if Path(asset['path']).suffix.lower()=='.xmp':self.sidecars_by_name[(Path(asset['path']).parent,asset['stem'])].append(asset)
        return assets

    def _snapshot(self, asset, dst, action, reason):
        p = checked(self.root, asset["path"], True)
        stat = p.stat()
        if (stat.st_size, stat.st_mtime_ns) != (asset["size"], asset["mtime_ns"]):
            raise ValueError("文件已变化，请重新生成清单")
        cached = self.digest_rows.get(asset['path'])
        if cached and (cached['size'], cached['mtime_ns']) == (stat.st_size, stat.st_mtime_ns):
            digest = cached['sha256']; self.timings['digest_cache_hits'] += 1
        else:
            started = time.perf_counter(); digest = sha256(p, self.control)
            self.timings['hash_ms'] += round((time.perf_counter()-started)*1000, 2)
            row = {'path': asset['path'], 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': digest}
            self.digest_rows[asset['path']] = row
            self.digest_writes.append(tuple(row[k] for k in ('path','size','mtime_ns','sha256')))
        after = p.stat()
        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("文件在生成清单时发生变化")
        return {"asset_id": asset["id"], "src": asset["path"], "dst": dst,
                "action": action, "reason": reason, "sha256": digest,
                "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def _raw_bundle(self, raw, assets, occupied, reason):
        items = [raw, *self._sidecars(raw, assets)]
        items = [a for a in items if not a["protected"]]
        return self._bundle_snapshots(raw,items,occupied,reason)

    def _bundle_snapshots(self,raw,items,occupied,reason):
        parent = Path(raw["album"]) / "deleteraw"
        base, index = Path(raw["path"]).stem, 0
        while True:
            stem = base if index == 0 else f"{base}__备份{index}"
            targets = [(parent / (stem + Path(a["path"]).suffix)).as_posix() for a in items]
            if all(t.casefold() not in occupied and not checked(self.root, t).exists() for t in targets):
                break
            index += 1
        occupied.update(t.casefold() for t in targets)
        return [self._snapshot(a, t, "move", reason) for a, t in zip(items, targets)]

    def _selected_cleanup_asset(self,asset):
        """Narrow exception for obsolete copies in 精选/raw; originals stay protected."""
        path=Path(asset['path'])
        if path.parent!=Path(asset['album'])/'精选'/'raw':return False
        if not (asset['role']=='selected_raw' and path.suffix.lower() in RAW_EXTS or
                asset['role']=='sidecar' and path.suffix.lower()=='.xmp'):return False
        matches=self.selected_raw_by_stem.get(asset['stem'],[])
        if len(matches)!=1:return False
        keys={asset['logical_key'],matches[0]['logical_key']}
        return not self.selected_by_stem.get(asset['stem']) and not (keys & self.selected_keys)

    def _target(self, parent, name, occupied):
        original = Path(parent) / name
        p, index = original, 1
        while p.as_posix().casefold() in occupied or checked(self.root, p.as_posix()).exists():
            p = original.with_name(f"{original.stem}__备份{index}{original.suffix}")
            index += 1
        occupied.add(p.as_posix().casefold())
        return p.as_posix()

    def _sidecars(self, raw, assets):
        parent = Path(raw["path"]).parent
        if hasattr(self,'sidecars_by_name'):return self.sidecars_by_name.get((parent,raw['stem']),[])
        return [a for a in assets if Path(a["path"]).parent == parent and a["stem"] == raw["stem"]
                and Path(a["path"]).suffix.lower() == ".xmp"]

    @staticmethod
    def _unique_raws(assets):
        by_key = defaultdict(list)
        for a in assets:
            if a["role"] == "ordinary_raw":
                by_key[a["logical_key"]].append(a)
        return by_key

    @staticmethod
    def _compatible(jpg, raw):
        left, right = json.loads(jpg["metadata"]), json.loads(raw["metadata"])
        if left.get("read_error") or right.get("read_error"):
            return False
        for key in ("camera", "capture"):
            if left.get(key) and right.get(key) and left[key].strip() != right[key].strip():
                return False
        return True

    def _save(self, album, kind, actions, warnings):
        plan = {"id": uuid.uuid4().hex, "album": album, "kind": kind, "created": time.time(),
                "actions": actions, "warnings": warnings, 'timings': dict(self.timings), 'checks': self.checks}
        with self.store.connect() as db:
            if self.digest_writes:
                db.executemany('INSERT OR REPLACE INTO digests VALUES (?,?,?,?)',self.digest_writes)
            db.execute("INSERT INTO plans VALUES (?,?,?,?,?,?)",
                       (plan["id"], album, kind, plan["created"], "preview", json.dumps(plan, ensure_ascii=False)))
        logging.info('文件检查 %s/%s: %s',album,kind,self.timings)
        return plan

    def deletion(self, album, include_raw=True, *, asset_ids=None):
        assets = self.inventory(album)
        scope = None if asset_ids is None else set(asset_ids)
        raws, occupied, actions, warnings = self._unique_raws(assets), set(), [], []
        included = set()
        for a in assets:
            if scope is not None and a['id'] not in scope:
                continue
            if scope is not None and a['protected']:
                warnings.append(f"重新检查后已受保护，未移动：{a['path']}")
                continue
            if a["decision"] != "delete":
                continue
            if a["protected"] or a["role"] != "ordinary_jpeg":
                warnings.append(f"跳过受保护或非普通 JPG：{a['path']}")
                continue
            # Validate the whole logical item before adding its JPG. Ambiguous
            # RAW pairing never silently degrades to a JPEG-only deletion.
            raw = None
            if include_raw:
                matches = raws.get(a["logical_key"], [])
                if len(matches) > 1:
                    warnings.append(f"RAW 配对不唯一，整项暂缓：{a['path']}")
                    continue
                if matches:
                    raw = matches[0]
                    if not self._compatible(a, raw):
                        warnings.append(f"RAW 相机/时间冲突或读取失败，整项暂缓：{a['path']}")
                        continue
                    if raw["protected"]:
                        warnings.append(f"对应 RAW 受保护，整项暂缓：{a['path']}")
                        continue
                else:
                    warnings.append(f"未发现对应 RAW，仅处理 JPG：{a['path']}")
            bundle = [raw, *self._sidecars(raw,assets)] if raw and raw['path'] not in included else []
            if any(item['protected'] for item in bundle):
                warnings.append(f"关联文件受保护，整项暂缓：{a['path']}"); continue
            dst = (Path('已删除备份')/album/Path(a['path']).name).as_posix()
            targets = [dst, *[(Path(album)/'deleteraw'/Path(item['path']).name).as_posix() for item in bundle]]
            if any(path.casefold() in occupied or checked(self.root,path).exists() for path in targets):
                warnings.append(f"备份目标已存在或同名冲突，整项暂缓：{a['path']}"); continue
            occupied.update(path.casefold() for path in targets)
            actions.append(self._snapshot(a,dst,'move','人工确认的待删 JPG'))
            actions.extend(self._snapshot(item,target,'move','与 JPG 联动的 RAW/XMP') for item,target in zip(bundle,targets[1:]))
            if raw: included.add(raw['path'])
        return self._save(album, "delete", actions, warnings)

    def orphan_raws(self, album):
        assets = self.inventory(album)
        jpeg_keys = {a["logical_key"] for a in assets if a["role"] in {"ordinary_jpeg", "selected_jpeg"}}
        raws, actions, occupied, warnings = self._unique_raws(assets), [], set(), []
        for key, matches in raws.items():
            if key in jpeg_keys:
                for raw in matches:self.checks.append({'asset_id':raw['id'],'path':raw['path'],'status':'有匹配 JPG','detail':'普通 JPG 与精选 JPG 均已检查，原文件保留'})
                continue
            if len(matches) != 1:
                for raw in matches:self.checks.append({'asset_id':raw['id'],'path':raw['path'],'status':'配对冲突','detail':'同名 RAW 不唯一，保留'})
                warnings.append(f"RAW 同名关系不唯一，保留：{key}")
                continue
            raw = matches[0]
            if raw["protected"]:
                self.checks.append({'asset_id':raw['id'],'path':raw['path'],'status':'保护排除','detail':'保留受保护 RAW'})
                continue
            bundle=self._raw_bundle(raw, assets, occupied, "本组普通及精选 JPG 中均无匹配；独立整理功能")
            actions.extend(bundle)
            self.checks.append({'asset_id':raw['id'],'path':raw['path'],'status':'孤立候选','detail':'；'.join(a['src']+' → '+a['dst'] for a in bundle)})
        return self._save(album, "orphan", actions, warnings)

    def fill_selected_raws(self, album):
        assets = self.inventory(album)
        selected_raw = {a["logical_key"] for a in assets if a["role"] == "selected_raw"}
        raws, actions, warnings, planned = self._unique_raws(assets), [], [], set()
        for jpg in assets:
            key = jpg["logical_key"]
            if jpg['role'] != 'selected_jpeg': continue
            check = {'asset_id':jpg['id'],'path':jpg['path'],'status':'可补齐','detail':''}
            self.checks.append(check)
            if key in selected_raw or key in planned:
                check['status']='已有目标'
                continue
            planned.add(key)
            matches = raws.get(key, [])
            if len(matches) != 1:
                check['status']='配对冲突' if matches else '未找到 RAW'
                warnings.append(f"未找到唯一 RAW，未复制：{jpg['path']}")
                continue
            raw = matches[0]
            if not self._compatible(jpg, raw):
                check['status']='相机/时间冲突'
                warnings.append(f"RAW 与精选相机/时间信息冲突或读取失败，未复制：{raw['path']}")
                continue
            for item in [raw, *self._sidecars(raw, assets)]:
                # Keep the JPEG stem when the selected copy has been renamed.
                name = Path(jpg["path"]).stem + Path(item["path"]).suffix
                dst = (Path(album) / "精选" / "raw" / name).as_posix()
                if checked(self.root, dst).exists():
                    check['status']='已有目标'
                    warnings.append(f"目标已存在，保留原文件：{dst}")
                    continue
                actions.append(self._snapshot(item, dst, "copy", "补齐精选 RAW/XMP（复制）"))
                check['detail']=dst
        occupied={item['dst'].casefold() for item in actions}
        for matches in self.selected_raw_by_stem.values():
            raw=matches[0]
            if self.selected_by_stem.get(raw['stem']) or raw['logical_key'] in self.selected_keys:continue
            check={'asset_id':raw['id'],'path':raw['path'],'status':'精选孤立候选','detail':''};self.checks.append(check)
            items=[raw,*self._sidecars(raw,assets)]
            if not all(self._selected_cleanup_asset(item) for item in items):
                check['status']='配对冲突';warnings.append(f"精选 RAW/XMP 配对不唯一或有关联精选 JPG，保留：{raw['path']}");continue
            bundle=self._bundle_snapshots(raw,items,occupied,'精选 JPG 已移出，清理精选 RAW/XMP 副本')
            actions.extend(bundle);check['detail']='；'.join(a['src']+' → '+a['dst'] for a in bundle)
        return self._save(album, "fill_raw", actions, warnings)

    def export_set(self, set_id):
        photo_set = self.store.one("SELECT * FROM photo_sets WHERE id=?", (set_id,))
        if not photo_set:
            raise ValueError("图集不存在")
        album = photo_set["album"]
        self.inventory(album)
        items = self.store.rows("SELECT s.*,a.path,a.present,a.role,a.protected FROM set_items s JOIN assets a ON a.id=s.asset_id WHERE set_id=? ORDER BY slot", (set_id,))
        if not items or items[0]["slot"] != 0 or items[0]["role"] != "finished":
            raise ValueError("图集首图必须来自成片")
        folder = photo_set["name"]
        if not folder or folder in {".", ".."} or any(c in folder for c in '\\/:*?"<>|') or folder.endswith((".", " ")):
            raise ValueError("图集名称不能作为有效目录名")
        actions, occupied = [], set()
        for item in items:
            a = self.store.asset(item["asset_id"])
            if not a["present"]:
                raise ValueError(f"照片已不在原目录：{a['path']}")
            name = f"{item['slot']+1:02d}_{Path(a['path']).name}"
            dst = self._target(Path(album) / "图集" / folder, name, occupied)
            actions.append(self._snapshot(a, dst, "copy", "图集导出副本"))
        plan = self._save(album, "export", actions, [])
        plan["set_id"] = set_id
        plan['set_signature'] = [[item['slot'],item['asset_id']] for item in items]
        plan['set_name'] = folder
        self.store.execute("UPDATE plans SET data=? WHERE id=?", (json.dumps(plan, ensure_ascii=False), plan["id"]))
        return plan

    def export_styled_set(self,set_id,prepared):
        if self.render_cache_root is None:raise ValueError('缺少调色缓存目录')
        plan=self.export_set(set_id)
        current=self.store.rows('SELECT s.slot,a.id,a.size,a.mtime_ns FROM set_items s JOIN assets a ON a.id=s.asset_id WHERE s.set_id=? ORDER BY s.slot',(set_id,))
        if [[a['slot'],a['id'],a['size'],a['mtime_ns']] for a in current]!=prepared['items']:
            raise ValueError('调色期间图集或源照片发生变化，未导出')
        parent=Path(plan['album'])/'图集';base=plan['set_name']+'_调色';folder=base;number=2
        while checked(self.root,(parent/folder).as_posix()).exists():folder=f'{base}{number}';number+=1
        outputs=prepared['outputs'];actions=[]
        for index,item in enumerate(plan['actions']):
            rendered=outputs.get(item['asset_id'])
            if index and not rendered:raise ValueError('调色结果不完整，未导出')
            name=f'{index+1:02d}_'+Path(item['src']).name
            if rendered:
                cache=checked(self.render_cache_root,rendered['cache'],True)
                if sha256(cache)!=rendered['sha256']:raise ValueError('调色缓存已改变，未导出')
                name=Path(name).with_suffix('.jpg').name
            actions.append({**item,'dst':(parent/folder/name).as_posix(),
                **({'render_cache':rendered['cache'],'render_sha256':rendered['sha256']} if rendered else {}),
                'reason':'按封面 XMP 调色并动态匹配（副本）' if rendered else '封面原样复制'})
        plan.update(kind='style_export',actions=actions,style={k:v for k,v in prepared.items() if k!='outputs'},output_folder=(parent/folder).as_posix())
        self.store.execute('UPDATE plans SET kind=?,data=? WHERE id=?',('style_export',json.dumps(plan,ensure_ascii=False),plan['id']))
        return plan

    def get_plan(self, plan_id):
        row = self.store.one("SELECT * FROM plans WHERE id=?", (plan_id,))
        if not row:
            raise ValueError("清单不存在")
        return row, json.loads(row["data"])

    def exclude_from_deletion(self, plan_id, jpeg_ids):
        """Create a smaller immutable plan, removing a kept photo AND its RAW/XMP.

        This API can only subtract from an existing preview; it cannot add or rewrite paths.
        """
        row, plan = self.get_plan(plan_id)
        if row["state"] != "preview" or plan["kind"] != "delete":
            raise ValueError("只能修改尚未执行的待删清单")
        assets={a['id']:a for a in self.store.rows('SELECT * FROM assets WHERE album=?',(plan['album'],))}
        if any(a['asset_id'] not in assets for a in plan['actions']):raise ValueError('清单照片记录已变化，请重新检查')
        allowed = {a["asset_id"] for a in plan["actions"] if assets[a['asset_id']]["role"] == "ordinary_jpeg"}
        if not set(jpeg_ids).issubset(allowed):
            raise ValueError("只能移出本清单中的 JPG")
        keys = {assets[i]["logical_key"] for i in jpeg_ids}
        actions = [a for a in plan["actions"] if assets[a["asset_id"]]["logical_key"] not in keys]
        updated = {**plan, "id": uuid.uuid4().hex, "created": time.time(), "actions": actions,
                   "warnings": [*plan["warnings"], f"复核移出 {len(keys)} 张逻辑照片，关联 RAW/XMP 已同时移出清单。"]}
        with self.store.connect() as db:
            changed = db.execute("UPDATE plans SET state='superseded' WHERE id=? AND state='preview'", (plan_id,)).rowcount
            if changed != 1:
                raise ValueError("原清单已变化，请重新生成")
            db.execute("INSERT INTO plans VALUES (?,?,?,?,?,?)", (updated["id"], updated["album"], updated["kind"],
                       updated["created"], "preview", json.dumps(updated, ensure_ascii=False)))
        return updated

    def _validate_structure(self, plan, item, asset):
        src, dst, album = Path(item["src"]), Path(item["dst"]), plan["album"]
        if item["action"] not in {"copy", "move"} or asset["album"] != album or asset["path"] != item["src"]:
            raise ValueError("清单来源无效")
        selected_cleanup=(plan['kind']=='fill_raw' and item['action']=='move' and self._selected_cleanup_asset(asset)
                          and dst.parent==Path(album)/'deleteraw')
        if item["action"] == "move" and asset["protected"] and not selected_cleanup:
            raise ValueError(f"照片已受保护：{item['src']}")
        kind = plan["kind"]
        if kind in {"delete", "orphan"}:
            if item["action"] != "move":
                raise ValueError("移动清单类型无效")
            if asset["role"] == "ordinary_jpeg" and kind == "delete":
                if asset["decision"] != "delete" or dst.parent != Path("已删除备份") / album:
                    raise ValueError("JPG 状态或备份位置已变化")
            elif asset["role"] in {"ordinary_raw", "sidecar"}:
                if dst.parent != Path(album) / "deleteraw" or src.suffix.lower() not in RAW_EXTS | {".xmp"}:
                    raise ValueError("RAW/XMP 备份位置不正确")
            else:
                raise ValueError("此类文件禁止移动")
        elif kind == "fill_raw":
            if item['action']=='move':
                if not selected_cleanup:raise ValueError('精选 RAW 已有匹配 JPG 或清理范围无效，请重新检查')
                return
            if item["action"] != "copy" or dst.parent != Path(album) / "精选" / "raw" or src.suffix.lower() not in RAW_EXTS | {".xmp"}:
                raise ValueError("补齐精选的清单无效")
            selected=self.selected_by_stem.get(dst.stem.casefold(),[])
            if len(selected)!=1 or selected[0]['logical_key']!=asset['logical_key'] or not self._compatible(selected[0],asset):
                raise ValueError('精选 JPG 的对应关系已变化，请重新检查补齐清单')
        elif kind in {"export","style_export"}:
            base = Path(album) / "图集"
            if item["action"] != "copy" or not dst.parent.is_relative_to(base) or src.suffix.lower() not in IMAGE_EXTS:
                raise ValueError("图集导出位置或类型无效")
            if kind=='style_export':
                if asset['decision']=='delete':raise ValueError('图集照片已标记待删，停止调色导出')
                if item.get('render_cache'):
                    if self.render_cache_root is None:raise ValueError('调色缓存目录不可用')
                    checked(self.render_cache_root,item['render_cache'],True)
        else:
            raise ValueError("未知清单类型")

    def execute(self, plan_id, progress=None, control=None):
        from .protection_watch import ProtectionWatch
        row, plan = self.get_plan(plan_id)
        started = time.perf_counter()
        metrics = self.execution_timings = dict(inventory_ms=0,hash_ms=0,protection_ms=0,
            protection_walks=0,protection_reuses=0,total_ms=0)
        try:
            with ProtectionWatch(self.root/plan['album']) as watch:
                result = self._execute(row,plan,progress,control,watch)
        finally:
            metrics['total_ms'] = round((time.perf_counter()-started)*1000,2)
            logging.info('文件执行 %s/%s: %s',plan['album'],plan['kind'],metrics)
            self.execution_timings = None
        result['timings'] = metrics
        return result

    def _execution_digest(self,path):
        started = time.perf_counter()
        try: return sha256(path,self.control)
        finally: self.execution_timings['hash_ms'] += round((time.perf_counter()-started)*1000,2)

    def _execution_protection_stamp(self,album):
        started = time.perf_counter()
        try: return self._protection_stamp(album)
        finally:
            self.execution_timings['protection_walks'] += 1
            self.execution_timings['protection_ms'] += round((time.perf_counter()-started)*1000,2)

    def _execute(self, row, plan, progress, control, watch):
        self.control, self.progress = control, progress
        plan_id = plan['id']
        if row["state"] != "preview":
            raise ValueError("清单已执行或已失效，不能重复执行")
        if plan['kind'] in {'export','style_export'} and 'set_signature' in plan:
            photo_set=self.store.one('SELECT album,name FROM photo_sets WHERE id=?',(plan['set_id'],))
            items=self.store.rows('SELECT slot,asset_id FROM set_items WHERE set_id=? ORDER BY slot',(plan['set_id'],))
            signature=[[item['slot'],item['asset_id']] for item in items]
            if not photo_set or photo_set['album']!=plan['album'] or photo_set['name']!=plan['set_name'] or signature!=plan['set_signature']:
                raise ValueError('图集在检查后已改变，请在本页重新检查导出')
        assets = self.inventory(plan["album"])
        protection_stamp = self._execution_protection_stamp(plan["album"])
        by_id = {a["id"]: a for a in assets}
        occupied = set()
        for index,item in enumerate(plan["actions"]):
            if control and not control.checkpoint():
                raise ValueError("已取消，尚未移动文件")
            asset = by_id.get(item["asset_id"])
            if not asset:
                raise ValueError("源文件已移走，请重新生成清单")
            self._validate_structure(plan, item, asset)
            src, dst = checked(self.root, item["src"], True), checked(self.root, item["dst"])
            stat = src.stat()
            if progress:progress(0,len(plan['actions']),f"执行前内容校验 {index+1}/{len(plan['actions'])}")
            if (stat.st_size, stat.st_mtime_ns) != (item["size"], item["mtime_ns"]) or ((item['action']=='move' or plan['kind']=='style_export') and self._execution_digest(src) != item["sha256"]):
                raise ValueError(f"文件在预览后发生变化：{item['src']}")
            if dst.exists() or item["dst"].casefold() in occupied:
                raise ValueError(f"目标已存在，拒绝覆盖：{item['dst']}")
            occupied.add(item["dst"].casefold())
            if plan["kind"] == "orphan" and asset["role"] == "ordinary_raw":
                if any(a["role"] in {"ordinary_jpeg", "selected_jpeg"} and a["logical_key"] == asset["logical_key"] for a in assets):
                    raise ValueError("RAW 已有匹配 JPG，清单失效")
        # Claim the plan atomically to prevent duplicate execution from multiple windows.
        with self.store.connect() as db:
            changed = db.execute("UPDATE plans SET state='running' WHERE id=? AND state='preview'", (plan_id,)).rowcount
            if changed != 1:
                raise ValueError("清单正在执行")
        done, error = 0, None
        for seq, item in enumerate(plan["actions"]):
            if control and not control.checkpoint():
                error = "已取消，已完成的项目保留在操作记录中"
                break
            src, dst = checked(self.root, item["src"], True), checked(self.root, item["dst"])
            self.store.execute("INSERT INTO journal(plan_id,seq,src,dst,action,sha256,state) VALUES (?,?,?,?,?,?,?)",
                               (plan_id, seq, item["src"], item["dst"], item["action"], item.get('render_sha256',item["sha256"]), "intent"))
            try:
                # Recheck immediately before the individual operation too.
                if item["action"] == "move":
                    if watch.unchanged():
                        self.execution_timings['protection_reuses'] += 1
                    elif self._execution_protection_stamp(plan["album"]) != protection_stamp:
                        raise ValueError("精选/成片目录发生变化，已停止剩余移动，请重新生成清单")
                current=src.stat()
                if ((current.st_size,current.st_mtime_ns)!=(item['size'],item['mtime_ns']) or dst.exists()
                        or ((item['action']=='move' or plan['kind']=='style_export') and self._execution_digest(src)!=item['sha256'])):
                    raise ValueError("源文件或目标位置刚刚发生变化")
                dst.parent.mkdir(parents=True, exist_ok=True)
                checked(self.root, item["dst"])
                if item["action"] == "move":
                    rename_no_replace(src, dst)
                else:
                    copy_source=checked(self.render_cache_root,item['render_cache'],True) if item.get('render_cache') else src
                    self._copy(copy_source, dst, item.get('render_sha256',item["sha256"]), control)
                # Advance only entries changed by this operation. Unrelated
                # external edits still differ from the expected protection stamp.
                if item['action']=='move':protection_stamp=[entry for entry in protection_stamp if entry[0]!=str(src)]
                if any(dst.is_relative_to(self.root/plan['album']/folder) for folder in ('精选','成片')):
                    result_stat=dst.stat();protection_stamp.append((str(dst),result_stat.st_size,result_stat.st_mtime_ns));protection_stamp.sort()
                self.store.execute("UPDATE journal SET state='done' WHERE plan_id=? AND seq=?", (plan_id, seq))
                done += 1
            except Exception as exc:
                error = str(exc)
                self.store.execute("UPDATE journal SET state='failed',error=? WHERE plan_id=? AND seq=?", (error, plan_id, seq))
                break
            if progress:
                progress(done, len(plan["actions"]), f"完成 {done}/{len(plan['actions'])}")
        state = "done" if error is None else "partial"
        self.store.execute("UPDATE plans SET state=? WHERE id=?", (state, plan_id))
        self.inventory(plan["album"])
        return {"done": done, "total": len(plan["actions"]), "state": state, "error": error}

    def _protection_stamp(self, album):
        stamp = []
        for name in ("精选", "成片"):
            folder = checked(self.root, (Path(album) / name).as_posix())
            if not folder.exists():
                continue
            for directory, dirs, files in os.walk(folder, followlinks=False):
                for filename in files:
                    p = Path(directory) / filename
                    checked(self.root, p.relative_to(self.root).as_posix(), True)
                    stat = p.stat()
                    stamp.append((str(p), stat.st_size, stat.st_mtime_ns))
        return sorted(stamp)

    @staticmethod
    def _copy(src, dst, expected, control=None):
        temporary = dst.with_name(dst.name + ".boyo-" + uuid.uuid4().hex + ".pending")
        try:
            with src.open("rb") as reader, temporary.open("xb") as writer:
                import hashlib
                copied=hashlib.sha256()
                for block in iter(lambda:reader.read(1024*1024),b''):
                    if control and not control.checkpoint(): raise ValueError('复制已取消，原文件保留')
                    writer.write(block); copied.update(block)
                writer.flush(); os.fsync(writer.fileno())
            if copied.hexdigest()!=expected or sha256(temporary,control) != expected:
                raise ValueError("复制校验失败")
            shutil.copystat(src, temporary)
            rename_no_replace(temporary, dst)
        finally:
            if temporary.exists():
                temporary.unlink()  # Only this function's uniquely named temporary file.

    def restore(self, journal_id):
        row = self.store.one("SELECT * FROM journal WHERE id=?", (journal_id,))
        if not row or row["action"] != "move" or row["state"] not in {"done", "intent", "failed"}:
            raise ValueError("此记录不能恢复（复制的导出文件不执行删除）")
        original = checked(self.root, row["src"])
        backup = checked(self.root, row["dst"], True)
        if original.exists():
            raise ValueError("原位置已有文件，拒绝覆盖")
        if sha256(backup) != row["sha256"]:
            raise ValueError("备份文件已变化，拒绝自动恢复")
        self.store.execute("UPDATE journal SET state='restoring' WHERE id=?", (journal_id,))
        original.parent.mkdir(parents=True, exist_ok=True)
        checked(self.root, row["src"])
        rename_no_replace(backup, original)
        self.store.execute("UPDATE journal SET state='restored' WHERE id=?", (journal_id,))
        self.store.execute("UPDATE assets SET decision='pending' WHERE path=?", (row["src"],))
        self.store.execute("DELETE FROM reviews WHERE asset_id IN (SELECT id FROM assets WHERE path=?)", (row["src"],))
        plan = self.store.one("SELECT album FROM plans WHERE id=?", (row["plan_id"],))
        self.inventory(plan["album"])
        return {"restored": row["src"]}
