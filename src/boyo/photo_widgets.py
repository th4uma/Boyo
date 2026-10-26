from __future__ import annotations

from pathlib import Path
from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor,QImage, QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,QSizePolicy,
)

from .fs import RAW_EXTS, checked
from .imaging import load_preview, thumbnail_path
from .thumbnail_geometry import card_geometry


STATE_NAMES = {"pending": "未决定", "keep": "保留", "delete": "待删"}
STATE_COLORS = {'pending':'#a6aec7','keep':'#9ed0ac','delete':'#e5a1a1'}


class ImageCache:
    """Bound decoded images by bytes, rather than keeping every browsed photo."""
    def __init__(self, max_bytes):
        self.max_bytes = max_bytes
        self.items = OrderedDict(); self.bytes = 0

    def get(self, key):
        entry = self.items.get(key)
        if entry is not None:
            self.items.move_to_end(key)
            return entry[0]

    def put(self, key, image):
        size = image.width() * image.height() * 4
        if key in self.items:
            self.bytes -= self.items.pop(key)[1]
        if size > self.max_bytes:
            return
        while self.items and self.bytes + size > self.max_bytes:
            self.bytes -= self.items.popitem(last=False)[1][1]
        self.items[key] = (image, size); self.bytes += size


THUMBNAILS = ImageCache(32 * 1024 * 1024)
PREVIEWS = ImageCache(64 * 1024 * 1024)


def display_pixmap(path, limit=QSize(2400, 2400)):
    """Read-only, EXIF-aware display. Decode a bounded size unless explicitly requested."""
    path = Path(path)
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns, limit.width(), limit.height()) if limit else None
    if key is not None:
        cached = PREVIEWS.get(key)
        if cached is not None:
            return QPixmap.fromImage(cached)
    image = read_image(path, limit)
    if key is not None: PREVIEWS.put(key, image)
    return QPixmap.fromImage(image)


def read_image(path, limit):
    """QImage decoding is safe in a worker; QPixmap stays on the UI thread."""
    path = Path(path)
    if path.suffix.lower() not in RAW_EXTS:
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        size = reader.size()
        if limit and size.isValid() and (size.width() > limit.width() or size.height() > limit.height()):
            reader.setScaledSize(size.scaled(limit, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            raise ValueError(reader.errorString())
        return image
    image = load_preview(path)
    if limit:
        image.thumbnail((limit.width(), limit.height()))
    rgb = image.convert("RGB")
    qimage = QImage(rgb.tobytes(), rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888).copy()
    return qimage


class DecodeSignals(QObject):
    done = Signal(object, QImage, str)


class DecodeJob(QRunnable):
    def __init__(self, request, signals):
        super().__init__(); self.request, self.signals = request, signals

    def run(self):
        token, path, limit, key = self.request
        try:
            result, error = read_image(path, limit), ""
        except Exception as exc:
            result, error = QImage(), str(exc)
        self.signals.done.emit(self.request, result, error)


class PreviewQueue(QObject):
    """One decode at a time, at most one pending (latest) request, no prefetch."""
    ready = Signal(int, QImage, str)

    def __init__(self):
        super().__init__(QApplication.instance())
        self.pool = QThreadPool(self); self.pool.setMaxThreadCount(1)
        self.signals = DecodeSignals(self); self.signals.done.connect(self.finished)
        self.active = None; self.pending = None; self.serial = 0

    def token(self):
        self.serial += 1; return self.serial

    def request(self, token, path, limit):
        try:
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns, limit.width(), limit.height()) if limit else None
            image = PREVIEWS.get(key) if key is not None else None
            if image is not None:
                self.pending = None
                self.ready.emit(token, image, ""); return
        except Exception as exc:
            self.ready.emit(token, QImage(), str(exc)); return
        self.pending = (token, path, limit, key)
        self.start_latest()

    def start_latest(self):
        if self.active is None and self.pending is not None:
            self.active, self.pending = self.pending, None
            self.pool.start(DecodeJob(self.active, self.signals))

    @Slot(object, QImage, str)
    def finished(self, request, image, error):
        self.active = None
        token, path, limit, key = request
        if not image.isNull() and key is not None: PREVIEWS.put(key, image)
        self.ready.emit(token, image, error)
        self.start_latest()

    def cancel(self, token):
        if self.pending is not None and self.pending[0] == token:
            self.pending = None


def preview_queue():
    app = QApplication.instance()
    if not hasattr(app, "boyo_preview_queue"):
        app.boyo_preview_queue = PreviewQueue()
    return app.boyo_preview_queue


def asset_thumbnail(runtime, root, asset, limit=QSize(320, 220), allow_source=True):
    cached = thumbnail_path(runtime, asset)
    key = (str(root), str(cached), limit.width(), limit.height())
    pix = THUMBNAILS.get(key)
    if pix is not None:
        return pix
    if cached.exists():
        pix = QPixmap(str(cached))
    else:
        if not allow_source: return QPixmap()
        try:
            # No feature extraction or AI calls: only the currently displayed page.
            pix = display_pixmap(checked(root, asset["path"], True), limit)
        except Exception:
            return QPixmap()
    if not pix.isNull(): THUMBNAILS.put(key, pix)
    return pix


def file_operation_locked(widget,store,root,asset):
    """The same file lock applies inside nested previews and plan dialogs."""
    parent=widget
    while parent is not None:
        context=getattr(parent,'task_context',None)
        if context and context['store'].path==store.path and Path(context['root'])==Path(root):
            return asset['id'] in context.get('locked_ids',set()) or asset['logical_key'] in context.get('locked_keys',set())
        parent=parent.parent()
    return False


class ThumbnailButton(QPushButton):
    doubleClicked=Signal()

    def mouseDoubleClickEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:self.doubleClicked.emit();event.accept()
        else:super().mouseDoubleClickEvent(event)


class PhotoCard(QFrame):
    previewRequested = Signal(int)
    decisionRequested = Signal(int, str)
    selectionRequested = Signal(int)

    def __init__(self, asset, runtime, root, suggestion="", editable=True, parent=None):
        super().__init__(parent)
        self.asset_id = asset["id"]
        self.editable = editable
        self.setObjectName("photoCard")
        layout = QVBoxLayout(self); layout.setContentsMargins(10, 10, 10, 10); layout.setSpacing(7)
        self.image_button = ThumbnailButton()
        self.image_button.setFixedHeight(205)
        pix = asset_thumbnail(runtime, root, asset, allow_source=False)
        self.thumb_request=(runtime,root,dict(asset))
        self.thumb_owner=None
        self.thumb_ready=not pix.isNull()
        if pix.isNull():
            self.image_button.setText("预览加载中")
        else:
            from PySide6.QtGui import QIcon
            self.image_button.setIcon(QIcon(pix)); self.image_button.setIconSize(QSize(290, 195))
        self.image_button.setToolTip("双击看大图；左右键切图，K 保留 / D 待删 / U 撤销")
        self.image_button.clicked.connect(lambda: self.selectionRequested.emit(self.asset_id))
        self.image_button.doubleClicked.connect(lambda:self.previewRequested.emit(self.asset_id))
        layout.addWidget(self.image_button)
        self.name_label = QLabel(Path(asset["path"]).name)
        self.filename=Path(asset['path']).name
        self.name_label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.name_label.setToolTip(asset["path"]);self.name_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.name_label)
        self.hint_label = QLabel(suggestion);self.hint_label.setWordWrap(True);self.hint_label.setObjectName('muted');self.hint_label.setToolTip(suggestion)
        self.hint_label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);layout.addWidget(self.hint_label)
        self.state_label = QLabel();self.state_label.setWordWrap(True);self.state_label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);layout.addWidget(self.state_label)
        bar = QHBoxLayout()
        self.keep_button = QPushButton("保留")
        self.delete_button = QPushButton("待删")
        self.pending_button = QPushButton("撤销决定")
        for btn, value in ((self.keep_button, "keep"), (self.delete_button, "delete"), (self.pending_button, "pending")):
            btn.setProperty('photoDecision',value)
            btn.clicked.connect(lambda checked=False, decision=value: self.decisionRequested.emit(self.asset_id, decision))
            bar.addWidget(btn)
        if asset["protected"]:
            self.delete_button.setToolTip("受保护照片不能加入待删")
        layout.addLayout(bar)
        self.update_state(asset)

    def showEvent(self,event):
        super().showEvent(event)
        if self.thumb_ready:return
        from .thumbnail_loader import thumbnail_loader
        loader=thumbnail_loader()
        if self.thumb_owner is None:
            self.thumb_owner=loader.new_owner();owner=self.thumb_owner
            self.destroyed.connect(lambda:loader.cancel(owner))
        loader.request(*self.thumb_request,self.thumb_owner,self.set_thumbnail)

    def hideEvent(self,event):
        if self.thumb_owner is not None:
            from .thumbnail_loader import thumbnail_loader
            thumbnail_loader().cancel(self.thumb_owner)
        super().hideEvent(event)

    def set_thumbnail(self,pix):
        from PySide6.QtGui import QIcon
        self.image_button.setText('');self.image_button.setIcon(QIcon(pix));self.update_image_geometry()
        self.thumb_ready=True

    def set_card_width(self,width):
        if getattr(self,'compact',False):
            width,height,image_height=card_geometry(width)
            self.setFixedSize(width,height);self.image_button.setFixedHeight(image_height)
            self.name_label.setFixedHeight(20);self.state_label.setFixedHeight(20);self.hint_label.setFixedHeight(18)
            self.layout().activate();self.update_image_geometry();return height
        height=max(145,min(230,round((width-20)*.64)))
        if self.image_button.height()!=height:self.image_button.setFixedHeight(height)
        line=self.fontMetrics().lineSpacing()
        self.hint_label.setMinimumHeight(2*line);self.state_label.setMinimumHeight(2*line)
        self.update_image_geometry()
        return height+5*line+106

    def set_compact(self):
        if getattr(self,'compact',False):return
        self.compact=True;layout=self.layout();self.saved_layout_items=[]
        while layout.count():self.saved_layout_items.append(layout.takeAt(0))
        layout.setContentsMargins(6,6,6,6);layout.setSpacing(5);layout.addWidget(self.image_button)
        self.image_button.setObjectName('thumbnailImage')
        row=QHBoxLayout();row.setSpacing(6);self.name_label.setObjectName('photoMeta');self.state_label.setObjectName('photoMeta')
        self.name_label.setWordWrap(False)
        self.state_label.setWordWrap(False);self.state_label.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Preferred)
        row.addWidget(self.name_label,1);row.addWidget(self.state_label);layout.addLayout(row)
        self.hint_label.setWordWrap(False);self.suggestion_text=self.hint_label.text();layout.addWidget(self.hint_label)
        for button in (self.keep_button,self.delete_button,self.pending_button):button.hide()
        self.update_state(self.asset_snapshot);self.set_card_width(max(160,self.width()))

    def update_image_geometry(self):
        inset=0 if getattr(self,'compact',False) else 1
        self.image_button.setIconSize(QSize(max(32,self.image_button.width()-16*inset),max(32,self.image_button.height()-12*inset)))
        if hasattr(self,'filename'):
            self.name_label.setText(self.name_label.fontMetrics().elidedText(self.filename,Qt.TextElideMode.ElideRight,max(1,self.name_label.width())))
        if getattr(self,'compact',False):self.hint_label.setText(self.hint_label.fontMetrics().elidedText(self.suggestion_text,Qt.TextElideMode.ElideRight,max(1,self.hint_label.width())))

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.update_image_geometry()

    def update_state(self, asset):
        self.asset_snapshot=dict(asset)
        self.state_label.setText("当前：" + STATE_NAMES[asset["decision"]] + (" · 精选/关联保护" if asset["protected"] else ""))
        if getattr(self,'compact',False):
            self.state_label.setText('已保护' if asset['protected'] else STATE_NAMES[asset['decision']])
            if self.property('decision')!=asset['decision']:
                self.setProperty('decision',asset['decision']);self.style().unpolish(self);self.style().polish(self)
        color = STATE_COLORS[asset['decision']]
        self.state_label.setStyleSheet(f"color:{color};font-weight:600;")
        ordinary = self.editable and asset["role"] == "ordinary_jpeg" and asset["present"]
        self.keep_button.setEnabled(ordinary)
        self.delete_button.setEnabled(ordinary and not asset["protected"])
        self.pending_button.setEnabled(ordinary and not asset["protected"])


class ImageView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.scene = QGraphicsScene(self); self.setScene(self.scene)
        self.item = QGraphicsPixmapItem(); self.scene.addItem(self.item)
        self.item.setShapeMode(QGraphicsPixmapItem.ShapeMode.BoundingRectShape)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self.fitted = True

    def set_pixmap(self, pixmap):
        self.item.setPixmap(pixmap); self.scene.setSceneRect(self.item.boundingRect()); self.fit_image()

    def fit_image(self):
        self.resetTransform(); self.fitInView(self.item, Qt.AspectRatioMode.KeepAspectRatio); self.fitted = True

    def zoom(self, factor):
        current = self.transform().m11()
        if 0.02 <= current * factor <= 16:
            self.scale(factor, factor); self.fitted = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fitted:
            self.fitInView(self.item, Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event):
        self.zoom(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
        event.accept()


class PhotoPreviewDialog(QDialog):
    decisionChanged = Signal(int, str)

    def __init__(self, store, runtime, root, asset_ids, current_id, context="manual", editable_ids=None, parent=None):
        super().__init__(parent)
        self.store, self.runtime, self.root, self.context = store, runtime, root, context
        self.asset_ids = list(dict.fromkeys(asset_ids))
        self.album=self.store.asset(current_id)['album']
        if current_id not in self.asset_ids:
            self.asset_ids.append(current_id)
        self.index = self.asset_ids.index(current_id)
        self.editable_ids = set(self.asset_ids if editable_ids is None else editable_ids)
        self.all_editable=editable_ids is None
        self.loader = preview_queue(); self.loader.ready.connect(self.image_ready)
        self.load_token = None; self.full_resolution = False
        self.load_timer = QTimer(self); self.load_timer.setSingleShot(True); self.load_timer.setInterval(60)
        self.load_timer.timeout.connect(self.request_image)
        self.resize(1200, 850); self.setMinimumSize(850, 620)
        layout = QVBoxLayout(self);layout.setContentsMargins(20,14,20,14);layout.setSpacing(12)
        top=QHBoxLayout();self.info = QLabel(); self.info.setWordWrap(True);top.addWidget(self.info,1)
        close=QPushButton('关闭 ×');close.setObjectName('quiet');close.clicked.connect(self.accept);top.addWidget(close);layout.addLayout(top)
        bar = QHBoxLayout()
        self.previous_button = QPushButton(self);self.previous_button.hide();self.previous_button.clicked.connect(lambda: self.navigate(-1))
        self.next_button = QPushButton(self);self.next_button.hide();self.next_button.clicked.connect(lambda: self.navigate(1))
        self.view = ImageView(); layout.addWidget(self.view, 1)
        self.view.setBackgroundBrush(QColor('#10131c'))
        self.state = QLabel()
        actions = QHBoxLayout()
        actions.addStretch();actions.addWidget(self.state)
        self.keep_button = QPushButton("保留  K")
        self.delete_button = QPushButton("标记待删  D")
        self.pending_button = QPushButton("撤销决定  U")
        for btn, decision in ((self.keep_button, "keep"), (self.delete_button, "delete"), (self.pending_button, "pending")):
            btn.setProperty('photoDecision',decision)
            btn.clicked.connect(lambda checked=False, value=decision: self.decide(value)); actions.addWidget(btn)
        layout.addLayout(actions)
        shortcuts = {"Left": lambda: self.navigate(-1), "Right": lambda: self.navigate(1),
                     "K": lambda: self.decide("keep"), "D": lambda: self.decide("delete"),
                     "U": lambda: self.decide("pending"), 'Ctrl+Z':lambda:self.decide('pending'), "Home": lambda: self.view.fit_image(), '1':self.load_full}
        self.shortcuts = []
        for key, fn in shortcuts.items():
            shortcut = QShortcut(QKeySequence(key), self)
            if key in {"K", "D", "U"}: shortcut.setAutoRepeat(False)
            shortcut.activated.connect(fn); self.shortcuts.append(shortcut)
        self.show_current()

    @property
    def current_id(self):
        return self.asset_ids[self.index] if self.asset_ids else None

    def synchronize(self,asset_ids):
        """Refresh a live queue without decoding the same current image again."""
        current=self.current_id;index=self.index
        self.asset_ids=list(dict.fromkeys(asset_ids))
        if self.all_editable:self.editable_ids=set(self.asset_ids)
        else:self.editable_ids.intersection_update(self.asset_ids)
        if not self.asset_ids:self.reject();return
        self.index=self.asset_ids.index(current) if current in self.asset_ids else min(index,len(self.asset_ids)-1)
        asset=self.store.asset(self.current_id)
        signature=(asset['id'],asset['size'],asset['mtime_ns'])
        if signature!=getattr(self,'display_identity',None):self.show_current()
        else:
            self.info.setText(f"{self.index+1} / {len(self.asset_ids)}　{asset['path']}");self.update_state()

    def show_current(self):
        asset = self.store.asset(self.current_id)
        self.display_identity=(asset['id'],asset['size'],asset['mtime_ns'])
        self.setWindowTitle(f"{self.index+1}/{len(self.asset_ids)} · {Path(asset['path']).name}")
        self.info.setText(f"{self.index+1} / {len(self.asset_ids)}　{asset['path']}")
        self.previous_button.setEnabled(self.index > 0); self.next_button.setEnabled(self.index < len(self.asset_ids)-1)
        self.loader.cancel(self.load_token); self.load_token = None; self.full_resolution = False
        self.view.set_pixmap(asset_thumbnail(self.runtime, self.root, asset, allow_source=False))
        self.info.setText(self.info.text() + "　正在加载清晰预览…")
        if self.isVisible(): self.load_timer.start()
        self.update_state()

    def request_image(self):
        if not self.isVisible(): return
        self.load_token = self.loader.token()
        try:
            asset = self.store.asset(self.current_id)
            path = checked(self.root, asset["path"], True)
            self.loader.request(self.load_token, path, None if self.full_resolution else QSize(2400, 2400))
        except Exception as exc:
            self.image_ready(self.load_token, QImage(), str(exc))

    def image_ready(self, token, image, error):
        if token != self.load_token: return
        base = self.info.text().split("　正在加载")[0]
        if error:
            self.info.setText(base + f"　读取失败：{error}"); return
        self.view.set_pixmap(QPixmap.fromImage(image))
        if self.full_resolution:
            self.view.resetTransform(); self.view.fitted = False
        self.info.setText(base)

    def update_state(self):
        asset = self.store.asset(self.current_id)
        editable = self.current_id in self.editable_ids and asset["present"] and asset["role"] == "ordinary_jpeg" and not file_operation_locked(self,self.store,self.root,asset)
        self.keep_button.setEnabled(editable)
        self.delete_button.setEnabled(editable and not asset["protected"])
        self.pending_button.setEnabled(editable and not asset["protected"])
        suffix = " · 受保护，不能淘汰" if asset["protected"] else ""
        if self.current_id not in self.editable_ids:
            suffix += " · 仅作比较参照"
        self.state.setText("当前：" + STATE_NAMES[asset["decision"]] + suffix)
        self.state.setStyleSheet('color:'+STATE_COLORS[asset['decision']])

    def navigate(self, delta):
        index = self.index + delta
        if 0 <= index < len(self.asset_ids):
            self.index = index; self.show_current()

    def load_full(self):
        self.load_timer.stop(); self.loader.cancel(self.load_token)
        self.full_resolution = True
        self.info.setText(self.info.text().split("　正在加载")[0] + "　正在加载原始分辨率…")
        self.request_image()

    def done(self, result):
        self.load_timer.stop(); self.loader.cancel(self.load_token); self.load_token = None
        super().done(result)

    def showEvent(self, event):
        super().showEvent(event)
        self.load_timer.start()

    def hideEvent(self, event):
        self.load_timer.stop(); self.loader.cancel(self.load_token); self.load_token = None
        super().hideEvent(event)

    def decide(self, decision):
        btn = {"keep": self.keep_button, "delete": self.delete_button, "pending": self.pending_button}[decision]
        if not btn.isEnabled():
            return
        try:
            if file_operation_locked(self,self.store,self.root,self.store.asset(self.current_id)):
                raise ValueError('此照片正在执行文件操作，请完成后再修改')
            context = self.context.get(self.current_id, "manual") if isinstance(self.context, dict) else self.context
            self.store.decide([self.current_id], decision, context)
        except ValueError as exc:
            QMessageBox.warning(self, "未修改状态", str(exc)); return
        self.update_state(); self.decisionChanged.emit(self.current_id, decision)
