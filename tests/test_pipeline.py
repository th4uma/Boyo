import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import concurrent.futures
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication

from boyo.analysis import analyze
from boyo.config import Runtime
from boyo.instance import SingleInstance
from boyo.scanner import Scanner
from boyo.settings_io import atomic_json
from boyo.store import Store
from boyo.tasks import Control, ProgressReporter


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boyo-pipeline-")
        self.root = Path(self.tmp.name)
        self.album = self.root / "拍摄组"
        folder = self.album / "jpg"; folder.mkdir(parents=True)
        for n in range(8):
            Image.effect_noise((240, 160), 20+n).convert("RGB").save(folder / f"{n}.jpg")
        self.runtime = Runtime(self.root / "tools/Boyo/local"); self.runtime.prepare()
        self.store = Store(self.runtime.database(self.root))
        self.scanner = Scanner(self.store, self.root); self.scanner.scan(self.album)
        self.settings = {**self.runtime.settings(), "delay_ms": 0, "analysis_workers": 2}

    def tearDown(self):
        self.tmp.cleanup()

    def run_analysis(self, control=None, progress=None):
        return analyze(self.store, self.runtime, self.root, self.album.name, self.settings, progress or (lambda *args: None), control or Control())

    def test_cold_analysis_parallelism_is_bounded_and_warm_run_reuses_groups(self):
        from boyo.imaging import extract
        lock = threading.Lock(); active = 0; peak = 0
        def measured(*args):
            nonlocal active, peak
            with lock: active += 1; peak = max(peak, active)
            try:
                time.sleep(0.03)
                return extract(*args)
            finally:
                with lock: active -= 1
        with patch("boyo.analysis.extract", side_effect=measured) as extraction:
            first = self.run_analysis()
            self.assertEqual(extraction.call_count, 8)
            self.assertEqual(peak, 2)
        self.assertFalse(first["cancelled"])
        with patch("boyo.analysis.extract") as extraction, patch("boyo.analysis.direct_suggestions") as grouping, patch.object(self.store, "feature") as per_file_query:
            second = self.run_analysis()
            self.assertTrue(second["reused_groups"])
            self.assertEqual(second["cached"], 8)
            extraction.assert_not_called(); grouping.assert_not_called(); per_file_query.assert_not_called()
        self.settings["strength"] = "较强"
        self.assertFalse(self.run_analysis()["reused_groups"])

    def test_cancel_stops_queue_and_keeps_originals_and_previous_groups(self):
        from boyo.imaging import extract
        control = Control()
        old = {"id": "previous", "keep": self.store.assets(self.album.name)[0]["id"], "candidates": [], "strength": "保守"}
        self.store.execute("INSERT INTO groups VALUES (?,?,?)", (old["id"], self.album.name, json.dumps(old)))
        def progress(n, total, label):
            if label.startswith("图像分析"): control.cancel.set()
        def slow(*args):
            time.sleep(0.03); return extract(*args)
        with patch("boyo.analysis.extract", side_effect=slow) as extraction:
            result = self.run_analysis(control, progress)
            self.assertTrue(result["cancelled"])
            self.assertLessEqual(extraction.call_count, 2)
        self.assertEqual(self.store.groups(self.album.name), [old])
        self.assertEqual(len(list((self.album / "jpg").glob("*.jpg"))), 8)

    def test_warm_scan_reuses_metadata_without_reading_photos(self):
        with patch("boyo.scanner.metadata") as read_metadata, patch("boyo.scanner.sha256") as digest:
            self.assertTrue(self.scanner.scan(self.album)["complete"])
            read_metadata.assert_not_called(); digest.assert_not_called()

    def test_file_changes_invalidate_cached_analysis(self):
        self.run_analysis()
        Image.new("RGB", (400, 300), "red").save(self.album / "jpg/0.jpg")
        with self.assertRaisesRegex(ValueError, "重新扫描"):
            self.run_analysis()
        self.scanner.scan(self.album)
        with patch("boyo.analysis.extract", wraps=__import__("boyo.imaging", fromlist=["extract"]).extract) as extraction:
            result = self.run_analysis()
            self.assertEqual(extraction.call_count, 1)
            self.assertFalse(result["reused_groups"])

    def test_settings_concurrent_writers_use_separate_atomic_files(self):
        def write(n):
            self.runtime.save_settings({"writer": n, "payload": "x"*1000})
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(16)))
        saved = json.loads((self.runtime.home / "settings/settings.json").read_text(encoding="utf-8"))
        self.assertIn(saved["writer"], range(16)); self.assertEqual(saved["payload"], "x"*1000)
        self.assertEqual(list((self.runtime.home / "settings").glob("*.tmp")), [])

    def test_settings_sharing_retry_has_a_fixed_limit(self):
        with patch("boyo.settings_io.os.replace", side_effect=PermissionError("sharing")) as replace, patch("boyo.settings_io.time.sleep"):
            with self.assertRaises(PermissionError): atomic_json(self.runtime.home / "settings/test.json", {})
            self.assertEqual(replace.call_count, 4)
        self.assertFalse(list((self.runtime.home / "settings").glob("*.tmp")))

    def test_corrupt_settings_does_not_prevent_startup_and_old_default_delay_migrates(self):
        path = self.runtime.home / "settings/settings.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(self.runtime.settings()["delay_ms"], 0)
        path.write_text('{"delay_ms":60}', encoding="utf-8")
        self.assertEqual(self.runtime.settings()["delay_ms"], 0)
        path.write_text('{"delay_ms":100}', encoding="utf-8")
        self.assertEqual(self.runtime.settings()["delay_ms"], 100)

    def test_single_instance_activates_existing_owner_and_releases_lock(self):
        first, second = SingleInstance(self.runtime), SingleInstance(self.runtime)
        try:
            self.assertTrue(first.claim())
            spy = QSignalSpy(first.activationRequested)
            self.assertFalse(second.claim())
            if not spy.count(): spy.wait(1000)
            self.assertGreaterEqual(spy.count(), 1)
            first.close()
            self.assertTrue(second.claim())
        finally:
            first.close(); second.close()

    def test_upgrade_activates_previous_instance_instead_of_opening_another(self):
        from PySide6.QtTest import QSignalSpy
        first, second = SingleInstance(self.runtime), SingleInstance(self.runtime)
        first.name = first.previous_name
        try:
            self.assertTrue(first.claim())
            spy = QSignalSpy(first.activationRequested)
            self.assertFalse(second.claim())
            if not spy.count(): spy.wait(1000)
            self.assertGreaterEqual(spy.count(), 1)
        finally:
            first.close(); second.close()

    def test_single_instance_permission_error_is_reported_instead_of_silent_exit(self):
        from PySide6.QtCore import QLockFile
        with patch("boyo.instance.QLockFile") as mocked:
            mocked.return_value.tryLock.return_value = False
            mocked.return_value.error.return_value = QLockFile.LockError.PermissionError
            guard = SingleInstance(self.runtime)
            with self.assertRaisesRegex(OSError, "启动锁"):
                guard.claim()

    def test_progress_messages_are_coalesced_instead_of_flooding_ui(self):
        events = []
        reporter = ProgressReporter(lambda *args: events.append(args))
        with patch("boyo.tasks.time.monotonic", return_value=100):
            for n in range(10000): reporter(n, 10000, "progress")
            reporter(10000, 10000, "done")
        self.assertEqual(len(events), 2)

    def test_gui_import_does_not_preload_scientific_analysis_libraries(self):
        code = "import sys,json; import boyo.gui; print(json.dumps([n for n in ('numpy','scipy','sklearn','imagehash') if n in sys.modules]))"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.strip()), [])

    def test_remembering_last_album_failure_does_not_crash_window(self):
        from boyo.gui import MainWindow
        with patch.object(self.runtime, "save_settings", side_effect=PermissionError("locked")), self.assertLogs(level="ERROR"):
            window = MainWindow(self.store, self.runtime, self.root, self.settings)
            self.assertEqual(window.gallery.count(), 8)
        window.close(); window.deleteLater(); self.app.processEvents()

    def test_uncached_gallery_thumbnails_decode_in_background_and_stop_when_hidden(self):
        from boyo.gui import MainWindow
        from boyo.thumbnail_loader import read_image
        calls = []
        main_thread = threading.get_ident()
        def slow(*args):
            calls.append(threading.get_ident()); time.sleep(0.02)
            return read_image(*args)
        window = MainWindow(self.store, self.runtime, self.root, self.settings, defer_loading=True)
        try:
            with patch("boyo.thumbnail_loader.read_image", side_effect=slow):
                self.assertEqual(calls, [])
                window.show()
                deadline = time.monotonic()+5
                while (window.gallery.count() < 8 or any(window.gallery.item(i).icon().isNull() for i in range(window.gallery.count()))) and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertEqual(window.gallery.count(), 8)
                self.assertTrue(all(not window.gallery.item(i).icon().isNull() for i in range(8)))
                from boyo.imaging import thumbnail_path
                self.assertTrue(all(thumbnail_path(self.runtime, a).exists() for a in window.assets if a["role"] == "ordinary_jpeg"))
                self.assertTrue(calls); self.assertTrue(all(t != main_thread for t in calls))
                self.assertEqual(window.thumbnail_loader.pool.maxThreadCount(), 1)
        finally:
            window.close(); window.thumbnail_loader.pool.waitForDone(2000)
            self.app.processEvents(); window.deleteLater()

    def test_full_launcher_starts_repeatedly_with_isolated_runtime(self):
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ, BOYO_LOCAL=str(self.runtime.home), QT_QPA_PLATFORM="offscreen")
        for n in range(2):
            output = self.runtime.home / f"logs/startup-{n}.png"
            result = subprocess.run([sys.executable, str(repo / "launcher.py"), "--workspace", str(self.root),
                                     "--album", self.album.name, "--screenshot", str(output)],
                                    capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
        self.assertFalse((self.runtime.home / "logs/startup-error.log").exists())


if __name__ == "__main__": unittest.main()
