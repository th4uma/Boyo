"""Shared inline viewer. Loading is bounded; decisions stay in the controller."""
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .fs import checked
from .photo_widgets import ImageView, PreviewQueue, asset_thumbnail, preview_queue


class PhotoViewer(QWidget):
    def __init__(self, reference=False, parent=None):
        super().__init__(parent)
        self.asset=None; self.runtime=None; self.root=None; self.key=None; self.load_token=None
        self.full_resolution=False
        app=QApplication.instance()
        if reference:
            if not hasattr(app,'boyo_reference_queue'): app.boyo_reference_queue=PreviewQueue()
            self.loader=app.boyo_reference_queue
        else: self.loader=preview_queue()
        self.loader.ready.connect(self.image_ready)
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        self.caption=QLabel('选择照片'); self.caption.setWordWrap(True); self.caption.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.caption)
        self.view=ImageView(); self.view.setMinimumHeight(96); layout.addWidget(self.view,1)
        bar=QHBoxLayout()
        fit=QPushButton('适应窗口'); fit.clicked.connect(self.view.fit_image); bar.addWidget(fit)
        self.full_button=QPushButton('检查原尺寸'); self.full_button.clicked.connect(self.load_full); bar.addWidget(self.full_button)
        self.message=QLabel(); self.message.setObjectName('muted'); self.message.setWordWrap(True); bar.addWidget(self.message,1)
        layout.addLayout(bar)
        self.timer=QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(60)
        self.timer.timeout.connect(self.request_image)

    def set_asset(self,asset,runtime,root,caption=None):
        self.runtime,self.root=runtime,Path(root)
        key=(str(root),asset['id'],asset['path'],asset['size'],asset['mtime_ns']) if asset else None
        self.asset=asset
        self.caption.setText(caption or (Path(asset['path']).name if asset else '没有可显示的照片'))
        self.full_button.setEnabled(bool(asset and asset.get('present',1)))
        if self.key==key:return
        self.key=key; self.timer.stop(); self.loader.cancel(self.load_token); self.load_token=None
        self.full_resolution=False; self.view.set_pixmap(QPixmap()); self.message.clear()
        if not asset:return
        pix=asset_thumbnail(runtime,self.root,asset,allow_source=False)
        if pix is not None:self.view.set_pixmap(pix)
        self.message.setText('正在加载预览…')
        if self.isVisible():self.timer.start()

    def request_image(self):
        if not self.asset or not self.isVisible():return
        self.load_token=self.loader.token()
        try:
            path=checked(self.root,self.asset['path'],True)
            self.loader.request(self.load_token,path,None if self.full_resolution else QSize(2400,2400))
        except (OSError,ValueError) as exc:self.image_ready(self.load_token,QImage(),str(exc))

    def image_ready(self,token,image,error):
        if token!=self.load_token:return
        if error:self.message.setText('无法读取 · '+error);return
        self.view.set_pixmap(QPixmap.fromImage(image)); self.message.setText('原始分辨率' if self.full_resolution else '滚轮缩放 · 拖动平移')
        if self.full_resolution:self.view.resetTransform();self.view.fitted=False

    def load_full(self):
        self.timer.stop();self.loader.cancel(self.load_token);self.full_resolution=True
        self.message.setText('正在读取原始分辨率…');self.request_image()

    def showEvent(self,event):
        super().showEvent(event)
        if self.asset:self.timer.start()

    def hideEvent(self,event):
        self.timer.stop();self.loader.cancel(self.load_token);self.load_token=None
        super().hideEvent(event)


class PhotoSequence(QWidget):
    """A stable photo queue, with an optional fixed reference alongside it."""
    def __init__(self,owner,kind):
        super().__init__();self.owner=owner;self.kind=kind;self.ids=[];self.index=0;self.reference_id=None
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        pictures=QHBoxLayout();self.reference=PhotoViewer(True);self.reference.hide();pictures.addWidget(self.reference,1)
        self.viewer=PhotoViewer();pictures.addWidget(self.viewer,1);layout.addLayout(pictures,1)
        self.evidence=QLabel();self.evidence.setWordWrap(True);layout.addWidget(self.evidence)
        bar=QHBoxLayout();self.previous=QPushButton('← 上一张');self.next=QPushButton('下一张 →')
        self.previous.clicked.connect(lambda:self.navigate(-1));self.next.clicked.connect(lambda:self.navigate(1))
        bar.addWidget(self.previous);bar.addWidget(self.next);self.position=QLabel();bar.addWidget(self.position);bar.addStretch()
        self.keep=QPushButton('保留 / 救回  K');self.delete=QPushButton('标记待删  D');self.pending=QPushButton('撤销决定  U')
        for btn,state in ((self.keep,'keep'),(self.delete,'delete'),(self.pending,'pending')):
            btn.clicked.connect(lambda checked=False,value=state:self.decide(value));bar.addWidget(btn)
        layout.addLayout(bar)

    @property
    def current_id(self):return self.ids[self.index] if self.ids and self.index<len(self.ids) else None

    def set_queue(self,ids,current=None,reference=None):
        previous=self.current_id
        self.ids=list(dict.fromkeys(ids));current=current if current in self.ids else previous
        self.index=self.ids.index(current) if current in self.ids else 0;self.reference_id=reference
        self.render()

    def render(self):
        owner=self.owner;asset=owner.asset_by_id.get(self.current_id)
        reference=owner.asset_by_id.get(self.reference_id)
        self.reference.setVisible(reference is not None)
        if reference:self.reference.set_asset(reference,owner.runtime,owner.root,'固定参照 · '+Path(reference['path']).name+' · 只读')
        caption=('当前操作：'+Path(asset['path']).name+' · '+{'keep':'已保留','delete':'待删','pending':'未决定'}[asset['decision']]) if asset else '没有候选照片'
        if asset and asset['protected']:caption+=' · 精选保护'
        self.viewer.set_asset(asset,owner.runtime,owner.root,caption)
        editable=bool(asset and asset.get('present',1) and asset['role']=='ordinary_jpeg' and not owner.photo_is_locked(asset))
        self.keep.setEnabled(editable);self.delete.setEnabled(editable and not asset['protected']);self.pending.setEnabled(editable and not asset['protected'])
        self.previous.setEnabled(self.index>0);self.next.setEnabled(self.index+1<len(self.ids))
        self.position.setText(f'{self.index+1 if self.ids else 0} / {len(self.ids)}')
        self.evidence.setText(owner.photo_evidence(self.current_id,self.kind))

    def navigate(self,delta):
        index=self.index+delta
        if 0<=index<len(self.ids):self.index=index;self.render();self.owner.sequence_position_changed(self.kind,self.current_id)

    def decide(self,state):
        button={'keep':self.keep,'delete':self.delete,'pending':self.pending}[state]
        if button.isEnabled():self.owner.sequence_decide(self,state)
