"""Explicit one-time installer. The photo application itself stays offline."""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


def prepare(home):
    home=Path(home)
    for name in ('cache/huggingface','cache/torch','cache/pycache','tmp','models/dinov2-small'):
        (home/name).mkdir(parents=True,exist_ok=True)
    os.environ['HF_HOME']=str(home/'cache/huggingface')
    os.environ['TORCH_HOME']=str(home/'cache/torch')
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    os.environ['HF_HUB_DISABLE_XET']='1'
    os.environ['HF_HUB_DOWNLOAD_TIMEOUT']='60'
    sys.pycache_prefix=str(home/'cache/pycache')
    from .config import Runtime
    from .embeddings import weights_path,ENCODER_NAME,WEIGHTS_SHA256
    from .settings_io import atomic_json
    path=weights_path(Runtime(home)); manifest=path.with_name('manifest.json')
    if path.exists() and manifest.exists():
        data=json.loads(manifest.read_text(encoding='utf-8'))
        if data.get('sha256')==WEIGHTS_SHA256 and data.get('bytes')==path.stat().st_size: return data
    from huggingface_hub import hf_hub_download
    repo='timm/'+ENCODER_NAME
    revision='4610ca143709d58a633b6397a74412c2c3842454'
    downloaded=hf_hub_download(repo_id=repo,filename='model.safetensors',revision=revision,
                               cache_dir=str(home/'cache/huggingface/hub'))
    temporary=path.with_suffix('.download')
    try:
        shutil.copyfile(downloaded,temporary)
        digest=hashlib.sha256()
        with temporary.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''): digest.update(block)
        if digest.hexdigest()!=WEIGHTS_SHA256: raise ValueError('预训练权重校验失败，未安装此文件')
        os.replace(temporary,path)
        result={'repository':repo,'revision':revision,'sha256':digest.hexdigest(),'license':'Apache-2.0',
                'source':'https://huggingface.co/'+repo,'format':'safetensors','bytes':path.stat().st_size}
        atomic_json(manifest,result)
        return result
    finally: temporary.unlink(missing_ok=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--home',type=Path,required=True)
    print(json.dumps(prepare(parser.parse_args().home),ensure_ascii=False),flush=True)
