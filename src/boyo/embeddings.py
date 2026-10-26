"""Frozen, local DINOv2 features. Nothing here runs on a decision click."""
from __future__ import annotations

import hashlib
import json
import math
import logging

from .imaging import thumbnail_path

ENCODER_NAME = 'vit_small_patch14_dinov2.lvd142m'
WEIGHTS_SHA256 = '04d27f3400d059fc0cfd7d17dd1909a75bf3ea8fb3eeb48b97cb99e57ee20081'
EMBEDDING_VERSION = 'dinov2-small-04d27f3400d0-224-letterbox-v1'


def weights_path(runtime):
    return runtime.home / 'models' / 'dinov2-small' / 'model.safetensors'


def cache_key(feature):
    digest=feature.get('sha256')
    return hashlib.sha256((EMBEDDING_VERSION+'|'+digest).encode()).hexdigest() if digest else None


def cached_embeddings(store, features):
    keys={key for f in features.values() if (key:=cache_key(f))}
    found={}
    ordered=sorted(keys)
    for start in range(0,len(ordered),400):
        part=ordered[start:start+400]
        rows=store.rows('SELECT cache_key,data FROM embeddings WHERE version=? AND cache_key IN ('+','.join('?' for _ in part)+')',
                        (EMBEDDING_VERSION,*part))
        for row in rows:
            try:
                vector=json.loads(row['data'])
                if len(vector)==384 and all(math.isfinite(x) for x in vector): found[row['cache_key']]=vector
            except (ValueError,TypeError): pass
    return {aid:found[key] for aid,f in features.items() if (key:=cache_key(f)) in found}


class FrozenEncoder:
    def __init__(self,runtime,control=None):
        path=weights_path(runtime)
        if not path.is_file(): raise FileNotFoundError('本地 DINOv2 权重尚未准备好')
        if control and hasattr(control,'trace'): control.trace('importing torch')
        import torch
        if control and hasattr(control,'trace'): control.trace('torch ready')
        if control and not control.checkpoint(): raise InterruptedError('学习已停止')
        import timm
        if control and hasattr(control,'trace'): control.trace('timm ready')
        if control and not control.checkpoint(): raise InterruptedError('学习已停止')
        torch.set_num_threads(1)
        try: torch.set_num_interop_threads(1)
        except RuntimeError: pass
        self.torch=torch
        self.model=timm.create_model(ENCODER_NAME,pretrained=True,num_classes=0,img_size=224,
                                    pretrained_cfg_overlay={'file':str(path)})
        self.model.eval().requires_grad_(False)
        if control and hasattr(control,'trace'): control.trace('encoder ready')

    def encode(self,path):
        import numpy as np
        from PIL import Image,ImageOps
        with Image.open(path) as image:
            image=ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((224,224),Image.Resampling.LANCZOS)
            canvas=Image.new('RGB',(224,224),(124,116,104))
            canvas.paste(image,((224-image.width)//2,(224-image.height)//2))
            array=np.asarray(canvas,dtype=np.float32)/255.
        tensor=self.torch.from_numpy(array).permute(2,0,1)
        mean=self.torch.tensor([.485,.456,.406])[:,None,None]
        std=self.torch.tensor([.229,.224,.225])[:,None,None]
        with self.torch.inference_mode():
            value=self.model(((tensor-mean)/std).unsqueeze(0))[0]
            value=self.torch.nn.functional.normalize(value,dim=0)
        return value.tolist()


def ensure_embeddings(store,runtime,assets,features,control=None,progress=None):
    result=cached_embeddings(store,features)
    if not weights_path(runtime).is_file(): return result
    encoder=None; writes=[]; failures=0
    shared={cache_key(features[aid]):vector for aid,vector in result.items()}
    def flush():
        if writes:
            with store.connect() as db: db.executemany('INSERT OR REPLACE INTO embeddings VALUES (?,?,?)',writes)
            writes.clear()
    try:
        for index,asset in enumerate(assets):
            if control and not control.checkpoint(): break
            aid=asset['id']; feature=features.get(aid)
            if not feature or aid in result or not (key:=cache_key(feature)): continue
            if key in shared: result[aid]=shared[key]; continue
            thumb=thumbnail_path(runtime,asset)
            if not thumb.is_file(): continue
            if encoder is None:
                try:
                    encoder=FrozenEncoder(runtime,control)
                except (ImportError,OSError,RuntimeError,ValueError):
                    logging.exception('本地视觉模型暂不可用，继续使用技术指标'); break
            if control and not control.checkpoint(): break
            try: vector=encoder.encode(thumb)
            except (OSError,RuntimeError,ValueError):
                failures+=1
                if failures>=3:
                    logging.exception('视觉特征连续失败，本轮停止补充'); break
                continue
            failures=0; shared[key]=vector; result[aid]=vector
            writes.append((key,EMBEDDING_VERSION,json.dumps(vector)))
            if len(writes)>=16: flush()
            if progress: progress(index+1,len(assets),'正在缓存本地视觉特征')
    finally: flush()
    return result
