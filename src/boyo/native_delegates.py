"""Paint thumbnail cards directly; there is no widget per offscreen photo."""
from pathlib import Path
from PySide6.QtCore import Qt,QRectF,QSize,QEvent
from PySide6.QtGui import QColor,QFont,QFontMetrics,QPainter,QPainterPath,QPen
from PySide6.QtWidgets import QStyledItemDelegate,QStyle
from .browser_widgets import QuietItemDelegate


def font(size,bold=False):
    value=QFont('Microsoft YaHei');value.setPixelSize(size);value.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal);return value


class NavigationDelegate(QuietItemDelegate):
    def paint(self,painter,option,index):
        painter.save();painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect=QRectF(option.rect).adjusted(0,2,0,-3)
        if option.state&QStyle.StateFlag.State_Selected:painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor('#3a3f60'));painter.drawRoundedRect(rect,6,6)
        label=index.data(Qt.ItemDataRole.DisplayRole).split('\n')[0];count=index.data(Qt.ItemDataRole.UserRole+3)
        painter.setFont(font(13));painter.setPen(QColor('#d9dce8'))
        right=30 if count is not None else 5;text_rect=rect.adjusted(10,0,-right,0)
        painter.drawText(text_rect,Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft,label)
        if count is not None:
            painter.setFont(font(11));painter.setPen(QColor('#b4bbd2'));painter.drawText(rect.adjusted(0,0,-10,0),Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter,str(count))
        painter.restore()
    def sizeHint(self,option,index):return QSize(132,48)


class FixedItemDelegate(QuietItemDelegate):
    def sizeHint(self,option,index):
        size=self.parent().gridSize()
        return size if size.width()>0 and size.height()>0 else super().sizeHint(option,index)


class ThumbnailDelegate(FixedItemDelegate):
    def __init__(self,owner,kind,parent):super().__init__(parent);self.owner,self.kind=owner,kind
    def paint(self,painter,option,index):
        aid=index.data(Qt.ItemDataRole.UserRole);asset=self.owner.asset_by_id.get(aid)
        slot=index.data(Qt.ItemDataRole.UserRole+1)
        selected=bool(option.state&QStyle.StateFlag.State_Selected)
        decision=(asset or {}).get('decision','pending');protected=(asset or {}).get('protected',False)
        painter.save();painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card=QRectF(option.rect).adjusted(6,6,-10,-10)
        border='#a3a2cf' if selected else '#435f56' if decision=='keep' else '#654653' if decision=='delete' else '#353a51'
        painter.setPen(QPen(QColor(border),1));painter.setBrush(QColor('#2b3048' if selected else '#202434'));painter.drawRoundedRect(card,8,8)
        image=QRectF(card.x()+7,card.y()+7,max(1,card.width()-14),max(1,(card.width()-14)*2/3))
        path=QPainterPath();path.addRoundedRect(image,4,4);painter.setClipPath(path)
        painter.fillRect(image,QColor('#11141e'))
        icon=index.data(Qt.ItemDataRole.DecorationRole)
        if icon and not icon.isNull():
            pix=icon.pixmap(image.size().toSize());size=pix.size().scaled(image.size().toSize(),Qt.AspectRatioMode.KeepAspectRatio)
            target=QRectF(image.center().x()-size.width()/2,image.center().y()-size.height()/2,size.width(),size.height())
            painter.drawPixmap(target,pix,QRectF(pix.rect()))
        elif not asset:
            missing=getattr(self.owner,'native_set_members',{}).get(slot,{}).get('present')==0 if self.kind=='set' else False
            painter.setFont(font(12));painter.setPen(QColor('#979eb8'));painter.drawText(image,Qt.AlignmentFlag.AlignCenter,'原照片已移出' if missing else '成片封面' if slot==0 else '空位')
        painter.setClipRect(option.rect)
        badge=''
        if self.kind=='set':badge=f'{int(slot or 0)+1:02d}'+(' · 封面' if slot==0 else '')
        elif protected:badge='成片' if asset['role']=='finished' else '精选保护'
        if badge:
            painter.setFont(font(10));width=QFontMetrics(painter.font()).horizontalAdvance(badge)+12
            pill=QRectF(image.x()+7,image.y()+7,width,21);painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor('#dd222335'));painter.drawRoundedRect(pill,3,3)
            painter.setPen(QColor('#d6c8e7'));painter.drawText(pill,Qt.AlignmentFlag.AlignCenter,badge)
        if self.kind not in {'set','candidate'}:
            circle=QRectF(image.right()-23,image.bottom()-23,14,14);painter.setPen(QPen(QColor('#a4a2cf' if selected else '#858da8'),1));painter.setBrush(QColor('#a4a2cf') if selected else QColor('#55151824'));painter.drawEllipse(circle)
        name=Path(asset['path']).name if asset else f'第 {int(slot or 0)+1} 位'
        state='已保护' if protected else {'keep':'已保留','delete':'待删','pending':'未决定'}[decision]
        color='#a2cdb0' if decision=='keep' else '#dfa3ad' if decision=='delete' else '#979eb8'
        if self.kind=='set':
            item=getattr(self.owner,'native_set_members',{}).get(slot,{})
            state='已锁定' if item.get('locked') else '锁定' if asset else '';color='#b4a4ce' if item.get('locked') else '#979eb8'
        if self.kind=='candidate':state=''
        removable=self.kind=='set' and bool(asset)
        painter.setFont(font(12));fm=QFontMetrics(painter.font());state_width=fm.horizontalAdvance(state)+10 if state else 0
        line=QRectF(card.x()+10,image.bottom()+7,card.width()-20-(19 if removable else 0),20)
        painter.setPen(QColor('#d9dce8'));painter.drawText(line.adjusted(0,0,-state_width,0),Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft,fm.elidedText(name,Qt.TextElideMode.ElideRight,max(1,int(line.width()-state_width))))
        painter.setFont(font(11));painter.setPen(QColor(color));painter.drawText(line,Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignRight,state)
        if removable:
            painter.setPen(QColor('#979eb8'));painter.drawText(QRectF(card.right()-25,line.y(),16,20),Qt.AlignmentFlag.AlignCenter,'×')
        if self.kind!='candidate':
            note='成片 · 用于图集封面' if asset and asset['role']=='finished' else '精选保护' if protected else '普通 JPG'
            if self.kind=='quality' and asset:
                row=self.owner.quality_entries.get(aid,{})
                note=' · '.join(issue.get('label','') for issue in row.get('issues',[])) or '疑似技术问题 · 请复核'
            elif self.kind=='set':note=('成片封面' if slot==0 else '普通照片') if asset else ''
            elif asset and self.owner.filter.currentText()=='待删 JPG' and decision!='delete':note='本轮已救回' if decision=='keep' else '本轮已撤销'
            painter.setFont(font(11));painter.setPen(QColor('#8e96b0'));fm=QFontMetrics(painter.font())
            painter.drawText(line.translated(0,20),Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft,fm.elidedText(note,Qt.TextElideMode.ElideRight,int(line.width())))
        painter.restore()

    def editorEvent(self,event,model,option,index):
        if self.kind=='set' and event.type()==QEvent.Type.MouseButtonRelease and event.button()==Qt.MouseButton.LeftButton:
            card=QRectF(option.rect).adjusted(6,6,-10,-10);top=card.y()+7+(card.width()-14)*2/3+7
            if index.data(Qt.ItemDataRole.UserRole):
                if QRectF(card.right()-27,top,20,22).contains(event.position()):
                    self.owner.set_grid.setCurrentIndex(index);self.owner.remove_set_item();return True
                if QRectF(card.right()-83,top,55,22).contains(event.position()):
                    self.owner.set_grid.setCurrentIndex(index);self.owner.toggle_set_lock();return True
        return super().editorEvent(event,model,option,index)
