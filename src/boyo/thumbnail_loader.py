"""Bounded background thumbnails for the currently visible page only."""
from collections import OrderedDict
import logging

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QSize, QSaveFile, QIODevice, QByteArray, Signal, Slot
from PySide6.QtGui import QImage, QPixmap, QImageWriter
from PySide6.QtWidgets import QApplication

from .fs import checked
from .imaging import thumbnail_path
from .photo_widgets import THUMBNAILS, read_image


class ThumbSignals(QObject):
    done = Signal(object, QImage)


class ThumbJob(QRunnable):
    def __init__(self, key, request, signals):
        super().__init__(); self.key, self.request, self.signals = key, request, signals

    def run(self):
        image = QImage()
        generated = False
        try:
            if self.request["thumb"].exists():
                try: image = read_image(self.request["thumb"], QSize(320, 220))
                except Exception: pass
            if image.isNull():
                a = self.request["asset"]
                path = checked(self.request["root"], a["path"], True)
                before = path.stat()
                if (before.st_size, before.st_mtime_ns) != (a["size"], a["mtime_ns"]): raise ValueError("changed")
                image = read_image(path, QSize(320, 220)); generated = True
                after = path.stat()
                if (after.st_size, after.st_mtime_ns) != (a["size"], a["mtime_ns"]): raise ValueError("changed")
        except Exception as exc:
            logging.debug("缩略图读取失败：%s", exc)
            image = QImage()
        if not image.isNull() and generated:
            try:
                output = QSaveFile(str(self.request["thumb"]))
                if output.open(QIODevice.OpenModeFlag.WriteOnly):
                    writer = QImageWriter(); writer.setDevice(output)
                    writer.setFormat(QByteArray(b"webp")); writer.setQuality(80)
                    if writer.write(image): output.commit()
                    else:
                        logging.debug("缩略图缓存写入失败：%s", writer.errorString()); output.cancelWriting()
            except Exception as exc:
                # An optional disk cache failure must not hide an otherwise valid image.
                logging.debug("缩略图缓存暂未保存：%s", exc)
        self.signals.done.emit(self.key, image)


class ThumbnailLoader(QObject):
    def __init__(self):
        super().__init__(QApplication.instance())
        self.pool = QThreadPool(self); self.pool.setMaxThreadCount(1)
        self.signals = ThumbSignals(self); self.signals.done.connect(self.finished)
        self.requests = OrderedDict(); self.active = None; self.serial = 0

    def new_owner(self):
        self.serial += 1; return self.serial

    def cancel(self, owner):
        for key, request in list(self.requests.items()):
            request["callbacks"] = [(o, fn) for o, fn in request["callbacks"] if o != owner]
            if not request["callbacks"] and key != self.active: self.requests.pop(key)

    def request(self, runtime, root, asset, owner, callback):
        thumb = thumbnail_path(runtime, asset)
        key = (str(root), str(thumb), 320, 220)
        pix = THUMBNAILS.get(key)
        if pix is not None: callback(pix); return
        if key not in self.requests:
            self.requests[key] = {"asset": dict(asset), "root": root, "thumb": thumb, "callbacks": []}
        self.requests[key]["callbacks"].append((owner, callback))
        self.start_next()

    def start_next(self):
        if self.active is None and self.requests:
            key = next(iter(self.requests)); self.active = key
            self.pool.start(ThumbJob(key, self.requests[key], self.signals))

    @Slot(object, QImage)
    def finished(self, key, image):
        request = self.requests.pop(key, None); self.active = None
        if request and not image.isNull():
            pix = QPixmap.fromImage(image); THUMBNAILS.put(key, pix)
            for owner, callback in request["callbacks"]: callback(pix)
        self.start_next()


def thumbnail_loader():
    app = QApplication.instance()
    if not hasattr(app, "boyo_thumbnail_loader"): app.boyo_thumbnail_loader = ThumbnailLoader()
    return app.boyo_thumbnail_loader
