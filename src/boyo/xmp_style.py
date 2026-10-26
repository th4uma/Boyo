"""Reuse Adobe global settings on disposable RAW copies; originals stay read-only."""
import copy
import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from .collections import Collections
from .fs import checked,sha256
from .colour_match import load_srgb,sample_lab,moments,fit_to_reference,apply_match
from .photoshop_renderer import render_raw

CRS='http://ns.adobe.com/camera-raw-settings/1.0/'
RDF='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
XMP='adobe:ns:meta/'
for prefix,uri in (('crs',CRS),('rdf',RDF),('x',XMP)):ET.register_namespace(prefix,uri)


def read_xmp(path):
    path=Path(path)
    if path.stat().st_size>8*1024*1024:raise ValueError('XMP 超过 8 MB，请选择全局调色预设')
    data=path.read_bytes()
    if b'<!DOCTYPE' in data.replace(b'\x00',b'').upper():raise ValueError('不支持含外部声明的 XMP')
    try:root=ET.fromstring(data)
    except ET.ParseError as exc:raise ValueError('XMP 文件不是有效的 XML：'+str(exc))
    descriptions=root.findall('.//{'+RDF+'}Description')
    if not any(any(k.startswith('{'+CRS+'}') for k in d.attrib) for d in descriptions):
        raise ValueError('XMP 没有 Adobe Camera Raw 调色参数')
    return root


def global_parameter(name):
    return not name.startswith(('Crop','Perspective','Lens','Mask','Retouch','Paint','Gradient','Circular','Upright','Warp','RedEye','Auto','RawFileName','AlreadyApplied','HasCrop','Snapshot','History','Original','Default','Digest','UUID','DocumentID'))


def write_global_xmp(reference,destination,raw_name):
    source=read_xmp(reference)
    root=ET.Element('{'+XMP+'}xmpmeta');rdf=ET.SubElement(root,'{'+RDF+'}RDF')
    target=ET.SubElement(rdf,'{'+RDF+'}Description',{'{'+RDF+'}about':''})
    count=0
    for desc in source.findall('.//{'+RDF+'}Description'):
        # Only top-level Camera Raw settings; nested Look data is copied with its parent.
        if not any(k.startswith('{'+CRS+'}') for k in desc.attrib):continue
        for key,value in desc.attrib.items():
            if key.startswith('{'+CRS+'}') and global_parameter(key.split('}',1)[1]):target.set(key,value);count+=1
        for child in desc:
            if child.tag.startswith('{'+CRS+'}') and global_parameter(child.tag.split('}',1)[1]):target.append(copy.deepcopy(child))
        break
    target.set('{'+CRS+'}HasCrop','False');target.set('{'+CRS+'}AlreadyApplied','False')
    target.set('{'+CRS+'}HasSettings','True')
    target.set('{'+CRS+'}RawFileName',raw_name)
    ET.ElementTree(root).write(destination,encoding='utf-8',xml_declaration=True)
    return count


class XmpStyle:
    def __init__(self,store,runtime,root):
        self.store,self.runtime,self.root=store,runtime,Path(root)
        self.cache_root=runtime.home/'cache/styles'

    def reference(self,set_id,override=None):
        items=Collections(self.store,self.root).items(set_id)
        if not items or items[0]['slot']!=0 or items[0]['role']!='finished':raise ValueError('图集首图必须是成片封面')
        cover=items[0]
        if override:
            path=Path(override)
            if path.is_file():read_xmp(path);return path
        assets=self.store.assets(cover['album'])
        candidates=[a for a in assets if Path(a['path']).suffix.lower()=='.xmp' and
                    (a['logical_key']==cover['logical_key'] or a['stem']==cover['stem'])]
        candidates.sort(key=lambda a:(Path(a['path']).parent!=Path(cover['path']).parent,a['path']))
        if not candidates:raise FileNotFoundError('未找到封面对应的 XMP')
        preferred=[a for a in candidates if Path(a['path']).parent==Path(cover['path']).parent]
        candidates=preferred or candidates
        if len(candidates)>1 and len({sha256(checked(self.root,a['path'],True)) for a in candidates})>1:
            raise FileNotFoundError('封面有多个不同 XMP，请指定一个')
        path=checked(self.root,candidates[0]['path'],True);read_xmp(path);return path

    def prepare(self,set_id,reference,job,progress,control):
        from PIL import ImageCms
        job=Path(job);items=Collections(self.store,self.root).items(set_id)
        reference=Path(reference);reference_digest=sha256(reference)
        read_xmp(reference)
        if not items or items[0]['role']!='finished':raise ValueError('缺少成片封面')
        for a in items:
            path=checked(self.root,a['path'],True);stat=path.stat()
            if not a['present'] or a['decision']=='delete' or (stat.st_size,stat.st_mtime_ns)!=(a['size'],a['mtime_ns']):raise ValueError('图集照片发生变化，请重新检查')
        cover=load_srgb(checked(self.root,items[0]['path'],True))
        reference_lab=sample_lab(cover)
        reference_moments=moments(reference_lab);cover_clip=float((reference_lab[:,:,0]>99).mean());cover.close()
        assets=self.store.assets(items[0]['album']);outputs={};warnings=[];parameters={}
        profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        for index,a in enumerate(items[1:],1):
            if not control.checkpoint():raise ValueError('调色已取消，原图未修改')
            progress(index-1,len(items)-1,f'图集调色 {index}/{len(items)-1} · {Path(a["path"]).name}')
            raws=[r for r in assets if r['logical_key']==a['logical_key'] and r['role']=='ordinary_raw']
            if not raws:raws=[r for r in assets if r['logical_key']==a['logical_key'] and r['role']=='selected_raw']
            working=job/'working';working.mkdir(exist_ok=True)
            developed=False
            if len(raws)==1:
                from .operations import Operations
                raw=raws[0]
                if not Operations._compatible(a,raw):raise ValueError('RAW 与 JPG 的相机/时间冲突：'+a['path'])
                raw_source=checked(self.root,raw['path'],True)
                before=raw_source.stat()
                if (before.st_size,before.st_mtime_ns)!=(raw['size'],raw['mtime_ns']):raise ValueError('RAW 已改变，请重新扫描')
                raw_copy=working/('source'+raw_source.suffix)
                shutil.copy2(raw_source,raw_copy)
                after=raw_source.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('RAW 在复制期间改变，已停止')
                write_global_xmp(reference,raw_copy.with_suffix('.xmp'),raw_copy.name)
                exposure_tree=ET.parse(raw_copy.with_suffix('.xmp'))
                exposure_desc=exposure_tree.getroot().find('.//{'+RDF+'}Description')
                try:base_exposure=float(exposure_desc.get('{'+CRS+'}Exposure2012','0'))
                except ValueError:raise ValueError('XMP 曝光参数无效')
                import math
                if not math.isfinite(base_exposure):raise ValueError('XMP 曝光参数无效')
                best_score=float('inf');best_render=None;exposure_delta=0.;best_delta=0.;renders=0
                for attempt in range(3):
                    if not control.checkpoint():raise ValueError('调色已取消')
                    exposure_desc.set('{'+CRS+'}Exposure2012',f'{base_exposure+exposure_delta:+.3f}')
                    exposure_tree.write(raw_copy.with_suffix('.xmp'),encoding='utf-8',xml_declaration=True)
                    progress(index-1,len(items)-1,f'应用 XMP · 第 {index}/{len(items)-1} 张 · 曝光校正 {attempt+1}/3')
                    rendered=working/f'rendered-{attempt}.png'
                    render_raw(raw_copy,rendered,working,control);renders+=1
                    probe=load_srgb(rendered)
                    try:lab=sample_lab(probe)
                    finally:probe.close()
                    brightness=float(moments(lab)[0][0]);clip=float((lab[:,:,0]>99).mean())
                    error=float(reference_moments[0][0])-brightness
                    score=abs(error)+max(0,clip-cover_clip)*30
                    if score>=best_score-.25:break
                    best_score,best_render,best_delta=score,rendered,exposure_delta
                    if abs(error)<6 and clip<=cover_clip+.04:break
                    step=-.5 if clip>cover_clip+.04 else max(-.6,min(.6,error/25))
                    next_delta=max(-1.2,min(1.2,exposure_delta+step))
                    if abs(next_delta-exposure_delta)<.05:break
                    exposure_delta=next_delta
                image=load_srgb(best_render);developed=True
            else:
                image=load_srgb(checked(self.root,a['path'],True))
                warnings.append(f'未找到唯一 RAW，使用封面参考匹配：{a["path"]}')
            try:
                params=fit_to_reference(image,reference_moments,developed=developed)
                if developed:params.update(raw_renders=renders,exposure_adjustment_ev=best_delta)
                adjusted=apply_match(image,params,control)
                output=job/f'{a["slot"]:02d}.jpg'
                try:adjusted.save(output,'JPEG',quality=96,subsampling=0,icc_profile=profile)
                finally:adjusted.close()
                outputs[a['id']]={'cache':output.relative_to(self.cache_root).as_posix(),'sha256':sha256(output),
                                  'source_size':a['size'],'source_mtime_ns':a['mtime_ns']}
                parameters[str(a['id'])]=params
            finally:
                image.close();shutil.rmtree(working)
        if sha256(reference)!=reference_digest:raise ValueError('调色期间参考 XMP 已改变，未导出')
        manifest={'reference_xmp':str(reference),'xmp_sha256':reference_digest,'parameters':parameters,'warnings':warnings}
        (job/'parameters.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        progress(len(items)-1,len(items)-1,'图集调色完成，正在检查副本导出')
        return {'outputs':outputs,'items':[[a['slot'],a['id'],a['size'],a['mtime_ns']] for a in items],**manifest}
