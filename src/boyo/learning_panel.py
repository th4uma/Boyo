"""A compact, truthful view of local learning; updated only by events."""
from PySide6.QtWidgets import QWidget,QLabel,QPushButton,QTextEdit,QVBoxLayout,QHBoxLayout


class LearningPanel(QWidget):
    def __init__(self,owner):
        super().__init__();layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(14)
        top=QHBoxLayout();title=QLabel('偏好学习');title.setObjectName('title');top.addWidget(title,1)
        self.activity=QLabel('积累中');self.activity.setObjectName('learningBadge');top.addWidget(self.activity);layout.addLayout(top)
        line=QHBoxLayout();self.metrics=[]
        for _ in range(3):
            value=QLabel();value.setObjectName('learningMetric');self.metrics.append(value);line.addWidget(value,1)
        layout.addLayout(line)
        self.base_reason='';self.reason=QLabel();self.reason.setWordWrap(True);self.reason.setObjectName('learningReason');layout.addWidget(self.reason)
        self.details=QTextEdit();self.details.setReadOnly(True);layout.addWidget(self.details,1)
        bottom=QHBoxLayout();self.last_result=QLabel();self.last_result.setObjectName('subtle');bottom.addWidget(self.last_result,1)
        self.verify=QPushButton('立即验证（可选）');self.verify.setToolTip('平时自动运行；此按钮不会放宽验证条件。');self.verify.clicked.connect(owner.train_preferences);bottom.addWidget(self.verify);layout.addLayout(bottom)

    def bind(self,report,activity):
        for label,(title,value,unit) in zip(self.metrics,(('主动留删',report['manual'],'张'),('反馈拍摄组',report['albums'],'组'),('上次同构图比较',report['pairs'],'对'))):
            label.setText(f'<span style="font-size:12px;color:#979eb8">{title}</span><br><span style="font-size:23px">{value}</span> {unit}')
        self.base_reason=report['reason'];self.details.setPlainText(report['details']);self.last_result.setText('最近验证：'+report['updated']);self.set_activity(activity)

    def set_activity(self,activity):
        self.activity.setText(activity['label']);self.activity.setToolTip(activity['message']);self.verify.setEnabled(activity['code'] not in {'working','paused','blocked','closed'})
        self.reason.setText((activity['message']+'\n' if activity['code'] in {'error','blocked','paused','working'} else '')+self.base_reason)
