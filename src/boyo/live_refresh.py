"""Apply structural changes once and retain the user's place by photo identity."""
from pathlib import Path
from PySide6.QtCore import Qt,QTimer,QItemSelectionModel
from PySide6.QtWidgets import QAbstractItemView
from .browser_widgets import visible_rows
from .grouping import group_members,decision_contexts
from .photo_widgets import PhotoPreviewDialog


class LiveRefreshUI:
    def capture_view_position(self):
        if not self.shell_ready or not self.assets or self.assets[0]['album']!=self.album:return None
        position={'context':self.current_context(),'group':self.current_group(),'grids':{}}
        for name in ('gallery','compare','quality_grid','set_grid'):
            grid=getattr(self,name);current=grid.currentItem();rows=visible_rows(grid,False)
            anchor=grid.item(rows[0]) if rows else current
            position['grids'][name]={'current':current.data(Qt.ItemDataRole.UserRole) if current else None,
                'row':grid.currentRow(),'selected':{item.data(Qt.ItemDataRole.UserRole) for item in grid.selectedItems()},
                'anchor':anchor.data(Qt.ItemDataRole.UserRole) if anchor else None,
                'offset':grid.visualItemRect(anchor).top() if anchor else 0,'scroll':grid.verticalScrollBar().value()}
        return position

    def refresh(self):
        position=self.capture_view_position() if self.shell_ready else None
        self._keep_pending_queue=bool(position and self.pending_queue_ids is not None)
        try:result=super().refresh()
        finally:self._keep_pending_queue=False
        if not self.shell_ready:return result
        self.deferred_results=False;self.new_results_button.hide()
        if position and self.context_matches(position['context']):
            old=position['group'];candidate=position['grids']['compare']['current']
            if old and self.group_data:
                members=set(group_members(old))
                target=max(range(len(self.group_data)),key=lambda row:(candidate in group_members(self.group_data[row]),
                    len(members.intersection(group_members(self.group_data[row]))),self.group_data[row]['id']==old['id']))
                if self.group_list.currentRow()!=target:self.group_list.setCurrentRow(target)
            self.restore_view_position(position)
            self._view_refresh_serial=getattr(self,'_view_refresh_serial',0)+1;serial=self._view_refresh_serial
            QTimer.singleShot(0,lambda:self.restore_view_position(position) if serial==self._view_refresh_serial and self.context_matches(position['context']) else None)
        self.synchronize_open_viewers()
        if hasattr(self,'update_native_header'):self.update_native_header()
        return result

    def restore_view_position(self,position):
        for name,state in position['grids'].items():
            grid=getattr(self,name)
            by_id={grid.item(row).data(Qt.ItemDataRole.UserRole):grid.item(row) for row in range(grid.count())}
            current=by_id.get(state['current'])
            if current is None and state['row']>=0 and grid.count():current=grid.item(min(state['row'],grid.count()-1))
            grid.blockSignals(True);grid.clearSelection()
            if current:grid.setCurrentItem(current,QItemSelectionModel.SelectionFlag.NoUpdate)
            for aid in state['selected']:
                if aid in by_id:by_id[aid].setSelected(True)
            grid.blockSignals(False)
            anchor=by_id.get(state['anchor'])
            if anchor:
                grid.scrollToItem(anchor,QAbstractItemView.ScrollHint.PositionAtTop)
                grid.verticalScrollBar().setValue(grid.verticalScrollBar().value()-state['offset'])
            else:grid.verticalScrollBar().setValue(state['scroll'])
        self.update_gallery_selection();self.update_review_sequence();self.update_quality_sequence()

    def synchronize_open_viewers(self):
        for dialog in self.findChildren(PhotoPreviewDialog):
            if not dialog.isVisible() or dialog.store.path!=self.store.path or Path(dialog.root)!=self.root or getattr(dialog,'album',None)!=self.album:continue
            ids=[aid for aid in dialog.asset_ids if aid in self.asset_by_id]
            scope=getattr(dialog,'review_scope',None)
            if scope=='similar':
                group=next((g for g in self.group_data if dialog.current_id in group_members(g)),None)
                if group:ids=[aid for aid in group_members(group) if aid in self.asset_by_id];dialog.context=decision_contexts(group,ids)
            elif scope=='gallery' and getattr(dialog,'review_filter',None)==self.filter.currentText():ids=[a['id'] for a in self.gallery_assets()]
            elif scope=='quality':ids=[row['id'] for row in self.filtered_quality()]
            dialog.synchronize(ids)
