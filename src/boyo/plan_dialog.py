from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .operations import Operations
from .photo_widgets import PhotoCard, PhotoPreviewDialog, asset_thumbnail, file_operation_locked
from .review_viewer import PhotoViewer
from .thumbnail_loader import thumbnail_loader
from .browser_widgets import ScrollUpdates,visible_rows,QuietItemDelegate


class PlanDialog(QDialog):
    """Visual review only edits decisions; execution remains a separate explicit step."""

    def __init__(self, plan, parent, *, store=None, runtime=None, root=None):
        super().__init__(parent)
        self.plan = self.original_plan = plan
        self.store, self.runtime, self.root = store or parent.store, runtime or parent.runtime, Path(root or parent.root)
        self.excluded_ids = set(); self.skipped_ids = set()
        self.thumb_loader=thumbnail_loader();self.thumb_owner=self.thumb_loader.new_owner()
        loader,owner=self.thumb_loader,self.thumb_owner
        self.destroyed.connect(lambda:loader.cancel(owner))
        self.page = 0
        self.contexts = {}; self.references = {}; self.candidate_ids = []
        self.album_assets = {a['id']:a for a in self.store.rows('SELECT * FROM assets WHERE album=?',(plan['album'],))} if plan.get('album') else {}
        self.action_assets = {a["asset_id"]: self.album_assets.get(a["asset_id"])
                              for a in plan["actions"] if "asset_id" in a}
        if plan.get("kind") == "delete":
            self.candidate_ids = [aid for aid, a in self.action_assets.items() if a and a["role"] == "ordinary_jpeg"]
            self.excluded_ids={aid for aid in self.candidate_ids if self.album_assets[aid]['decision']!='delete' or self.album_assets[aid]['protected'] or not self.album_assets[aid]['present']}
            for g in self.store.groups(plan["album"]):
                if g.get('kind')=='quality':
                    for m in g['candidates']: self.contexts[m['id']]=g['id']
                    continue
                from .grouping import group_members,member_context
                members = group_members(g)
                assets = {aid: self.album_assets.get(aid) for aid in members}
                references={m['id']:m.get('reference',g['keep']) for m in g['candidates']}
                for aid in members:
                    self.contexts[aid] = member_context(g,aid)
                    allowed=set(g['matches'].get(str(aid),[])) if 'matches' in g else set(members)-{aid}
                    alternatives = [a for other, a in assets.items() if other in allowed and a and a["present"] and a["decision"] != "delete"]
                    alternatives.sort(key=lambda a: (a['id']!=references.get(aid,g['keep']),not a["protected"], a["decision"] != "keep"))
                    if alternatives:
                        self.references[aid] = alternatives[0]["id"]
        self.setWindowTitle("待删看图复核 · 尚未移动文件" if self.candidate_ids else "文件操作清单 · 尚未执行")
        self.resize(1200, 790)
        layout = QVBoxLayout(self)
        self.summary = QLabel(); self.summary.setWordWrap(True); layout.addWidget(self.summary)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        if self.candidate_ids:
            self.build_visual()
        elif plan.get('checks') or plan.get('actions'):
            self.build_pairing()
        self.file_table = QTableWidget(0, 4)
        self.file_table.setHorizontalHeaderLabels(["操作", "源文件", "目标文件", "依据"])
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setStretchLastSection(True)
        self.tabs.addTab(self.file_table, "文件明细")
        self.tabs.currentChanged.connect(lambda _: self.update_file_table() if self.tabs.currentWidget() is self.file_table else None)
        if plan["warnings"]:
            warning = QTextEdit(); warning.setReadOnly(True); warning.setMaximumHeight(80)
            warning.setPlainText("未自动处理：\n" + "\n".join(plan["warnings"])); layout.addWidget(warning)
        self.agree = QCheckBox("我已核对这些照片、关联 RAW 和备份位置，执行当前清单")
        layout.addWidget(self.agree)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.execute_button = buttons.addButton("执行清单", QDialogButtonBox.ButtonRole.AcceptRole)
        self.execute_button.setEnabled(False)
        self.agree.toggled.connect(lambda value: self.execute_button.setEnabled(value and bool(self.remaining_actions())))
        self.execute_button.clicked.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.update_summary()
        if self.candidate_ids:
            self.fill_page()
            self.scroll_updates=ScrollUpdates(self,[self.candidates],self.load_plan_thumbnails)
        self.update_file_table()

    def build_pairing(self):
        pane=QWidget();layout=QHBoxLayout(pane)
        self.check_table=QTableWidget(0,3);self.check_table.setHorizontalHeaderLabels(['照片 / RAW','检查结果','目标 / 原因'])
        self.check_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.check_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.check_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.check_table.horizontalHeader().setStretchLastSection(True)
        rows=self.plan.get('checks') or [{'asset_id':a.get('asset_id'),'path':a['src'],'status':'可移动' if a['action']=='move' else '可复制','detail':a['dst']} for a in self.plan['actions']]
        self.check_rows=rows;self.check_table.setRowCount(len(rows))
        for index,row in enumerate(rows):
            for col,key in enumerate(('path','status','detail')):self.check_table.setItem(index,col,QTableWidgetItem(row.get(key,'')))
        layout.addWidget(self.check_table,3);self.pairing_view=PhotoViewer();layout.addWidget(self.pairing_view,2)
        self.check_table.currentCellChanged.connect(self.preview_pairing);self.tabs.addTab(pane,'配对检查')
        if rows:self.check_table.setCurrentCell(0,0)

    def preview_pairing(self,row,*args):
        if not 0<=row<len(self.check_rows):return
        asset=self.album_assets.get(self.check_rows[row].get('asset_id'))
        if asset and asset['role'] in {'ordinary_raw','selected_raw','sidecar'}:
            asset=next((a for a in self.album_assets.values() if a['logical_key']==asset['logical_key'] and a['role'] in {'ordinary_jpeg','selected_jpeg'}),None)
        self.pairing_view.set_asset(asset,self.runtime,self.root,'对应 JPG 预览' if asset else '没有可预览的对应 JPG；请查看文件路径与配对说明')

    def build_visual(self):
        pane = QWidget(); layout = QVBoxLayout(pane)
        note = QLabel("左侧选择待删照片，右侧对照相似组参照。点击保留或撤销决定，会同时移出它关联的 RAW/XMP；此处只改清单。")
        note.setWordWrap(True); layout.addWidget(note)
        split = QSplitter()
        left = QWidget(); left_layout = QVBoxLayout(left)
        self.candidates = QListWidget(); self.candidates.setIconSize(QSize(130, 90))
        self.candidates.setWordWrap(True)
        self.candidates.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.candidates.currentRowChanged.connect(self.select_candidate); left_layout.addWidget(self.candidates, 1)
        bar = QHBoxLayout()
        self.previous_page = QPushButton(self); self.previous_page.hide();self.previous_page.clicked.connect(lambda: self.change_page(-1))
        self.page_label = QLabel(); bar.addWidget(self.page_label)
        self.next_page = QPushButton(self);self.next_page.hide();self.next_page.clicked.connect(lambda: self.change_page(1))
        left_layout.addLayout(bar); split.addWidget(left)
        right = QWidget(); right_layout = QVBoxLayout(right)
        self.cards = QHBoxLayout(); right_layout.addLayout(self.cards)
        self.bundle = QLabel(); self.bundle.setWordWrap(True); right_layout.addWidget(self.bundle)
        right_layout.addStretch()
        bar = QHBoxLayout()
        self.previous_photo = QPushButton(self);self.previous_photo.hide();self.previous_photo.clicked.connect(lambda: self.change_candidate(-1))
        self.next_photo = QPushButton(self);self.next_photo.hide();self.next_photo.clicked.connect(lambda: self.change_candidate(1))
        self.skip_button=QPushButton('本次暂不执行（不改待删状态）');self.skip_button.clicked.connect(self.toggle_skip);bar.addWidget(self.skip_button)
        right_layout.addLayout(bar); split.addWidget(right); split.setSizes([280, 850])
        layout.addWidget(split, 1); self.tabs.addTab(pane, "看图复核")

    def remaining_actions(self):
        keys = {self.action_assets[aid]["logical_key"] for aid in self.excluded_ids | self.skipped_ids}
        return [a for a in self.original_plan["actions"]
                if a.get("asset_id") not in self.action_assets or self.action_assets[a["asset_id"]]["logical_key"] not in keys]

    def update_summary(self):
        actions = self.remaining_actions()
        copies = sum(a["action"] == "copy" for a in actions)
        jpgs = sum(self.action_assets.get(a.get("asset_id"), {}).get("role") == "ordinary_jpeg" for a in actions)
        prefix = f"{self.plan.get('album','')} · 待删 JPG {jpgs} 张 · 已救回/撤销 {len(self.excluded_ids)} 张 · 仅跳过本次 {len(self.skipped_ids-self.excluded_ids)} 张 · " if self.candidate_ids else f"{self.plan.get('album','')} · "
        self.summary.setText(prefix + f"共 {len(actions)} 个文件：移动 {len(actions)-copies} 个，复制 {copies} 个。尚未执行。")
        if self.tabs.currentWidget() is self.file_table:
            self.update_file_table()

    def update_file_table(self):
        actions = self.remaining_actions()
        self.file_table.setUpdatesEnabled(False)
        self.file_table.setRowCount(len(actions))
        for row, action in enumerate(actions):
            values = ["移动" if action["action"] == "move" else "复制", action["src"], action["dst"], action["reason"]]
            for col, value in enumerate(values):
                self.file_table.setItem(row, col, QTableWidgetItem(value))
        self.file_table.setUpdatesEnabled(True)

    def fill_page(self, selected=None):
        self.thumb_loader.cancel(self.thumb_owner);self.candidates.blockSignals(True); self.candidates.clear()
        for aid in self.candidate_ids:
            a = self.album_assets[aid]
            status = "已移出" if aid in self.excluded_ids else '仅跳过本次' if aid in self.skipped_ids else "待删"
            item = QListWidgetItem(f"{status}\n{Path(a['path']).name}")
            item.setToolTip(a["path"])
            item.setData(Qt.ItemDataRole.UserRole, aid); self.candidates.addItem(item)
        self.candidates.blockSignals(False)
        self.page_label.setText(f'{len(self.candidate_ids)} 张 · 滚动浏览，双击查看大图')
        row = self.candidate_ids.index(selected) if selected in self.candidate_ids else 0
        self.candidates.setCurrentRow(max(0, min(row, self.candidates.count()-1)))
        self.load_plan_thumbnails()

    def load_plan_thumbnails(self):
        self.thumb_loader.cancel(self.thumb_owner)
        if not self.candidates.isVisible():return
        for row in visible_rows(self.candidates):
            item=self.candidates.item(row);asset=self.album_assets[item.data(Qt.ItemDataRole.UserRole)]
            self.thumb_loader.request(self.runtime,self.root,asset,self.thumb_owner,lambda pix,item=item:item.setIcon(QIcon(pix)))

    def change_page(self, delta):
        page = self.page + delta
        if 0 <= page < (len(self.candidate_ids)+23)//24:
            self.page = page; self.fill_page()

    def change_candidate(self, delta):
        item = self.candidates.currentItem()
        if not item: return
        index = self.candidate_ids.index(item.data(Qt.ItemDataRole.UserRole)) + delta
        if 0 <= index < len(self.candidate_ids):
            self.candidates.setCurrentRow(index);self.candidates.scrollToItem(self.candidates.item(index))

    def select_candidate(self, row):
        while self.cards.count():
            item = self.cards.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        item = self.candidates.currentItem()
        if not item: return
        aid = item.data(Qt.ItemDataRole.UserRole); asset = self.album_assets[aid]
        self.skip_button.setText('重新加入本次执行' if aid in self.skipped_ids else '本次暂不执行（不改待删状态）')
        self.candidate_card = PhotoCard(asset, self.runtime, self.root, "本次待删候选" if aid not in self.excluded_ids else "已移出本次清单")
        self.candidate_card.previewRequested.connect(self.open_preview)
        self.candidate_card.decisionRequested.connect(self.decide); self.cards.addWidget(self.candidate_card, 1)
        ref = self.album_assets.get(self.references[aid]) if aid in self.references else None
        if ref and ref["present"] and ref["decision"] != "delete":
            self.reference_card = PhotoCard(ref, self.runtime, self.root, "同组比较参照 · 以当前状态为准", editable=False)
            self.reference_card.previewRequested.connect(self.open_preview); self.cards.addWidget(self.reference_card, 1)
        else:
            self.reference_card = None
            note = QLabel("没有可用的同组保留参照。\n可点击候选看大图，再自行决定。")
            note.setWordWrap(True); self.cards.addWidget(note, 1)
        related = [a for a in self.original_plan["actions"] if self.action_assets.get(a.get("asset_id"), {}).get("logical_key") == asset["logical_key"]]
        self.bundle.setText(("已从本次清单移出：" if aid in self.excluded_ids else "本张涉及的文件：") + "\n" + "\n".join(f"{a['src']} → {a['dst']}" for a in related))
        index = self.candidate_ids.index(aid)
        self.previous_photo.setEnabled(index > 0); self.next_photo.setEnabled(index+1 < len(self.candidate_ids))

    def decide(self, aid, decision):
        try:
            if file_operation_locked(self,self.store,self.root,self.album_assets[aid]):raise ValueError('此照片正在执行文件操作，请完成后再修改')
            self.store.decide([aid], decision, self.contexts.get(aid, "manual"))
        except ValueError as exc:
            QMessageBox.warning(self, "未修改状态", str(exc)); return
        self.decision_changed(aid, decision)

    def decision_changed(self, aid, decision):
        parent=self.parent()
        if hasattr(parent,'auto_learning') and parent.store.path==self.store.path: parent.auto_learning.changed()
        self.album_assets[aid]["decision"] = decision
        # A logical photo's RAW/XMP must follow any removal of its JPG from this plan.
        key = self.action_assets[aid]["logical_key"]
        # Another explicitly kept version sharing this key cannot be re-added indirectly.
        same_key = [i for i in self.candidate_ids if self.action_assets[i]["logical_key"] == key]
        all_deleted = all(self.album_assets[i]["decision"] == "delete" for i in same_key)
        for other in same_key:
            if all_deleted: self.excluded_ids.discard(other)
            else: self.excluded_ids.add(other)
        self.agree.setChecked(False); self.execute_button.setEnabled(False)
        self.update_summary()
        for row in range(self.candidates.count()):
            item = self.candidates.item(row); other = item.data(Qt.ItemDataRole.UserRole)
            if other in same_key:
                status = "已移出" if other in self.excluded_ids else "待删"
                item.setText(f"{status}\n{Path(self.album_assets[other]['path']).name}")
        selected = self.candidates.currentItem().data(Qt.ItemDataRole.UserRole)
        if selected == aid:
            self.candidate_card.update_state(self.album_assets[aid])
        if selected in same_key:
            lines = self.bundle.text().split("\n", 1)
            self.bundle.setText(("已从本次清单移出：" if selected in self.excluded_ids else "本张涉及的文件：") + "\n" + (lines[1] if len(lines) > 1 else ""))

    def open_preview(self, aid):
        ids = list(self.candidate_ids)
        if aid not in ids: ids.append(aid)
        dialog = PhotoPreviewDialog(self.store, self.runtime, self.root, ids, aid,
                                    context=self.contexts, editable_ids=self.candidate_ids, parent=self)
        dialog.decisionChanged.connect(self.decision_changed); dialog.exec()
        dialog.deleteLater()

    def toggle_skip(self):
        item=self.candidates.currentItem()
        if not item:return
        aid=item.data(Qt.ItemDataRole.UserRole);key=self.album_assets[aid]['logical_key']
        related={i for i in self.candidate_ids if self.album_assets[i]['logical_key']==key}
        if aid in self.skipped_ids:self.skipped_ids.difference_update(related)
        else:self.skipped_ids.update(related)
        self.agree.setChecked(False);self.update_summary();self.fill_page(aid)

    def done(self,result):
        if hasattr(self,'scroll_updates'):self.scroll_updates.timer.stop()
        self.thumb_loader.cancel(self.thumb_owner)
        super().done(result)

    def hideEvent(self,event):
        if hasattr(self,'scroll_updates'):self.scroll_updates.timer.stop()
        self.thumb_loader.cancel(self.thumb_owner)
        super().hideEvent(event)

    def accept(self):
        if not self.agree.isChecked() or not self.remaining_actions():
            return
        if self.excluded_ids or self.skipped_ids:
            try:
                self.plan = Operations(self.store, self.root).exclude_from_deletion(self.original_plan["id"], self.excluded_ids | self.skipped_ids)
            except ValueError as exc:
                QMessageBox.warning(self, "清单已变化", str(exc)); return
        super().accept()
