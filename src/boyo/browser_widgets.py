"""Scroll metadata freely and decode only the visible thumbnail range."""
from PySide6.QtCore import QPoint,QTimer,QEvent,QObject
from PySide6.QtWidgets import QAbstractItemView,QListView,QStyledItemDelegate,QStyle,QStyleOptionViewItem


class QuietItemDelegate(QStyledItemDelegate):
    def paint(self,painter,option,index):
        clean=QStyleOptionViewItem(option)
        clean.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter,clean,index)


def visible_rows(grid,margin=True):
    if not grid.count():return []
    size=grid.gridSize();width=grid.viewport().width();height=grid.viewport().height()
    step_x=max(12,size.width()//2) if size.width()>0 else max(12,width//2)
    step_y=max(12,size.height()//2) if size.height()>0 else max(12,grid.sizeHintForRow(0)//2)
    found=set()
    for y in range(1,max(2,height),step_y):
        for x in range(1,max(2,width),step_x):
            index=grid.indexAt(QPoint(x,y))
            if index.isValid():found.add(index.row())
    if not found:
        # Before thumbnails arrive the painted item can be much shorter than
        # its grid cell. Sampling may hit only padding, especially at the end.
        # Find the visible metadata range without walking the entire album.
        horizontal=not grid.isWrapping() and grid.flow()==QListView.Flow.LeftToRight
        def edge(row,last):
            rect=grid.visualItemRect(grid.item(row))
            return (rect.right() if last else rect.left()) if horizontal else (rect.bottom() if last else rect.top())
        lo,hi=0,grid.count()
        while lo<hi:
            mid=(lo+hi)//2
            if edge(mid,True)<0:lo=mid+1
            else:hi=mid
        start=lo;hi=grid.count();limit=width if horizontal else height
        while lo<hi:
            mid=(lo+hi)//2
            if edge(mid,False)<limit:lo=mid+1
            else:hi=mid
        if start==lo:return []
        found={start,lo-1}
    columns=max(1,width//max(1,size.width())) if size.width()>0 else 1
    extra=columns if margin else 0
    return list(range(max(0,min(found)-extra),min(grid.count(),max(found)+extra+1)))


class ScrollUpdates(QObject):
    def __init__(self,owner,grids,callback):
        super().__init__(owner)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(45)
        self.timer.timeout.connect(callback)
        for grid in grids:
            grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
            grid.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
            grid.setItemDelegate(QuietItemDelegate(grid))
            grid.viewport().installEventFilter(self)
            grid.verticalScrollBar().valueChanged.connect(self.schedule)
            grid.horizontalScrollBar().valueChanged.connect(self.schedule)

    def schedule(self,*args):self.timer.start()

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Type.Resize,QEvent.Type.Show):self.schedule()
        return False
