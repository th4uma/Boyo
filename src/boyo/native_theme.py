"""Native Boyo palette and spacing, matching the HTML layout reference."""
BOYO_STYLE='''
QMainWindow,QDialog,QWidget {background:#171925;color:#d9dce8;font-family:'Microsoft YaHei';font-size:14px;}
QWidget#topbar {border-bottom:1px solid #353a51;}
QWidget#albumBar {border-bottom:1px solid #2b3043;}
QWidget#sidebar {border-right:1px solid #353a51;}
QWidget#statusBar {border-top:1px solid #2b3043;}
QLabel {background:transparent;}
QLabel#brand {font-family:'Segoe UI';font-size:24px;font-weight:600;color:#e5e2ef;}
QLabel#brandMark {background:transparent;border:0;}
QLabel#title,QLabel#photoTitle {font-size:24px;font-weight:600;color:#d9dce8;}
QLabel#drawerTitle {font-size:18px;font-weight:600;color:#d9dce8;}
QLabel#learningBadge {background:#323952;color:#c9c7e9;border:1px solid #4d5577;border-radius:6px;padding:7px 12px;}
QLabel#learningMetric {background:#202434;border:1px solid #353a51;border-radius:7px;padding:12px;}
QLabel#learningReason {background:#262b40;border:1px solid #424b69;border-radius:7px;padding:12px;color:#c5cbe0;}
QLabel#eyebrow {font-size:10px;color:#8d92b1;}
QLabel#muted,QLabel#description {color:#979eb8;font-size:12px;}
QLabel#subtle,QLabel#statusText {color:#929ab4;font-size:11px;}
QLabel#photoStat {color:#d1d5e5;font-size:18px;}
QLabel#photoMeta {font-size:12px;}
QPushButton,QToolButton {background:#292d40;border:1px solid #353a51;border-radius:6px;padding:8px 13px;outline:0;}
QPushButton:hover,QToolButton:hover {background:#343a52;border-color:#565c7d;}
QPushButton:disabled,QToolButton:disabled {color:#636a82;background:#202332;border-color:#30354a;}
QPushButton#primary {background:#51577e;border-color:#696f99;color:#f0effb;}
QPushButton#primary:hover {background:#62698f;}
QPushButton#danger {background:#4b3546;border-color:#765263;color:#efb7bf;}
QPushButton#quiet {background:transparent;border-color:transparent;color:#979eb8;}
QPushButton[photoDecision="keep"] {color:#a2cdb0;}
QPushButton[photoDecision="delete"] {color:#dfa3ad;}
QPushButton[photoDecision]:disabled {color:#636a82;}
QComboBox,QSpinBox,QLineEdit {background:#292d40;border:1px solid #353a51;border-radius:5px;padding:7px 10px;selection-background-color:#3a3f60;}
QComboBox::drop-down {border:0;width:22px;}
QComboBox QAbstractItemView {background:#202332;selection-background-color:#3a3f60;outline:0;}
QTabWidget::pane {border:0;}
QTabBar::tab {background:transparent;color:#979eb8;padding:14px 16px;border:0;border-bottom:2px solid transparent;outline:0;}
QTabBar::tab:selected {color:#dedcf0;border-bottom-color:#a4a7d6;}
QTabBar::tab:hover {color:#d9dce8;}
QTabBar#mainNavigation::tab {min-height:34px;}
QListWidget {background:transparent;border:0;outline:0;}
QListWidget#sideNavigation {font-size:13px;}
QListWidget::item {padding:0;border:0;}
QListWidget::item:selected {background:transparent;}
QListWidget#groupNavigation {border-right:1px solid #353a51;font-size:12px;}
QListWidget#groupNavigation::item {padding:10px 9px;margin:2px 6px 2px 0;border-radius:6px;}
QListWidget#groupNavigation::item:selected {background:#3a3f60;color:#dedff0;}
QTableWidget,QTextEdit {background:#1d2130;border:1px solid #353a51;border-radius:7px;outline:0;selection-background-color:#333951;}
QHeaderView::section {background:#292e43;color:#b9bfd7;padding:12px 15px;border:0;font-size:12px;}
QTableWidget {font-size:12px;gridline-color:#2c3145;}
QTableWidget::item {padding:9px 12px;border-bottom:1px solid #2c3145;}
QTextEdit {padding:8px 10px;color:#c2b8d8;font-size:12px;}
QCheckBox {spacing:8px;font-size:12px;}
QCheckBox::indicator {width:14px;height:14px;border:1px solid #59617d;background:#202332;border-radius:3px;}
QCheckBox::indicator:checked {background:#9496c6;border-color:#b7bbdc;}
QSlider::groove:horizontal {height:4px;border-radius:2px;background:#424964;}
QSlider::sub-page:horizontal {background:#8585b0;border-radius:2px;}
QSlider::handle:horizontal {background:#a4a7d6;width:13px;margin:-5px 0;border-radius:6px;}
QProgressBar {border:0;background:#252b3f;border-radius:3px;max-height:5px;min-height:5px;color:transparent;}
QProgressBar::chunk {background:#8a8eb8;border-radius:3px;}
QFrame#photoCard {background:#202434;border:1px solid #353a51;border-radius:8px;}
QFrame#photoCard[decision="keep"] {border-color:#435f56;}
QFrame#photoCard[decision="delete"] {border-color:#654653;}
QFrame#photoCard[selected="true"] {background:#2b3048;border-color:#a3a2cf;}
QPushButton#thumbnailImage {background:#11141e;border:0;border-radius:4px;padding:0;color:#979eb8;font-size:11px;}
QMenu {background:#292d40;border:1px solid #494e6a;}
QMenu::item {padding:9px 20px;}
QMenu::item:selected {background:#3a3f60;}
QScrollBar:vertical {background:transparent;width:10px;margin:0;}
QScrollBar::handle:vertical {background:#4b536e;min-height:28px;border-radius:4px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {background:transparent;}
QScrollBar:horizontal {background:transparent;height:9px;}
QScrollBar::handle:horizontal {background:#4b536e;min-width:28px;border-radius:4px;}
QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal {width:0;}
QSplitter::handle {background:#292e43;width:1px;}
QToolTip {background:#292d40;color:#d9dce8;border:1px solid #565c7d;padding:6px;}
'''
