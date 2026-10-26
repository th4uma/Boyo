"""Retrieve a limited number of plausible pairs, then verify geometry."""
import hashlib
import json

from .imaging import hash_distance, thumbnail_path
from .vision import MATCH_VERSION, Matcher, is_duplicate, is_related
from .screening import SECONDARY_STRENGTH


def find_neighbors(store, runtime, album, assets, features, strength, progress, control,review_neighbors=None):
    import cv2
    import numpy as np
    from sklearn.neighbors import NearestNeighbors
    cv2.setNumThreads(1)  # Outer extraction pool owns parallelism; no nested thread storm.
    neighbors={a['id']:{} for a in assets}
    if review_neighbors is not None: review_neighbors.update({a['id']:{} for a in assets})
    if len(assets)<2: return neighbors
    hashes=[int(features[a['id']]['phash'],16) for a in assets]
    bits=np.array([[(value>>bit)&1 for bit in range(64)] for value in hashes],dtype=np.uint8)
    search=NearestNeighbors(metric='hamming',algorithm='brute',n_jobs=1).fit(bits)
    pairs=set()
    exact_buckets={}
    for i,a in enumerate(assets):
        digest=features[a['id']].get('sha256')
        if digest: exact_buckets.setdefault(digest,[]).append(i)
    for indices in exact_buckets.values():
        anchor=min(indices,key=lambda i:(not assets[i]['protected'],assets[i].get('decision')!='keep',i))
        for i in indices:
            if i!=anchor: pairs.add(tuple(sorted((i,anchor))))
    secondary = strength == SECONDARY_STRENGTH
    neighbor_limit, hash_limit, frame_limit = (49,32,12) if secondary else (15,24,6)
    for start in range(0,len(assets),32):
        if not control.checkpoint(): return None
        distances,matches=search.kneighbors(bits[start:start+32],n_neighbors=min(neighbor_limit,len(assets)))
        for offset,(dd,indices) in enumerate(zip(distances,matches)):
            i=start+offset
            for distance,j in zip(dd,indices):
                j=int(j)
                if i!=j and distance*64<=hash_limit: pairs.add(tuple(sorted((i,j))))
            # Nearby frames also cover changed crops that global hashes miss.
            for j in range(i+1,min(i+frame_limit+1,len(assets))): pairs.add((i,j))
        progress(min(start+32,len(assets)),len(assets),'正在检索可能重复的构图')
    cache={r['cache_key']:json.loads(r['data']) for r in store.rows('SELECT * FROM pair_matches WHERE album=?',(album,))}
    matcher=Matcher(); writes=[]
    def flush():
        if writes:
            with store.connect() as db:
                db.executemany('INSERT OR REPLACE INTO pair_matches VALUES (?,?,?)',writes)
            writes.clear()
    try:
        for index,(i,j) in enumerate(sorted(pairs)):
            if not control.checkpoint(): return None
            a,b=assets[i],assets[j]; fa,fb=features[a['id']],features[b['id']]
            pa,pb=thumbnail_path(runtime,a),thumbnail_path(runtime,b)
            key=hashlib.sha256((MATCH_VERSION+'|'+ '|'.join(sorted((pa.name,pb.name)))).encode()).hexdigest()
            exact=fa.get('sha256') and fa['sha256']==fb.get('sha256')
            if exact:
                evidence={'method':'exact','reason':'文件内容完全相同'}
                related=evidence
            else:
                if key in cache: match=cache[key]
                else:
                    match=matcher.compare(pa,pb)
                    writes.append((album,key,json.dumps(match)))
                    if len(writes)>=64: flush()
                evidence={'method':'sift',**match} if is_duplicate(match,strength) else None
                related={'method':'sift',**match} if is_related(match,strength) else None
            if evidence or related:
                pd=int(hash_distance(fa['phash'],fb['phash'])); dd=int(hash_distance(fa['dhash'],fb['dhash']))
                if evidence:
                    edge=(pd,dd,evidence)
                    neighbors[a['id']][b['id']]=edge; neighbors[b['id']][a['id']]=edge
                if review_neighbors is not None and (related or evidence):
                    edge=(pd,dd,related or evidence)
                    review_neighbors[a['id']][b['id']]=edge; review_neighbors[b['id']][a['id']]=edge
            progress(index+1,len(pairs),f'构图几何校验 {index+1}/{len(pairs)}')
    finally:
        flush(); matcher.load.cache_clear()
    return neighbors
