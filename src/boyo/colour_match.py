"""Small, bounded Lab moment matching after RAW development, using OpenCV."""
import io


def load_srgb(path):
    from PIL import Image,ImageOps,ImageCms
    with Image.open(path) as source:
        image=ImageOps.exif_transpose(source)
        if source.info.get('icc_profile'):
            try:image=ImageCms.profileToProfile(image,ImageCms.ImageCmsProfile(io.BytesIO(source.info['icc_profile'])),ImageCms.createProfile('sRGB'),outputMode='RGB')
            except (OSError,ValueError,ImageCms.PyCMSError):raise ValueError(f'无法读取照片颜色配置：{path}')
        else:image=image.convert('RGB')
    return image


def sample_lab(image):
    import cv2
    import numpy as np
    from PIL import Image
    cv2.setNumThreads(1)
    sample=image.copy();sample.thumbnail((960,960),Image.Resampling.LANCZOS)
    return cv2.cvtColor(np.asarray(sample,dtype=np.float32)/255,cv2.COLOR_RGB2LAB)


def moments(lab):
    import numpy as np
    pixels=lab.reshape(-1,3)
    selected=pixels[(pixels[:,0]>5)&(pixels[:,0]<96)]
    if len(selected)<32:selected=pixels
    lo,hi=np.quantile(selected,[.01,.99],axis=0)
    clipped=np.clip(selected,lo,hi)
    return clipped.mean(axis=0),clipped.std(axis=0)


def fit_to_reference(image,reference,*,developed=True):
    import numpy as np
    lab=sample_lab(image);target_mean,target_std=reference
    scale=np.ones(3,dtype=np.float32);offset=np.zeros(3,dtype=np.float32)
    bounds=np.array([8,3,3] if developed else [15,7,7],dtype=np.float32)
    def loss(s,o):
        mean,std=moments(lab*s+o)
        return float(np.mean(abs(mean-target_mean))+np.mean(abs(std-target_std))*.25)
    best=loss(scale,offset);initial=best;rounds=0
    for _ in range(3):
        mean,std=moments(lab*scale+offset)
        ratio=np.clip(target_std/np.maximum(std,1),.94,1.06)
        step=(target_mean-mean)*.45
        proposed_scale=np.clip(scale*ratio,.82,1.18)
        proposed_offset=np.clip(offset*ratio+step,-bounds,bounds)
        score=loss(proposed_scale,proposed_offset)
        if score>=best-.001:break
        scale,offset,best=proposed_scale,proposed_offset,score;rounds+=1
    return {'scale':scale.tolist(),'offset':offset.tolist(),'rounds':rounds,
            'initial_distance':initial,'final_distance':best,'developed_from_xmp':developed}


def apply_match(image,parameters,control=None):
    import cv2
    import numpy as np
    from PIL import Image
    cv2.setNumThreads(1)
    scale=np.array(parameters['scale'],np.float32);offset=np.array(parameters['offset'],np.float32)
    if np.allclose(scale,1) and np.allclose(offset,0):return image.copy()
    result=Image.new('RGB',image.size)
    for top in range(0,image.height,256):
        if control and not control.checkpoint():raise ValueError('调色已取消，原图未修改')
        box=(0,top,image.width,min(top+256,image.height))
        rgb=np.asarray(image.crop(box),dtype=np.float32)/255
        lab=cv2.cvtColor(rgb,cv2.COLOR_RGB2LAB)*scale+offset
        lab[:,:,0]=np.clip(lab[:,:,0],0,100)
        adjusted=cv2.cvtColor(lab,cv2.COLOR_LAB2RGB)
        result.paste(Image.fromarray(np.rint(np.clip(adjusted,0,1)*255).astype(np.uint8)),box)
    return result
