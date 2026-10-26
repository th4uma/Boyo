"""Small sklearn models trained from ordinary keep/delete actions, with provenance."""
from __future__ import annotations

import json
import hashlib
import math
import time
from collections import defaultdict

from .imaging import FEATURE_VERSION
from .embeddings import EMBEDDING_VERSION, ensure_embeddings

MODEL_VERSION='behavior-v1'
MIN_NEW_ACTIONS=12
MAX_PHOTOS=2048
MAX_PAIRS_PER_GROUP=64
_UNSET=object()


def model_path(runtime,database=None):
    return runtime.home/'models'/'personal'/((database or runtime.db_path).stem+'.json')


def load_model(runtime,database=None):
    try:
        model=json.loads(model_path(runtime,database).read_text(encoding='utf-8'))
        if isinstance(model,dict) and model.get('enabled') and model.get('version')==MODEL_VERSION and model.get('feature_version')==FEATURE_VERSION:
            return model
    except (OSError,ValueError,TypeError): pass
    return None


def technical_vector(feature):
    vector=feature.get('vector',[])
    metrics=feature.get('metrics',{})
    values=[*vector,min(3.,metrics.get('acutance',0))/3.,min(3.,metrics.get('detail_acutance',0))/3.,
            metrics.get('clipping',0),metrics.get('brightness',.5)]
    return values if len(vector)==9 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in values) else None


def linear_score(head,vector):
    if not isinstance(head,dict) or vector is None: return None
    try:
        if not len(vector)==len(head['coef'])==len(head['mean'])==len(head['scale']): return None
        if not all(isinstance(s,(int,float)) and math.isfinite(s) and s>0 for s in head['scale']): return None
        value=head.get('intercept',0.)+sum((x-m)/s*c for x,m,s,c in zip(vector,head['mean'],head['scale'],head['coef']))
        if not math.isfinite(value): return None
        return value
    except (KeyError,TypeError,ValueError,ZeroDivisionError): return None


def probability(head,vector):
    value=linear_score(head,vector)
    return None if value is None else 1/(1+math.exp(-max(-30,min(30,value))))


def base_score(feature):
    return .8*feature['quality']+.2*min(1.,feature.get('metrics',{}).get('acutance',0)/1.5)


def personal_score(runtime,feature,model=_UNSET,embedding=None):
    base=feature['quality']
    if model is _UNSET: model=load_model(runtime)
    if not model or not (head:=model.get('rank')): return base
    vector=technical_vector(feature)
    if vector is None: return base
    if head.get('visual'):
        if embedding is None or head.get('embedding_version')!=EMBEDDING_VERSION: return base
        vector=[*vector,*embedding]
    value=linear_score(head,vector)
    # A linear pairwise margin preserves relative differences; sigmoid saturation
    # on absolute image vectors would destroy them. Influence is validated below.
    return base if value is None else base+head.get('influence',0.)*value


def pair_vector(left,right,edge):
    a,b=technical_vector(left),technical_vector(right)
    if a is None or b is None: return None
    # Same inputs at training and inference; deletion thresholds remain in matching.py.
    return [abs(x-y) for x,y in zip(a,b)]+[edge[0]/64.,edge[1]/64.]


def retain_both(model,left,right,edge):
    p=probability(model.get('retention') if model else None,pair_vector(left,right,edge))
    return p is not None and p>=.75


def quality_hint(model,asset,feature):
    try: camera=json.loads(asset.get('metadata','{}')).get('camera','')
    except (ValueError,TypeError): camera=''
    head=model.get('quality',{}).get(camera) if model else None
    return probability(head,quality_vector(feature.get('metrics',{})))


def quality_vector(metrics):
    values=[min(3.,metrics.get('acutance',0))/3.,min(3.,metrics.get('detail_acutance',0))/3.,
            metrics.get('brightness',.5),metrics.get('clipping',0),
            min(16,metrics.get('detail_tiles',0))/16.,min(64,metrics.get('textured_tiles',0))/64.]
    return values if all(isinstance(x,(int,float)) and math.isfinite(x) for x in values) else None


def feedback_data(store):
    rows=store.rows("""SELECT a.*,f.context,f.source,f.decision AS explicit_decision,f.updated
        FROM feedback f JOIN assets a ON a.id=f.asset_id
        WHERE a.decision=f.decision AND a.role='ordinary_jpeg'
        AND (a.protected=0 OR (f.source='manual' AND f.decision='keep'))
        AND a.size=f.size AND a.mtime_ns=f.mtime_ns ORDER BY f.updated DESC LIMIT ?""",(MAX_PHOTOS,))
    features={}
    for album in {a['album'] for a in rows}: features.update(store.feature_snapshot(album,FEATURE_VERSION))
    result={}
    from .quality import QUALITY_VERSION
    quality={r['path']:json.loads(r['data']) for r in store.rows('''SELECT q.* FROM quality_cache q
        JOIN feedback f ON f.asset_id=(SELECT id FROM assets WHERE path=q.path)
        WHERE f.size=q.size AND f.mtime_ns=q.mtime_ns AND q.version=?''',(QUALITY_VERSION,))}
    for a in rows:
        f=features.get(a['path'])
        if not f and a['path'] in quality:
            f={'vector':[0.]*9,'quality':.5,'metrics':quality[a['path']],'quality_only':True}
        if f and not f.get('error') and technical_vector(f) is not None: result[a['id']]=(a,f)
    return result


def _examples(store,items,embeddings=None,group_data=None):
    """Compare directly related frames only; never use untouched or all-adopted pairs."""
    pairs={}; families={}; strict_members=set()
    if group_data is None:
        group_data=[(album,g) for album in sorted({a['album'] for a,_ in items.values()}) for g in store.groups(album)]
    for album,group in group_data:
        if group.get('kind')=='quality': continue
        matches=group.get('matches',{})
        strict_members.update(int(aid) for aid in matches)
        for aid,others in matches.items():
            for other in others:
                key=tuple(sorted((int(aid),int(other))))
                pairs[key]=None; families[key]=(album,group['id'])
    # Original contexts survive regrouping. Where an explicit match graph exists,
    # it takes precedence, so a broad review merge never invents a training pair.
    contexts=defaultdict(list)
    for aid,(a,f) in items.items():
        if a['context']!='manual' and not a['context'].startswith('quality:'):
            contexts[(a['album'],a['context'])].append(aid)
    for family,ids in contexts.items():
        ids=sorted(ids); added=0
        for index,aid in enumerate(ids):
            if added>=MAX_PAIRS_PER_GROUP: break
            for other in ids[index+1:index+1+MAX_PAIRS_PER_GROUP]:
                if added>=MAX_PAIRS_PER_GROUP: break
                key=(aid,other)
                if aid not in strict_members and other not in strict_members and key not in pairs:
                    pairs[key]=None; families[key]=family; added+=1
    rank=[]; retention=[]; family_counts=defaultdict(int)
    for left,right in sorted(pairs):
        if left not in items or right not in items: continue
        a,fa=items[left]; b,fb=items[right]
        if fa.get('quality_only') or fb.get('quality_only'): continue
        if a['album']!=b['album'] or a['context'].startswith('quality:') or b['context'].startswith('quality:'): continue
        if a['source']==b['source']=='adopt': continue
        family=families[(left,right)]
        if family_counts[family]>=MAX_PAIRS_PER_GROUP: continue
        family_counts[family]+=1
        weight=1. if a['source']==b['source']=='manual' else .25
        va,vb=technical_vector(fa),technical_vector(fb)
        if embeddings is not None:
            if left not in embeddings or right not in embeddings: continue
            va=[*va,*embeddings[left]]; vb=[*vb,*embeddings[right]]
        if a['explicit_decision']!=b['explicit_decision']:
            sign=1 if a['explicit_decision']=='keep' else -1
            diff=[sign*(x-y) for x,y in zip(va,vb)]
            baseline=sign*(base_score(fa)-base_score(fb))
            rank.append((diff,1,a['album'],weight,baseline,family))
            rank.append(([-v for v in diff],0,a['album'],weight,-baseline,family))
        if a['explicit_decision']=='keep' or b['explicit_decision']=='keep':
            edge=(0,0)
            try:
                from .imaging import hash_distance
                edge=(hash_distance(fa['phash'],fb['phash']),hash_distance(fa['dhash'],fb['dhash']))
            except KeyError: pass
            vector=pair_vector(fa,fb,edge)
            both=int(a['explicit_decision']==b['explicit_decision']=='keep')
            retention.append((vector,both,a['album'],weight,0,family))
    for examples in (rank,retention):
        counts=defaultdict(int)
        for row in examples: counts[row[5]]+=1
        for index,row in enumerate(examples):
            examples[index]=(*row[:3],row[3]/counts[row[5]],*row[4:])
    return rank,retention


def _fit(examples,min_samples=30,pairwise=False,control=None):
    # The sample/album guard runs before importing scientific/training libraries.
    if len(examples)<min_samples or len({r[2] for r in examples})<2 or len({r[1] for r in examples})<2: return None
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GroupKFold
    from sklearn.preprocessing import StandardScaler
    X=np.asarray([r[0] for r in examples],dtype=float); y=np.asarray([r[1] for r in examples])
    groups=[r[2] for r in examples]; weights=np.asarray([r[3] for r in examples])
    weights=weights/weights.mean()
    baseline=np.asarray([r[4] for r in examples]); predictions=np.zeros(len(y),dtype=int)
    margins=np.zeros(len(y),dtype=float)
    def estimator(index):
        scaler=StandardScaler(with_mean=not pairwise).fit(X[index])
        model=LogisticRegression(C=.1,max_iter=300,fit_intercept=not pairwise,random_state=42)
        model.fit(scaler.transform(X[index]),y[index],sample_weight=weights[index])
        return scaler,model
    for training,validation in GroupKFold(n_splits=min(3,len(set(groups)))).split(X,y,groups):
        if control and not control.checkpoint(): return None
        if len(set(y[training]))<2: return None
        scaler,model=estimator(training)
        predictions[validation]=model.predict(scaler.transform(X[validation]))
        margins[validation]=model.decision_function(scaler.transform(X[validation]))
    influence=0.
    if pairwise:
        original=float(np.average((baseline>0)==y,weights=weights)); accuracy=original
        for amount in (.025,.05,.1,.2,.35,.5,.75,1.):
            score=float(np.average((baseline+.8*amount*margins>0)==y,weights=weights))
            if score>accuracy: accuracy=score; influence=amount
    else:
        accuracy=float(np.average(predictions==y,weights=weights))
        original=float(np.average(baseline==y,weights=weights))
    if accuracy<.65 or accuracy<=original+.02: return None
    if control and not control.checkpoint(): return None
    scaler,model=estimator(np.arange(len(y)))
    return {'mean':scaler.mean_.tolist() if not pairwise else [0.]*X.shape[1],
            'scale':scaler.scale_.tolist(),'coef':model.coef_[0].tolist(),
            'intercept':float(model.intercept_[0]) if not pairwise else 0.,
            'accuracy':accuracy,'baseline':original,'samples':len(examples),'influence':influence}


def _diagnostic(examples,minimum,head,title,label_minimum=1):
    groups=len({row[2] for row in examples});classes=[sum(row[1]==value for row in examples) for value in (0,1)]
    if head:reason='已通过独立拍摄组验证。'
    elif len(examples)<minimum:reason=f'{title}：有效样本 {len(examples)}/{minimum}，继续积累。'
    elif groups<2:reason=f'{title}：有效样本只来自 {groups} 个拍摄组，至少需要 2 个组。'
    elif min(classes)<label_minimum:reason=f'{title}：两类选择分布不足，需要更多保留和待删的实际比较。'
    else:reason=f'{title}：跨组验证未通过，类别分布或验证一致率未达到启用条件。'
    return dict(enabled=bool(head),samples=len(examples),albums=groups,reason=reason)


def train(store,runtime,root=None,control=None,force=True):
    revision=store.feedback_revision(); state=store.learning_state()
    new=store.one("SELECT COUNT(*) AS n FROM feedback_events WHERE source='manual' AND id>?",(state.get('revision',0),))['n']
    if not force and new<MIN_NEW_ACTIONS:
        return {'skipped':True,'message':f'已自动记录，积累 {MIN_NEW_ACTIONS} 次新选择后批量验证。'}
    items=feedback_data(store)
    group_data=[(album,g) for album in sorted({a['album'] for a,_ in items.values()}) for g in store.groups(album)]
    inputs=hashlib.sha256(json.dumps([MODEL_VERSION,FEATURE_VERSION,EMBEDDING_VERSION,
        [(album,g['id'],g.get('matches')) for album,g in group_data],
        [(aid,a['explicit_decision'],a['source'],a['context'],a['size'],a['mtime_ns'],f) for aid,(a,f) in sorted(items.items())]],sort_keys=True).encode()).hexdigest()
    if inputs==state.get('inputs') and state.get('diagnostics'):
        if store.feedback_revision()==revision:
            store.execute("INSERT OR REPLACE INTO learning_state VALUES ('automatic',?)",(json.dumps({**state,'revision':revision},ensure_ascii=False),))
        return {'skipped':True,'message':'选择和可用特征未变化，复用上次验证结果。'}
    rank,retention=_examples(store,items,group_data=group_data)
    pairs=len(rank)//2; visual=None
    if root is not None and pairs>=30 and len({r[2] for r in rank})>=2:
        visual=ensure_embeddings(store,runtime,[a for a,_ in items.values()],{aid:f for aid,(_,f) in items.items()},control)
    if control and not control.checkpoint(): return {'cancelled':True}
    technical=_fit(rank,60,True,control); rank_head=technical
    if visual:
        visual_examples,_=_examples(store,items,visual,group_data)
        candidate=_fit(visual_examples,60,True,control)
        if candidate and (technical is None or candidate['accuracy']>technical['accuracy']+.02):
            candidate.update(visual=True,embedding_version=EMBEDDING_VERSION); rank_head=candidate
    retention_head=(_fit(retention,40,False,control)
                    if min(sum(r[1]==n for r in retention) for n in (0,1))>=8 else None)
    quality_examples=defaultdict(list)
    for aid,(a,f) in items.items():
        if a['source']!='manual' or not a['context'].startswith('quality:'): continue
        try: camera=json.loads(a['metadata']).get('camera','')
        except (ValueError,TypeError): camera=''
        if not camera: continue
        vector=quality_vector(f.get('metrics',{}))
        if vector is not None:
            quality_examples[camera].append((vector,int(a['explicit_decision']=='keep'),a['album'],1.,0,(a['album'],aid)))
    quality={}
    for camera,examples in quality_examples.items():
        if min(sum(r[1]==n for r in examples) for n in (0,1))<6: continue
        head=_fit(examples,24,False,control)
        if head: quality[camera]=head
    if control and not control.checkpoint(): return {'cancelled':True}
    enabled=bool(rank_head or retention_head or quality); head=rank_head or {}
    diagnostics={'rank':_diagnostic(rank,60,rank_head,'同构图排序'),
        'retention':_diagnostic(retention,40,retention_head,'构图差异保留',8)}
    if quality:
        diagnostics['quality']=dict(enabled=True,reason='相机内技术问题复核顺序已通过验证。')
    elif not quality_examples:
        diagnostics['quality']=dict(enabled=False,reason='技术容忍：还没有可用的、带相机信息的废片筛查主动留删记录。')
    else:
        diagnostics['quality']=dict(enabled=False,reason='；'.join(_diagnostic(examples,24,None,'相机 '+camera,6)['reason'] for camera,examples in quality_examples.items()))
    payload={'version':MODEL_VERSION,'feature_version':FEATURE_VERSION,'enabled':enabled,'revision':revision,'inputs':inputs,
             'pairs':pairs,'photos':sum(a['source']=='manual' for a,_ in items.values()),
             'rank':rank_head,'retention':retention_head,'quality':quality,
             'accuracy':head.get('accuracy'),'baseline':head.get('baseline'),
             'updated':time.time(),'diagnostics':diagnostics,'validation':'按拍摄组交叉验证；批量采纳不单独生成训练比较',
             'scope':'普通照片留删；相近构图排序、保留差异及相机内技术容忍；图集不参与'}
    payload['message']=(f'已记录 {payload["photos"]} 张主动选择、{pairs} 对同构图比较。'
        + ('通过独立拍摄组验证的学习项已启用，下次分析生效。' if enabled else '证据不足或尚未优于基础算法，继续积累。'))
    if store.feedback_revision()!=revision:
        return {'stale':True,'message':'筛图期间有了新选择，本轮模型未启用；下批一起验证。'}
    from .settings_io import atomic_json
    path=model_path(runtime,store.path)
    if items or path.exists(): atomic_json(path,payload)
    store.execute("INSERT OR REPLACE INTO learning_state VALUES ('automatic',?)",(json.dumps(payload,ensure_ascii=False),))
    return payload
