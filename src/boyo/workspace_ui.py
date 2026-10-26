"""Three workspaces around the existing services and a shared photo viewer."""
from collections import deque,OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt,QSize,QTimer
from PySide6.QtGui import QAction,QKeySequence,QShortcut,QIcon
from PySide6.QtWidgets import (QAbstractItemView,QAbstractSpinBox,QApplication,QCheckBox,QComboBox,QHeaderView,
    QDialog,QDialogButtonBox,QHBoxLayout,QInputDialog,QLabel,QLineEdit,QListWidget,QListWidgetItem,
    QMenu,QMessageBox,QPushButton,QSizePolicy,QSplitter,QStackedWidget,QTabBar,QTableWidgetItem,QTextEdit,QToolButton,QVBoxLayout,QWidget)

from .collections import Collections
from .grouping import group_members,group_references,decision_contexts,member_context
from .operations import Operations
from .scanner import Scanner
from .review_viewer import PhotoSequence
from .photo_widgets import PhotoCard,PhotoPreviewDialog
from .plan_dialog import PlanDialog
from .file_panel import FilePlanPanel
from .browser_widgets import ScrollUpdates,QuietItemDelegate,visible_rows
from .branding import APP_NAME,APP_TITLE
from .screening import SECONDARY,suggestion_is_current,evidence_is_current

WORKSPACE_STYLE='''
QMainWindow,QDialog,QWidget {background:#191b28;color:#cdd0e1;font-family:'Microsoft YaHei';font-size:13px;}
QLabel#title {font-size:23px;font-weight:700;color:#e0e1ed;}
QLabel#muted {color:#9399b3;}
QPushButton,QToolButton {background:#2a2e45;border:1px solid #424762;border-radius:5px;padding:7px 11px;outline:0;}
QPushButton:hover,QToolButton:hover {background:#373d59;}
QPushButton:checked {background:#3c3b59;border-color:#77749f;}
QPushButton#primary {background:#4d527b;color:#e3e3f1;border-color:#70769c;font-weight:600;}
QPushButton#danger {background:#51405e;border-color:#796388;color:#d8c9df;}
QPushButton:disabled {background:#222536;color:#626983;border-color:#32364c;}
QListWidget,QTableWidget,QTextEdit {background:#202335;border:1px solid #353b53;border-radius:5px;outline:0;}
QListWidget::item {padding:8px;border:0;}
QListWidget::item:selected {background:#3b4163;color:#dedff0;}
QComboBox,QSpinBox,QLineEdit {background:#292d43;border:1px solid #424864;border-radius:4px;padding:6px;}
QHeaderView::section {background:#2d324b;color:#b9bfd8;padding:8px;border:0;}
QTabWidget::pane {border:0;}
QTabBar::tab {background:transparent;padding:10px 19px;color:#959bb6;border-bottom:2px solid transparent;outline:0;}
QTabBar::tab:selected {color:#adaad0;border-bottom:2px solid #7e7ca5;}
QProgressBar {border:1px solid #3c425e;border-radius:4px;background:#222638;text-align:center;max-height:16px;}
QProgressBar::chunk {background:#5d638c;}
QFrame#photoCard {background:#292d43;border:1px solid #404660;border-radius:6px;}
QFrame#photoCard[selected="true"] {background:#333952;border-color:#76749d;}
QPushButton[photoDecision="keep"] {color:#9ed0ac;}
QPushButton[photoDecision="delete"],QPushButton#danger {color:#e5a1a1;}
QPushButton[photoDecision]:disabled {color:#626983;}
QCheckBox {spacing:7px;}
QLabel {background:transparent;}
QMenu {background:#292d43;border:1px solid #494e6a;}
QMenu::item {padding:8px 20px;} QMenu::item:selected {background:#3b4163;}
QScrollBar:vertical {background:#202335;width:10px;margin:0;}
QScrollBar::handle:vertical {background:#4a506d;min-height:28px;border-radius:4px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {background:none;}
'''


def action_button(text,fn,layout,name=None):
    button=QPushButton(text);button.clicked.connect(fn)
    if name:button.setObjectName(name)
    layout.addWidget(button);return button


class WorkspaceUI:
    def __init__(self,*args,**kwargs):
        self.shell_ready=False;self.pending_queue_ids=None;self.deferred_results=False
        self.task_context=None;self.task_queue=deque();self.file_results=OrderedDict();self.task_serial=0;self.closed_export_panels=set()
        self.review_reference=None;self.review_candidate=None;self.last_photo_task='ordinary'
        super().__init__(*args,**kwargs)
        self.settings.setdefault('all_review_large',True);self.settings.setdefault('auto_advance',False)
        self.assemble_workspaces()
        self.shell_ready=True
        self.setWindowTitle(APP_TITLE);self.setStyleSheet(WORKSPACE_STYLE)
        self.gallery_mode.setCurrentIndex(0);self.review_mode.setCurrentIndex(1);self.quality_mode.setCurrentIndex(1)
        self.restore_work_position();self.render_review_page();self.update_gallery_sequence();self.update_quality_sequence()
        self.update_workspace_counts();self.update_busy_controls()
        self.scroll_updates=ScrollUpdates(self,[self.gallery,self.compare,self.quality_grid,self.set_grid,self.set_candidates],self.load_visible_thumbnails)
        for grid in (self.photo_navigation,self.set_list,self.file_navigation,self.group_list):grid.setItemDelegate(QuietItemDelegate(grid))
        self.workspaces.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.compare.itemSelectionChanged.connect(self.highlight_review_selection)
        self.auto_learning.activityChanged.connect(self.update_learning_activity);self.update_learning_activity()
        for button in self.findChildren(QPushButton):
            if any(word in button.text() for word in ('上一页','下一页','上一张','下一张','上一组','下一组')):button.hide()

    def assemble_workspaces(self):
        old=self.takeCentralWidget();old.hide();self.legacy_shell=old
        body=QWidget();self.setCentralWidget(body);old.setParent(body);old.hide();layout=QVBoxLayout(body);layout.setContentsMargins(16,12,16,10);layout.setSpacing(10)
        header=QHBoxLayout();title=QLabel(APP_NAME);title.setObjectName('title');header.addWidget(title)
        self.workspaces=QTabBar();self.workspaces.setDrawBase(False);self.workspaces.setExpanding(False)
        for name in ('照片精简','文件工具','自动图集'):self.workspaces.addTab(name)
        header.addWidget(self.workspaces);header.addStretch()
        action_button('设置',self.open_settings,header);layout.addLayout(header)
        top=QHBoxLayout();self.album_picker.setMinimumWidth(210);top.addWidget(self.album_picker)
        self.scan_button.setObjectName('primary');top.addWidget(self.scan_button)
        more=QToolButton();more.setText('更多');more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu=QMenu(more);menu.addAction('重新分析',self.start_analysis);menu.addAction('取消排队任务',self.clear_queued_tasks);more.setMenu(menu);top.addWidget(more)
        self.new_results_button=action_button('更新分析结果',self.apply_new_results,top);self.new_results_button.hide()
        top.addStretch();self.learning_status=QPushButton('偏好 · 积累中');self.learning_status.setObjectName('quiet');self.learning_status.clicked.connect(self.open_learning_details);top.addWidget(self.learning_status)
        layout.addLayout(top);self.summary.setWordWrap(True);self.summary.setMinimumWidth(0)
        self.summary.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);layout.addWidget(self.summary)
        split=QSplitter();self.main_splitter=split;self.side_stack=QStackedWidget();self.side_stack.setMinimumWidth(128);self.side_stack.setMaximumWidth(160)
        self.photo_navigation=QListWidget()
        for key,label in (('ordinary','普通照片'),('similar','相似复核'),('quality','废片筛查'),('pending','待删复核'),('protected','精选与成片')):
            item=QListWidgetItem(label);item.setData(Qt.ItemDataRole.UserRole,key);self.photo_navigation.addItem(item)
        self.photo_navigation.currentRowChanged.connect(self.photo_task_changed);self.side_stack.addWidget(self.photo_navigation)
        self.set_list=QListWidget();self.set_list.currentRowChanged.connect(self.set_list_changed)
        self.file_navigation=QListWidget();self.file_navigation.addItems(['补齐精选 RAW','孤立 RAW','操作记录与恢复'])
        self.file_navigation.currentRowChanged.connect(self.file_task_changed);self.side_stack.addWidget(self.file_navigation)
        self.side_stack.addWidget(self.set_list)
        for navigation in (self.photo_navigation,self.set_list,self.file_navigation):
            navigation.setWordWrap(True);navigation.setTextElideMode(Qt.TextElideMode.ElideNone)
            navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.photo_navigation.setObjectName('primaryNavigation')
        self.group_list.setMinimumWidth(180);self.group_list.setMaximumWidth(200);self.group_list.setWordWrap(True)
        self.group_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.group_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.group_list.parentWidget().setStretchFactor(0,0);self.group_list.parentWidget().setStretchFactor(1,1)
        self.group_list.parentWidget().setSizes([190,1100])
        split.addWidget(self.side_stack);self.tabs.tabBar().hide();split.addWidget(self.tabs);split.setStretchFactor(0,0);split.setStretchFactor(1,1);split.setSizes([136,1160]);layout.addWidget(split,1)
        footer=QHBoxLayout();self.status.setWordWrap(True);self.status.setMinimumWidth(0)
        self.status.setMaximumHeight(self.fontMetrics().lineSpacing()*2+4)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);footer.addWidget(self.status,1)
        self.queue_label=QLabel();footer.addWidget(self.queue_label);footer.addWidget(self.pause_button);footer.addWidget(self.cancel_button);layout.addLayout(footer)
        layout.addWidget(self.progress)
        self.install_gallery_view();self.install_review_view();self.install_quality_view();self.install_file_view();self.install_set_view();self.install_settings()
        self.workspaces.currentChanged.connect(self.workspace_changed)
        self.position_timer=QTimer(self);self.position_timer.setSingleShot(True);self.position_timer.setInterval(600);self.position_timer.timeout.connect(self.flush_work_position)
        self.shortcuts=[]
        for key,fn in {'K':lambda:self.decide_current('keep'),'D':lambda:self.decide_current('delete'),
                       'U':lambda:self.decide_current('pending'),'Ctrl+Z':lambda:self.decide_current('pending'),
                       'Return':self.expand_current,'Escape':self.return_to_grid,
                       'Ctrl+A':self.select_current_page}.items():
            shortcut=QShortcut(QKeySequence(key),self);shortcut.setAutoRepeat(False)
            shortcut.activated.connect(lambda fn=fn:self.shortcut_action(fn));self.shortcuts.append(shortcut)
        self.analyze_button.setParent(body);self.analyze_button.hide()
        self.quality_button.setParent(self.tabs.widget(5));self.quality_button.hide()
        self.pause_button.hide();self.cancel_button.hide();self.progress.hide()

    def install_gallery_view(self):
        page=self.tabs.widget(0);layout=page.layout();layout.removeWidget(self.gallery)
        self.gallery_stack=QStackedWidget();self.gallery_stack.addWidget(self.gallery)
        self.gallery_sequence=PhotoSequence(self,'gallery');self.gallery_stack.addWidget(self.gallery_sequence);layout.insertWidget(1,self.gallery_stack,1)
        modes=QHBoxLayout();self.gallery_mode=QComboBox();self.gallery_mode.addItems(['网格','大图'])
        self.gallery_mode.hide();self.gallery_selection=QLabel();modes.addWidget(self.gallery_selection);modes.addStretch()
        layout.insertLayout(0,modes)
        self.gallery_mode.currentIndexChanged.connect(self.gallery_mode_changed)
        self.gallery.currentItemChanged.connect(lambda *args:self.update_gallery_selection())
        self.gallery.itemSelectionChanged.connect(self.update_gallery_selection)
        self.gallery.itemDoubleClicked.disconnect();self.gallery.itemDoubleClicked.connect(lambda item:self.expand_gallery(item))
        self.filter.currentTextChanged.connect(self.gallery_filter_changed)
        self.filter.setToolTip('照片来源与状态范围；待删复核使用同一份人工决定')
        # Retain the existing batch actions, but place them beside the queue footer.
        actions=layout.takeAt(1);self.gallery_action_layout=actions.layout()
        self.gallery_actions=QWidget();self.gallery_actions.setLayout(self.gallery_action_layout);layout.insertWidget(layout.count()-1,self.gallery_actions)
        self.delete_report=QTextEdit();self.delete_report.setReadOnly(True);self.delete_report.setMaximumHeight(90);self.delete_report.hide();layout.addWidget(self.delete_report)
        self.delete_bar=QWidget();delete_layout=QHBoxLayout(self.delete_bar);delete_layout.setContentsMargins(0,0,0,0);delete_layout.addStretch()
        self.delete_entry=action_button('执行删除',self.execute_delete,delete_layout,'danger')
        self.delete_entry.setToolTip('直接执行：JPG 移至已删除备份 / 当前组；关联 RAW 与 XMP 移至本组 deleteraw。目标不覆盖，保护或配对冲突整项暂缓。')
        layout.addWidget(self.delete_bar);self.delete_bar.hide()
        for button in page.findChildren(QPushButton):
            if '加入当前图集' in button.text():button.setText('加入图集…')

    def install_review_view(self):
        inner=self.compare.parentWidget().layout();inner.removeWidget(self.compare)
        self.review_stack=QStackedWidget();self.review_sequence=PhotoSequence(self,'similar');self.review_stack.addWidget(self.review_sequence);self.review_stack.addWidget(self.compare)
        inner.insertWidget(0,self.review_stack,1)
        modes=QHBoxLayout();modes.addWidget(QLabel('精简强度'));modes.addWidget(self.strength)
        self.review_mode=QComboBox();self.review_mode.addItems(['并排大图','网格']);self.review_mode.hide();modes.addStretch()
        inner.insertLayout(0,modes);self.review_mode.currentIndexChanged.connect(self.review_mode_changed)
        self.review_strip=QListWidget();self.review_strip.setViewMode(QListWidget.ViewMode.IconMode);self.review_strip.setFlow(QListWidget.Flow.LeftToRight)
        self.review_strip.setWrapping(False);self.review_strip.setIconSize(QSize(108,64));self.review_strip.setGridSize(QSize(142,90));self.review_strip.setFixedHeight(108)
        self.review_strip.currentItemChanged.connect(self.review_strip_changed);self.review_strip.hide()
        self.review_thumb_owner=self.thumbnail_loader.new_owner()
        self.adopt_button.setText('采纳本组有效建议')
        self.review_summary.hide()
        for button in self.tabs.widget(1).findChildren(QPushButton):
            if button.text() in {'选中照片留下','选中照片待删'}:button.hide()
        self.compare.itemDoubleClicked.disconnect();self.compare.itemDoubleClicked.connect(lambda item:self.expand_review_asset(item.data(Qt.ItemDataRole.UserRole)))

    def install_quality_view(self):
        layout=self.quality_pane.layout();layout.removeWidget(self.quality_grid)
        self.quality_stack=QStackedWidget();self.quality_sequence=PhotoSequence(self,'quality');self.quality_stack.addWidget(self.quality_sequence);self.quality_stack.addWidget(self.quality_grid)
        layout.insertWidget(2,self.quality_stack,1)
        controls=QHBoxLayout();self.quality_mode=QComboBox();self.quality_mode.addItems(['单张大图','网格']);self.quality_mode.hide()
        self.issue_filter=QComboBox();self.issue_filter.addItems(['全部问题','模糊 / 虚焦','细节偏软','曝光异常','运动模糊 / 手抖']);controls.addWidget(self.issue_filter)
        controls.addStretch();layout.insertLayout(1,controls)
        self.quality_mode.currentIndexChanged.connect(self.quality_mode_changed);self.issue_filter.currentIndexChanged.connect(self.reset_quality_page)
        self.quality_grid.itemDoubleClicked.disconnect();self.quality_grid.itemDoubleClicked.connect(lambda item:self.expand_quality(item))
        self.quality_grid.currentItemChanged.connect(lambda *args:self.update_quality_sequence())
        for button in self.quality_pane.findChildren(QPushButton):
            if button.text() in {'选中保留','选中待删','撤销决定'}:button.hide()

    def install_file_view(self):
        self.include_raw.setChecked(True);self.include_raw.hide()
        pane=self.tabs.widget(2);layout=pane.layout()
        for widget in pane.findChildren(QWidget):
            if widget is not self.journal and not self.journal.isAncestorOf(widget):widget.hide()
        while layout.count():layout.takeAt(0)
        self.file_stack=QStackedWidget();layout.addWidget(self.file_stack);self.file_panels={}
        for kind,title,note,execute_text in (
            ('fill_raw','补齐精选 RAW','补齐缺少的 RAW/XMP，同时将精选/raw 内无对应精选 JPG 的 RAW/XMP 移至本组 deleteraw。','执行补齐与清理'),
            ('orphan','孤立 RAW','同时检查普通与精选 JPG；两处均无对应 JPG 且不受保护的 RAW，移入当前组的 deleteraw。','移入 deleteraw')):
            panel=FilePlanPanel(self,kind,title,note,execute_text);self.file_panels[kind]=panel;self.file_stack.addWidget(panel)
        page=QWidget();inner=QVBoxLayout(page);intro=QLabel('操作记录与恢复 · 每项记录真实结果；移动可恢复，复制的文件不会通过恢复删除。');intro.setWordWrap(True);inner.addWidget(intro)
        inner.addWidget(self.journal,1);self.journal.show();bar=QHBoxLayout();action_button('恢复选中的移动项目',self.restore_item,bar);bar.addStretch();inner.addLayout(bar);self.file_stack.addWidget(page)

    def install_set_view(self):
        page=self.tabs.widget(3);layout=page.layout()
        for button in page.findChildren(QPushButton):
            if button.text()=='自动补齐 / 重做未锁定项':button.hide()
            if button.text()=='新建图集':button.setText('手动新建')
        self.set_picker.hide()
        main=QHBoxLayout();self.generate_button=action_button('自动生成图集',self.generate_new_set,main,'primary')
        self.fill_set_button=action_button('自动补齐空位',lambda:self.generate_set_mode(False),main)
        self.redo_set_button=action_button('重做未锁定项',lambda:self.generate_set_mode(True),main)
        self.set_summary=QLabel();main.addWidget(self.set_summary,1);layout.insertLayout(0,main)
        body=QSplitter();layout.removeWidget(self.set_grid);body.addWidget(self.set_grid)
        candidates=QWidget();candidate_layout=QVBoxLayout(candidates)
        candidate_layout.addWidget(QLabel('就地选图 · 手动指定后锁定'))
        self.set_candidate_filter=QComboBox();self.set_candidate_filter.addItems(['可用照片','成片封面','普通照片']);candidate_layout.addWidget(self.set_candidate_filter)
        self.set_candidates=QListWidget();self.set_candidates.setIconSize(QSize(100,70));candidate_layout.addWidget(self.set_candidates,1)
        self.set_candidate_page=0;bar=QHBoxLayout();action_button('上一页',lambda:self.page_set_candidates(-1),bar);action_button('下一页',lambda:self.page_set_candidates(1),bar);candidate_layout.addLayout(bar)
        self.set_candidate_info=QLabel();self.set_candidate_info.setWordWrap(True);candidate_layout.addWidget(self.set_candidate_info)
        action_button('放到当前空位 / 替换',self.use_set_candidate,candidate_layout)
        action_button('查看候选大图',self.preview_set_candidate,candidate_layout)
        body.addWidget(candidates);body.setSizes([780,290]);layout.insertWidget(2,body,1)
        self.set_candidate_filter.currentIndexChanged.connect(lambda:self.refresh_set_candidates(reset=True))
        self.set_candidates.itemDoubleClicked.connect(lambda item:self.preview_set_candidate())
        self.set_grid.currentItemChanged.connect(lambda *args:self.update_set_slot_info())
        self.set_grid.itemDoubleClicked.disconnect();self.set_grid.itemDoubleClicked.connect(self.preview_set_slot)
        self.set_candidate_thumb_owner=self.thumbnail_loader.new_owner()
        self.set_count.valueChanged.connect(self.set_options_changed);self.second_cover.toggled.connect(self.set_options_changed)
        self.set_count.setMinimumWidth(65);self.current_set_view=None
        self.export_panel=FilePlanPanel(self,'export','图集导出','将当前图集按顺序复制到本组的 图集/图集名称，不修改原片。','执行导出')
        self.export_panel.setMaximumHeight(250);self.export_panel.hide();layout.addWidget(self.export_panel)

    def install_settings(self):
        learning=self.tabs.widget(4);self.tabs.removeTab(4);self.tabs.insertTab(4,QWidget(),'设置')
        self.settings_dialog=QDialog(self);self.settings_dialog.setWindowTitle(f'{APP_NAME} 设置');self.settings_dialog.resize(850,700)
        layout=QVBoxLayout(self.settings_dialog);layout.addWidget(self.workspace_button);self.workspace_button.show()
        self.auto_advance=QCheckBox('连续大图标记后看下一张（不自动跳组）');self.auto_advance.setChecked(self.settings.get('auto_advance',False))
        self.auto_advance.hide()
        layout.addWidget(learning,1);learning.show();self.learning_page=learning
        self.learning_refresh_timer=QTimer(self);self.learning_refresh_timer.setSingleShot(True);self.learning_refresh_timer.setInterval(250)
        self.learning_refresh_timer.timeout.connect(self.refresh_learning)
        self.workspace_button.setToolTip('迁移说明见 README 和 docs/MIGRATION.md；状态、模型和缓存保存在 local。')
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close);buttons.rejected.connect(self.settings_dialog.hide);layout.addWidget(buttons)

    def open_settings(self):self.refresh_learning();self.settings_dialog.show();self.settings_dialog.raise_()

    def open_learning_details(self):self.open_settings()

    def workspace_changed(self,index):
        self.side_stack.setCurrentIndex(index)
        if index==0:
            row=next((i for i in range(self.photo_navigation.count()) if self.photo_navigation.item(i).data(Qt.ItemDataRole.UserRole)==self.last_photo_task),0)
            self.photo_navigation.setCurrentRow(row);self.photo_task_changed(row)
        elif index==1:
            self.tabs.setCurrentIndex(2);self.file_navigation.setCurrentRow(max(0,self.file_navigation.currentRow()))
        else:self.tabs.setCurrentIndex(3);self.refresh_sets()
        self.sync_file_results()
        self.load_visible_thumbnails();self.remember_work_position()

    def photo_task_changed(self,row):
        if row<0:return
        task=self.photo_navigation.item(row).data(Qt.ItemDataRole.UserRole);previous=self.last_photo_task;self.last_photo_task=task
        if task!=previous:
            for grid in (self.gallery,self.compare,self.quality_grid):grid.clearSelection()
        if task=='similar':self.tabs.setCurrentIndex(1);self.update_review_sequence()
        elif task=='quality':self.tabs.setCurrentIndex(5);self.update_quality_sequence()
        else:
            mode={'ordinary':'普通 JPG','pending':'待删 JPG','protected':'受保护图片'}[task]
            if task=='pending' and previous!='pending':self.pending_queue_ids=None
            self.filter.setCurrentText(mode);self.tabs.setCurrentIndex(0)
            if task=='pending' and previous!='pending':self.refresh_gallery()
            self.update_gallery_actions(task=='pending')
            self.filter.setVisible(task=='protected')
            self.gallery_mode.blockSignals(True);self.gallery_mode.setCurrentIndex(0);self.gallery_mode.blockSignals(False)
            self.gallery_mode_changed(self.gallery_mode.currentIndex())
        self.remember_work_position()

    def file_task_changed(self,row):
        self.tabs.setCurrentIndex(2)
        self.file_stack.setCurrentIndex(max(0,row))
        if row==2:self.refresh_journal();self.journal.setFocus()
        if self.shell_ready:self.sync_file_results()

    def refresh_journal(self):
        context=(str(self.store.path),self.album)
        same=getattr(self,'history_context',None)==context
        if not same:self.history_limit=300
        selected=self.journal.item(self.journal.currentRow(),0) if same and self.journal.currentRow()>=0 else None
        selected_id=selected.text() if selected else None;scroll=self.journal.verticalScrollBar().value() if same else 0
        self.history_context=context
        with self.store.connect() as db:
            totals={row['action']:row['n'] for row in db.execute('SELECT j.action,COUNT(*) AS n FROM journal j JOIN plans p ON p.id=j.plan_id WHERE p.album=? GROUP BY j.action',(self.album,))}
            rows=[dict(row) for row in db.execute('SELECT j.* FROM journal j JOIN plans p ON p.id=j.plan_id WHERE p.album=? ORDER BY j.id DESC LIMIT ?',(self.album,self.history_limit))]
        self.journal.blockSignals(True);self.journal.setUpdatesEnabled(False);self.journal.setRowCount(len(rows))
        states={'done':'已完成','restored':'已恢复','intent':'待核对','failed':'未完成','restoring':'恢复中'}
        current=-1
        for index,row in enumerate(rows):
            if str(row['id'])==selected_id:current=index
            for col,key in enumerate(('id','state','action','src','dst')):
                value=states.get(row[key],row[key]) if key=='state' else {'move':'移动','copy':'复制'}.get(row[key],row[key]) if key=='action' else str(row[key])
                item=QTableWidgetItem(value);item.setToolTip(value+(('\n'+row['error']) if row.get('error') else ''))
                if col==0:item.setData(Qt.ItemDataRole.UserRole,row)
                self.journal.setItem(index,col,item)
        self.journal.setCurrentCell(current,0);self.journal.setUpdatesEnabled(True);self.journal.blockSignals(False)
        self.journal.verticalScrollBar().setValue(scroll)
        if hasattr(self,'history_summary'):
            total=sum(totals.values());self.history_summary.setText(f'{self.album or "未选择照片组"} · 已显示 {len(rows)}/{total} 条 · 移动 {totals.get("move",0)} · 复制 {totals.get("copy",0)}')
            self.history_more.setVisible(len(rows)<total);self.history_stack.setCurrentIndex(0 if rows else 1)
            self.update_history_controls()

    def load_older_history(self):
        self.history_limit+=300;self.refresh_journal()

    def update_history_controls(self):
        if not hasattr(self,'history_restore'):return
        item=self.journal.item(self.journal.currentRow(),0) if self.journal.currentRow()>=0 else None
        row=item.data(Qt.ItemDataRole.UserRole) if item else None
        self.history_restore.setEnabled(bool(row and row['action']=='move' and row['state'] in {'done','intent','failed'}) and not self.album_is_moving())

    def set_list_changed(self,row):
        item=self.set_list.item(row)
        if item:self.set_picker.setCurrentIndex(self.set_picker.findData(item.data(Qt.ItemDataRole.UserRole)))

    def refresh(self):
        if self.shell_ready and not getattr(self,'_keep_pending_queue',False):self.pending_queue_ids=None
        self.analysis_report_ready=bool(self.store.one('SELECT 1 AS found FROM analysis_runs WHERE album=?',(self.album,))) if self.album else False
        result=super().refresh()
        if self.shell_ready:
            self.update_workspace_counts();self.update_gallery_sequence();self.update_review_sequence();self.update_quality_sequence()
        return result

    def refresh_summary(self):
        super().refresh_summary()
        if self.shell_ready:self.update_workspace_counts()

    def update_workspace_counts(self):
        if not self.shell_ready:return
        photos=[a for a in self.assets if a['role']=='ordinary_jpeg'];ordinary={a['id'] for a in photos};protected={a['id'] for a in photos if a['protected']}
        failed={a['id'] for a in photos if __import__('json').loads(a['metadata']).get('read_error')}
        similar={aid for group in self.group_data for aid in group_members(group) if aid in ordinary and aid not in protected}
        similar_refs={aid for group in self.group_data for aid in group_members(group) if aid in protected}
        quality={row['id'] for row in self.quality_results};pending={a['id'] for a in photos if a['decision']=='delete'}
        analyzed=bool(getattr(self,'analysis_report_ready',False))
        screened=bool(getattr(self,'quality_report_ready',False))
        union=str(len(similar|quality))+' 张' if analyzed and screened else '部分结果' if analyzed or screened else '尚未分析'
        self.summary.setText(f'{self.album or "选择拍摄组"}  ·  普通 JPG {len(photos)} 张  ·  保护 {len(protected)} 张  ·  可精简 {len(ordinary-protected-failed)} 张  ·  候选并集 {union}  ·  待删 {len(pending)} 张')
        labels=[f'普通照片\n{len(photos)} 张' if self.assets else '普通照片\n未扫描',f'相似复核\n{len(similar)} 张 · {len(self.group_data)} 组' if analyzed else '相似复核\n尚未分析',f'废片筛查\n{len(quality)} 张' if screened else '废片筛查\n尚未检测',f'待删复核\n{len(pending)} 张',f'精选与成片\n{sum(a["role"] in {"selected_jpeg","finished"} for a in self.assets)} 张']
        for row,label in enumerate(labels):
            item=self.photo_navigation.item(row);item.setText(label);item.setToolTip(label.replace('\n',' · '))
            item.setSizeHint(QSize(110,self.photo_navigation.fontMetrics().lineSpacing()*2+18))
        self.summary.setToolTip(f'相似与废片按照片去重合并；只读保护参照 {len(similar_refs)} 张另计。无法读取 {len(failed)} 张，不计入可精简数。\n'+ '\n'.join(a['path'] for a in photos if a['id'] in failed))
        self.delete_entry.setEnabled(bool(pending) and not self.album_is_moving() and not self.operation_pending('delete'))

    def gallery_assets(self):
        if self.shell_ready and self.filter.currentText()=='待删 JPG':
            if self.pending_queue_ids is None:self.pending_queue_ids=[a['id'] for a in self.assets if a['role']=='ordinary_jpeg' and a['decision']=='delete']
            known=set(self.pending_queue_ids)
            self.pending_queue_ids.extend(a['id'] for a in self.assets if a['role']=='ordinary_jpeg' and a['decision']=='delete' and a['id'] not in known)
            return [self.asset_by_id[aid] for aid in self.pending_queue_ids if aid in self.asset_by_id]
        return super().gallery_assets()

    def gallery_label(self,asset):
        text=super().gallery_label(asset)
        if self.shell_ready and self.filter.currentText()=='待删 JPG' and asset['decision']!='delete':text+='\n本轮已救回' if asset['decision']=='keep' else '\n本轮已撤销'
        return text

    def gallery_filter_changed(self,text):
        if not self.shell_ready:return
        if text=='待删 JPG':self.pending_queue_ids=None
        self.update_gallery_actions(text=='待删 JPG');self.update_gallery_sequence()

    def update_gallery_actions(self,pending):
        self.gallery_actions.setVisible(not pending);self.delete_bar.setVisible(pending)
        self.delete_entry.setVisible(pending)
        if not pending:self.delete_report.hide()
        elif self.shell_ready:self.sync_file_results()

    def gallery_mode_changed(self,index):
        if index==0 and self.shell_ready and self.gallery_stack.currentIndex()==1:self.position_grid(self.gallery,self.gallery_sequence.current_id,60)
        self.gallery_stack.setCurrentIndex(index)
        if self.shell_ready:self.save_setting('view_'+self.last_photo_task,self.gallery_mode.currentText())
        for i in range(self.gallery_action_layout.count()):
            widget=self.gallery_action_layout.itemAt(i).widget()
            if isinstance(widget,QPushButton) and widget.text() in ('保留','标记待删','撤销决定'):widget.setVisible(index==0)
        if index:self.gallery.clearSelection();self.update_gallery_sequence()
        self.load_visible_thumbnails()

    def update_gallery_selection(self):
        self.gallery_selection.setText(f'已选 {len(self.gallery.selectedItems())} 张')
        if self.gallery_stack.currentIndex():self.update_gallery_sequence()

    def update_gallery_sequence(self):
        if not self.shell_ready:return
        item=self.gallery.currentItem();current=item.data(Qt.ItemDataRole.UserRole) if item else self.gallery_sequence.current_id
        self.gallery_sequence.set_queue([a['id'] for a in self.gallery_assets()],current)

    def expand_gallery(self,item):
        self.gallery.setCurrentItem(item);self.preview(item)

    def render_review_page(self):
        super().render_review_page()
        if self.shell_ready:
            for card in self.review_cards.values():
                card.previewRequested.disconnect();card.previewRequested.connect(self.expand_review_asset)
            self.refresh_review_strip();self.update_review_sequence()

    def expand_review_asset(self,aid):
        self.open_review_preview(aid)

    def select_group(self,index):
        self.review_reference=None;self.review_candidate=None
        super().select_group(index)

    def refresh_review_strip(self):
        self.review_strip.blockSignals(True);self.review_strip.clear();group=self.current_group()
        if group:
            for aid in group_members(group)[self.review_page*24:(self.review_page+1)*24]:
                asset=self.asset_by_id.get(aid)
                if asset:
                    item=QListWidgetItem(self.icon(asset),Path(asset['path']).name+(' · 保护' if asset['protected'] else ''))
                    item.setData(Qt.ItemDataRole.UserRole,aid);self.review_strip.addItem(item)
        self.review_strip.blockSignals(False);self.load_visible_thumbnails()

    def update_review_sequence(self):
        if not self.shell_ready:return
        group=self.current_group()
        if not group:self.review_sequence.set_queue([]);return
        members=[aid for aid in group_members(group) if aid in self.asset_by_id]
        if self.review_reference not in members:self.review_reference=group['keep'] if group['keep'] in members else next(iter(members),None)
        candidates=[aid for aid in members if aid!=self.review_reference]
        self.review_sequence.set_queue(candidates,self.review_candidate,self.review_reference)
        self.review_candidate=self.review_sequence.current_id
        protected=sum(self.asset_by_id[aid]['protected'] for aid in members)
        pending=sum(self.asset_by_id[aid]['decision']=='delete' for aid in members)
        self.review_page_label.setText(f'当前组 {len(members)} 张 · 保护参照 {protected} 张 · 待删 {pending} 张 · 组内第 {self.review_page+1}/{max(1,(len(members)+23)//24)} 页')

    def review_strip_changed(self,current,previous):
        if not current:return
        aid=current.data(Qt.ItemDataRole.UserRole)
        if aid==self.review_reference:
            group=self.current_group();self.review_reference=next((x for x in group_members(group) if x!=aid),None)
        self.review_candidate=aid;self.update_review_sequence();self.remember_work_position()

    def set_review_reference(self):
        current=self.review_sequence.current_id
        if current:self.review_reference=current;self.review_candidate=None;self.update_review_sequence();self.refresh_review_strip()

    def review_mode_changed(self,index):
        if index==1 and self.shell_ready:self.position_grid(self.compare,self.review_sequence.current_id,24)
        self.review_stack.setCurrentIndex(1);self.review_strip.hide()
        self.review_summary.setVisible(index==1)
        for button in self.tabs.widget(1).findChildren(QPushButton):
            if button.text() in {'选中照片留下','选中照片待删'}:button.setVisible(index==1)
        self.save_setting('view_similar','grid' if index else 'compare');self.update_review_sequence()

    def filtered_quality(self):
        rows=super().filtered_quality()
        if not self.shell_ready:return rows
        choice=self.issue_filter.currentIndex()
        if not choice:return rows
        words={1:('模糊','虚焦'),2:('软','细节'),3:('曝光','过曝','过暗'),4:('运动','手抖','拖影')}[choice]
        return [row for row in rows if any(word in ' '.join(issue['label'] for issue in row['issues']) for word in words)]

    def refresh_quality_grid(self):
        super().refresh_quality_grid()
        if self.shell_ready:self.update_quality_sequence()

    def refresh_quality(self):
        self.quality_report_ready=bool(self.store.quality_report(self.album)) if self.album else False
        return super().refresh_quality()

    def update_quality_sequence(self):
        if not self.shell_ready:return
        item=self.quality_grid.currentItem();current=item.data(Qt.ItemDataRole.UserRole) if item else self.quality_sequence.current_id
        self.quality_sequence.set_queue([row['id'] for row in self.filtered_quality()],current,self.quality_sequence.reference_id)

    def quality_mode_changed(self,index):
        if index==1 and self.shell_ready:self.position_grid(self.quality_grid,self.quality_sequence.current_id,24)
        self.quality_stack.setCurrentIndex(index);self.save_setting('view_quality','grid' if index else 'single');self.update_quality_sequence();self.load_visible_thumbnails()
        for button in self.quality_pane.findChildren(QPushButton):
            if button.text() in {'选中保留','选中待删','撤销决定'}:button.setVisible(index==1)

    def expand_quality(self,item):self.quality_grid.setCurrentItem(item);self.quality_preview(item)

    def choose_quality_reference(self):
        assets=[asset for asset in self.assets if asset['role']=='ordinary_jpeg' and asset['id']!=self.quality_sequence.current_id]
        choice,ok=QInputDialog.getItem(self,'临时比较参照','只作为比较，不改变留删决定',[a['path'] for a in assets],0,False)
        if ok:self.quality_sequence.reference_id=next(a['id'] for a in assets if a['path']==choice);self.quality_sequence.render()

    def clear_quality_reference(self):self.quality_sequence.reference_id=None;self.quality_sequence.render()

    def photo_evidence(self,aid,kind):
        asset=self.asset_by_id.get(aid)
        if not asset:return '当前范围没有照片；可以切换任务或扫描分析。'
        if self.photo_is_locked(asset):return '当前逻辑照片正在执行文件操作，暂时不能改动；其他照片仍可浏览。'
        if kind=='quality':
            result=self.quality_entries.get(aid)
            return '；'.join(i['detail'] for i in result['issues']) if result else '技术问题仅作提示，由你确认。'
        if kind=='similar':
            group=self.current_group();suggestion=next((m for m in group['candidates'] if m['id']==aid),None) if group else None
            if suggestion:
                reference=self.asset_by_id.get(suggestion.get('reference',group['keep']))
                return 'AI 建议可精简 · 严格匹配参照：'+(Path(reference['path']).name if reference else '参照已不可用')+'。人工决定优先。'
            return '同组构图关系，未给出直接删减建议；请自行比较。'
        if self.filter.currentText()=='待删 JPG' and asset['decision']!='delete':return '这张已从实时待删数中移出，仍留在本轮浏览队列，便于返回检查。'
        hints=[]
        if aid in getattr(self,'quality_entries',{}):hints.append('技术提示：'+'；'.join(i['detail'] for i in self.quality_entries[aid]['issues']))
        if aid in self.group_rows:hints.append('出现在相似复核组中；需比较直接参照后决定')
        if asset['protected']:
            sources=[a['path'] for a in self.assets if a['logical_key']==asset['logical_key'] and a['role'] in {'selected_jpeg','finished'}]
            hints.append('精选/关联保护：'+'、'.join(sources))
        else:hints.append('保留 / 待删仅记录状态，执行删除时才移动至备份。')
        return '\n'.join(hints)

    def sequence_decide(self,sequence,state):
        aid=sequence.current_id
        context=member_context(self.current_group(),aid) if sequence.kind=='similar' and self.current_group() else 'quality:standalone' if sequence.kind=='quality' else 'manual'
        try:self.apply_decisions({aid:state},context)
        except (ValueError,OSError) as exc:self.show_error(exc);return
        sequence.render()
        if state!='pending' and self.settings.get('auto_advance',False):sequence.navigate(1)

    def sequence_position_changed(self,kind,aid):
        if kind=='similar':self.review_candidate=aid
        self.remember_work_position()

    def active_sequence(self):
        return None

    def shortcut_action(self,fn):
        if QApplication.activeModalWidget():return
        if isinstance(QApplication.focusWidget(),(QLineEdit,QTextEdit,QAbstractSpinBox,QComboBox)):return
        fn()

    def decide_current(self,state):
        sequence=self.active_sequence()
        if sequence:sequence.decide(state)
        elif self.tabs.currentIndex()==0:self.gallery_decide(state)
        elif self.tabs.currentIndex()==1:self.review_decide(state)
        elif self.tabs.currentIndex()==5:self.quality_decide(state)

    def navigate_current(self,delta):
        sequence=self.active_sequence()
        if sequence:sequence.navigate(delta)

    def expand_current(self):
        if self.tabs.currentIndex()==0 and self.gallery.currentItem():self.expand_gallery(self.gallery.currentItem())
        elif self.tabs.currentIndex()==5 and self.quality_grid.currentItem():self.expand_quality(self.quality_grid.currentItem())

    def return_to_grid(self):
        if self.tabs.currentIndex()==0:self.gallery_mode.setCurrentText('网格')
        elif self.tabs.currentIndex()==1:self.review_mode.setCurrentIndex(1)
        elif self.tabs.currentIndex()==5:self.quality_mode.setCurrentIndex(1)

    def position_grid(self,grid,aid,size):
        if aid is None:return
        if grid is self.gallery:
            ids=[a['id'] for a in self.gallery_assets()]
            if aid in ids:self.page=ids.index(aid)//size;self.refresh_gallery()
        elif grid is self.compare:
            ids=group_members(self.current_group()) if self.current_group() else []
            if aid in ids:self.review_page=ids.index(aid)//size;self.render_review_page()
        else:
            ids=[r['id'] for r in self.filtered_quality()]
            if aid in ids:self.quality_page=ids.index(aid)//size;self.refresh_quality_grid()
        for row in range(grid.count()):
            if grid.item(row).data(Qt.ItemDataRole.UserRole)==aid:
                grid.setCurrentRow(row);grid.scrollToItem(grid.item(row));break

    def review_decide(self,state):
        if self.shell_ready and self.active_sequence() is self.review_sequence:self.review_sequence.decide(state)
        else:super().review_decide(state)

    def quality_decide(self,state):
        if self.shell_ready and self.active_sequence() is self.quality_sequence:self.quality_sequence.decide(state)
        else:super().quality_decide(state)

    def select_current_page(self):
        if self.active_sequence():return
        grid={0:self.gallery,1:self.compare,5:self.quality_grid}.get(self.tabs.currentIndex())
        if grid:grid.selectAll()

    def navigate_group(self,delta):
        row=self.group_list.currentRow()+delta
        if self.tabs.currentIndex()==1 and 0<=row<self.group_list.count():self.group_list.setCurrentRow(row)

    def remember_work_position(self):
        if not self.shell_ready or not self.album:return
        positions=self.settings.setdefault('ui_positions',{})
        positions[self.store.path.stem+'|'+self.album]={'workspace':('photos','files','sets')[self.workspaces.currentIndex()],'task':self.last_photo_task,'page':self.page,
            'group':(self.current_group() or {}).get('id'),'candidate':self.review_sequence.current_id,'gallery':self.gallery_sequence.current_id}
        self.position_timer.start()

    def flush_work_position(self):
        try:self.runtime.save_settings(self.settings)
        except OSError:__import__('logging').exception('工作位置暂未保存')

    def restore_work_position(self):
        position=self.settings.get('ui_positions',{}).get(self.store.path.stem+'|'+str(self.album),{})
        self.last_photo_task=position.get('task','ordinary')
        workspace=position.get('workspace','photos')
        # Read positions saved before the file-tools tab moved to second place.
        if isinstance(workspace,int):workspace={0:'photos',1:'sets',2:'files'}.get(workspace,'photos')
        self.workspaces.setCurrentIndex({'photos':0,'files':1,'sets':2}.get(workspace,0));self.workspace_changed(self.workspaces.currentIndex())
        self.page=position.get('page',0);self.refresh_gallery()
        for row,group in enumerate(self.group_data):
            if group['id']==position.get('group'):self.group_list.setCurrentRow(row);break
        if position.get('candidate'):self.review_candidate=position['candidate'];self.update_review_sequence()
        self.gallery_sequence.set_queue([a['id'] for a in self.gallery_assets()],position.get('gallery'))

    def select_album(self,album):
        if self.shell_ready:self.remember_work_position()
        self.pending_queue_ids=None;self.deferred_results=False
        if self.shell_ready:self.new_results_button.hide()
        super().select_album(album)
        if self.shell_ready:self.restore_work_position();self.update_busy_controls()

    def tab_changed(self,index):
        if not self.shell_ready:return super().tab_changed(index)
        if index==0 and self.gallery_dirty:
            self.pending_queue_ids=None;self.refresh_gallery()
        self.load_visible_thumbnails()

    def load_visible_thumbnails(self):
        if not self.shell_ready:return super().load_visible_thumbnails()
        grids=[(self.gallery,self.gallery_thumb_owner),(self.set_grid,self.set_thumb_owner),(self.quality_grid,self.quality_thumb_owner),
               (self.review_strip,self.review_thumb_owner),(self.set_candidates,self.set_candidate_thumb_owner)]
        for grid,owner in grids:
            self.thumbnail_loader.cancel(owner)
            if not grid.isVisible():continue
            for row in range(grid.count()):
                item=grid.item(row);asset=self.asset_by_id.get(item.data(Qt.ItemDataRole.UserRole))
                if asset:self.thumbnail_loader.request(self.runtime,self.root,asset,owner,lambda pix,item=item:item.setIcon(QIcon(pix)))

    def decisions_changed(self,decisions,source='manual'):
        if not self.shell_ready:return super().decisions_changed(decisions,source)
        if source!='refresh' and hasattr(self,'secondary_resolved_ids'):self.secondary_resolved_ids.update(decisions)
        if source!='refresh':self.auto_learning.changed(len(decisions),source)
        if self.settings_dialog.isVisible():self.learning_refresh_timer.start()
        for aid,state in decisions.items():
            asset=self.asset_by_id.get(aid)
            if asset:
                asset['decision']=state
                if aid in self.review_cards:self.review_cards[aid].update_state(asset)
        for row in {row for aid in decisions for row in self.group_rows.get(aid,[])}:self.group_list.item(row).setText(self.group_label(self.group_data[row]))
        for grid in (self.gallery,self.quality_grid):
            for row in range(grid.count()):
                item=grid.item(row);aid=item.data(Qt.ItemDataRole.UserRole)
                if aid in decisions:item.setText(self.gallery_label(self.asset_by_id[aid]) if grid is self.gallery else self.quality_label(self.quality_entries[aid]))
        self.update_review_summary();self.update_workspace_counts()
        group=self.current_group()
        if group:
            members=[self.asset_by_id[aid] for aid in group_members(group) if aid in self.asset_by_id]
            self.review_page_label.setText(f'当前组 {len(members)} 张 · 保护参照 {sum(a["protected"] for a in members)} 张 · 待删 {sum(a["decision"]=="delete" for a in members)} 张 · 组内第 {self.review_page+1}/{max(1,(len(members)+23)//24)} 页')
        for sequence in (self.gallery_sequence,self.review_sequence,self.quality_sequence):
            if sequence.isVisible() and sequence.current_id in decisions:sequence.render()
        if self.filter.currentText()=='待删 JPG' and self.tabs.currentIndex()!=0:self.gallery_dirty=True

    def apply_decisions(self,decisions,context,source='manual'):
        locked=[aid for aid in decisions if self.asset_by_id.get(aid) and self.photo_is_locked(self.asset_by_id[aid])]
        if locked:raise ValueError('这些照片正在执行文件操作，完成后可继续决定')
        return super().apply_decisions(decisions,context,source)

    def start_secondary_screening(self):
        if not self.album:return
        context=self.current_context();store,runtime,root,album=(context[k] for k in ('store','runtime','root','album'))
        context['origin_photo_view']=self.photo_navigation.currentRow() if self.workspaces.currentIndex()==0 else None
        settings=dict(self.settings,screening_mode=SECONDARY)
        def work(progress,control):
            scanned=Scanner(store,root).scan(root/album,progress,control.cancel,control)
            if not scanned['complete']:return {**scanned,'cancelled':control.cancel.is_set()}
            if not control.checkpoint():return {'cancelled':True}
            from .analysis import analyze
            return analyze(store,runtime,root,album,settings,progress,control)
        self.run_task(work,self.secondary_screening_finished,kind='高强度二次筛查',context=context)

    def secondary_screening_finished(self,result):
        if result.get('cancelled'):
            self.status.setText('已取消高强度二次筛查；已有留删标签不变。');return
        if result.get('complete') is False:
            self.status.setText('扫描未完整完成，未开始高强度二次筛查。');return
        self.progress.setValue(100)
        self.status.setText(f"高强度二次筛查完成 · 相似候选 {result['candidates']} 张 · 疑似废片 {result.get('quality_candidates',0)} 张 · 原标签未改变")
        origin=(self.completion_context or {}).get('origin_photo_view')
        if self.context_matches(self.completion_context) and self.workspaces.currentIndex()==0 and origin in {0,1} and self.photo_navigation.currentRow()==origin:
            self.workspaces.setCurrentIndex(0)
            self.photo_navigation.setCurrentRow(1 if result.get('groups') else 2)

    def learning_updated(self,message):
        if not self.shell_ready:return super().learning_updated(message)
        self.update_learning_activity()
        self.learning_panel.last_result.setToolTip(message)
        if not self.auto_learning.closed and (self.settings_dialog.isVisible() or self.auto_learning.process is None):self.learning_refresh_timer.start()

    def update_learning_activity(self):
        if not self.shell_ready:return
        activity=self.auto_learning.activity()
        if activity['code']=='collecting' and getattr(self,'learning_snapshot',{}).get('enabled'):activity={**activity,'label':'已启用'}
        self.learning_status.setText('偏好 · '+activity['label']);self.learning_status.setToolTip(activity['message']+'\n点击查看学习记录、验证原因和成果。')
        self.learning_panel.set_activity(activity)

    def closeEvent(self,event):
        if self.shell_ready:
            if hasattr(self,'scroll_updates'):self.scroll_updates.timer.stop()
            self.learning_refresh_timer.stop()
            self.task_queue.clear();self.position_timer.stop();self.remember_work_position();self.position_timer.stop();self.flush_work_position()
            for owner in (self.review_thumb_owner,self.set_candidate_thumb_owner):self.thumbnail_loader.cancel(owner)
            for dialog in self.findChildren(PlanDialog):self.thumbnail_loader.cancel(dialog.thumb_owner)
            for card in self.findChildren(PhotoCard):
                if card.thumb_owner is not None:self.thumbnail_loader.cancel(card.thumb_owner)
        super().closeEvent(event)
        if event.isAccepted():self.thumbnail_loader.pool.waitForDone(1000)

    def current_context(self):
        return dict(store=self.store,runtime=self.runtime,root=self.root,album=self.album)

    def context_matches(self,context):
        return bool(context and context['store'].path==self.store.path and context['root']==self.root and context['album']==self.album)

    def run_task(self,fn,callback=None,*,kind=None,locked_ids=(),set_id=None,context=None):
        if not self.shell_ready:return super().run_task(fn,callback)
        context=dict(context or self.current_context())
        name=getattr(callback,'__name__','')
        kind=kind or {'scan_finished':'扫描并分析','quality_finished':'废片筛查'}.get(name,'重新分析')
        context.update(kind=kind,locked_ids=set(locked_ids),set_id=set_id)
        context['locked_keys']={a['logical_key'] for a in context['store'].rows('SELECT id,logical_key FROM assets WHERE album=?',(context['album'],)) if a['id'] in context['locked_ids']} if context['locked_ids'] else set()
        request=(fn,callback,context)
        if self.thread is not None:
            key=(str(context['store'].path),context['album'],kind,set_id)
            if kind not in {'执行文件清单','恢复文件','手动指定图集照片','加入图集'}:
                self.task_queue=deque(item for item in self.task_queue if (str(item[2]['store'].path),item[2]['album'],item[2]['kind'],item[2]['set_id'])!=key)
            if len(self.task_queue)>=4:
                self.queue_label.setText('队列已满（4 项），请完成或取消后重试');return False
            self.task_queue.append(request);self.update_busy_controls();return True
        self.start_request(request);return True

    def start_request(self,request):
        fn,callback,self.task_context=request
        self.status.setText(f"{self.task_context['album']} · {self.task_context['kind']} · 正在开始")
        self.status.setToolTip(str(self.task_context['root']));self.progress.setRange(0,0)
        super().run_task(fn,callback)

    def set_busy(self,busy):
        if not self.shell_ready:return super().set_busy(busy)
        if busy:self.auto_learning.suspend()
        else:self.auto_learning.resume()
        # Read work never disables the workspace or the photo decisions.
        self.tabs.setEnabled(True);self.album_picker.setEnabled(True);self.workspace_button.setEnabled(True)
        self.scan_button.setEnabled(True);self.analyze_button.setEnabled(True);self.quality_button.setEnabled(True);self.strength.setEnabled(True)
        self.pause_button.setVisible(busy);self.cancel_button.setVisible(busy);self.progress.setVisible(busy)
        self.pause_button.setEnabled(busy);self.cancel_button.setEnabled(busy)
        self.update_busy_controls();QTimer.singleShot(0,self.load_visible_thumbnails)

    def update_busy_controls(self):
        if not self.shell_ready:return
        self.queue_label.setText(f'排队 {len(self.task_queue)} 项' if self.task_queue else '')
        self.sync_file_results()
        self.update_workspace_counts()
        for sequence in (self.gallery_sequence,self.review_sequence,self.quality_sequence):
            if sequence.isVisible():sequence.render()
        set_busy=self.set_is_busy(self.selected_set())
        self.set_count.setEnabled(not set_busy);self.second_cover.setEnabled(not set_busy)
        self.fill_set_button.setEnabled(bool(self.selected_set()) and not set_busy);self.redo_set_button.setEnabled(bool(self.selected_set()) and not set_busy)
        if hasattr(self,'style_set_button'):self.style_set_button.setEnabled(bool(self.selected_set()) and not set_busy and not self.style_set_pending(self.selected_set()))

    def style_set_pending(self,set_id):
        return bool(set_id and any(c.get('set_id')==set_id and c['kind']=='图集调色导出' and self.context_matches(c) for _,_,c in self.task_queue))

    def style_current_set(self):
        from .xmp_style import XmpStyle
        from PySide6.QtWidgets import QFileDialog
        set_id=self.selected_set()
        if not set_id or self.set_is_busy(set_id) or self.style_set_pending(set_id):return
        context=self.current_context();service=XmpStyle(self.store,self.runtime,self.root)
        saved=self.settings.get('set_xmp_references',{}).get(set_id)
        override=(self.root/saved if saved and not Path(saved).is_absolute() else Path(saved)) if saved else None
        try:reference=service.reference(set_id,override)
        except FileNotFoundError:
            filename,_=QFileDialog.getOpenFileName(self,'选择封面成片对应的 XMP',str(self.root/self.album),'Camera Raw 参数 (*.xmp *.XMP)')
            if not filename:return
            try:reference=service.reference(set_id,filename)
            except (ValueError,OSError) as exc:self.show_error(exc);return
        except (ValueError,OSError) as exc:self.show_error(exc);return
        refs=dict(self.settings.get('set_xmp_references',{}))
        refs[set_id]=reference.relative_to(self.root).as_posix() if reference.is_relative_to(self.root) else str(reference)
        self.save_setting('set_xmp_references',refs)
        def work(progress,control):
            import tempfile
            service.cache_root.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='job-',dir=service.cache_root) as job:
                prepared=service.prepare(set_id,reference,job,progress,control)
                if not control.checkpoint():return {'cancelled':True}
                operations=Operations(context['store'],context['root'],render_cache_root=service.cache_root)
                operations.control,operations.progress=control,progress
                plan=operations.export_styled_set(set_id,prepared)
                result=operations.execute(plan['id'],progress,control)
                return {**result,'folder':plan['output_folder'],'warnings':prepared['warnings']}
        self.run_task(work,self.style_set_finished,kind='图集调色导出',set_id=set_id,context=context)

    def style_set_finished(self,result):
        if result.get('cancelled'):self.status.setText('图集调色已取消，原图未修改');return
        if self.context_matches(self.completion_context):self.refresh()
        message=f"图集调色副本已导出 {result['done']}/{result['total']}：{result['folder']}"
        if result.get('warnings'):message+=f" · {len(result['warnings'])} 张缺少唯一 RAW，使用封面参考匹配"
        if result.get('error'):message+=' · '+result['error']
        self.status.setText(message)

    def clear_queued_tasks(self):
        for _,_,context in self.task_queue:
            key=context.get('operation_key');record=self.file_results.get(key)
            if record and record['state']=='busy':
                ready=bool(record.get('plan'))
                self.set_file_result(key,dict(record,state='ready' if ready else 'cancelled',message='排队已取消，可重新操作。'))
        self.task_queue.clear();self.update_busy_controls()

    def task_progress(self,n,total,label):
        validating=label.startswith('执行前内容校验 ')
        if validating:self.progress.setRange(0,0)
        elif total>0:self.progress.setRange(0,100);self.progress.setValue(int(100*n/max(total,1)))
        else:self.progress.setRange(0,0)
        context=self.task_context or self.current_context()
        count='' if validating else f' · {n}/{total}' if total>0 else ' · 总量待确定'
        self.status.setText(f"{context['album']} · {context.get('kind','任务')} · {label}{count}")
        self.status.setToolTip(self.status.text())

    def task_finished(self):
        context=self.task_context;thread=self.thread;self.thread=None;self.worker=None;self.control=None
        if thread:thread.deleteLater()
        result=getattr(self,'pending_result',None);self.pending_result=None
        callback,self.task_done=self.task_done,None;self.task_context=None
        self.set_busy(False);self.pause_button.setText('暂停')
        if result is not None:
            read=context['kind'] in {'扫描并分析','重新分析','废片筛查','高强度二次筛查'}
            same=self.context_matches(context)
            if same and read:
                self.refresh()
            elif same and (context['kind'] in {'执行文件清单','恢复文件'} or context.get('operation_key')):self.refresh()
            if context['kind'] in {'执行文件清单','恢复文件'}:
                for key,record in list(self.file_results.items()):
                    saved=record.get('context',{})
                    if record['state']=='ready' and saved.get('album')==context['album'] and saved.get('root')==context['root'] and saved.get('store') is context['store']:
                        self.set_file_result(key,dict(record,state='stale',message='照片组已更新，请重新检查后执行。'))
            self.completion_context=context
            if callback:self.safe(lambda:callback(result))
            elif read:
                self.status.setText(f"{context['album']} · 分析完成，结果已自动更新" if not result.get('cancelled') else f"{context['album']} · 已取消，完整旧结果保留")
            else:
                message=result.get('message','') if isinstance(result,dict) else str(result)
                if isinstance(result,dict) and 'done' in result:
                    message=f"实际完成 {result['done']}/{result['total']} 个文件 · {result['state']}"+(' · '+result['error'] if result.get('error') else '')
                self.status.setText(f"{context['album']} · {context['kind']} · {message}")
            self.completion_context=None
            if not self.status.text().startswith(str(context['album'])):self.status.setText(str(context['album'])+' · '+self.status.text())
            if read:self.auto_learning.data_ready()
        elif self.context_matches(context) and context['kind'] in {'执行文件清单','恢复文件'}:self.refresh()
        self.update_busy_controls()
        if self.task_queue:QTimer.singleShot(0,self.start_next_request)

    def start_next_request(self):
        if self.thread is None and self.task_queue:self.start_request(self.task_queue.popleft())

    def apply_new_results(self):
        group=(self.current_group() or {}).get('id');candidate=self.review_sequence.current_id
        self.deferred_results=False;self.new_results_button.hide();self.refresh()
        for row,item in enumerate(self.group_data):
            if item['id']==group:self.group_list.setCurrentRow(row);break
        self.review_candidate=candidate;self.update_review_sequence()

    def photo_is_locked(self,asset):
        context=self.task_context
        if not context or not self.context_matches(context):return False
        return asset['id'] in context.get('locked_ids',set()) or asset['logical_key'] in context.get('locked_keys',set())

    def album_is_moving(self):
        return self.context_matches(self.task_context) and self.task_context['kind'] in {'执行文件清单','恢复文件'}

    def set_is_busy(self,set_id):
        return bool(self.context_matches(self.task_context) and self.task_context.get('set_id')==set_id and set_id)

    def make_plan(self,kind):
        if not self.album:return
        if kind=='delete':return self.execute_delete()
        if self.operation_pending(kind):return
        context=self.current_context();service=Operations(self.store,self.root);album=self.album
        key=self.file_result_key(kind,context);context['operation_key']=key
        previous=self.file_results.get(key)
        self.set_file_result(key,dict(context=context,state='busy',message='正在后台检查，完成后直接显示在本页。'))
        def work(progress,control):
            service.control,service.progress=control,progress
            return {'fill_raw':lambda:service.fill_selected_raws(album),'orphan':lambda:service.orphan_raws(album)}[kind]()
        accepted=self.run_task(work,self.plan_ready,kind={'fill_raw':'检查精选 RAW','orphan':'检查孤立 RAW'}[kind],context=context)
        if not accepted:self.set_file_result(key,previous or dict(context=context,state='cancelled',message='队列已满，请稍后重试。'))
        self.sync_file_results()

    def plan_ready(self,plan):
        context=getattr(self,'completion_context',None) or self.current_context()
        key=context.get('operation_key') or self.file_result_key(plan.get('kind','fill_raw'),context)
        count=len(plan['actions']);checks=len(plan.get('checks',[]));kind=plan.get('kind',key[3])
        if kind=='fill_raw':
            copies=sum(a['action']=='copy' for a in plan['actions']);moves=count-copies
            message=f"{context['album']} · 待补齐 {copies} 个文件 · 精选孤立 RAW/XMP 待移入备份 {moves} 个文件"
        elif kind=='export':message=f"{context['album']} · 待导出 {count} 张照片（复制）"
        else:message=f"{context['album']} · 检查 {checks} 项 · 可处理 {count} 个文件"
        self.set_file_result(key,dict(plan=plan,context=dict(context),state='ready',message=message))
        self.status.setText(message);self.sync_file_results()

    def show_plan(self,plan,context=None):
        context=dict(context or getattr(self,'completion_context',None) or self.current_context())
        key=self.file_result_key(plan['kind'],context)
        self.set_file_result(key,dict(plan=plan,context=context,state='ready',message=f"可处理 {len(plan['actions'])} 个文件。"));self.sync_file_results()

    def file_result_key(self,kind,context=None):
        context=context or self.current_context()
        return (str(context['store'].path),str(context['root']),context['album'],kind,context.get('set_id') if kind=='export' else None)

    def set_file_result(self,key,record):
        self.file_results[key]=record;self.file_results.move_to_end(key)
        # Keep the most recent page result, rather than accumulating clickable plans.
        while len(self.file_results)>12:
            expired,_=self.file_results.popitem(last=False);self.closed_export_panels.discard(expired)

    def operation_pending(self,kind):
        key=self.file_result_key(kind)
        return any(context and context.get('operation_key')==key for context in [self.task_context,*[request[2] for request in self.task_queue]])

    def sync_file_results(self):
        if not self.shell_ready:return
        for kind,panel in self.file_panels.items():
            record=self.file_results.get(self.file_result_key(kind))
            if panel.record is not record:panel.bind(record)
            panel.check_button.setEnabled(bool(self.album) and not self.operation_pending(kind) and not self.album_is_moving())
            panel.execute_button.setEnabled(bool(record and record.get('plan',{}).get('actions') and record['state']=='ready') and not self.album_is_moving())
        context=self.current_context();context['set_id']=self.selected_set()
        record=self.file_results.get(self.file_result_key('export',context))
        if self.export_panel.record is not record:self.export_panel.bind(record)
        self.export_panel.setVisible(bool(record) and self.file_result_key('export',context) not in self.closed_export_panels)
        self.export_panel.check_button.setEnabled(not self.set_is_busy(self.selected_set()))
        self.export_panel.execute_button.setEnabled(bool(record and record.get('plan',{}).get('actions') and record['state']=='ready') and not self.album_is_moving())
        record=self.file_results.get(self.file_result_key('delete'))
        self.delete_report.setVisible(bool(record) and self.filter.currentText()=='待删 JPG')
        if record:
            text=record.get('message','')
            warnings=record.get('plan',{}).get('warnings',[])
            if warnings:text+='\n'+'\n'.join(warnings)
            if text!=self.delete_report.toPlainText():self.delete_report.setPlainText(text)

    def execute_inline_plan(self,kind):
        context=self.current_context()
        if kind=='export':context['set_id']=self.selected_set()
        key=self.file_result_key(kind,context);record=self.file_results.get(key)
        if not record or record['state']!='ready' or self.album_is_moving():return
        plan=record.get('plan')
        if not plan or not plan['actions']:return
        context=dict(record['context'],operation_key=key);service=Operations(context['store'],context['root'])
        ids={a['asset_id'] for a in plan['actions'] if a.get('asset_id')}
        self.set_file_result(key,dict(record,state='busy',message='正在执行；文件逐项记录，不覆盖已有目标。'))
        def execute(progress,control):
            result=service.execute(plan['id'],progress,control)
            return dict(result,plan=plan)
        if not self.run_task(execute,self.file_execution_finished,kind='执行文件清单',locked_ids=ids,set_id=context.get('set_id'),context=context):self.set_file_result(key,record)
        self.sync_file_results()

    def close_export_panel(self):
        context=self.current_context();context['set_id']=self.selected_set()
        self.closed_export_panels.add(self.file_result_key('export',context));self.export_panel.hide()

    def execute_delete(self):
        if not self.album or self.album_is_moving() or self.operation_pending('delete'):return
        ids={a['id'] for a in self.assets if a['role']=='ordinary_jpeg' and a['decision']=='delete'}
        if not ids:return
        context=self.current_context();key=self.file_result_key('delete',context);context['operation_key']=key
        service=Operations(context['store'],context['root']);album=context['album']
        previous=self.file_results.get(key)
        self.set_file_result(key,dict(context=context,state='busy',message='正在检查并移至备份；JPG 与对应 RAW / XMP 联动处理。'))
        def execute(progress,control):
            service.control,service.progress=control,progress
            plan=service.deletion(album,True,asset_ids=ids)
            result=service.execute(plan['id'],progress,control) if plan['actions'] else dict(done=0,total=0,state='empty',error='')
            return dict(result,plan=plan)
        if not self.run_task(execute,self.file_execution_finished,kind='执行文件清单',locked_ids=ids,context=context):
            self.set_file_result(key,previous or dict(context=context,state='cancelled',message='队列已满，请稍后重试。'))
        self.sync_file_results()

    def file_execution_finished(self,result):
        context=self.completion_context;plan=result['plan'];key=context['operation_key']
        message=(f"已移至备份 {result['done']}/{result['total']} 个文件" if plan['kind']=='delete' else f"已完成 {result['done']}/{result['total']} 个文件")
        if result['state']=='empty':message='当前待删范围已无可处理的文件。'
        if result.get('error'):message+=' · '+result['error']
        if plan.get('warnings'):message+=f" · {len(plan['warnings'])} 条提示见下方"
        self.set_file_result(key,dict(plan=plan,context=dict(context),state='finished',message=message,done=result['done']))
        self.status.setText(f"{context['album']} · {message}");self.sync_file_results()

    def show_error(self,error):
        context=(getattr(self,'completion_context',None) or self.task_context) if self.shell_ready else None
        if context and context.get('operation_key'):
            key=context['operation_key'];previous=self.file_results.get(key,{})
            self.set_file_result(key,dict(previous,context=dict(context),state='error',message='未完成：'+str(error)))
            self.status.setText(f"{context['album']} · 未完成：{error}");self.sync_file_results();return
        super().show_error(error)

    def restore_item(self):
        row=self.journal.currentRow()
        if row<0:return
        journal_id=int(self.journal.item(row,0).text());context=self.current_context();service=Operations(self.store,self.root)
        if QMessageBox.question(self,'恢复移动','恢复选中项目到记录中的原位置？已有文件不会覆盖。')!=QMessageBox.StandardButton.Yes:return
        record=self.store.one('SELECT * FROM journal WHERE id=?',(journal_id,))
        asset=self.store.one('SELECT id FROM assets WHERE path=?',(record['src'],)) if record else None
        self.run_task(lambda progress,control:service.restore(journal_id),kind='恢复文件',locked_ids=[asset['id']] if asset else (),context=context)

    def choose_workspace(self):
        if self.shell_ready:self.remember_work_position();self.flush_work_position()
        super().choose_workspace()
        if self.thread is not None:self.auto_learning.suspend()

    def accept_group(self):
        group=self.current_group()
        if not group or group.get('kind')=='quality':return
        def action():
            fresh={a['id']:a for a in self.store.assets(self.album)}
            secondary=group.get('screening_mode')==SECONDARY
            stamps=self.store.review_stamps(self.album) if secondary else {}
            refs={aid for aid in group_references(group) if aid in fresh and fresh[aid]['present'] and fresh[aid]['decision']!='delete'}
            if secondary:
                refs={aid for aid in refs if group.get('review_stamps',{}).get(str(aid),[])[:2]==stamps.get(str(aid),[])[:2]
                      and evidence_is_current(group,fresh[aid],self.root)}
            decisions={}
            for member in group['candidates']:
                aid=member['id'];asset=fresh.get(aid);ref=member.get('reference',group['keep'])
                direct=ref in group.get('matches',{}).get(str(aid),[]) if 'matches' in group else True
                eligible=(suggestion_is_current(group,asset,stamps.get(str(aid))) if secondary and asset else bool(asset and asset['decision']=='pending'))
                if secondary and eligible:eligible=evidence_is_current(group,asset,self.root)
                if asset and asset['present'] and asset['role']=='ordinary_jpeg' and not asset['protected'] and eligible and aid not in refs and ref in refs and direct:
                    decisions[aid]='delete'
                    if fresh[ref]['decision']=='pending' and not fresh[ref]['protected']:decisions[ref]='keep'
            if decisions:self.apply_decisions(decisions,decision_contexts(group,decisions),source='adopt')
            else:self.status.setText('本组没有可采纳的有效建议；人工决定与受保护照片均已跳过')
        self.safe(action)

    def delete_group(self):
        group=self.current_group()
        if not group:return
        fresh={asset['id']:asset for asset in self.store.assets(self.album)}
        ids=[aid for aid in group_members(group) if aid in fresh and fresh[aid]['present'] and fresh[aid]['role']=='ordinary_jpeg' and not fresh[aid]['protected']]
        if ids:self.safe(lambda:self.apply_decisions(dict.fromkeys(ids,'delete'),decision_contexts(group,ids)))

    def refresh_sets(self):
        super().refresh_sets()
        if not self.shell_ready:return
        selected=self.selected_set();self.set_list.blockSignals(True);self.set_list.clear()
        for entry in self.collections.list(self.album):
            count=self.store.one('SELECT COUNT(*) AS n FROM set_items WHERE set_id=?',(entry['id'],))['n']
            item=QListWidgetItem(f"{entry['name']} · {count} 张");item.setData(Qt.ItemDataRole.UserRole,entry['id']);self.set_list.addItem(item)
            if entry['id']==selected:self.set_list.setCurrentItem(item)
        self.set_list.blockSignals(False);self.refresh_set_candidates()

    def refresh_set_items(self):
        if not self.shell_ready:return super().refresh_set_items()
        previous=self.set_grid.currentItem();slot=previous.data(Qt.ItemDataRole.UserRole+1) if previous else 0
        self.thumbnail_loader.cancel(self.set_thumb_owner);self.set_grid.blockSignals(True);self.set_grid.clear()
        set_id=self.selected_set()
        if set_id and self.current_set_view!=set_id:
            options=self.collections.options(set_id)
            self.set_count.blockSignals(True);self.second_cover.blockSignals(True)
            self.set_count.setValue(options['requested']);self.second_cover.setChecked(bool(options['second_cover']))
            self.set_count.blockSignals(False);self.second_cover.blockSignals(False)
        self.current_set_view=set_id
        items=self.collections.items(set_id) if set_id else [];by_slot={a['slot']:a for a in items}
        size=max(self.set_count.value(),max(by_slot,default=-1)+1) if set_id else 0
        for index in range(size):
            asset=by_slot.get(index)
            label=f'{index+1:02d} · '+(Path(asset['path']).name if asset else '空位 · 成片封面' if index==0 else '空位')
            if asset:label+='\n'+('锁定 · 手动保留位置' if asset['locked'] else '已占用 · 可自动替换')
            item=QListWidgetItem(self.icon(asset) if asset else QIcon(),label)
            item.setData(Qt.ItemDataRole.UserRole,asset['id'] if asset else None);item.setData(Qt.ItemDataRole.UserRole+1,index);self.set_grid.addItem(item)
        self.set_grid.blockSignals(False)
        if size:self.set_grid.setCurrentRow(min(slot or 0,size-1))
        self.set_summary.setText(f'{len(items)}/{size} 张 · 锁定 {sum(a["locked"] for a in items)} · 空位 {size-len(items)}')
        self.refresh_set_candidates();self.update_set_slot_info();self.load_visible_thumbnails();self.sync_file_results()

    def set_options_changed(self,*args):
        if not self.shell_ready:return
        set_id=self.selected_set()
        if set_id and not self.set_is_busy(set_id):self.collections.save_options(set_id,self.set_count.value(),self.second_cover.isChecked())
        self.save_setting('set_size',self.set_count.value());self.save_setting('second_cover',self.second_cover.isChecked());self.refresh_set_items()

    def refresh_set_candidates(self,reset=False):
        if not self.shell_ready:return
        if not self.album:
            self.set_candidates.clear();self.set_candidate_assets=[];return
        if reset:self.set_candidate_page=0
        self.thumbnail_loader.cancel(self.set_candidate_thumb_owner);self.set_candidates.clear()
        rows=self.store.rows('SELECT logical_key,content_hash FROM set_items');used={r['logical_key'] for r in rows};used_hash={r['content_hash'] for r in rows}
        hashes=self.store.digest_snapshot(self.album);choice=self.set_candidate_filter.currentIndex()
        pool=[a for a in self.assets if a['decision']!='delete' and a['role'] in {'ordinary_jpeg','selected_jpeg','finished','extra_image'}
              and a['logical_key'] not in used and hashes.get(a['path'],{}).get('sha256') not in used_hash]
        if choice==1:pool=[a for a in pool if a['role']=='finished']
        if choice==2:pool=[a for a in pool if a['role']=='ordinary_jpeg']
        self.set_candidate_assets=pool;self.set_candidate_page=0
        for asset in pool:
            label=Path(asset['path']).name+(' · 成片' if asset['role']=='finished' else ' · 精选保护' if asset['protected'] else '')
            item=QListWidgetItem(self.icon(asset),label);item.setData(Qt.ItemDataRole.UserRole,asset['id']);self.set_candidates.addItem(item)
        self.set_candidate_info.setText(f'可用 {len(pool)} 张 · 滚动浏览\n已占用照片不会出现在候选区；解锁仍占用，移出后释放。')
        self.load_visible_thumbnails()

    def page_set_candidates(self,delta):
        self.set_candidate_page=max(0,self.set_candidate_page+delta);self.refresh_set_candidates()

    def update_set_slot_info(self):
        if not self.shell_ready:return
        item=self.set_grid.currentItem()
        if item:self.set_grid.setToolTip(f"当前操作位置：第 {item.data(Qt.ItemDataRole.UserRole+1)+1} 张；首位必须成片。")

    def generation_finished(self,result):
        context=getattr(self,'completion_context',None) or self.current_context()
        if self.context_matches(context):
            self.refresh_sets()
            if result.get('set_id'):self.set_picker.setCurrentIndex(self.set_picker.findData(result['set_id']));self.refresh_sets()
        self.status.setText(f"{context['album']} · 图集 {result.get('count',0)}/{result.get('requested',0)} 张。"+result.get('message','完成'))

    def generate_new_set(self):
        if not self.album:return
        service=self.collections;album=self.album;count=self.set_count.value();second=self.second_cover.isChecked()
        self.run_task(lambda progress,control:service.create_and_generate(album,count,second,control),self.generation_finished,kind='自动生成图集')

    def generate_set_mode(self,replace_unlocked):
        set_id=self.selected_set()
        if not set_id:return self.generate_new_set()
        if self.set_is_busy(set_id):return
        service=self.collections;count=self.set_count.value();second=self.second_cover.isChecked()
        self.run_task(lambda progress,control:service.generate(set_id,count,second,replace_unlocked=replace_unlocked,control=control),
                      self.generation_finished,kind='重做未锁定项' if replace_unlocked else '自动补齐空位',set_id=set_id)

    def generate_set(self):self.generate_set_mode(True)

    def use_set_candidate(self):
        item=self.set_candidates.currentItem();set_id,slot=self.set_item()
        if not item or not set_id or self.set_is_busy(set_id):return
        service=self.collections;aid=item.data(Qt.ItemDataRole.UserRole)
        self.run_task(lambda progress,control:(service.replace(set_id,slot,aid) or {'message':'已替换并锁定'}),
                      lambda result:self.refresh_sets(),kind='手动指定图集照片',set_id=set_id)

    def preview_set_candidate(self):
        item=self.set_candidates.currentItem()
        if item:self.open_set_preview([a['id'] for a in self.set_candidate_assets],item.data(Qt.ItemDataRole.UserRole))

    def preview_set_slot(self,item):
        aid=item.data(Qt.ItemDataRole.UserRole)
        if aid:self.open_set_preview([self.set_grid.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.set_grid.count()) if self.set_grid.item(i).data(Qt.ItemDataRole.UserRole)],aid)

    def open_set_preview(self,ids,aid):
        dialog=PhotoPreviewDialog(self.store,self.runtime,self.root,ids,aid,editable_ids=[],parent=self)
        dialog.exec();dialog.deleteLater()

    def add_to_set(self):
        ids=[item.data(Qt.ItemDataRole.UserRole) for item in self.gallery.selectedItems()]
        if not ids:return
        sets=self.collections.list(self.album);labels=[s['name'] for s in sets]+['新建图集']
        label,ok=QInputDialog.getItem(self,'加入图集','选择目标图集；手选照片自动锁定',labels,max(0,self.set_picker.currentIndex()),False)
        if not ok:return
        service=self.collections;album=self.album;set_id=next((s['id'] for s in sets if s['name']==label),None)
        def work(progress,control):
            target=set_id or service.create(album)
            try:
                for aid in ids:
                    if not control.checkpoint():break
                    asset=service.store.asset(aid)
                    # A missing cover stays vacant when ordinary pictures are added first.
                    slots={a['slot'] for a in service.items(target)}
                    slot=next(i for i in range(0 if asset['role']=='finished' else 1,1000) if i not in slots)
                    service.add(target,aid,slot)
                return {'set_id':target,'count':len(service.items(target)),'message':'手选照片已锁定'}
            except Exception:
                if not set_id and not service.items(target):service.drop(target)
                raise
        self.run_task(work,self.generation_finished,kind='加入图集',set_id=set_id)

    def toggle_set_lock(self):
        set_id,slot=self.set_item()
        if not set_id or self.set_is_busy(set_id):return
        item=next((a for a in self.collections.items(set_id) if a['slot']==slot),None)
        if item:self.collections.lock(set_id,slot,not item['locked']);self.refresh_set_items()

    def remove_set_item(self):
        set_id,slot=self.set_item()
        if set_id and not self.set_is_busy(set_id):self.collections.remove(set_id,slot);self.refresh_sets()

    def reorder_set(self,delta):
        set_id,slot=self.set_item()
        if set_id and not self.set_is_busy(set_id):self.safe(lambda:self.collections.reorder(set_id,slot,delta));self.refresh_set_items()

    def drop_set(self):
        if not self.set_is_busy(self.selected_set()):super().drop_set()

    def export_set(self):
        set_id=self.selected_set();service=Operations(self.store,self.root)
        if not set_id or self.set_is_busy(set_id):return
        context=self.current_context();context['set_id']=set_id
        key=self.file_result_key('export',context);context['operation_key']=key
        self.closed_export_panels.discard(key)
        previous=self.file_results.get(key)
        self.set_file_result(key,dict(context=context,state='busy',message='正在检查图集导出文件。'))
        def check(progress,control):
            service.control,service.progress=control,progress
            return service.export_set(set_id)
        if not self.run_task(check,self.plan_ready,kind='检查图集导出',set_id=set_id,context=context):
            self.set_file_result(key,previous or dict(context=context,state='cancelled',message='队列已满，请稍后重试。'))
        self.sync_file_results()
