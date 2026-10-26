from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .tasks import Control, ProgressReporter
from .collections import Collections
from .fs import EXCLUDED
from .operations import Operations
from .photo_widgets import PhotoCard, PhotoPreviewDialog, THUMBNAILS
from .imaging import thumbnail_path
from .plan_dialog import PlanDialog
from .scanner import Scanner
from .store import Store
from .thumbnail_loader import thumbnail_loader
from .grouping import group_members, group_references, member_context, decision_contexts
from .auto_learning import AutoLearning
from .branding import APP_NAME,APP_TITLE,app_icon


STYLE = """
QMainWindow,QDialog {background:#10251e;color:#eaf2ed;}
QWidget {font-family:'Microsoft YaHei';font-size:13px;color:#eaf2ed;}
QLabel#title {font-size:28px;font-weight:700;color:#f0f7ed;}
QLabel#muted {color:#9fb8aa;}
QPushButton {background:#234538;border:1px solid #3b6653;padding:8px 13px;border-radius:6px;}
QPushButton:hover {background:#315e49;}
QPushButton:disabled {color:#71877b;background:#1b3027;}
QComboBox,QSpinBox {background:#1b352b;padding:6px;border:1px solid #3b6653;border-radius:5px;}
QListWidget,QTableWidget,QTextEdit {background:#142e24;border:1px solid #31503f;border-radius:6px;}
QListWidget::item {padding:7px;border-bottom:1px solid #244333;}
QListWidget::item:selected {background:#34684c;}
QHeaderView::section {background:#234538;color:#dcebe0;padding:6px;border:0;}
QTabWidget::pane {border:0;}
QTabBar::tab {background:#1b352b;padding:11px 18px;margin-right:4px;border-radius:5px;}
QTabBar::tab:selected {background:#367350;}
QProgressBar {border:1px solid #31503f;border-radius:5px;text-align:center;background:#142e24;}
QProgressBar::chunk {background:#4b9867;}
QCheckBox {spacing:7px;}
QFrame#photoCard {background:#1b352b;border:1px solid #3b6653;border-radius:7px;}
"""

ROLES = {"ordinary_jpeg": "普通 JPG", "ordinary_raw": "RAW", "selected_jpeg": "精选 JPG",
         "selected_raw": "精选 RAW", "finished": "成片", "extra_image": "附加图片", "sidecar": "附属文件", "other": "其他"}
DECISIONS = {"pending": "未决定", "keep": "保留", "delete": "待删"}


def button(text, fn, layout):
    b = QPushButton(text); b.clicked.connect(fn); layout.addWidget(b); return b


def table(columns):
    t = QTableWidget(0, len(columns)); t.setHorizontalHeaderLabels(columns)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


class Worker(QObject):
    progress = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, fn, control):
        super().__init__(); self.fn, self.control = fn, control

    def run(self):
        try:
            self.succeeded.emit(self.fn(ProgressReporter(self.progress.emit), self.control))
        except Exception as exc:
            logging.exception("后台任务失败")
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class BaseWindow(QMainWindow):
    def __init__(self, store, runtime, root, settings, defer_loading=False):
        super().__init__()
        self.store, self.runtime, self.root, self.settings = store, runtime, root, settings
        self.album = None; self.assets = []; self.page = 0; self.group_data = []; self.review_page=0
        self.asset_by_id = {}; self.group_rows = {}
        self.gallery_dirty = False; self.preview_open = False
        self.quality_page = 0; self.quality_results = []; self.quality_entries = {}
        self.thumbnail_loader = thumbnail_loader()
        self.gallery_thumb_owner = self.thumbnail_loader.new_owner(); self.set_thumb_owner = self.thumbnail_loader.new_owner()
        self.quality_thumb_owner = self.thumbnail_loader.new_owner()
        loader, owners = self.thumbnail_loader, (self.gallery_thumb_owner, self.set_thumb_owner, self.quality_thumb_owner)
        self.destroyed.connect(lambda: [loader.cancel(owner) for owner in owners])
        self.thread = None; self.worker = None; self.control = None; self.task_done = None
        self.collections = Collections(store, root)
        self.setWindowTitle(APP_TITLE); self.setWindowIcon(app_icon())
        self.resize(1400, 900); self.setMinimumSize(1000, 680); self.setStyleSheet(STYLE)
        body = QWidget(); self.setCentralWidget(body); layout = QVBoxLayout(body)
        header = QHBoxLayout(); title = QLabel(APP_NAME); title.setObjectName("title"); header.addWidget(title)
        subtitle = QLabel("精选由你决定，让重复照片更容易整理"); subtitle.setObjectName("muted"); header.addWidget(subtitle); header.addStretch()
        self.workspace_button = button("切换工作区", self.choose_workspace, header)
        layout.addLayout(header)
        tools = QHBoxLayout()
        self.album_picker = QComboBox(); self.album_picker.setMinimumWidth(280); tools.addWidget(self.album_picker)
        self.scan_button = button("扫描并分析", self.scan, tools)
        self.scan_button.setToolTip("扫描完整成功后自动分析；失败或取消时停止。只生成建议，不移动照片。")
        self.analyze_button = button("重新分析", self.start_analysis, tools)
        self.analyze_button.setToolTip("调整强度后重新分析，未变化的照片复用缓存。")
        self.quality_button = button('筛查废片', self.start_quality, tools)
        self.quality_button.setToolTip('独立检查模糊、疑似虚焦和严重曝光异常，不需要先做重复图对比。')
        self.strength = QComboBox(); self.strength.addItems(["保守", "适中", "较强"]); self.strength.setCurrentText(settings["strength"])
        self.strength.setToolTip('保守：非常接近的构图；适中：容许轻微取景变化；较强：合并重合较多的同场景构图。均只生成复核建议。')
        self.strength.currentTextChanged.connect(lambda value: self.save_setting("strength", value)); tools.addWidget(self.strength)
        tools.addStretch()
        self.pause_button = button("暂停", self.pause, tools); self.cancel_button = button("取消任务", self.cancel, tools)
        self.pause_button.setEnabled(False); self.cancel_button.setEnabled(False)
        layout.addLayout(tools)
        self.summary = QLabel("选择一个拍摄组，点击“扫描并分析”生成精简建议。"); self.summary.setObjectName("muted"); layout.addWidget(self.summary)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.build_gallery(); self.build_review(); self.build_files(); self.build_sets(); self.build_learning(); self.build_quality()
        self.tabs.currentChanged.connect(self.tab_changed)
        self.progress = QProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0); layout.addWidget(self.progress)
        self.status = QLabel(f"工作区：{root}   |   单任务队列 · 无远程 API · 无永久删除"); self.status.setObjectName("muted"); layout.addWidget(self.status)
        self.auto_learning=AutoLearning(self)
        self.auto_learning.updated.connect(self.learning_updated)
        if defer_loading:
            self.summary.setText("正在读取照片库记录…")
            QTimer.singleShot(10, self.load_albums)
        else:
            self.load_albums()

    def save_setting(self, key, value):
        if self.settings.get(key) == value: return
        self.settings[key] = value
        try:
            self.runtime.save_settings(self.settings)
        except OSError:
            logging.exception("本次设置暂未写入；程序继续运行")

    def show_error(self, error):
        QMessageBox.warning(self, "未完成", str(error))

    def safe(self, fn):
        try:
            return fn()
        except Exception as exc:
            self.show_error(exc)

    def load_albums(self):
        preferred = self.settings.get("last_album")
        self.album_picker.blockSignals(True); self.album_picker.clear()
        for p in sorted(self.root.iterdir(), key=lambda p: p.name):
            if p.is_dir() and not p.name.startswith(".") and p.name.lower() not in EXCLUDED:
                self.album_picker.addItem(p.name)
        if preferred and self.album_picker.findText(preferred) >= 0:
            self.album_picker.setCurrentText(preferred)
        self.album_picker.blockSignals(False)
        if self.album_picker.count():
            self.select_album(self.album_picker.currentText())
        self.album_picker.currentTextChanged.connect(self.select_album, Qt.ConnectionType.UniqueConnection)

    def choose_workspace(self):
        folder = QFileDialog.getExistingDirectory(self, "选择包含多个拍摄组的工作区", str(self.root))
        if folder:
            self.auto_learning.suspend()
            self.root = Path(folder).resolve(); self.store = Store(self.runtime.database(self.root))
            self.collections = Collections(self.store, self.root)
            self.save_setting("workspace", str(self.root)); self.load_albums()
            self.auto_learning.dirty=True; self.auto_learning.resume()

    def select_album(self, album):
        self.album = album; self.page = 0; self.quality_page = 0
        if album:
            self.save_setting("last_album", album)
        self.refresh()

    def refresh(self):
        if not self.album:
            return
        self.assets = self.store.assets(self.album)
        self.asset_by_id = {a["id"]: a for a in self.assets}
        self.refresh_summary()
        self.refresh_gallery(); self.refresh_review(); self.refresh_sets(); self.refresh_journal(); self.refresh_learning(); self.refresh_quality()

    def refresh_summary(self):
        photos = [a for a in self.assets if a["role"] == "ordinary_jpeg"]
        protected = sum(a["protected"] for a in photos)
        deletions = sum(a["decision"] == "delete" for a in photos)
        self.summary.setText(f"{self.album}   |   普通 JPG {len(photos)} 张 · 保护 {protected} 张 · 待删 {deletions} 张 · 已登记文件 {len(self.assets)} 个")

    def decisions_changed(self, decisions, source='manual'):
        """Update state labels in place. No image reads or rebuilding other tabs."""
        self.auto_learning.changed(len(decisions),source)
        for aid, decision in decisions.items():
            if aid in self.asset_by_id:
                self.asset_by_id[aid]["decision"] = decision
                if aid in self.review_cards:
                    self.review_cards[aid].update_state(self.asset_by_id[aid])
        self.refresh_summary()
        for row in {r for aid in decisions for r in self.group_rows.get(aid, [])}:
            self.group_list.item(row).setText(self.group_label(self.group_data[row]))
        self.update_review_summary()
        for row in range(self.gallery.count()):
            item = self.gallery.item(row); aid = item.data(Qt.ItemDataRole.UserRole)
            if aid in decisions:
                item.setText(self.gallery_label(self.asset_by_id[aid]))
        for row in range(self.quality_grid.count()):
            item=self.quality_grid.item(row); aid=item.data(Qt.ItemDataRole.UserRole)
            if aid in decisions: item.setText(self.quality_label(self.quality_entries[aid]))
        if self.filter.currentText() == "待删 JPG":
            self.gallery_dirty = True
            if self.tabs.currentIndex() == 0 and not self.preview_open:
                self.refresh_gallery()

    def tab_changed(self, index):
        self.thumbnail_loader.cancel(self.gallery_thumb_owner); self.thumbnail_loader.cancel(self.set_thumb_owner)
        self.thumbnail_loader.cancel(self.quality_thumb_owner)
        if index == 0 and self.gallery_dirty: self.refresh_gallery()
        if index == 4: self.refresh_learning()
        QTimer.singleShot(0, self.load_visible_thumbnails)

    def load_visible_thumbnails(self):
        if self.thread is not None: return
        for grid, owner in ((self.gallery, self.gallery_thumb_owner), (self.set_grid, self.set_thumb_owner), (self.quality_grid,self.quality_thumb_owner)):
            if not grid.isVisible(): continue
            self.thumbnail_loader.cancel(owner)
            for row in range(grid.count()):
                item = grid.item(row); asset = self.asset_by_id.get(item.data(Qt.ItemDataRole.UserRole))
                if asset:
                    self.thumbnail_loader.request(self.runtime, self.root, asset, owner, lambda pix, item=item: item.setIcon(QIcon(pix)))

    def apply_decisions(self, decisions, context, source='manual'):
        self.store.decide_many(decisions, context, source=source)
        self.decisions_changed(decisions,source)

    def run_task(self, fn, callback=None):
        if self.thread is not None:
            self.show_error("已有任务运行，请等待完成或先取消。"); return
        self.control = Control(); self.task_done = callback
        self.pending_result = None
        self.thread = QThread(self); self.worker = Worker(fn, self.control)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.task_progress)
        self.worker.succeeded.connect(self.task_success)
        self.worker.failed.connect(self.show_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.task_finished)
        self.set_busy(True); self.thread.start()

    def set_busy(self, busy):
        if busy:
            self.auto_learning.suspend()
            self.thumbnail_loader.cancel(self.gallery_thumb_owner); self.thumbnail_loader.cancel(self.set_thumb_owner)
            self.thumbnail_loader.cancel(self.quality_thumb_owner)
        else:
            self.auto_learning.resume()
            QTimer.singleShot(0, self.load_visible_thumbnails)
        self.tabs.setEnabled(not busy); self.scan_button.setEnabled(not busy); self.analyze_button.setEnabled(not busy)
        self.quality_button.setEnabled(not busy)
        self.album_picker.setEnabled(not busy); self.workspace_button.setEnabled(not busy); self.strength.setEnabled(not busy)
        self.pause_button.setEnabled(busy); self.cancel_button.setEnabled(busy)

    def task_progress(self, n, total, label):
        self.progress.setValue(int(100 * n / max(total, 1))); self.status.setText(label)

    def task_success(self, result):
        self.pending_result = result

    def task_finished(self):
        thread = self.thread; self.thread = None; self.worker = None; self.control = None
        thread.deleteLater(); self.set_busy(False); self.pause_button.setText("暂停")
        self.refresh()
        result = getattr(self, "pending_result", None)
        self.pending_result = None
        callback, self.task_done = self.task_done, None
        if result is not None:
            if isinstance(result,dict) and ('analysis' in result or 'groups' in result or 'sensitivity' in result):
                self.auto_learning.data_ready()
            if callback:
                self.safe(lambda: callback(result))
            else:
                self.status.setText(json.dumps(result, ensure_ascii=False))

    def pause(self):
        if self.control:
            if self.control.pause.is_set():
                self.control.pause.clear(); self.pause_button.setText("暂停")
            else:
                self.control.pause.set(); self.pause_button.setText("继续")

    def cancel(self):
        if self.control:
            self.control.cancel.set(); self.control.pause.clear(); self.status.setText("正在等待当前文件完成，随后取消……")

    def scan(self):
        album = self.album
        if album:
            store, runtime, root, settings = self.store, self.runtime, self.root, dict(self.settings)
            def work(progress, control):
                result = Scanner(store, root).scan(root / album, progress, control.cancel, control)
                if not result["complete"]:
                    return {**result, "cancelled": control.cancel.is_set()}
                if not control.checkpoint():
                    return {**result, "cancelled": True}
                progress(0, 1, "扫描完成，正在开始分析…")
                from .analysis import analyze
                result["analysis"] = analyze(store, runtime, root, album, settings, progress, control)
                return result
            self.run_task(work, self.scan_finished)

    def scan_finished(self, result):
        analysis = result.get("analysis")
        if analysis is not None:
            if analysis.get("cancelled"):
                self.status.setText("已取消分析，已完成的缓存会保留。")
            else:
                self.progress.setValue(100)
                text = f"扫描并分析完成 · 重复候选 {analysis['candidates']} 张（精简复核）· 疑似废片 {analysis.get('quality_candidates',0)} 张（废片筛查）· 复用缓存 {analysis['cached']} 张。"
                if analysis.get("failures"): text += f" 读取失败 {len(analysis['failures'])} 张。"
                self.status.setText(text)
        elif result.get("cancelled"):
            self.status.setText("已取消扫描，未开始分析。")
        else:
            errors = result.get("errors", [])
            self.status.setText("扫描未完整完成，未开始分析。" + (str(errors[0]) if errors else ""))

    def start_analysis(self):
        album = self.album
        if album:
            settings = dict(self.settings)
            store, runtime, root = self.store, self.runtime, self.root
            def work(progress, control):
                from .analysis import analyze
                return analyze(store, runtime, root, album, settings, progress, control)
            self.run_task(work)

    def icon(self, asset):
        key = (str(self.root), str(thumbnail_path(self.runtime, asset)), 320, 220)
        pix = THUMBNAILS.get(key)
        return QIcon(pix) if pix is not None else QIcon()

    def build_gallery(self):
        pane = QWidget(); layout = QVBoxLayout(pane); bar = QHBoxLayout()
        self.filter = QComboBox(); self.filter.addItems(["普通 JPG", "待删 JPG", "受保护图片", "精选", "成片", "全部图片"])
        self.filter.currentTextChanged.connect(lambda: self.reset_page()); bar.addWidget(self.filter)
        button("保留", lambda: self.gallery_decide("keep"), bar)
        button("标记待删", lambda: self.gallery_decide("delete"), bar)
        button("撤销决定", lambda: self.gallery_decide("pending"), bar)
        button("加入当前图集并锁定", self.add_to_set, bar)
        button("关联原片", self.link_source, bar)
        bar.addStretch(); layout.addLayout(bar)
        self.gallery = QListWidget(); self.gallery.setViewMode(QListWidget.ViewMode.IconMode)
        self.gallery.setIconSize(QSize(230, 153)); self.gallery.setGridSize(QSize(260, 220))
        self.gallery.setResizeMode(QListWidget.ResizeMode.Adjust); self.gallery.setMovement(QListWidget.Movement.Static)
        self.gallery.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.gallery.itemDoubleClicked.connect(self.preview); layout.addWidget(self.gallery, 1)
        foot = QHBoxLayout(); button("上一页", lambda: self.change_page(-1), foot)
        self.page_label = QLabel(); foot.addWidget(self.page_label); button("下一页", lambda: self.change_page(1), foot)
        foot.addStretch(); layout.addLayout(foot); self.tabs.addTab(pane, "图库")

    def reset_page(self):
        self.page = 0; self.refresh_gallery()

    def change_page(self, amount):
        self.page = max(0, self.page + amount); self.refresh_gallery()

    def gallery_assets(self):
        mode = self.filter.currentText()
        pictures = [a for a in self.assets if a["role"] in {"ordinary_jpeg", "selected_jpeg", "finished", "extra_image"}]
        if mode == "普通 JPG": return [a for a in pictures if a["role"] == "ordinary_jpeg"]
        if mode == "待删 JPG": return [a for a in pictures if a["decision"] == "delete"]
        if mode == "受保护图片": return [a for a in pictures if a["protected"]]
        if mode == "精选": return [a for a in pictures if a["role"] == "selected_jpeg"]
        if mode == "成片": return [a for a in pictures if a["role"] == "finished"]
        return pictures

    def refresh_gallery(self):
        self.thumbnail_loader.cancel(self.gallery_thumb_owner)
        self.gallery_dirty = False
        self.gallery.clear(); assets = self.gallery_assets(); size = self.settings["page_size"]
        pages = max(1, (len(assets) + size - 1) // size); self.page = min(self.page, pages - 1)
        used = {r["logical_key"]: r["name"] for r in self.store.rows("SELECT i.logical_key,s.name FROM set_items i JOIN photo_sets s ON s.id=i.set_id")}
        self.used_in_sets = used
        for a in assets[self.page*size:(self.page+1)*size]:
            item = QListWidgetItem(self.icon(a), self.gallery_label(a)); item.setData(Qt.ItemDataRole.UserRole, a["id"]); item.setToolTip(a["path"])
            self.gallery.addItem(item)
        self.page_label.setText(f"第 {self.page+1}/{pages} 页 · {len(assets)} 张 · 双击看大图，左右键连续翻看")
        self.load_visible_thumbnails()

    def gallery_label(self, a):
        label = ("[保护] " if a["protected"] else "") + Path(a["path"]).name + "\n" + ROLES[a["role"]] + " · " + DECISIONS[a["decision"]]
        if a["logical_key"] in self.used_in_sets: label += "\n" + self.used_in_sets[a["logical_key"]]
        return label

    def gallery_decide(self, decision):
        ids = [i.data(Qt.ItemDataRole.UserRole) for i in self.gallery.selectedItems()]
        if ids:
            self.safe(lambda: self.apply_decisions(dict.fromkeys(ids, decision), "manual"))

    def preview(self, item):
        aid = item.data(Qt.ItemDataRole.UserRole)
        if self.sender() is self.set_grid:
            ids = [self.set_grid.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.set_grid.count())]
            self.open_preview(aid, ids, editable_ids=[])
        else:
            self.open_preview(aid, [a["id"] for a in self.gallery_assets()])

    def open_preview(self, aid, ids, context="manual", editable_ids=None):
        dialog = PhotoPreviewDialog(self.store, self.runtime, self.root, ids, aid,
                                    context=context, editable_ids=editable_ids, parent=self)
        if editable_ids is None:
            dialog.review_scope={0:'gallery',1:'similar',5:'quality'}.get(self.tabs.currentIndex())
            dialog.review_filter=self.filter.currentText()
        dialog.decisionChanged.connect(lambda aid, decision: self.decisions_changed({aid: decision}))
        self.preview_open = True
        try:
            dialog.exec()
        finally:
            current=dialog.current_id
            self.preview_open = False; dialog.deleteLater()
        if self.gallery_dirty and self.tabs.currentIndex() == 0:
            self.refresh_gallery()
        return current

    def open_review_preview(self, aid):
        g = self.current_group()
        if g:
            ids = group_members(g)
            self.open_preview(aid, ids, context=decision_contexts(g,ids))

    def link_source(self):
        selected = self.gallery.selectedItems()
        if len(selected) != 1:
            self.show_error("请只选一张需要关联的成片或精选副本"); return
        a = self.store.asset(selected[0].data(Qt.ItemDataRole.UserRole))
        sources = [i for i in self.assets if i["role"] == "ordinary_jpeg"]
        choices = [i["path"] for i in sources]
        text, ok = QInputDialog.getItem(self, "关联同一张原片", "选择对应普通 JPG。用于保护及图集去重，不是相似分组。", choices, 0, False)
        if ok:
            source = sources[choices.index(text)]
            self.safe(lambda: self.store.alias(a["id"], source["id"])); self.scan()

    def build_review(self):
        pane = QWidget(); layout = QVBoxLayout(pane)
        note = QLabel("滚动浏览本组缩略图，双击看大图；大图内左右键切图，K 保留 / D 待删 / U 撤销。标记只记状态。"); note.setWordWrap(True); layout.addWidget(note)
        split = QSplitter(); self.group_list = QListWidget(); self.group_list.currentRowChanged.connect(self.select_group); split.addWidget(self.group_list)
        right = QWidget(); inner = QVBoxLayout(right); self.compare = QListWidget()
        self.compare.setViewMode(QListWidget.ViewMode.IconMode); self.compare.setGridSize(QSize(350, 385))
        self.compare.setResizeMode(QListWidget.ResizeMode.Adjust); self.compare.setMovement(QListWidget.Movement.Static)
        self.compare.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.compare.itemDoubleClicked.connect(lambda item: self.open_review_preview(item.data(Qt.ItemDataRole.UserRole)))
        inner.addWidget(self.compare)
        self.review_summary = QLabel(); self.review_summary.setWordWrap(True); inner.addWidget(self.review_summary)
        self.review_cards = {}
        actions = QHBoxLayout(); self.adopt_button = button("仅给未决定项采用建议", self.accept_group, actions)
        button("选中照片留下", lambda: self.review_decide("keep"), actions)
        button("选中照片待删", lambda: self.review_decide("delete"), actions)
        button("整组保留", self.keep_group, actions); inner.addLayout(actions)
        pages=QHBoxLayout(); button('上一页',lambda:self.change_review_page(-1),pages)
        self.review_page_label=QLabel(); pages.addWidget(self.review_page_label)
        button('下一页',lambda:self.change_review_page(1),pages); pages.addStretch(); inner.addLayout(pages)
        split.addWidget(right); split.setSizes([240, 1000]); layout.addWidget(split, 1)
        self.tabs.addTab(pane, "精简复核")

    def refresh_review(self):
        previous = self.group_list.currentRow(); self.group_list.blockSignals(True); self.group_list.clear()
        self.group_data=[]
        for group in self.store.groups(self.album):
            if group.get('kind')=='quality':continue
            members=[aid for aid in group_members(group) if aid in self.asset_by_id]
            if len(members)<2:continue
            # Prune presentation only; never invent a replacement AI reference.
            self.group_data.append({**group,'members':members,'candidates':[m for m in group['candidates'] if m['id'] in self.asset_by_id],
                'keeps':[aid for aid in group_references(group) if aid in self.asset_by_id]})
        self.group_rows = {}
        for row, g in enumerate(self.group_data):
            self.group_list.addItem(self.group_label(g))
            for aid in group_members(g):
                self.group_rows.setdefault(aid, []).append(row)
        self.group_list.blockSignals(False)
        if self.group_data:
            self.group_list.setCurrentRow(max(0, min(previous, len(self.group_data)-1)))
        else:
            self.compare.clear(); self.review_cards = {}; self.review_summary.setText("当前没有重复构图建议。模糊、疑似虚焦等问题请到“废片筛查”查看。")
            self.review_page_label.setText('')
            self.adopt_button.setEnabled(False)

    def group_label(self, g):
        keep = self.asset_by_id.get(g["keep"])
        members = [self.asset_by_id.get(i) for i in group_members(g)]
        done = sum(a is not None and a["decision"] != "pending" for a in members)
        title='疑似技术问题 · 逐张确认' if g.get('kind')=='quality' else (Path(keep['path']).name if keep else '照片已移出')
        refs=len(group_references(g))
        progress='高强度二次筛查' if g.get('screening_mode') else f'已决定 {done}/{len(members)}'
        return f"{title}\n同组 {len(members)} · 候选 {len(g['candidates'])} · 参照 {refs}\n{progress}"

    def current_group(self):
        index = self.group_list.currentRow()
        return self.group_data[index] if 0 <= index < len(self.group_data) else None

    def select_group(self, index):
        self.review_page=0; self.render_review_page()

    def change_review_page(self,amount):
        self.review_page=max(0,self.review_page+amount); self.render_review_page()

    def render_review_page(self):
        self.compare.clear(); self.review_cards = {}; g = self.current_group()
        if not g: return
        members=group_members(g); refs=set(group_references(g)); suggestions={m['id']:m for m in g['candidates']}
        size=24; pages=max(1,(len(members)+size-1)//size); self.review_page=min(self.review_page,pages-1)
        self.review_page_label.setText(f'组内第 {self.review_page+1}/{pages} 页 · {len(members)} 张 · 大图可连续翻看整组')
        for aid in members[self.review_page*size:(self.review_page+1)*size]:
            m=suggestions.get(aid,{'id':aid})
            a = self.asset_by_id.get(m["id"])
            if not a or not a["present"]: continue
            if g.get('kind')=='quality':
                detail='；'.join(m['issues'])+' · 请看大图确认'
            elif aid in refs:
                detail="建议保留 / 比较参照"
            elif aid not in suggestions:
                detail='由你决定 / 未生成自动删减建议'
            else:
                evidence=m.get('evidence',{})
                if evidence.get('method')=='sift':
                    detail=f"建议删减 · 构图重合 {evidence['overlap']:.0%} · 匹配点 {evidence['inliers']}"
                elif evidence.get('method')=='exact': detail='建议删减 · 文件内容完全相同'
                else: detail=f"建议删减 · pHash {m['phash_distance']} · dHash {m['dhash_distance']}"
                reference=self.asset_by_id.get(m.get('reference',g['keep']))
                if reference: detail+=' · 对照 '+Path(reference['path']).name
                if m.get('quality_note'): detail+=' · '+m['quality_note']
            item = QListWidgetItem(); item.setSizeHint(QSize(330, 365)); item.setData(Qt.ItemDataRole.UserRole, a["id"])
            self.compare.addItem(item)
            card = PhotoCard(a, self.runtime, self.root, detail)
            card.previewRequested.connect(self.open_review_preview); card.decisionRequested.connect(self.review_photo_decide)
            self.review_cards[a["id"]] = card; self.compare.setItemWidget(item, card)
        self.update_review_summary()

    def update_review_summary(self):
        g = self.current_group()
        if not g: return
        states = {"pending": 0, "keep": 0, "delete": 0}
        for aid in group_members(g):
            a = self.asset_by_id.get(aid)
            if a: states[a["decision"]] += 1
        note='技术问题只有提示，请逐张确认删留。' if g.get('kind')=='quality' else '你的单张决定会保留，不被批量建议覆盖。'
        if len(group_references(g))>1: note+=f" 本组有 {len(group_references(g))} 张保留参照，各候选按标出的对照图给出建议。"
        self.review_summary.setText(f"本组：保留 {states['keep']} · 待删 {states['delete']} · 未决定 {states['pending']}。"+note)
        valid_refs={aid for aid in group_references(g) if self.asset_by_id.get(aid) and self.asset_by_id[aid]['decision']!='delete'}
        secondary=g.get('screening_mode')=='secondary-v1'
        actionable=False if secondary else any(self.asset_by_id[aid]['decision']=='pending' for aid in valid_refs)
        for member in g['candidates']:
            a=self.asset_by_id.get(member['id'])
            eligible=(a and a['decision'] in {'pending','keep'} and not a['protected'] and a['role']=='ordinary_jpeg'
                      and a['id'] not in getattr(self,'secondary_resolved_ids',set())) if secondary else (a and a['decision']=='pending')
            if eligible and member.get('reference',g['keep']) in valid_refs: actionable=True
        self.adopt_button.setEnabled(bool(g.get('kind')!='quality' and actionable))
        self.adopt_button.setToolTip('只给未决定项采用建议；每张候选必须有仍保留的直接参照。参照已待删的候选需逐张决定。')

    def accept_group(self):
        g = self.current_group()
        if not g: return
        def action():
            if g.get('kind')=='quality': raise ValueError('技术问题请逐张确认删留。')
            valid_refs={aid for aid in group_references(g) if self.asset_by_id.get(aid) and self.asset_by_id[aid]['decision']!='delete'}
            if not valid_refs:
                raise ValueError("参照已被你标为待删，请逐张决定其余照片。")
            decisions = {}
            for aid in valid_refs:
                a = self.asset_by_id.get(aid)
                if a and a["present"] and a["decision"] == "pending":
                    decisions[aid] = 'keep'
            for member in g['candidates']:
                aid=member['id']; a=self.asset_by_id.get(aid)
                if a and a['present'] and a['decision']=='pending' and member.get('reference',g['keep']) in valid_refs:
                    decisions[aid]='keep' if a['protected'] else 'delete'
            self.apply_decisions(decisions, decision_contexts(g,decisions), source='adopt')
        self.safe(action)

    def review_photo_decide(self, aid, decision):
        g = self.current_group()
        if g:
            self.safe(lambda: self.apply_decisions({aid: decision}, member_context(g,aid)))

    def review_decide(self, decision):
        g = self.current_group()
        if g:
            ids = [i.data(Qt.ItemDataRole.UserRole) for i in self.compare.selectedItems()]
            self.safe(lambda: self.apply_decisions(dict.fromkeys(ids, decision), decision_contexts(g,ids)))

    def keep_group(self):
        g = self.current_group()
        if g:
            ids = group_members(g)
            self.safe(lambda: self.apply_decisions(dict.fromkeys(ids, "keep"), decision_contexts(g,ids)))

    def build_quality(self):
        self.quality_pane=QWidget(); layout=QVBoxLayout(self.quality_pane)
        note=QLabel('独立检查每张普通 JPG 的模糊、疑似虚焦和严重曝光异常，不要求有重复图。精选及对应原片受保护。双击看大图，左右键连续翻看；K 保留 / D 待删 / U 撤销。')
        note.setWordWrap(True); layout.addWidget(note)
        bar=QHBoxLayout(); bar.addWidget(QLabel('筛查敏感度'))
        self.quality_sensitivity=QComboBox(); self.quality_sensitivity.addItems(['仅明显问题','标准','多找可疑'])
        self.quality_sensitivity.setCurrentIndex(1)
        self.quality_sensitivity.setCurrentText(self.settings.get('quality_sensitivity','标准'))
        self.quality_sensitivity.setToolTip('提高敏感度会找出更多偏软照片，也可能包含正常的浅景深或柔焦照片。调整后点击开始筛查。')
        bar.addWidget(self.quality_sensitivity)
        button('开始筛查',self.start_quality,bar)
        self.quality_filter=QComboBox(); self.quality_filter.addItems(['全部候选','仅明显问题','仅可疑问题'])
        self.quality_filter.currentTextChanged.connect(self.reset_quality_page); bar.addWidget(self.quality_filter)
        bar.addStretch(); layout.addLayout(bar)
        self.quality_summary=QLabel(); self.quality_summary.setWordWrap(True); layout.addWidget(self.quality_summary)
        self.quality_grid=QListWidget(); self.quality_grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.quality_grid.setIconSize(QSize(230,153)); self.quality_grid.setGridSize(QSize(285,250))
        self.quality_grid.setResizeMode(QListWidget.ResizeMode.Adjust); self.quality_grid.setMovement(QListWidget.Movement.Static)
        self.quality_grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.quality_grid.itemDoubleClicked.connect(self.quality_preview); layout.addWidget(self.quality_grid,1)
        actions=QHBoxLayout()
        button('选中保留',lambda:self.quality_decide('keep'),actions)
        button('选中待删',lambda:self.quality_decide('delete'),actions)
        button('撤销决定',lambda:self.quality_decide('pending'),actions)
        actions.addStretch(); layout.addLayout(actions)
        foot=QHBoxLayout(); button('上一页',lambda:self.change_quality_page(-1),foot)
        self.quality_page_label=QLabel(); foot.addWidget(self.quality_page_label)
        button('下一页',lambda:self.change_quality_page(1),foot); foot.addStretch(); layout.addLayout(foot)
        self.quality_sensitivity.currentTextChanged.connect(lambda value:self.save_setting('quality_sensitivity',value))
        self.tabs.addTab(self.quality_pane,'废片筛查')

    def start_quality(self):
        if not self.album: return
        self.tabs.setCurrentWidget(self.quality_pane)
        store,runtime,root,album,settings=self.store,self.runtime,self.root,self.album,dict(self.settings)
        def work(progress,control):
            scanned=Scanner(store,root).scan(root/album,progress,control.cancel,control)
            if not scanned['complete']: return {**scanned,'cancelled':control.cancel.is_set()}
            if not control.checkpoint(): return {'cancelled':True}
            from .quality import screen_quality
            return screen_quality(store,runtime,root,album,settings,progress,control)
        self.run_task(work,self.quality_finished)

    def quality_finished(self,result):
        if result.get('cancelled'):
            self.status.setText('已取消废片筛查；旧的完整结果保留，已完成的缓存下次可用。')
        elif result.get('complete') is False:
            self.status.setText('扫描未完整完成，未开始废片筛查。')
        else:
            self.progress.setValue(100)
            self.status.setText(f"废片筛查完成 · 检查 {result['total']} 张 · 保护 {result['protected']} 张 · 疑似废片 {result['candidates']} 张 · 读取失败 {len(result['failures'])} 张。")

    def refresh_quality(self):
        report=self.store.quality_report(self.album)
        self.quality_screening_mode=(report or {}).get('summary',{}).get('screening_mode')
        self.quality_results=[]
        if report:
            for result in report['results']:
                a=self.asset_by_id.get(result['id'])
                if a and a['role']=='ordinary_jpeg' and not a['protected'] and (a['size'],a['mtime_ns'])==(result['size'],result['mtime_ns']):
                    if self.quality_screening_mode=='secondary-v1' and a['decision']=='delete':continue
                    self.quality_results.append(result)
            summary=report['summary']
            text=f"上次筛查：{summary['sensitivity']} · 检查 {summary['total']} 张 · 跳过受保护 {summary['protected']} 张 · 疑似 {len(self.quality_results)} 张（明显问题 {sum(r['severity']=='明显问题' for r in self.quality_results)} 张）。"
            current=[[a[k] for k in ('id','size','mtime_ns','protected')] for a in self.assets if a['role']=='ordinary_jpeg']
            if current!=report.get('inventory'): text+=' 目录已变化，请重新筛查更新结果。'
            if summary.get('failures'): text+=f" 读取失败 {len(summary['failures'])} 张，未对这些照片作判断。"
            if not self.quality_results: text+=' 当前未发现符合这档敏感度的候选。'
        else: text='尚未筛查。点击开始筛查；结果是技术指标提示，是否为废片由你确认。'
        self.quality_summary.setText(text)
        if self.quality_results:
            from .preferences import load_model,quality_hint
            model=load_model(self.runtime,self.store.path)
            for result in self.quality_results:
                result['personal_keep']=quality_hint(model,self.asset_by_id[result['id']],{'metrics':result['metrics']})
            self.quality_results.sort(key=lambda r:(r['severity']!='明显问题',
                r['personal_keep'] if r['personal_keep'] is not None else .5,r['metrics'].get('acutance',1),r['path']))
        self.quality_entries={r['id']:r for r in self.quality_results}
        self.refresh_quality_grid()

    def filtered_quality(self):
        choice=self.quality_filter.currentText()
        if choice=='仅明显问题': return [r for r in self.quality_results if r['severity']=='明显问题']
        if choice=='仅可疑问题': return [r for r in self.quality_results if r['severity']=='可疑问题']
        return self.quality_results

    def quality_label(self,result):
        a=self.asset_by_id[result['id']]
        hint=' · 按以往习惯可能想保留' if (result.get('personal_keep') or 0)>=.75 else ''
        return Path(a['path']).name+'\n'+result['severity']+' · '+' / '.join(i['label'] for i in result['issues'])+'\n'+DECISIONS[a['decision']]+hint

    def refresh_quality_grid(self):
        self.thumbnail_loader.cancel(self.quality_thumb_owner); self.quality_grid.clear()
        results=self.filtered_quality(); size=24
        pages=max(1,(len(results)+size-1)//size); self.quality_page=min(self.quality_page,pages-1)
        for result in results[self.quality_page*size:(self.quality_page+1)*size]:
            a=self.asset_by_id[result['id']]
            item=QListWidgetItem(self.icon(a),self.quality_label(result)); item.setData(Qt.ItemDataRole.UserRole,a['id'])
            item.setToolTip(a['path']+'\n'+'\n'.join(i['detail'] for i in result['issues']))
            self.quality_grid.addItem(item)
        self.quality_page_label.setText(f'第 {self.quality_page+1}/{pages} 页 · {len(results)} 张候选 · 标记只记状态，不移动原片')
        self.load_visible_thumbnails()

    def reset_quality_page(self,*args):
        self.quality_page=0; self.refresh_quality_grid()

    def change_quality_page(self,amount):
        self.quality_page=max(0,self.quality_page+amount); self.refresh_quality_grid()

    def quality_preview(self,item):
        self.open_preview(item.data(Qt.ItemDataRole.UserRole),[r['id'] for r in self.filtered_quality()],context='quality:standalone')

    def quality_decide(self,decision):
        ids=[i.data(Qt.ItemDataRole.UserRole) for i in self.quality_grid.selectedItems()]
        if ids: self.safe(lambda:self.apply_decisions(dict.fromkeys(ids,decision),'quality:standalone'))

    def build_files(self):
        pane = QWidget(); layout = QVBoxLayout(pane)
        layout.addWidget(QLabel("以下按钮只生成预览清单。执行前需要核对；不永久删除、不覆盖。"))
        bar = QHBoxLayout(); self.include_raw = QCheckBox("待删 JPG 清单中同时列出唯一匹配的 RAW/XMP"); bar.addWidget(self.include_raw); bar.addStretch(); layout.addLayout(bar)
        bar = QHBoxLayout(); button("预览待删备份清单", lambda: self.make_plan("delete"), bar)
        button("预览补齐精选 RAW", lambda: self.make_plan("fill_raw"), bar)
        button("预览孤立 RAW 整理", lambda: self.make_plan("orphan"), bar); bar.addStretch(); layout.addLayout(bar)
        layout.addWidget(QLabel("操作记录：仅支持恢复移动项目；复制的精选和图集导出不会通过恢复功能删除。"))
        self.journal = table(["记录号", "状态", "操作", "原位置", "备份/复制位置"]); layout.addWidget(self.journal, 1)
        bar = QHBoxLayout(); button("恢复选中的移动项目", self.restore_item, bar); bar.addStretch(); layout.addLayout(bar)
        self.tabs.addTab(pane, "文件整理")

    def make_plan(self, kind):
        if not self.album: return
        service = Operations(self.store, self.root); album = self.album; raw = self.include_raw.isChecked()
        fn = {"delete": lambda: service.deletion(album, raw), "orphan": lambda: service.orphan_raws(album), "fill_raw": lambda: service.fill_selected_raws(album)}[kind]
        def prepare(progress, control):
            service.control, service.progress = control, progress
            return fn()
        self.run_task(prepare, self.show_plan)

    def show_plan(self, plan):
        dialog = PlanDialog(plan, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        plan_id = dialog.plan.get("id")
        dialog.deleteLater()
        self.refresh()
        if accepted:
            self.run_task(lambda progress, control: Operations(self.store, self.root).execute(plan_id, progress, control))

    def refresh_journal(self):
        rows = self.store.rows("SELECT j.* FROM journal j JOIN plans p ON p.id=j.plan_id WHERE p.album=? ORDER BY j.id DESC LIMIT 300", (self.album,))
        self.journal.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for col, key in enumerate(["id", "state", "action", "src", "dst"]): self.journal.setItem(i, col, QTableWidgetItem(str(r[key])))

    def restore_item(self):
        row = self.journal.currentRow()
        if row < 0: return
        journal_id = int(self.journal.item(row, 0).text())
        if QMessageBox.question(self, "恢复移动", "恢复选中项目到记录中的原位置？已有文件不会覆盖。") == QMessageBox.StandardButton.Yes:
            self.run_task(lambda progress, control: Operations(self.store, self.root).restore(journal_id))

    def build_sets(self):
        pane = QWidget(); layout = QVBoxLayout(pane)
        bar = QHBoxLayout(); self.set_picker = QComboBox(); self.set_picker.setMinimumWidth(220); self.set_picker.currentIndexChanged.connect(self.refresh_set_items); bar.addWidget(self.set_picker)
        button("新建图集", self.new_set, bar); button("删除图集并释放照片", self.drop_set, bar)
        self.set_count = QSpinBox(); self.set_count.setRange(1, 30); self.set_count.setValue(self.settings["set_size"]); bar.addWidget(QLabel("张数")); bar.addWidget(self.set_count)
        self.second_cover = QCheckBox("第 2 张可用成片"); self.second_cover.setChecked(self.settings["second_cover"]); bar.addWidget(self.second_cover)
        button("自动补齐 / 重做未锁定项", self.generate_set, bar); bar.addStretch(); layout.addLayout(bar)
        self.set_grid = QListWidget(); self.set_grid.setViewMode(QListWidget.ViewMode.IconMode); self.set_grid.setIconSize(QSize(250, 166))
        self.set_grid.setGridSize(QSize(285, 235)); self.set_grid.setResizeMode(QListWidget.ResizeMode.Adjust); self.set_grid.setMovement(QListWidget.Movement.Static)
        self.set_grid.itemDoubleClicked.connect(self.preview); layout.addWidget(self.set_grid, 1)
        bar = QHBoxLayout(); button("锁定 / 解锁", self.toggle_set_lock, bar); button("移出图集", self.remove_set_item, bar)
        button("向前", lambda: self.reorder_set(-1), bar); button("向后", lambda: self.reorder_set(1), bar)
        button("预览导出清单", self.export_set, bar); bar.addStretch(); layout.addLayout(bar)
        note = QLabel("首图必须来自成片；在图库选图后点击“加入当前图集”。手选图自动锁定。图集不参与偏好学习。自动组图目前依据结构/色彩差异。")
        note.setWordWrap(True); layout.addWidget(note); self.tabs.addTab(pane, "图集")

    def selected_set(self):
        return self.set_picker.currentData()

    def refresh_sets(self):
        previous = self.selected_set(); self.set_picker.blockSignals(True); self.set_picker.clear()
        for s in self.collections.list(self.album): self.set_picker.addItem(s["name"], s["id"])
        index = self.set_picker.findData(previous)
        if index >= 0: self.set_picker.setCurrentIndex(index)
        self.set_picker.blockSignals(False); self.refresh_set_items()

    def refresh_set_items(self):
        self.thumbnail_loader.cancel(self.set_thumb_owner)
        self.set_grid.clear(); set_id = self.selected_set()
        if not set_id: return
        for a in self.collections.items(set_id):
            label = f"{a['slot']+1:02d} · {Path(a['path']).name}\n" + ("已锁定" if a["locked"] else "可自动替换")
            item = QListWidgetItem(self.icon(a), label); item.setData(Qt.ItemDataRole.UserRole, a["id"]); item.setData(Qt.ItemDataRole.UserRole+1, a["slot"]); self.set_grid.addItem(item)
        self.load_visible_thumbnails()

    def new_set(self):
        if self.album:
            set_id = self.collections.create(self.album); self.refresh_sets(); self.set_picker.setCurrentIndex(self.set_picker.findData(set_id))

    def drop_set(self):
        set_id = self.selected_set()
        if set_id and QMessageBox.question(self, "删除图集记录", "释放此图集占用的照片？照片文件及导出副本不受影响。") == QMessageBox.StandardButton.Yes:
            self.collections.drop(set_id); self.refresh()

    def generate_set(self):
        set_id = self.selected_set()
        if not set_id: self.new_set(); set_id = self.selected_set()
        if set_id:
            result = self.safe(lambda: self.collections.generate(set_id, self.set_count.value(), self.second_cover.isChecked()))
            self.refresh()
            if result: self.status.setText(f"已生成 {result['count']}/{result['requested']} 张。" + result["message"])

    def add_to_set(self):
        set_id = self.selected_set()
        if not set_id:
            self.show_error("请先在图集页面新建图集，再选择封面或普通图。"); return
        for item in self.gallery.selectedItems():
            if self.safe(lambda i=item: self.collections.add(set_id, i.data(Qt.ItemDataRole.UserRole))) is None:
                pass
        self.refresh()

    def set_item(self):
        item = self.set_grid.currentItem()
        return (self.selected_set(), item.data(Qt.ItemDataRole.UserRole+1)) if item else (None, None)

    def toggle_set_lock(self):
        set_id, slot = self.set_item()
        if set_id:
            a = next(a for a in self.collections.items(set_id) if a["slot"] == slot)
            self.collections.lock(set_id, slot, not a["locked"]); self.refresh_set_items()

    def remove_set_item(self):
        set_id, slot = self.set_item()
        if set_id: self.collections.remove(set_id, slot); self.refresh()

    def reorder_set(self, delta):
        set_id, slot = self.set_item()
        if set_id: self.safe(lambda: self.collections.reorder(set_id, slot, delta)); self.refresh_set_items()

    def export_set(self):
        set_id = self.selected_set()
        if set_id: self.run_task(lambda progress, control: Operations(self.store, self.root).export_set(set_id), self.show_plan)

    def build_learning(self):
        pane = QWidget(); layout = QVBoxLayout(pane)
        from .learning_panel import LearningPanel
        self.learning_panel=LearningPanel(self);self.learning=self.learning_panel.details;layout.addWidget(self.learning_panel,1)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("同时分析照片数")); self.analysis_workers = QSpinBox(); self.analysis_workers.setRange(0, 8); self.analysis_workers.setSpecialValueText("自动")
        self.analysis_workers.setValue(self.settings.get("analysis_workers", 0)); self.analysis_workers.valueChanged.connect(lambda v: self.save_setting("analysis_workers", v)); bar.addWidget(self.analysis_workers)
        bar.addWidget(QLabel("额外间隔")); self.delay = QSpinBox(); self.delay.setRange(0, 2000); self.delay.setSuffix(" ms"); self.delay.setValue(self.settings["delay_ms"])
        self.delay.valueChanged.connect(lambda v: self.save_setting("delay_ms", v)); bar.addWidget(self.delay)
        bar.addStretch(); layout.addLayout(bar)
        self.tabs.addTab(pane, "学习与资源")

    def refresh_learning(self):
        from .learning_report import learning_report
        self.learning_snapshot=learning_report(self.store,self.runtime)
        self.learning_panel.bind(self.learning_snapshot,self.auto_learning.activity())
        if hasattr(self,'update_learning_activity'):self.update_learning_activity()

    def learning_updated(self,message):
        if self.tabs.currentIndex()==4: self.refresh_learning()
        # Keep background notices away from the active review/status workflow.
        self.learning.setToolTip(message)

    def train_preferences(self):
        self.auto_learning.maybe_start(force=True)

    def closeEvent(self, event):
        if self.thread is not None:
            self.cancel(); self.status.setText("正在取消任务，请任务停止后再关闭。"); event.ignore()
        else:
            self.auto_learning.close()
            self.thumbnail_loader.cancel(self.gallery_thumb_owner); self.thumbnail_loader.cancel(self.set_thumb_owner)
            self.thumbnail_loader.cancel(self.quality_thumb_owner)
            event.accept()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.load_visible_thumbnails)


from .workspace_ui import WorkspaceUI
from .scroll_ui import ScrollUI
from .native_ui import NativeUI
from .live_refresh import LiveRefreshUI


class MainWindow(NativeUI, LiveRefreshUI, ScrollUI, WorkspaceUI, BaseWindow):
    """The services stay independent from the three-workspace presentation."""
