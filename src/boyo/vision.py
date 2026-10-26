"""Local, bounded OpenCV matching. Imports stay off the GUI startup path."""
from functools import lru_cache
import math

MATCH_VERSION = 'sift-ransac-overlap-focus-v3'


def descriptor_path(thumb):
    return thumb.with_suffix('.npz')


def focus_metrics(image):
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    gray = np.asarray(image.convert('L'))
    height, width = gray.shape
    if max(height, width) > 1800:
        scale = 1800 / max(height, width)
        gray = cv2.resize(gray, (round(width*scale), round(height*scale)), interpolation=cv2.INTER_AREA)
    gray=cv2.GaussianBlur(gray.astype(np.float32)/255,(0,0),.8)
    # Upper quartile of textured tiles: a sharp subject may have a blurred background.
    values = []
    for y in range(0, gray.shape[0]-63, 96):
        for x in range(0, gray.shape[1]-63, 96):
            tile = gray[y:y+96, x:x+96]
            contrast = float(tile.std())
            if contrast < .025: continue
            dx = cv2.Sobel(tile, cv2.CV_32F, 1, 0, ksize=3)/8
            dy = cv2.Sobel(tile, cv2.CV_32F, 0, 1, ksize=3)/8
            energy = float(np.mean(dx*dx+dy*dy))
            lap = cv2.Laplacian(tile, cv2.CV_32F)
            values.append(float(np.mean(lap*lap))/(energy+1e-8))
    return {'acutance': float(np.quantile(values,.75)) if values else 0.,
            'textured_tiles': len(values), 'focus_scale': max(gray.shape)}


def save_descriptors(image, thumb):
    import cv2
    import numpy as np
    from PIL import Image
    cv2.setNumThreads(1)
    small = image.copy(); small.thumbnail((960,960), Image.Resampling.LANCZOS)
    gray = np.asarray(small.convert('L'))
    sift = cv2.SIFT_create(nfeatures=1600, contrastThreshold=.025)
    keys, descriptors = sift.detectAndCompute(gray,None)
    points = np.float32([k.pt for k in keys]).reshape(-1,2)
    if descriptors is None: descriptors = np.empty((0,128),np.float32)
    # NumPy archive, no pickle. A small grayscale preview supports alignment checks.
    import os, tempfile
    path = descriptor_path(thumb)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.npz', delete=False) as output:
        temp = output.name
        np.savez_compressed(output, points=points, descriptors=descriptors.astype(np.uint8), shape=np.array(gray.shape), gray=cv2.resize(gray,(320,320)))
    try: os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


class Matcher:
    def __init__(self):
        self.load = lru_cache(maxsize=24)(self._load)

    @staticmethod
    def _load(path):
        import numpy as np
        with np.load(path, allow_pickle=False) as f:
            return {k:f[k] for k in f.files}

    def compare(self, first, second):
        import cv2
        import numpy as np
        a,b = self.load(str(descriptor_path(first))),self.load(str(descriptor_path(second)))
        da,db = a['descriptors'].astype(np.float32),b['descriptors'].astype(np.float32)
        if min(len(da),len(db))<12: return None
        matcher = cv2.FlannBasedMatcher(dict(algorithm=1,trees=4),dict(checks=48))
        forward=matcher.knnMatch(da,db,k=2)
        good=[m for pair in forward if len(pair)==2 for m,n in [pair] if m.distance<.72*n.distance]
        # One target feature cannot count repeatedly as evidence.
        unique={}
        for m in sorted(good,key=lambda m:m.distance): unique.setdefault(m.trainIdx,m)
        good=list(unique.values())
        if len(good)<12: return None
        pa=np.float32([a['points'][m.queryIdx] for m in good]); pb=np.float32([b['points'][m.trainIdx] for m in good])
        h,mask=cv2.findHomography(pa,pb,cv2.RANSAC,3.,maxIters=1500,confidence=.995)
        if h is None or mask is None or not np.isfinite(h).all(): return None
        inside=mask.ravel().astype(bool); count=int(inside.sum()); ratio=count/len(good)
        # Keep weaker measurements for composition review. Deletion gates below stay strict.
        if count<8 or ratio<.1: return None
        ha,wa=map(int,a['shape']); hb,wb=map(int,b['shape'])
        qa=np.float32([[0,0],[wa,0],[wa,ha],[0,ha]])
        qb=np.float32([[0,0],[wb,0],[wb,hb],[0,hb]])
        projected=cv2.perspectiveTransform(qa.reshape(-1,1,2),h).reshape(4,2)
        if not cv2.isContourConvex(projected): return None
        area=float(cv2.contourArea(projected))
        if area<=0: return None
        overlap,_=cv2.intersectConvexConvex(projected,qb)
        coverage=min(float(overlap)/area,float(overlap)/(wb*hb))
        spread=min(cv2.contourArea(cv2.convexHull(pa[inside]))/(wa*ha),cv2.contourArea(cv2.convexHull(pb[inside]))/(wb*hb))
        # Align small previews, test photometric agreement outside interpolation borders.
        transform=np.diag([320/wb,320/hb,1])@h@np.diag([wa/320,ha/320,1])
        aligned=cv2.warpPerspective(a['gray'],transform,(320,320))
        valid=cv2.warpPerspective(np.ones((320,320),np.uint8),transform,(320,320)).astype(bool)
        valid=cv2.erode(valid.astype(np.uint8),np.ones((5,5),np.uint8)).astype(bool)
        if valid.sum()<1000: return None
        av,bv=aligned[valid].astype(float),b['gray'][valid].astype(float)
        correlation=float(np.corrcoef(av,bv)[0,1]) if min(av.std(),bv.std())>1 else 0.
        if not math.isfinite(correlation): return None
        return {'inliers':count,'inlier_ratio':round(ratio,4),'overlap':round(coverage,4),
                'spread':round(float(spread),4),'correlation':round(correlation,4)}


def is_duplicate(evidence, strength):
    if not evidence: return False
    if evidence.get('inliers',16)<16 or evidence.get('inlier_ratio',1.)<.3: return False
    overlap,spread,correlation = {'保守':(.9,.22,.84),'适中':(.8,.17,.76),'较强':(.6,.10,.65),
                                  '高强度':(.5,.09,.58)}.get(strength,(.9,.22,.84))
    return evidence['overlap']>=overlap and evidence['spread']>=spread and evidence['correlation']>=correlation


def is_related(evidence,strength):
    """Broader composition relation for co-review only, never a deletion criterion."""
    if not evidence: return False
    if evidence.get('inliers',12)<12 or evidence.get('inlier_ratio',1.)<.18: return False
    overlap,spread,correlation={'保守':(.75,.14,.68),'适中':(.6,.10,.55),'较强':(.45,.08,.48),
                                '高强度':(.4,.07,.45)}.get(strength,(.75,.14,.68))
    return evidence['overlap']>=overlap and evidence['spread']>=spread and evidence['correlation']>=correlation


def quality_issues(feature):
    metrics=feature.get('metrics',{})
    issues=[]
    if metrics.get('textured_tiles',0)>=8 and metrics.get('focus_scale',0)>=800 and metrics.get('acutance',1)<.4:
        issues.append('疑似整体模糊：有纹理区域也缺少清晰边缘')
    if metrics.get('clipping',0)>.85 and (metrics.get('brightness',.5)<.06 or metrics.get('brightness',.5)>.94):
        issues.append('疑似严重曝光丢失：大部分画面接近纯黑或纯白')
    return issues
