from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path


from .fs import RAW_EXTS, sha256

FEATURE_VERSION = "imagehash-sift-focus-v2"


def load_preview(path):
    from PIL import Image, ImageOps
    path = Path(path)
    if path.suffix.lower() in RAW_EXTS:
        import rawpy
        with rawpy.imread(str(path)) as raw:
            # Deliberately do not develop full RAWs when embedded previews are absent.
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(io.BytesIO(thumb.data)) as im:
                    return ImageOps.exif_transpose(im).convert("RGB")
            return Image.fromarray(thumb.data).convert("RGB")
    with Image.open(path) as im:
        return ImageOps.exif_transpose(im).convert("RGB")


def thumbnail_path(runtime, asset):
    key = f"{asset['path']}|{asset['size']}|{asset['mtime_ns']}|{FEATURE_VERSION}"
    return runtime.home / "cache" / "thumbs" / (hashlib.sha256(key.encode()).hexdigest() + ".webp")


def extract(path, thumb_path):
    import imagehash
    import numpy as np
    from PIL import Image
    im = load_preview(path)
    original_width, original_height = im.size
    from .vision import save_descriptors
    from .quality import focus_measurements
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    focus = focus_measurements(im)
    save_descriptors(im, thumb_path)
    im.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    phash, dhash = str(imagehash.phash(im)), str(imagehash.dhash(im))
    rgb = np.asarray(im, dtype=np.float32) / 255.0
    gray = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    gray8 = np.asarray(im.convert("L"), dtype=np.uint8)
    # Indicators produce review suggestions, never file operations.
    lap = (-4 * gray[1:-1, 1:-1] + gray[:-2, 1:-1] + gray[2:, 1:-1]
           + gray[1:-1, :-2] + gray[1:-1, 2:])
    sharp = min(1.0, math.log1p(float(lap.var()) * 10000) / 7.0) if lap.size else 0.0
    contrast = min(1.0, float(gray.std()) * 4)
    clipped = float(((gray < 0.02) | (gray > 0.98)).mean())
    saturation = float((rgb.max(axis=2) - rgb.min(axis=2)).mean())
    hist = np.bincount(gray8.ravel(), minlength=256).astype(float)
    hist /= max(hist.sum(), 1)
    entropy = float(-(hist[hist > 0] * np.log2(hist[hist > 0])).sum() / 8)
    brightness = float(gray.mean())
    means = rgb.mean(axis=(0, 1)).tolist()
    vector = [sharp, contrast, 1 - clipped, saturation, entropy, brightness, *means]
    quality = 0.5 * sharp + 0.2 * contrast + 0.15 * (1 - clipped) + 0.15 * entropy
    thumb = im.copy()
    thumb.thumbnail((360, 240), Image.Resampling.LANCZOS)
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    thumb.save(thumb_path, "WEBP", quality=80)
    return {"phash": phash, "dhash": dhash, "vector": vector, "quality": quality,
            "sha256": sha256(Path(path)),
            "width": original_width, "height": original_height,
            "metrics": {"sharpness": sharp, "contrast": contrast, "clipping": clipped,
                        "brightness": brightness, "entropy": entropy, **focus}, "error": None}


def hash_distance(a, b):
    import imagehash
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)
