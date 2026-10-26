"""Independent technical-quality screening; no image matching or file operations."""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json

from .fs import checked
from .imaging import load_preview, thumbnail_path
from .tasks import worker_count
from .vision import focus_metrics
from .screening import is_secondary, SECONDARY

QUALITY_VERSION = 'focus-native-patches-motion-v4'
SENSITIVITIES = ('仅明显问题', '标准', '多找可疑')


def focus_measurements(image):
    """Overview plus sampled original-resolution detail; no subject recognition."""
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    measured = focus_metrics(image)
    width, height = image.size
    from .motion_quality import patch_motion,summarize_motion
    values = [];motion=[]
    large=max(width,height)>1800
    if large or min(width,height)>=128:
        # Sixteen fixed native-resolution crops, at most 1.64 megapixels in total.
        for fy in (.125, .375, .625, .875):
            for fx in (.125, .375, .625, .875):
                x, y = round(fx*width), round(fy*height)
                crop = image.crop((max(0,x-160),max(0,y-160),min(width,x+160),min(height,y+160)))
                gray = np.asarray(crop.convert('L'),dtype=np.float32)/255
                gray = cv2.GaussianBlur(gray,(0,0),.8)
                if float(gray.std()) < .025: continue
                direction=patch_motion(gray)
                if direction is not None:motion.append(direction)
                dx = cv2.Sobel(gray,cv2.CV_32F,1,0,ksize=3)/8
                dy = cv2.Sobel(gray,cv2.CV_32F,0,1,ksize=3)/8
                energy = float(np.mean(dx*dx+dy*dy))
                lap = cv2.Laplacian(gray,cv2.CV_32F)
                values.append(float(np.mean(lap*lap))/(energy+1e-8))
        detail = float(np.quantile(values,.9)) if values else 0.
    if not large:
        detail = measured['acutance']
        values = [detail]*min(16,measured['textured_tiles'])
    return {**measured,'detail_acutance':detail,'detail_tiles':len(values),
            'quality_version':QUALITY_VERSION,**summarize_motion(motion)}


def assess(metrics, sensitivity='标准', *, secondary=False):
    sensitivity = sensitivity if sensitivity in SENSITIVITIES else '标准'
    overview_limit, detail_limit = {'仅明显问题':(.4,.3),'标准':(.55,.45),'多找可疑':(.75,.65)}[sensitivity]
    if secondary: overview_limit,detail_limit = .95,.85
    min_tiles = 4 if secondary else 8
    issues = []
    overview = metrics.get('acutance',1.)
    detail = metrics.get('detail_acutance',1.)
    if metrics.get('textured_tiles',0)>=min_tiles and metrics.get('focus_scale',0)>=800 and overview<overview_limit:
        issues.append({'code':'blur','label':'模糊 / 疑似虚焦',
                       'detail':'有纹理的区域普遍缺少清晰边缘，请放大确认是否失焦或手抖。',
                       'severity':'明显问题' if overview<.4 else '可疑问题'})
    elif metrics.get('detail_tiles',0)>=min_tiles and detail<detail_limit:
        issues.append({'code':'soft_detail','label':'疑似细节虚焦',
                       'detail':'缩略图可能正常，但原尺寸取样的细节普遍偏软，请看原尺寸确认。',
                       'severity':'明显问题' if detail<.3 else '可疑问题'})
    motion_floor,motion_ratio = (.30,.65) if secondary else (.26,.60) if sensitivity=='多找可疑' else (.18,.30) if sensitivity=='仅明显问题' else (.23,.60)
    if (metrics.get('motion_tiles',0)>=6 and metrics.get('motion_consistency',0)>=.65
            and metrics.get('motion_acutance',1)<motion_floor and metrics.get('motion_ratio',1)<motion_ratio
            and metrics.get('motion_sharpness_ratio',1)<.7):
        issues.append({'code':'motion_blur','label':'疑似运动模糊 / 手抖拖影',
                       'detail':'多个有纹理区域在同一方向上细节明显衰减，可能有手抖、拖影或主体运动；请放大确认。',
                       'severity':'可疑问题'})
    brightness, clipping = metrics.get('brightness',.5), metrics.get('clipping',0.)
    if clipping>.85 and (brightness<.06 or brightness>.94):
        issues.append({'code':'exposure','label':'严重曝光异常',
                       'detail':'大部分画面接近纯黑或纯白，可能丢失大量细节。', 'severity':'明显问题'})
    elif secondary and clipping>.65 and (brightness<.12 or brightness>.88):
        issues.append({'code':'exposure_suspect','label':'疑似曝光丢失',
                       'detail':'画面中纯黑或纯白区域较多，请确认是否为有意的光影取舍。','severity':'可疑问题'})
    return issues


def extract_quality(path, thumb_path):
    """Only quality indicators and a thumbnail. No SIFT, hashes, model or network."""
    import numpy as np
    from PIL import Image
    image = load_preview(path)
    try:
        metrics = focus_measurements(image)
        image.thumbnail((1024,1024),Image.Resampling.LANCZOS)
        gray = np.asarray(image.convert('L'),dtype=np.float32)/255
        metrics['brightness'] = float(gray.mean())
        metrics['clipping'] = float(((gray<.02)|(gray>.98)).mean())
        if not thumb_path.exists():
            image.thumbnail((360,240),Image.Resampling.LANCZOS)
            thumb_path.parent.mkdir(parents=True,exist_ok=True)
            image.save(thumb_path,'WEBP',quality=80)
        return metrics
    finally:
        image.close()


def screen_quality(store,runtime,root,album,settings,progress,control,feature_data=None):
    scan = store.one('SELECT * FROM albums WHERE path=?',(album,))
    if not scan or not scan['complete']: raise ValueError('请先完整扫描拍摄组')
    photos = [a for a in store.assets(album) if a['role']=='ordinary_jpeg']
    secondary = is_secondary(settings)
    assets = [a for a in photos if not a['protected'] and (not secondary or a['decision'] in {'pending','keep'})]
    sensitivity = settings.get('quality_sensitivity','标准')
    if sensitivity not in SENSITIVITIES: sensitivity='标准'
    signature = hashlib.sha256(json.dumps([QUALITY_VERSION,sensitivity,SECONDARY if secondary else 'normal',
        [[a[k] for k in ('id','path','size','mtime_ns','protected',*(['decision'] if secondary else []))] for a in photos]]).encode()).hexdigest()
    previous = store.one('SELECT * FROM quality_runs WHERE album=?',(album,))
    existing = {r['path']:json.loads(r['data']) for r in store.rows('''
        SELECT q.* FROM quality_cache q JOIN assets a ON a.path=q.path
        WHERE a.album=? AND q.size=a.size AND q.mtime_ns=a.mtime_ns AND q.version=?''',(album,QUALITY_VERSION))}
    feature_data = feature_data if feature_data is not None else store.feature_snapshot(album)
    cached, measurements, jobs, writes, failures = 0, {}, [], [], []
    def cancelled():
        return {'cancelled':True,'total':len(assets),'cached':cached,'failures':failures}
    def flush():
        if writes:
            with store.connect() as db:
                db.executemany('INSERT OR REPLACE INTO quality_cache VALUES (?,?,?,?,?)',writes)
            writes.clear()
    for index,a in enumerate(assets):
        if not control.checkpoint(): return cancelled()
        path = checked(root,a['path'],True); stat=path.stat()
        if (stat.st_size,stat.st_mtime_ns)!=(a['size'],a['mtime_ns']): raise ValueError('照片已变化，请重新扫描')
        data = existing.get(a['path'])
        if not data:
            candidate = feature_data.get(a['path'],{}).get('metrics',{})
            if candidate.get('quality_version')==QUALITY_VERSION and 'clipping' in candidate:
                data=candidate
        if data and thumbnail_path(runtime,a).exists():
            cached+=1; measurements[a['id']]=data
            if a['path'] not in existing:
                writes.append((a['path'],a['size'],a['mtime_ns'],QUALITY_VERSION,json.dumps(data)))
        else: jobs.append((a,path))
        progress(index+1,len(assets),f'核对废片筛查缓存 {index+1}/{len(assets)}')
    flush()
    if not control.checkpoint(): return cancelled()
    if previous and previous['signature']==signature and not jobs:
        report=json.loads(previous['data'])
        if not report['summary'].get('failures'):
            return {**report['summary'],'cached':cached,'reused_results':True}
    def compute(job):
        a,path=job
        try:
            data=extract_quality(path,thumbnail_path(runtime,a))
            stat=path.stat()
            if (stat.st_size,stat.st_mtime_ns)!=(a['size'],a['mtime_ns']): raise ValueError('照片在筛查期间发生变化，请重新扫描')
            return a,data,None
        except Exception as exc: return a,None,str(exc)
    remaining,active=deque(jobs),{}
    workers=worker_count(settings)
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='boyo-quality') as pool:
        try:
            while remaining or active:
                if not control.checkpoint():
                    for future in active: future.cancel()
                    return cancelled()
                while remaining and len(active)<workers:
                    job=remaining.popleft(); active[pool.submit(compute,job)]=job
                done,_=wait(active,timeout=.1,return_when=FIRST_COMPLETED)
                for future in done:
                    active.pop(future); a,data,error=future.result()
                    if error: failures.append({'path':a['path'],'error':error})
                    else:
                        measurements[a['id']]=data
                        writes.append((a['path'],a['size'],a['mtime_ns'],QUALITY_VERSION,json.dumps(data)))
                    progress(len(measurements)+len(failures),len(assets),f'筛查模糊与虚焦 {len(measurements)+len(failures)}/{len(assets)}')
                    if len(writes)>=16: flush()
        finally: flush()
    if not control.checkpoint(): return cancelled()
    results=[]
    for a in assets:
        metrics=measurements.get(a['id'])
        if metrics is None: continue
        issues=assess(metrics,sensitivity,secondary=secondary)
        if issues:
            results.append({'id':a['id'],'path':a['path'],'size':a['size'],'mtime_ns':a['mtime_ns'],
                            'issues':issues,'metrics':metrics,
                            'severity':'明显问题' if any(i['severity']=='明显问题' for i in issues) else '可疑问题'})
    results.sort(key=lambda r:(r['severity']!='明显问题',r['metrics'].get('acutance',1),r['path']))
    summary={'cancelled':False,'total':len(assets),'protected':sum(bool(a['protected']) for a in photos),'cached':cached,
             'candidates':len(results),'obvious':sum(r['severity']=='明显问题' for r in results),
             'sensitivity':'高强度二次筛查' if secondary else sensitivity,'screening_mode':SECONDARY if secondary else 'normal',
             'failures':failures,'reused_results':False}
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO quality_runs VALUES (?,?,?)',
                   (album,signature,json.dumps({'summary':summary,'results':results,
                    'inventory':[[a[k] for k in ('id','size','mtime_ns','protected')] for a in photos]})))
    progress(len(assets),len(assets),'废片筛查完成')
    return summary
