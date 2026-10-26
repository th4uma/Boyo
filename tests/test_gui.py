import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import shutil
import tempfile
import unittest
import time
from unittest.mock import patch
from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QApplication, QDialogButtonBox
from PySide6.QtTest import QTest

from boyo.config import Runtime
from boyo.gui import MainWindow, PlanDialog
from boyo.scanner import Scanner
from boyo.store import Store
from boyo.tasks import Control


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boyo-gui-")
        self.root = Path(self.tmp.name)
        folder = self.root / "拍摄组/jpg"; folder.mkdir(parents=True)
        Image.new("RGB", (80, 60), "green").save(folder / "test.JPG")
        self.runtime = Runtime(self.root / "tools/Boyo/local"); self.runtime.prepare()
        self.store = Store(self.runtime.database(self.root))
        Scanner(self.store, self.root).scan(self.root / "拍摄组")
        self.window = MainWindow(self.store, self.runtime, self.root, self.runtime.settings())

    def tearDown(self):
        self.window.close(); self.window.deleteLater(); self.app.processEvents()
        self.tmp.cleanup()

    def test_gallery_mark_changes_state_not_file(self):
        self.assertEqual(self.window.gallery.count(), 1)
        self.window.gallery.item(0).setSelected(True)
        self.window.gallery_decide("delete")
        self.assertTrue((self.root / "拍摄组/jpg/test.JPG").exists())
        self.assertEqual(self.store.assets("拍摄组")[0]["decision"], "delete")

    def test_selected_filter_and_protection(self):
        selected = self.root / "拍摄组/精选/jpg"; selected.mkdir(parents=True)
        shutil.copy2(self.root / "拍摄组/jpg/test.JPG", selected / "test.JPG")
        Scanner(self.store, self.root).scan(self.root / "拍摄组")
        self.window.refresh(); self.window.filter.setCurrentText("受保护图片")
        self.assertEqual(self.window.gallery.count(), 2)
        self.assertTrue(all(a["protected"] for a in self.window.gallery_assets()))

    def test_plan_execution_requires_explicit_checkbox(self):
        plan = {"actions": [{"action": "move", "src": "拍摄组/jpg/test.JPG", "dst": "已删除备份/拍摄组/test.JPG", "reason": "测试"}], "warnings": []}
        dialog = PlanDialog(plan, self.window)
        buttons = dialog.findChild(QDialogButtonBox)
        execute = next(b for b in buttons.buttons() if b.text() == "执行清单")
        self.assertFalse(execute.isEnabled())
        dialog.agree.setChecked(True); self.assertTrue(execute.isEnabled())
        dialog.close()

    def test_scan_button_runs_scan_then_analysis_in_one_background_task(self):
        self.window.scan_button.click()
        self.assertIsNotNone(self.window.thread)
        deadline = time.monotonic()+30
        while self.window.thread is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)  # Release the GIL so the Python worker can run.
        self.assertIsNone(self.window.thread)
        self.assertEqual(len(self.store.feature_snapshot("拍摄组")), 1)
        self.assertIn("扫描并分析完成", self.window.status.text())
        self.assertTrue((self.root / "拍摄组/jpg/test.JPG").exists())
        self.assertFalse((self.root / "已删除备份").exists())

    def test_incomplete_scan_never_starts_analysis(self):
        with patch.object(self.window, "run_task") as queue:
            self.window.scan()
        work, done = queue.call_args.args
        with patch("boyo.gui.Scanner") as scanner, patch("boyo.analysis.analyze") as analysis:
            scanner.return_value.scan.return_value = {"complete": False, "errors": ["denied"]}
            result = work(lambda *args: None, Control())
            analysis.assert_not_called()
        done(result)
        self.assertIn("未开始分析", self.window.status.text())

    def test_cancel_between_scan_and_analysis_never_starts_analysis(self):
        with patch.object(self.window, "run_task") as queue:
            self.window.scan()
        work, done = queue.call_args.args
        control = Control()
        def finish_scan(*args):
            control.cancel.set()
            return {"complete": True}
        with patch("boyo.gui.Scanner") as scanner, patch("boyo.analysis.analyze") as analysis:
            scanner.return_value.scan.side_effect = finish_scan
            result = work(lambda *args: None, control)
            analysis.assert_not_called()
        done(result)
        self.assertIn("已取消扫描", self.window.status.text())


if __name__ == "__main__":
    unittest.main()
