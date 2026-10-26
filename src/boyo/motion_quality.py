"""Bounded directional sharpness evidence, not a semantic judgement of intent.

Use directional image derivatives (the anisotropic-blur family of methods),
and demand evidence in multiple textured native-resolution patches.
"""


def patch_motion(gray):
    import cv2
    import numpy as np
    gx=cv2.Sobel(gray,cv2.CV_32F,1,0,ksize=3)/8
    gy=cv2.Sobel(gray,cv2.CV_32F,0,1,ksize=3)/8
    xx=cv2.Sobel(gx,cv2.CV_32F,1,0,ksize=3)/8
    xy=cv2.Sobel(gx,cv2.CV_32F,0,1,ksize=3)/8
    yy=cv2.Sobel(gy,cv2.CV_32F,0,1,ksize=3)/8
    sharpness=[];energies=[]
    for angle in range(0,180,15):
        c,s=np.cos(np.deg2rad(angle)),np.sin(np.deg2rad(angle))
        first=c*gx+s*gy;second=c*c*xx+2*c*s*xy+s*s*yy
        energy=float(np.mean(first[4:-4,4:-4]**2))
        energies.append(energy)
        sharpness.append(float(np.mean(second[4:-4,4:-4]**2))/(energy+1e-9))
    # A wall or a set of parallel lines alone cannot establish motion blur.
    if min(energies)<max(energies)*.001 or max(energies)<1e-6:return None
    return {'sharpness':sharpness,'energy':energies}


def summarize_motion(patches):
    import numpy as np
    if len(patches)<4:return {'motion_tiles':len(patches)}
    values=np.asarray([p['sharpness'] for p in patches])
    energies=np.asarray([p['energy'] for p in patches])
    directional=np.quantile(values,.75,axis=0)
    energy=np.quantile(energies,.75,axis=0)
    direction=int(np.argmin(energy))
    minima=np.argmin(energies,axis=1)
    distances=np.minimum((minima-direction)%12,(direction-minima)%12)
    return {'motion_tiles':len(patches),'motion_acutance':float(directional[direction]),
            'motion_ratio':float(energy[direction]/max(float(energy.max()),1e-8)),
            'motion_sharpness_ratio':float(directional[direction]/max(float(directional.max()),1e-8)),
            'motion_consistency':float(np.mean(distances<=2)),'motion_angle':direction*15}
