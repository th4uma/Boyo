from __future__ import annotations

import hashlib
import json
import shutil
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .fs import checked
from .imaging import FEATURE_VERSION, extract, thumbnail_path
from .tasks import Control, worker_count
from .vision import MATCH_VERSION, descriptor_path
from .grouping import GROUPING_VERSION, merge_review_groups
from .screening import is_secondary, SECONDARY, SECONDARY_STRENGTH


def direct_suggestions(assets, neighbors, scores, retain=None):
    """A candidate must directly match a retained representative: no chain deletion."""
    by_id = {a["id"]: a for a in assets}
    # Explicit keeps remain representatives; an explicit delete can never anchor a group.
    order = sorted((k for k in by_id if by_id[k].get('decision') != 'delete'),
                   key=lambda k: (not by_id[k]["protected"], by_id[k].get('decision') != 'keep', -scores.get(k, 0), by_id[k]["path"]))
    order += [k for k in by_id if by_id[k].get('decision') == 'delete']
    keep, groups = [], {}
    for asset_id in order:
        asset = by_id[asset_id]
        if asset["protected"] or asset.get('decision')=='keep':
            keep.append(asset_id)
            continue
        refs = [k for k in keep if k in neighbors.get(asset_id, {})
                and (retain is None or not retain(asset_id,k,neighbors[asset_id][k]))]
        if refs:
            ref = min(refs, key=lambda k: neighbors[asset_id][k][0])
            edge=neighbors[asset_id][ref]
            member={"id": asset_id, "phash_distance": edge[0], "dhash_distance": edge[1]}
            if len(edge)>2: member['evidence']=edge[2]
            groups.setdefault(ref, []).append(member)
        else:
            if asset.get('decision') != 'delete': keep.append(asset_id)
    return [{"keep": ref, "candidates": members} for ref, members in groups.items()]


def analyze(store, runtime, root, album, settings, progress, control):
    scan = store.one("SELECT * FROM albums WHERE path=?", (album,))
    if not scan or not scan["complete"]:
        raise ValueError("请先完整扫描拍摄组")
    assets = [a for a in store.assets(album) if a["role"] in {"ordinary_jpeg", "selected_jpeg", "finished", "extra_image"}]
    secondary = is_secondary(settings)
    review_stamps = store.review_stamps(album) if secondary else {}
    if secondary:
        assets = [a for a in assets if a['protected'] or a['role'] != 'ordinary_jpeg'
                  or a['decision'] in {'pending','keep'}]
    strength = SECONDARY_STRENGTH if secondary else settings['strength']
    snapshot = store.feature_snapshot(album, FEATURE_VERSION)
    digests = store.digest_snapshot(album)
    total, features, cached, failures, writes = len(assets), {}, 0, [], []
    shared, jobs, linked, pending_digests = {}, [], {}, {}
    for asset in assets:
        data = snapshot.get(asset["path"])
        if data and data.get("sha256") and not data.get("error") and thumbnail_path(runtime, asset).exists() and descriptor_path(thumbnail_path(runtime,asset)).exists():
            shared[data["sha256"]] = (data, thumbnail_path(runtime, asset))

    def cancelled():
        return {"cancelled": True, "cached": cached, "total": total, "failures": failures}

    def remember(asset, data):
        if data.get("error"): failures.append({"path": asset["path"], "error": data["error"]})
        else: features[asset["id"]] = data

    def flush():
        store.save_features(writes, FEATURE_VERSION); writes.clear()

    for index, asset in enumerate(assets):
        if not control.checkpoint(): return cancelled()
        path = checked(root, asset["path"], True); stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (asset["size"], asset["mtime_ns"]):
            raise ValueError("目录已变化，请重新扫描")
        thumb = thumbnail_path(runtime, asset)
        data = snapshot.get(asset["path"])
        digest = digests.get(asset["path"])
        digest = digest["sha256"] if digest and (digest["size"], digest["mtime_ns"]) == (asset["size"], asset["mtime_ns"]) else None
        if data and not data.get("error") and thumb.exists() and descriptor_path(thumb).exists():
            cached += 1; remember(asset, data)
        elif digest in shared:
            data, original_thumb = shared[digest]
            if original_thumb != thumb:
                shutil.copy2(original_thumb, thumb)
                shutil.copy2(descriptor_path(original_thumb), descriptor_path(thumb))
            writes.append((asset, data)); cached += 1; remember(asset, data)
        elif digest and digest in pending_digests:
            linked.setdefault(pending_digests[digest], []).append(asset)
        else:
            jobs.append((asset, path, thumb))
            if digest: pending_digests[digest] = asset["id"]
        progress(index+1, total, f"正在核对缓存 {index+1}/{total}")
    if writes: flush()

    def compute(job):
        asset, path, thumb = job
        try:
            data = extract(path, thumb)
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (asset["size"], asset["mtime_ns"]):
                raise ValueError("照片在分析期间发生变化，请重新扫描")
        except Exception as exc:
            data = {"error": str(exc)}
        return asset, data, thumb

    workers = worker_count(settings)
    delay = max(0, int(settings.get("delay_ms", 0)))/1000
    if jobs:
        remaining, active = deque(jobs), {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="boyo-photo") as pool:
            try:
                while remaining or active:
                    if not control.checkpoint():
                        for future in active: future.cancel()
                        return cancelled()
                    while remaining and len(active) < workers:
                        job = remaining.popleft(); active[pool.submit(compute, job)] = job
                    finished, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                    for future in finished:
                        active.pop(future)
                        asset, data, thumb = future.result()
                        writes.append((asset, data)); remember(asset, data)
                        for copy in linked.get(asset["id"], []):
                            copy_thumb = thumbnail_path(runtime, copy)
                            if not data.get("error"):
                                if copy_thumb != thumb:
                                    shutil.copy2(thumb, copy_thumb)
                                    shutil.copy2(descriptor_path(thumb), descriptor_path(copy_thumb))
                                cached += 1
                            writes.append((copy, data)); remember(copy, data)
                        progress(len(features)+len(failures), total, f"图像分析 {len(features)+len(failures)}/{total} · 缓存 {cached} · 并行 {workers}")
                        if len(writes) >= 16: flush()
                    if finished and delay: control.cancel.wait(delay)
            finally:
                if writes: flush()

    from .quality import screen_quality
    quality = screen_quality(store,runtime,root,album,settings,progress,control,
                             feature_data={a['path']:features[a['id']] for a in assets if a['id'] in features})
    if quality.get('cancelled'): return cancelled()
    from .preferences import load_model, personal_score
    model = load_model(runtime,store.path)
    run_signature = hashlib.sha256(json.dumps({"backend": FEATURE_VERSION, "matching":MATCH_VERSION, "grouping":GROUPING_VERSION, "strength": strength, "model": model,
        "screening_mode": SECONDARY if secondary else 'normal', "review_stamps":review_stamps,
        "assets": [[a[k] for k in ("id", "path", "role", "logical_key", "size", "mtime_ns", "protected", "decision")] for a in assets]}, sort_keys=True).encode()).hexdigest()
    prior = store.one("SELECT * FROM analysis_runs WHERE album=?", (album,))
    if not control.checkpoint(): return cancelled()
    if not failures and not jobs and prior and prior["signature"] == run_signature:
        progress(total, total, "照片与设置未变化，复用相似分组")
        return {**json.loads(prior["summary"]), "quality_candidates":quality['candidates'], "quality":quality,
                "cached": cached, "reused_groups": True}
    # Collapse selected copies by logical photo; edited/extra outputs are not delete candidates.
    representatives = {}
    for a in sorted(assets, key=lambda a: a["role"] != "ordinary_jpeg"):
        if a["id"] in features and a["role"] in {"ordinary_jpeg", "selected_jpeg"}:
            representatives.setdefault(a["logical_key"], a)
    candidates = list(representatives.values())
    if secondary:
        # A calculation-only overlay. No decision, review or feedback is written.
        candidates = [{**a,'decision':'pending'} for a in candidates]
    from .matching import find_neighbors
    review_neighbors={}
    neighbors=find_neighbors(store,runtime,album,candidates,features,strength,progress,control,review_neighbors=review_neighbors)
    if neighbors is None: return cancelled()
    embeddings={}
    if model and model.get('rank') and model['rank'].get('visual'):
        from .embeddings import ensure_embeddings
        embeddings=ensure_embeddings(store,runtime,candidates,features,control,progress)
        if not control.checkpoint(): return cancelled()
    scores = {a['id']: .8*personal_score(runtime,features[a['id']],model,embeddings.get(a['id']))
              + .2*min(1.,features[a['id']]['metrics'].get('acutance',0)/1.5) for a in candidates}
    from .preferences import retain_both
    groups = direct_suggestions(candidates, neighbors, scores,
        retain=None if secondary else lambda left,right,edge:retain_both(model,features[left],features[right],edge))
    for g in groups:
        g['kind']='duplicate'
        reference=features[g['keep']]['metrics'].get('acutance',0)
        for member in g['candidates']:
            fm=features[member['id']]['metrics']
            if reference>0 and fm.get('acutance',reference)<reference*.6:
                member['quality_note']='同构图中边缘清晰度明显较低'
    for g in groups:
        signature = f"{album}|{g.get('kind')}|{g['keep']}|" + ",".join(str(m["id"]) for m in g["candidates"])
        g["id"] = hashlib.sha256(signature.encode()).hexdigest()[:20]
        g["strength"] = strength
        g["backend"] = FEATURE_VERSION
    groups=merge_review_groups(candidates,neighbors,groups,scores,album,review_neighbors=review_neighbors)
    if secondary:
        from .grouping import group_members
        for group in groups:
            group['screening_mode'] = SECONDARY
            group['review_stamps'] = {str(aid):review_stamps[str(aid)] for aid in group_members(group)}
    if not control.checkpoint():
        return {"cancelled": True, "cached": cached, "failures": failures}
    summary = {"cancelled": False, "screening_mode":SECONDARY if secondary else 'normal', "groups": len(groups), "candidates": sum(len(g["candidates"]) for g in groups),
               "quality_candidates":quality['candidates'], "quality":quality,
               "cached": cached, "total": total, "failures": failures, "workers": workers, "reused_groups": False}
    with store.connect() as db:
        db.execute("DELETE FROM groups WHERE album=?", (album,))
        for g in groups:
            db.execute("INSERT INTO groups VALUES (?,?,?)", (g["id"], album, json.dumps(g)))
        if not failures:
            db.execute("INSERT OR REPLACE INTO analysis_runs VALUES (?,?,?)", (album, run_signature, json.dumps(summary)))
    return summary
