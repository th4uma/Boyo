"""Single-layer thumbnail browsing; a separate viewer owns image navigation."""
from pathlib import Path
from PySide6.QtCore import QSize,Qt,QRect
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QListWidgetItem,QStyle

from .browser_widgets import visible_rows
from .grouping import group_members,group_references
from .photo_widgets import PhotoCard


class ScrollUI:
    def refresh_gallery(self):
        if not self.shell_ready:return super().refresh_gallery()
        self.thumbnail_loader.cancel(self.gallery_thumb_owner);self.gallery.blockSignals(True);self.gallery.clear();self.gallery_dirty=False
        assets=self.gallery_assets();self.page=0
        self.used_in_sets={r['logical_key']:r['name'] for r in self.store.rows('SELECT i.logical_key,s.name FROM set_items i JOIN photo_sets s ON s.id=i.set_id')}
        for asset in assets:
            item=QListWidgetItem(self.icon(asset),self.gallery_label(asset));item.setData(Qt.ItemDataRole.UserRole,asset['id']);item.setToolTip(asset['path']);self.gallery.addItem(item)
        self.gallery.blockSignals(False)
        self.page_label.setText(f'{len(assets)} 张 · 滚动浏览 · 双击大图，左右键切图')
        self.update_gallery_selection();self.load_visible_thumbnails()

    def refresh_quality_grid(self):
        if not self.shell_ready:return super().refresh_quality_grid()
        self.thumbnail_loader.cancel(self.quality_thumb_owner);self.quality_grid.blockSignals(True);self.quality_grid.clear();self.quality_page=0
        rows=self.filtered_quality()
        for row in rows:
            asset=self.asset_by_id[row['id']]
            item=QListWidgetItem(self.icon(asset),self.quality_label(row));item.setData(Qt.ItemDataRole.UserRole,asset['id'])
            item.setToolTip(asset['path']+'\n'+'\n'.join(i['detail'] for i in row['issues']));self.quality_grid.addItem(item)
        self.quality_grid.blockSignals(False)
        self.quality_page_label.setText(f'{len(rows)} 张候选 · 滚动浏览 · 双击大图，左右键切图')
        self.load_visible_thumbnails()

    def render_review_page(self):
        if not self.shell_ready:return super().render_review_page()
        for card in self.review_cards.values():
            if card.thumb_owner is not None:self.thumbnail_loader.cancel(card.thumb_owner)
        self.compare.blockSignals(True);self.compare.clear();self.review_cards={};self.review_items={};self.review_hints={};self.review_page=0
        group=self.current_group()
        if not group:self.review_page_label.clear();self.compare.blockSignals(False);return
        suggestions={m['id']:m for m in group['candidates']};refs=set(group_references(group))
        for aid in group_members(group):
            asset=self.asset_by_id.get(aid)
            if not asset or not asset['present']:continue
            member=suggestions.get(aid);detail='建议保留 / 比较参照' if aid in refs else '同组构图关系，请自行比较'
            if member and aid not in refs:
                reference=self.asset_by_id.get(member.get('reference',group['keep']))
                detail='建议精简 · 对照 '+(Path(reference['path']).name if reference else '参照已移出')
                evidence=member.get('evidence',{})
                if evidence.get('method')=='sift':detail+=f" · 重合 {evidence['overlap']:.0%}"
                if member.get('quality_note'):detail+=' · '+member['quality_note']
            self.review_hints[aid]=detail
            item=QListWidgetItem();item.setSizeHint(QSize(260,355));item.setData(Qt.ItemDataRole.UserRole,aid);item.setToolTip(asset['path']);self.compare.addItem(item)
            self.review_items[aid]=item
        self.compare.blockSignals(False)
        # Warm only a bounded first screen; the remaining cards are virtualized.
        self.relayout_thumbnail_grids();self.sync_review_cards(range(min(12,self.compare.count())))
        self.update_review_summary();self.review_scroll_summary();self.load_visible_thumbnails()

    def sync_review_cards(self,rows):
        keep={self.compare.item(row).data(Qt.ItemDataRole.UserRole) for row in rows}
        for aid,card in list(self.review_cards.items()):
            if aid not in keep:
                if card.thumb_owner is not None:self.thumbnail_loader.cancel(card.thumb_owner)
                card.hide();self.compare.removeItemWidget(self.review_items[aid]);card.deleteLater();del self.review_cards[aid]
        for aid in keep:
            if aid in self.review_cards:continue
            asset=self.asset_by_id[aid];item=self.review_items[aid]
            card=PhotoCard(asset,self.runtime,self.root,self.review_hints[aid])
            card.previewRequested.connect(self.open_review_preview);card.decisionRequested.connect(self.review_photo_decide)
            card.selectionRequested.connect(self.select_review_asset)
            card.set_card_width(max(240,self.compare.gridSize().width()-20))
            self.review_cards[aid]=card;self.compare.setItemWidget(item,self.review_card_widget(card))
        self.highlight_review_selection()

    def review_card_widget(self,card):return card

    def highlight_review_selection(self):
        if not self.compare.count():return
        for aid,card in self.review_cards.items():
            selected=self.review_items[aid].isSelected()
            if card.property('selected')!=selected:
                card.setProperty('selected',selected);card.style().unpolish(card);card.style().polish(card)

    def select_review_asset(self,aid):
        item=self.review_items.get(aid)
        if item:self.compare.setCurrentItem(item)

    def review_scroll_summary(self):
        group=self.current_group()
        if not group:return
        assets=[self.asset_by_id[i] for i in group_members(group) if i in self.asset_by_id]
        self.review_page_label.setText(f'{len(assets)} 张 · 保护 {sum(a["protected"] for a in assets)} 张 · 待删 {sum(a["decision"]=="delete" for a in assets)} 张 · 滚动浏览，双击大图')

    def update_review_sequence(self):
        result=super().update_review_sequence()
        if self.shell_ready:self.review_scroll_summary()
        return result

    def decisions_changed(self,decisions,source='manual'):
        super().decisions_changed(decisions,source)
        if self.shell_ready:self.review_scroll_summary()

    def load_visible_thumbnails(self):
        if not self.shell_ready:return super().load_visible_thumbnails()
        self.relayout_thumbnail_grids()
        for grid,owner in ((self.gallery,self.gallery_thumb_owner),(self.quality_grid,self.quality_thumb_owner),
                           (self.set_grid,self.set_thumb_owner),(self.set_candidates,self.set_candidate_thumb_owner)):
            self.thumbnail_loader.cancel(owner)
            if not grid.isVisible():continue
            for row in visible_rows(grid):
                item=grid.item(row);asset=self.asset_by_id.get(item.data(Qt.ItemDataRole.UserRole))
                if asset:self.thumbnail_loader.request(self.runtime,self.root,asset,owner,lambda pix,item=item:item.setIcon(QIcon(pix)))
        if self.compare.isVisible() and hasattr(self,'review_items'):self.sync_review_cards(visible_rows(self.compare))

    def relayout_thumbnail_grids(self):
        label_width=max(1,self.group_list.viewport().width()-18)
        label_key=(label_width,self.group_list.count())
        if getattr(self,'group_label_geometry',None)!=label_key:
            self.group_label_geometry=label_key;metrics=self.group_list.fontMetrics()
            for row in range(self.group_list.count()):
                item=self.group_list.item(row)
                height=metrics.boundingRect(QRect(0,0,label_width,10000),Qt.TextFlag.TextWordWrap,item.text()).height()+20
                item.setSizeHint(QSize(label_width,height))
        grids=((self.gallery,250,False),(self.quality_grid,270,False),(self.set_grid,260,False),(self.compare,280,True))
        for grid,minimum,review in grids:
            if not grid.isVisible() and not (review and grid.count()):continue
            reserve=max(10,grid.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent))
            available=max(1,grid.width()-2*grid.frameWidth()-reserve-2)
            columns=max(1,available//minimum);width=max(1,available//columns)
            picture=max(145,min(230,round((width-40)*.64)))
            height=picture+5*self.fontMetrics().lineSpacing()+126 if review else (250 if grid is self.quality_grid else 230)
            size=QSize(width,height)
            if grid.gridSize()==size:continue
            grid.setGridSize(size)
            if review:
                for row in range(grid.count()):grid.item(row).setSizeHint(QSize(max(1,width-20),height-20))
                for card in self.review_cards.values():card.set_card_width(max(240,width-20))
            else:grid.setIconSize(QSize(max(80,min(320,width-30)),153))

    def open_preview(self,aid,ids,context='manual',editable_ids=None):
        grid={0:self.gallery,1:self.compare,5:self.quality_grid}.get(self.tabs.currentIndex())
        target=(self.store.path,self.album)
        current=super().open_preview(aid,ids,context,editable_ids)
        if grid and isinstance(current,int) and target==(self.store.path,self.album):
            for row in range(grid.count()):
                item=grid.item(row)
                if item.data(Qt.ItemDataRole.UserRole)==current:
                    grid.setCurrentItem(item);grid.scrollToItem(item);self.load_visible_thumbnails();break
        return current

    def select_current_page(self):
        grid={0:self.gallery,1:self.compare,5:self.quality_grid}.get(self.tabs.currentIndex())
        if grid:
            grid.clearSelection()
            for row in visible_rows(grid,margin=False):grid.item(row).setSelected(True)

    def expand_current(self):
        grid={0:self.gallery,1:self.compare,5:self.quality_grid}.get(self.tabs.currentIndex())
        if grid and grid.currentItem():
            item=grid.currentItem()
            if grid is self.gallery:self.expand_gallery(item)
            elif grid is self.compare:self.open_review_preview(item.data(Qt.ItemDataRole.UserRole))
            else:self.expand_quality(item)
