from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .config import Runtime
from .store import Store
from .branding import APP_NAME,app_icon


def main():
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description=f"{APP_NAME}：本地照片整理。CLI 仅提供只读扫描/分析，不提供文件移动命令。")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--scan", type=Path, help="只读扫描拍摄组")
    parser.add_argument("--analyze", action="store_true", help="扫描后分析并缓存，不移动照片")
    parser.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--album", help="启动时选择已登记的拍摄组（不自动分析）")
    parser.add_argument("--view", choices=["图库", "精简复核", "文件整理", "图集", "学习与资源"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    runtime = Runtime.default(); runtime.prepare()
    logging.basicConfig(filename=runtime.home / "logs" / "app.log", level=logging.INFO, encoding="utf-8")
    settings = runtime.settings()
    if args.scan:
        from .scanner import Scanner
        root = (args.workspace or Path(settings["workspace"])).resolve(strict=True)
        store = Store(runtime.database(root))
        result = Scanner(store, root).scan(args.scan)
        if args.analyze and result["complete"]:
            from .analysis import Control, analyze
            result["analysis"] = analyze(store, runtime, root, result["album"], settings,
                                        lambda n, total, label: None, Control())
        if result["complete"]:
            settings["workspace"] = str(root); settings["last_album"] = result["album"]
            runtime.save_settings(settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    from PySide6.QtWidgets import QApplication, QFileDialog
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont, QFontDatabase
    app = QApplication(sys.argv)
    from .instance import SingleInstance
    instance = SingleInstance(runtime) if not args.screenshot else None
    if instance and not instance.claim():
        logging.info("重复启动：已请求激活现有窗口")
        return
    app.aboutToQuit.connect(instance.close) if instance else None
    # Windows already registers these fonts; manually loading full TTC files is
    # only needed by the offscreen verification platform.
    if app.platformName() == "offscreen":
        for font_name in ("msyh.ttc", "msyhbd.ttc"):
            font_path = Path("C:/Windows/Fonts") / font_name
            if font_path.exists(): QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont("Microsoft YaHei", 10))
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(app_icon())
    root = (args.workspace or Path(settings["workspace"])).resolve()
    if not root.is_dir():
        folder = QFileDialog.getExistingDirectory(None, "原工作区不可用，请选择照片工作区")
        if not folder:
            if instance: instance.close()
            return
        root = Path(folder)
    store = Store(runtime.database(root))
    from .gui import MainWindow
    if args.album: settings["last_album"] = args.album
    window = MainWindow(store, runtime, root, settings, defer_loading=True)
    if instance:
        def activate():
            window.showNormal(); window.raise_(); window.activateWindow()
        instance.activationRequested.connect(activate)
    if args.view:
        window.tabs.setCurrentIndex(["图库", "精简复核", "文件整理", "图集", "学习与资源"].index(args.view))
    window.show()
    logging.info("主窗口已显示：%.3f 秒；分析库未预加载", time.perf_counter()-started)
    if not args.screenshot:
        window.raise_(); window.activateWindow()
    if args.screenshot:
        def capture():
            window.grab().save(str(args.screenshot))
            app.quit()
        QTimer.singleShot(1500, capture)
    code = app.exec()
    if instance: instance.close()
    return code
