"""Boyo's native Qt layout, following the accepted HTML reference."""
from pathlib import Path
import sys
from PySide6.QtCore import Qt,QSize,QTimer
from PySide6.QtWidgets import (QWidget,QLabel,QVBoxLayout,QHBoxLayout,QSplitter,QPushButton,QComboBox,QSlider,
    QListWidget,QAbstractItemView,QMenu,QSizePolicy,QApplication,QStackedWidget,QHeaderView)
from .native_theme import BOYO_STYLE
from .branding import APP_NAME,APP_TITLE,app_icon
from .screening import SECONDARY
from .native_delegates import NavigationDelegate,ThumbnailDelegate,FixedItemDelegate
from .grouping import group_members
from .thumbnail_geometry import card_geometry,CELL_GUTTER


def label(text='',name=None):
    value=QLabel(text)
    if name:value.setObjectName(name)
    return value


def empty_layout(layout):
    """Retain legacy controllers/widgets; only remove their old placement."""
    removed=[]
    while layout.count():
        item=layout.takeAt(0);removed.append(item)
        if item.widget():item.widget().hide()
        if item.layout():removed.extend(empty_layout(item.layout()))
    return removed


class NativeUI:
    def __init__(self,*args,**kwargs):
        self.native_ready=False;self.native_set_members={};self.native_layout_items=[]
        super().__init__(*args,**kwargs)
        self.setWindowTitle(APP_TITLE);self.setStyleSheet(BOYO_STYLE);self.setWindowIcon(app_icon())
        for navigation in (self.photo_navigation,self.file_navigation,self.set_list):navigation.setItemDelegate(NavigationDelegate(navigation))
        for grid,kind in ((self.gallery,'gallery'),(self.quality_grid,'quality'),(self.set_grid,'set'),(self.set_candidates,'candidate')):
            grid.setItemDelegate(ThumbnailDelegate(self,kind,grid))
        self.compare.setItemDelegate(FixedItemDelegate(self.compare))
        for grid in (self.gallery,self.quality_grid,self.compare,self.set_grid,self.set_candidates):
            grid.setUniformItemSizes(True);grid.setWordWrap(False);grid.setSpacing(0)
        for card in self.review_cards.values():card.set_compact()
        self.sync_group_list();self.update_workspace_counts()
        if self.album:self.refresh_set_items()
        self.update_native_header();self.load_visible_thumbnails()

    def assemble_workspaces(self):
        super().assemble_workspaces()
        old=self.takeCentralWidget();old.hide();self.legacy_workspace_shell=old
        body=QWidget();self.setCentralWidget(body);old.setParent(body);old.hide()
        root=QVBoxLayout(body);root.setContentsMargins(0,0,0,0);root.setSpacing(0)
        top=QWidget();top.setObjectName('topbar');top.setFixedHeight(66);row=QHBoxLayout(top);row.setContentsMargins(24,0,24,0);row.setSpacing(22)
        monogram=label('','brandMark');monogram.setFixedSize(36,36);monogram.setAlignment(Qt.AlignmentFlag.AlignCenter)
        monogram.setPixmap(app_icon().pixmap(QSize(36,36),self.devicePixelRatioF()));row.addWidget(monogram)
        row.addWidget(label(APP_NAME,'brand'));self.workspaces.setObjectName('mainNavigation');row.addWidget(self.workspaces);row.addStretch()
        settings=QPushButton('设置');settings.setObjectName('quiet');settings.clicked.connect(self.open_settings);row.addWidget(settings);root.addWidget(top)
        toolbar=QWidget();toolbar.setObjectName('albumBar');toolbar.setFixedHeight(76);bar=QHBoxLayout(toolbar);bar.setContentsMargins(24,12,24,12);bar.setSpacing(14)
        bar.addWidget(label('当前照片组','subtle'));self.album_picker.setMinimumWidth(210);self.album_picker.setMaximumWidth(290);bar.addWidget(self.album_picker)
        bar.addWidget(self.scan_button)
        self.scan_button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu);self.scan_button.customContextMenuRequested.connect(self.scan_menu)
        self.task_action=QPushButton('更多');self.task_action.setObjectName('quiet');self.task_action.clicked.connect(lambda:self.scan_menu(self.scan_button.rect().bottomLeft()));bar.addWidget(self.task_action)
        bar.addWidget(self.pause_button);bar.addWidget(self.cancel_button);bar.addStretch();bar.addWidget(self.learning_status);root.addWidget(toolbar)
        middle=QSplitter();middle.setHandleWidth(0);middle.setChildrenCollapsible(False);self.main_splitter=middle
        self.native_sidebar=QWidget();self.native_sidebar.setObjectName('sidebar');self.native_sidebar.setFixedWidth(156);side=QVBoxLayout(self.native_sidebar);side.setContentsMargins(10,20,10,12);side.setSpacing(12)
        self.side_caption=label('照片精简','subtle');side.addWidget(self.side_caption)
        self.side_stack.setMinimumWidth(0);self.side_stack.setMaximumWidth(16777215);side.addWidget(self.side_stack,1)
        for nav in (self.photo_navigation,self.file_navigation,self.set_list):
            nav.setObjectName('sideNavigation');nav.setSpacing(0);nav.setFocusPolicy(Qt.FocusPolicy.NoFocus);nav.setWordWrap(False)
        self.new_set_native=QPushButton('＋ 手动新建');self.new_set_native.setObjectName('quiet');self.new_set_native.clicked.connect(self.new_set);self.new_set_native.hide();side.addWidget(self.new_set_native)
        middle.addWidget(self.native_sidebar)
        content=QWidget();self.native_content=content;self.content_layout=QVBoxLayout(content);self.content_layout.setContentsMargins(26,23,26,14);self.content_layout.setSpacing(12)
        self.build_photo_header();self.build_native_photo_pages()
        self.tabs.setDocumentMode(True)
        for index in range(self.tabs.count()):self.tabs.widget(index).layout().setContentsMargins(0,0,0,0) if self.tabs.widget(index).layout() else None
        self.content_layout.addWidget(self.tabs,1);middle.addWidget(content);middle.setStretchFactor(1,1);middle.setSizes([156,1200]);root.addWidget(middle,1)
        footer=QWidget();footer.setObjectName('statusBar');footer.setFixedHeight(34);line=QHBoxLayout(footer);line.setContentsMargins(24,0,24,0);line.setSpacing(12)
        self.status.clear();self.status.setObjectName('statusText');self.status.setWordWrap(False);self.status.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);line.addWidget(self.status,1)
        line.addWidget(self.queue_label);self.progress.setFixedWidth(120);line.addWidget(self.progress);line.addWidget(label('本地照片整理','subtle'));root.addWidget(footer)
        self.new_results_button.hide();self.summary.hide()
        self.build_native_sets();self.build_native_history()
        self.native_ready=True

    def build_photo_header(self):
        self.photo_heading=QWidget();row=QHBoxLayout(self.photo_heading);row.setContentsMargins(0,0,0,0);row.setSpacing(24)
        text=QVBoxLayout();text.setSpacing(4);self.photo_eyebrow=label('REVIEW / PHOTOS','eyebrow');self.photo_title=label('普通照片','photoTitle')
        text.addWidget(self.photo_eyebrow);text.addWidget(self.photo_title);row.addLayout(text,1)
        self.photo_stats=label();self.photo_stats.setTextFormat(Qt.TextFormat.RichText);row.addWidget(self.photo_stats);self.content_layout.addWidget(self.photo_heading)
        self.photo_tools=QWidget();tools=QHBoxLayout(self.photo_tools);tools.setContentsMargins(0,0,0,0);tools.setSpacing(12)
        self.strength_caption=label('精简强度','muted');tools.addWidget(self.strength_caption);tools.addWidget(self.strength)
        tools.addWidget(self.issue_filter);tools.addWidget(self.quality_filter);tools.addWidget(self.quality_sensitivity);self.quality_sensitivity.setToolTip('废片筛查敏感度；修改后从更多菜单重新筛查。');tools.addWidget(self.gallery_selection);tools.addStretch()
        self.thumb_caption=label('缩略图','muted');tools.addWidget(self.thumb_caption);self.thumbnail_slider=QSlider(Qt.Orientation.Horizontal);self.thumbnail_slider.setRange(170,350);self.thumbnail_slider.setValue(int(self.settings.get('thumbnail_width',230)));self.thumbnail_slider.setFixedWidth(96);tools.addWidget(self.thumbnail_slider)
        self.thumbnail_timer=QTimer(self);self.thumbnail_timer.setSingleShot(True);self.thumbnail_timer.setInterval(60);self.thumbnail_timer.timeout.connect(self.load_visible_thumbnails)
        self.thumbnail_slider.valueChanged.connect(lambda _:self.thumbnail_timer.start());self.thumbnail_slider.sliderReleased.connect(lambda:self.save_setting('thumbnail_width',self.thumbnail_slider.value()))
        self.content_layout.addWidget(self.photo_tools)

    def build_native_photo_pages(self):
        for index,stack in ((0,self.gallery_stack),(1,self.review_stack),(5,self.quality_stack)):
            page=self.tabs.widget(index);layout=page.layout();self.native_layout_items.extend(empty_layout(layout));layout.setContentsMargins(0,0,0,0);layout.setSpacing(10)
            if index==1:
                self.native_review_splitter=QSplitter();self.native_review_splitter.setChildrenCollapsible(False)
                self.group_list.setObjectName('groupNavigation');self.group_list.setMinimumWidth(170);self.group_list.setMaximumWidth(220)
                self.native_review_splitter.addWidget(self.group_list);self.native_review_splitter.addWidget(stack)
                self.native_review_splitter.setStretchFactor(0,0);self.native_review_splitter.setStretchFactor(1,1);self.native_review_splitter.setSizes([190,950])
                layout.addWidget(self.native_review_splitter,1);self.group_list.show()
            else:layout.addWidget(stack,1)
            stack.show()
            footer=QWidget();row=QHBoxLayout(footer);row.setContentsMargins(0,4,0,0);row.setSpacing(8)
            row.addStretch()
            if index==1:
                self.native_review_footer=footer
                self.review_keep_selection=QPushButton('选中保留');self.review_keep_selection.setProperty('photoDecision','keep');self.review_keep_selection.clicked.connect(lambda:self.decide_native_selection('keep'));row.addWidget(self.review_keep_selection)
                self.review_delete_selection=QPushButton('选中待删');self.review_delete_selection.setProperty('photoDecision','delete');self.review_delete_selection.clicked.connect(lambda:self.decide_native_selection('delete'));row.addWidget(self.review_delete_selection)
                row.addSpacing(12)
                self.adopt_button.setText('采纳本组有效建议');row.addWidget(self.adopt_button);self.adopt_button.show()
                self.group_keep_button=QPushButton('整组保留');self.group_keep_button.setProperty('photoDecision','keep');self.group_keep_button.clicked.connect(self.keep_group);row.addWidget(self.group_keep_button)
                self.group_delete_button=QPushButton('整组待删');self.group_delete_button.setProperty('photoDecision','delete');self.group_delete_button.clicked.connect(self.delete_group);row.addWidget(self.group_delete_button)
            else:
                for text,state in (('保留','keep'),('标记待删','delete'),('撤销决定','pending')):
                    button=QPushButton(text);button.setProperty('photoDecision',state);button.clicked.connect(lambda checked=False,state=state:self.decide_native_selection(state));row.addWidget(button)
            if index==0:
                self.native_gallery_footer=footer;layout.addWidget(self.delete_report);layout.addWidget(footer);layout.addWidget(self.delete_bar)
            else:layout.addWidget(footer)
            grid={0:self.gallery,1:self.compare,5:self.quality_grid}[index]
            grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu);grid.customContextMenuRequested.connect(lambda point,grid=grid:self.photo_context_menu(grid,point))
        self.page_label.hide();self.review_page_label.hide();self.quality_page_label.hide();self.gallery_actions.hide()
        self.gallery_selection.show();self.strength.hide();self.strength_caption.hide();self.issue_filter.hide();self.quality_filter.hide();self.quality_sensitivity.hide()

    def build_native_sets(self):
        page=self.tabs.widget(3);layout=page.layout();self.native_layout_items.extend(empty_layout(layout));layout.setSpacing(18)
        header=QHBoxLayout();text=QVBoxLayout();text.setSpacing(4);text.addWidget(label('COLLECTIONS / PLACES','eyebrow'));text.addWidget(label('自动图集','title'));header.addLayout(text,1);header.addWidget(self.generate_button);self.generate_button.show();layout.addLayout(header)
        options=QHBoxLayout();options.setSpacing(12);options.addWidget(label('每集','muted'));options.addWidget(self.set_count);self.set_count.show();options.addWidget(label('张','muted'));options.addSpacing(8);options.addWidget(self.second_cover);self.second_cover.show();options.addStretch();layout.addLayout(options)
        split=QSplitter();split.setHandleWidth(1);split.setChildrenCollapsible(False)
        left=QWidget();inner=QVBoxLayout(left);inner.setContentsMargins(0,0,20,0);inner.setSpacing(12)
        title=QHBoxLayout();self.native_set_title=label('图集','photoMeta');title.addWidget(self.native_set_title,1);title.addWidget(self.set_summary);self.set_summary.setObjectName('muted');self.set_summary.show();inner.addLayout(title)
        inner.addWidget(self.set_grid,1);self.set_grid.show()
        actions=QHBoxLayout();actions.setSpacing(8);actions.addWidget(self.fill_set_button);actions.addWidget(self.redo_set_button);self.fill_set_button.show();self.redo_set_button.show()
        export=QPushButton('导出图集');export.clicked.connect(self.export_set);actions.addWidget(export);actions.addStretch();inner.addLayout(actions);split.addWidget(left)
        self.style_set_button=QPushButton('一键修图');self.style_set_button.setObjectName('primary');self.style_set_button.setToolTip('XMP 优先：复用封面全局调色，再参考封面校正亮度与色调；RAW 渲染使用本机 Photoshop / Camera Raw。只导出副本。');self.style_set_button.clicked.connect(self.style_current_set);actions.insertWidget(3,self.style_set_button)
        right=QWidget();right.setMinimumWidth(190);right.setMaximumWidth(300);candidate=QVBoxLayout(right);candidate.setContentsMargins(20,0,0,0);candidate.setSpacing(12)
        candidate.addWidget(label('可用照片','photoMeta'));self.set_candidates.setToolTip('先选图集中的位置，再点候选替换。手选自动锁定。')
        candidate.addWidget(self.set_candidate_filter);self.set_candidate_filter.show();candidate.addWidget(self.set_candidates,1);self.set_candidates.show()
        self.set_candidates.setViewMode(QListWidget.ViewMode.IconMode);self.set_candidates.setResizeMode(QListWidget.ResizeMode.Adjust);self.set_candidates.setMovement(QListWidget.Movement.Static)
        self.set_candidates.itemClicked.connect(lambda _:self.use_set_candidate());self.set_candidates.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu);self.set_candidates.customContextMenuRequested.connect(self.candidate_context_menu)
        split.addWidget(right);split.setSizes([900,240]);split.setStretchFactor(0,1);layout.addWidget(split,1);layout.addWidget(self.export_panel)
        self.set_grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu);self.set_grid.customContextMenuRequested.connect(self.set_context_menu)

    def build_native_history(self):
        page=self.file_stack.widget(2);layout=page.layout();self.native_layout_items.extend(empty_layout(layout));layout.setSpacing(16)
        layout.addWidget(label('FILES / LOCAL','eyebrow'));layout.addWidget(label('操作记录与恢复','title'))
        line=QHBoxLayout();self.history_summary=label('','muted');line.addWidget(self.history_summary,1)
        self.history_more=QPushButton('加载更早记录');self.history_more.clicked.connect(self.load_older_history);line.addWidget(self.history_more);layout.addLayout(line)
        self.history_stack=QStackedWidget();self.history_stack.addWidget(self.journal)
        empty=label('当前拍摄组还没有文件操作记录','muted');empty.setAlignment(Qt.AlignmentFlag.AlignCenter);self.history_stack.addWidget(empty);layout.addWidget(self.history_stack,1)
        self.journal.show();self.journal.viewport().show();self.journal.horizontalHeader().show()
        self.journal.setWordWrap(False);self.journal.verticalHeader().hide();self.journal.verticalHeader().setDefaultSectionSize(46)
        header=self.journal.horizontalHeader();header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for col,width in ((0,65),(1,95),(2,70)):header.setSectionResizeMode(col,QHeaderView.ResizeMode.Fixed);self.journal.setColumnWidth(col,width)
        bar=QHBoxLayout();bar.addStretch()
        self.history_restore=QPushButton('恢复选中的移动项目');self.history_restore.clicked.connect(self.restore_item);bar.addWidget(self.history_restore);layout.addLayout(bar)
        self.journal.currentCellChanged.connect(lambda *args:self.update_history_controls());self.refresh_journal()

    def workspace_changed(self,index):
        result=super().workspace_changed(index)
        if self.native_ready:
            self.photo_heading.setVisible(index==0);self.photo_tools.setVisible(index==0);self.new_set_native.setVisible(index==2);self.side_caption.setText(('照片精简','文件工具','我的图集')[index]);self.update_native_header()
        return result

    def photo_task_changed(self,row):
        result=super().photo_task_changed(row)
        if self.native_ready:self.update_native_header()
        return result

    def update_gallery_actions(self,pending):
        super().update_gallery_actions(pending)
        if self.native_ready:
            self.gallery_actions.hide();self.native_gallery_footer.setVisible(not pending and self.last_photo_task!='protected')

    def update_workspace_counts(self):
        result=super().update_workspace_counts()
        if self.native_ready and self.shell_ready:
            for row in range(self.photo_navigation.count()):
                item=self.photo_navigation.item(row);parts=item.text().split('\n');count=parts[1].split(' ')[0] if len(parts)>1 else None
                item.setData(Qt.ItemDataRole.UserRole+3,count if count and count.isdigit() else None);item.setSizeHint(QSize(132,48))
            self.update_native_header()
        return result

    def update_native_header(self):
        if not self.native_ready or not self.shell_ready:return
        task=self.last_photo_task
        names={'ordinary':'普通照片','similar':'相似复核','quality':'废片筛查','pending':'待删复核','protected':'精选与成片'}
        secondary=(getattr(self,'review_screening_mode',None) if task=='similar' else getattr(self,'quality_screening_mode',None) if task=='quality' else None)==SECONDARY
        self.photo_title.setText(names[task]+(' · 二次筛查' if secondary else ''));self.photo_eyebrow.setText('REVIEW / '+task.upper()+(' / SECOND PASS' if secondary else ''))
        self.strength_caption.setVisible(task=='similar' and not secondary);self.strength.setVisible(task=='similar' and not secondary)
        self.issue_filter.setVisible(task=='quality');self.quality_filter.setVisible(task=='quality');self.quality_sensitivity.setVisible(task=='quality' and not secondary);self.gallery_selection.setVisible(task in {'ordinary','pending','protected'})
        self.filter.setVisible(False)
        grid=self.compare if task=='similar' else self.quality_grid if task=='quality' else self.gallery
        visible=[self.asset_by_id[aid] for row in range(grid.count()) if (aid:=grid.item(row).data(Qt.ItemDataRole.UserRole)) in self.asset_by_id]
        counts={'保护':0,'保留':0,'待删':0,'未决定':0}
        for asset in visible:counts['保护' if asset['protected'] else {'keep':'保留','delete':'待删'}.get(asset['decision'],'未决定')]+=1
        scope={'similar':'当前组','quality':'筛查结果','pending':'本轮复核'}.get(task,'当前列表')
        group_info=f' · 第 {self.group_list.currentRow()+1}/{len(self.group_data)} 组' if task=='similar' and self.group_data else ''
        detail=' · '.join(f'{name} {count}' for name,count in counts.items())
        self.photo_stats.setText(f'<div style="color:#d1d5e5;font-size:14px">{scope} {len(visible)} 张{group_info}</div><div style="color:#979eb8;font-size:12px">{detail}</div>')
        self.photo_stats.setToolTip('统计范围仅限当前列表，四种状态互不重复，相加等于显示张数。保护照片单独计数，不再重复计入保留。')

    def refresh_review(self):
        result=super().refresh_review()
        from json import loads
        run=self.store.one('SELECT summary FROM analysis_runs WHERE album=?',(self.album,))
        self.review_screening_mode=loads(run['summary']).get('screening_mode') if run else None
        secondary_groups=[g for g in self.group_data if g.get('screening_mode')==SECONDARY]
        self.secondary_resolved_ids=set()
        if secondary_groups:
            stamps=self.store.review_stamps(self.album)
            if not self.review_screening_mode:self.review_screening_mode=SECONDARY
            self.secondary_resolved_ids={int(aid) for group in secondary_groups for aid,baseline in group.get('review_stamps',{}).items() if baseline!=stamps.get(aid)}
        self.update_review_summary()
        if self.native_ready:self.sync_group_list()
        return result

    def sync_group_list(self):
        height=self.group_list.fontMetrics().lineSpacing()*3+24
        for row in range(self.group_list.count()):
            self.group_list.item(row).setSizeHint(QSize(170,height))

    def select_group(self,index):
        result=super().select_group(index)
        if self.native_ready:
            self.update_native_header()
        return result

    def refresh_gallery(self):
        result=super().refresh_gallery()
        if self.native_ready:self.update_native_header()
        return result

    def refresh_quality_grid(self):
        result=super().refresh_quality_grid()
        if self.native_ready:self.update_native_header()
        return result

    def sync_review_cards(self,rows):
        result=super().sync_review_cards(rows)
        if self.native_ready:
            for card in self.review_cards.values():card.set_compact();card.set_card_width(self.compare.gridSize().width()-CELL_GUTTER)
        return result

    def review_card_widget(self,card):
        if not self.native_ready:return super().review_card_widget(card)
        card.set_compact();card.set_card_width(self.compare.gridSize().width()-CELL_GUTTER)
        wrapper=QWidget();layout=QVBoxLayout(wrapper);layout.setContentsMargins(6,6,10,10);layout.setSpacing(0);layout.addWidget(card,0,Qt.AlignmentFlag.AlignTop|Qt.AlignmentFlag.AlignLeft)
        return wrapper

    def relayout_thumbnail_grids(self):
        if not self.native_ready:return super().relayout_thumbnail_grids()
        zoom=self.thumbnail_slider.value()
        for grid in (self.gallery,self.quality_grid,self.compare,self.set_grid,self.set_candidates):
            width,height,image_height=card_geometry(max(70,min(120,round(zoom*.45))) if grid is self.set_candidates else zoom)
            size=QSize(width+CELL_GUTTER,height+CELL_GUTTER)
            if grid.gridSize()==size and (not grid.count() or grid.item(0).sizeHint()==size):continue
            grid.setUpdatesEnabled(False)
            try:
                grid.setUniformItemSizes(True);grid.setWordWrap(False);grid.setGridSize(size);grid.setIconSize(QSize(width-14,image_height));grid.setSpacing(0)
                for row in range(grid.count()):grid.item(row).setSizeHint(size)
                if grid is self.compare:
                    for card in self.review_cards.values():card.set_card_width(width)
                grid.doItemsLayout()
            finally:grid.setUpdatesEnabled(True)
            grid.viewport().update()

    def refresh_set_items(self):
        result=super().refresh_set_items()
        if self.native_ready and self.shell_ready:
            self.native_set_members={item['slot']:item for item in self.collections.items(self.selected_set())} if self.selected_set() else {}
            entry=self.store.one('SELECT name FROM photo_sets WHERE id=?',(self.selected_set(),)) if self.selected_set() else None
            self.native_set_title.setText(entry['name'] if entry else '还没有图集，点击自动生成开始')
            available=sum(item['present'] for item in self.native_set_members.values());missing=len(self.native_set_members)-available
            self.set_summary.setText(f"{available}/{self.set_grid.count()} 张 · 锁定 {sum(item['locked'] for item in self.native_set_members.values())}"+(f' · 已移出 {missing} 张' if missing else ''))
            busy=self.set_is_busy(self.selected_set());enabled=bool(self.selected_set()) and not busy
            self.fill_set_button.setEnabled(enabled);self.redo_set_button.setEnabled(enabled)
            self.set_grid.viewport().update()
        return result

    def decide_native_selection(self,state):
        grid={0:self.gallery,1:self.compare,5:self.quality_grid}.get(self.tabs.currentIndex())
        if grid is None:return
        if not grid.selectedItems():
            self.status.setText('请先选中要标记的缩略图');return
        if self.tabs.currentIndex()==1:self.review_decide(state)
        elif self.tabs.currentIndex()==5:self.quality_decide(state)
        else:self.gallery_decide(state)

    def start_quality(self):
        if self.native_ready and self.shell_ready:
            self.workspaces.setCurrentIndex(0);self.photo_navigation.setCurrentRow(2)
        return super().start_quality()

    def scan_menu(self,point):
        menu=QMenu(self);menu.addAction('重新分析',self.start_analysis);menu.addAction('筛查废片',self.start_quality)
        menu.addAction('高强度二次筛查',self.start_secondary_screening);menu.addSeparator();menu.addAction('取消排队任务',self.clear_queued_tasks)
        menu.exec(self.scan_button.mapToGlobal(point))

    def photo_context_menu(self,grid,point):
        item=grid.itemAt(point)
        if not item:return
        if not item.isSelected():grid.setCurrentItem(item)
        menu=QMenu(grid);menu.addAction('查看大图',self.expand_current)
        for text,state in (('保留','keep'),('标记待删','delete'),('撤销决定','pending')):menu.addAction(text,lambda state=state:self.decide_native_selection(state))
        if grid is self.gallery:menu.addSeparator();menu.addAction('加入图集…',self.add_to_set);menu.addAction('关联原片…',self.link_source)
        menu.exec(grid.viewport().mapToGlobal(point))

    def set_context_menu(self,point):
        item=self.set_grid.itemAt(point)
        if not item:return
        self.set_grid.setCurrentItem(item);menu=QMenu(self.set_grid)
        menu.addAction('锁定 / 解锁',self.toggle_set_lock);menu.addAction('移出图集',self.remove_set_item);menu.addAction('向前移动',lambda:self.reorder_set(-1));menu.addAction('向后移动',lambda:self.reorder_set(1));menu.exec(self.set_grid.viewport().mapToGlobal(point))

    def candidate_context_menu(self,point):
        item=self.set_candidates.itemAt(point)
        if item:self.set_candidates.setCurrentItem(item);menu=QMenu(self.set_candidates);menu.addAction('查看大图',self.preview_set_candidate);menu.exec(self.set_candidates.viewport().mapToGlobal(point))

    def resizeEvent(self,event):
        result=super().resizeEvent(event)
        if self.native_ready:
            compact=self.width()<=1000;self.native_sidebar.setFixedWidth(140 if compact else 156);self.content_layout.setContentsMargins(18 if compact else 26,23,18 if compact else 26,14)
            self.main_splitter.setSizes([self.native_sidebar.width(),max(1,self.width()-self.native_sidebar.width())])
            self.thumb_caption.setVisible(self.width()>850);self.thumbnail_slider.setVisible(self.width()>850)
        return result

    def showEvent(self,event):
        super().showEvent(event)
        if sys.platform=='win32' and QApplication.platformName()!='offscreen':
            import ctypes
            dark=ctypes.c_int(1)
            try:ctypes.windll.dwmapi.DwmSetWindowAttribute(int(self.winId()),20,ctypes.byref(dark),ctypes.sizeof(dark))
            except (OSError,AttributeError):pass
