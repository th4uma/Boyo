"""File checks and execution live in the workspace, with bounded table widths."""
from PySide6.QtGui import QColor
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView,QHeaderView,QHBoxLayout,QLabel,
    QPushButton,QSizePolicy,QTableWidget,QTableWidgetItem,QTabWidget,QTextEdit,QVBoxLayout,QWidget)


class FilePlanPanel(QWidget):
    def __init__(self,owner,kind,title,note,execute_text):
        super().__init__()
        self.owner,self.kind,self.record=owner,kind,None
        self.setMinimumWidth(0)
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(8 if kind=='export' else 14)
        if kind!='export':
            eyebrow=QLabel('FILES / LOCAL');eyebrow.setObjectName('eyebrow');layout.addWidget(eyebrow)
        header=QHBoxLayout();heading=QLabel(title);heading.setObjectName('drawerTitle' if kind=='export' else 'title');heading.setToolTip(note);header.addWidget(heading,1)
        self.close_button=QPushButton('收起');self.close_button.setObjectName('quiet');self.close_button.setVisible(kind=='export');header.addWidget(self.close_button);layout.addLayout(header)
        if kind=='export':self.close_button.clicked.connect(owner.close_export_panel)
        self.summary=QLabel();self.summary.setWordWrap(True);self.summary.setMinimumWidth(0)
        self.summary.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);layout.addWidget(self.summary)
        overview=QHBoxLayout();overview.setSpacing(35);self.overview_labels=[]
        for _ in range(3):
            value=QLabel();self.overview_labels.append(value);overview.addWidget(value)
        overview.addStretch()
        if kind!='export':layout.addLayout(overview)
        else:
            for value in self.overview_labels:value.hide()
        self.tabs=QTabWidget();layout.addWidget(self.tabs,1)
        self.check_table=self.table(['照片 / RAW','检查结果','目标 / 原因'])
        self.action_table=self.table(['操作','源文件','目标文件','依据'])
        self.tabs.addTab(self.check_table,'检查结果');self.tabs.addTab(self.action_table,'将处理的文件')
        self.check_table.cellDoubleClicked.connect(lambda row,col:self.preview(row,True))
        self.action_table.cellDoubleClicked.connect(lambda row,col:self.preview(row,False))
        self.notes=QTextEdit();self.notes.setReadOnly(True);self.notes.setMaximumHeight(90);self.notes.hide();layout.addWidget(self.notes)
        bar=QHBoxLayout();bar.addStretch();self.check_button=QPushButton('检查 / 刷新');bar.addWidget(self.check_button)
        self.check_button.setToolTip('已有目标不覆盖，配对冲突暂缓处理。')
        self.execute_button=QPushButton(execute_text);self.execute_button.setObjectName('primary');bar.addWidget(self.execute_button);layout.addLayout(bar)
        self.check_button.clicked.connect(lambda:owner.export_set() if kind=='export' else owner.make_plan(kind))
        self.execute_button.clicked.connect(lambda:owner.execute_inline_plan(kind))
        self.bind(None)

    @staticmethod
    def table(labels):
        table=QTableWidget(0,len(labels));table.setHorizontalHeaderLabels(labels)
        table.setMinimumWidth(0);table.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Expanding)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setMinimumSectionSize(45)
        table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)
        table.verticalHeader().hide();table.verticalHeader().setDefaultSectionSize(46)
        table.setWordWrap(False)
        return table

    @staticmethod
    def fill(table,rows,keys):
        table.setUpdatesEnabled(False);table.setRowCount(len(rows))
        for index,row in enumerate(rows):
            for col,key in enumerate(keys):
                value=str(row.get(key,''))
                if key=='action':value={'move':'移动','copy':'复制'}.get(value,value)
                item=QTableWidgetItem(value);item.setToolTip(value)
                if key=='status':item.setForeground(QColor('#a2cdb0' if value in {'可补齐','孤立候选','精选孤立候选','已复制','已移入备份'} else '#c9b8d8' if '冲突' in value or '未找到' in value else '#b9bfd7'))
                table.setItem(index,col,item)
        table.setUpdatesEnabled(True)

    def bind(self,record):
        self.record=record
        plan=(record or {}).get('plan') or {}
        checks=[dict(row) for row in plan.get('checks',[])]
        if (record or {}).get('state')=='finished' and record.get('done')==len(plan.get('actions',[])):
            for row in checks:
                if row.get('status')=='可补齐':row['status']='已复制'
                elif row.get('status') in {'孤立候选','精选孤立候选'}:row['status']='已移入备份'
        self.fill(self.check_table,checks,('path','status','detail'))
        self.fill(self.action_table,plan.get('actions',[]),('action','src','dst','reason'))
        self.tabs.setTabVisible(0,self.kind!='export')
        if self.kind=='export':self.tabs.setCurrentIndex(1)
        actionable=sum(row.get('status') in {'可补齐','孤立候选','精选孤立候选'} for row in checks)
        pending_files=len(plan.get('actions',[])) if (record or {}).get('state')=='ready' else 0
        for widget,(name,count) in zip(self.overview_labels,(('本次检查',len(checks)),('可处理文件',pending_files),('保留 / 已完成',len(checks)-actionable))):widget.setText(f'<span style="color:#979eb8;font-size:12px">{name}</span>　<span style="font-size:22px">{count}</span>')
        self.summary.setText((record or {}).get('message') or '点击检查，配对结果和文件明细会直接显示在这里。检查不会移动或复制文件。')
        warnings=plan.get('warnings',[])
        self.notes.setPlainText('\n'.join(warnings));self.notes.setVisible(bool(warnings))
        self.execute_button.setEnabled(bool(plan.get('actions')) and (record or {}).get('state')=='ready')

    def preview(self,row,check):
        if not self.record or not self.owner.context_matches(self.record['context']):return
        plan=self.record.get('plan') or {};rows=plan.get('checks' if check else 'actions',[])
        if not 0<=row<len(rows):return
        asset=self.owner.asset_by_id.get(rows[row].get('asset_id'))
        if asset and asset['role'] in {'ordinary_raw','selected_raw','sidecar'}:
            asset=next((a for a in self.owner.asset_by_id.values() if a['logical_key']==asset['logical_key'] and a['role'] in {'ordinary_jpeg','selected_jpeg'}),None)
        if asset:self.owner.open_preview(asset['id'],[asset['id']],context='files:'+self.kind,editable_ids=[] if self.kind=='export' else None)
