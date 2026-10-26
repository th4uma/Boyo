import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import json
import shutil
import tempfile
import unittest
import threading
import time
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageOps
from PySide6.QtCore import QSize, Qt
from PySide6.QtTest import QTest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog

from boyo.config import Runtime
from boyo.gui import MainWindow, PlanDialog
from boyo.operations import Operations
from boyo.photo_widgets import ImageCache, PhotoPreviewDialog, PreviewQueue, display_pixmap
from boyo.scanner import Scanner
from boyo.store import Store


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boyo-review-")
        self.root = Path(self.tmp.name)
        self.album = self.root / "拍摄组"
        (self.album / "jpg").mkdir(parents=True)
        for n, color in enumerate(("green", "red", "blue", "yellow"), 1):
            Image.new("RGB", (120, 80), color).save(self.album / f"jpg/{n}.JPG")
        (self.album / "精选/jpg").mkdir(parents=True)
        shutil.copy2(self.album / "jpg/4.JPG", self.album / "精选/jpg/4.JPG")
        (self.album / "raw").mkdir()
        (self.album / "raw/2.ARW").write_bytes(b"RAW sample")
        (self.album / "raw/2.xmp").write_text("metadata", encoding="utf-8")
        self.runtime = Runtime(self.root / "tools/Boyo/local"); self.runtime.prepare()
        self.store = Store(self.runtime.database(self.root))
        Scanner(self.store, self.root).scan(self.album)
        self.ids = {Path(a["path"]).stem: a["id"] for a in self.store.assets(self.album.name) if a["role"] == "ordinary_jpeg"}
        self.group = {"id": "review-group", "keep": self.ids["1"], "strength": "保守",
                      "candidates": [{"id": self.ids[n], "phash_distance": 2, "dhash_distance": 3} for n in ("2", "3", "4")]}
        self.store.execute("INSERT INTO groups VALUES (?,?,?)", (self.group["id"], self.album.name, json.dumps(self.group)))
        self.window = MainWindow(self.store, self.runtime, self.root, self.runtime.settings())
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close(); dialog.deleteLater()
        self.window.close(); self.window.deleteLater(); self.app.processEvents()
        self.tmp.cleanup()

    def preview(self, ids=None, current=None, **kwargs):
        dialog = PhotoPreviewDialog(self.store, self.runtime, self.root,
                                    ids or list(self.ids.values()), current or self.ids["1"], parent=self.window, **kwargs)
        self.dialogs.append(dialog)
        return dialog

    def plan(self):
        self.store.decide([self.ids["2"], self.ids["3"]], "delete", self.group["id"])
        plan = Operations(self.store, self.root).deletion(self.album.name, include_raw=True)
        dialog = PlanDialog(plan, self.window); self.dialogs.append(dialog)
        return dialog

    def test_preview_honors_all_eight_exif_orientations_including_mirrors(self):
        source = Image.new("RGB", (96, 64))
        for box, color in (((0, 0, 48, 32), "red"), ((48, 0, 96, 32), "green"),
                           ((0, 32, 48, 64), "blue"), ((48, 32, 96, 64), "yellow")):
            source.paste(color, box)
        for orientation in range(1, 9):
            with self.subTest(orientation=orientation):
                path = self.root / f"orientation-{orientation}.jpg"
                exif = Image.Exif(); exif[274] = orientation
                source.save(path, exif=exif, quality=100, subsampling=0)
                with Image.open(path) as image:
                    expected = ImageOps.exif_transpose(image).convert("RGB")
                actual = display_pixmap(path).toImage()
                self.assertEqual((actual.width(), actual.height()), expected.size)
                for x, y in ((8, 8), (actual.width()-9, 8), (8, actual.height()-9), (actual.width()-9, actual.height()-9)):
                    self.assertLess(max(abs(a-b) for a, b in zip(actual.pixelColor(x, y).getRgb()[:3], expected.getpixel((x, y)))), 4)
        large = self.root / "large.jpg"
        exif = Image.Exif(); exif[274] = 6
        Image.new("RGB", (3600, 2400)).save(large, exif=exif)
        pix = display_pixmap(large, QSize(600, 600))
        self.assertLessEqual(max(pix.width(), pix.height()), 600)
        self.assertLess(pix.width(), pix.height())

    def test_arrow_keys_buttons_boundaries_and_decisions_stay_in_open_dialog(self):
        dialog = self.preview(context=self.group["id"])
        dialog.decisionChanged.connect(lambda aid, decision: self.window.decisions_changed({aid: decision}))
        dialog.show(); dialog.activateWindow(); self.app.processEvents()
        self.assertFalse(dialog.previous_button.isEnabled())
        QTest.keyClick(dialog, Qt.Key.Key_Right); self.app.processEvents()
        self.assertEqual(dialog.current_id, self.ids["2"])
        QTest.keyClick(dialog, Qt.Key.Key_D)
        self.assertEqual(self.store.asset(self.ids["2"])["decision"], "delete")
        QTest.keyClick(dialog, Qt.Key.Key_K)
        self.assertEqual(self.store.asset(self.ids["2"])["decision"], "keep")
        QTest.keyClick(dialog, Qt.Key.Key_U)
        self.assertEqual(self.store.asset(self.ids["2"])["decision"], "pending")
        dialog.next_button.click(); dialog.next_button.click()
        self.assertEqual(dialog.current_id, self.ids["4"])
        self.assertFalse(dialog.next_button.isEnabled())
        QTest.keyClick(dialog, Qt.Key.Key_Right)
        self.assertEqual(dialog.current_id, self.ids["4"])
        QTest.keyClick(dialog, Qt.Key.Key_Left)
        self.assertEqual(dialog.current_id, self.ids["3"])
        self.assertTrue(dialog.isVisible())
        self.assertTrue((self.album / "jpg/2.JPG").exists())
        self.assertFalse((self.root / "已删除备份").exists())

    def test_protected_and_reference_only_previews_cannot_mark_delete(self):
        dialog = self.preview(current=self.ids["4"])
        self.assertFalse(dialog.delete_button.isEnabled())
        dialog.decide("delete")
        self.assertNotEqual(self.store.asset(self.ids["4"])["decision"], "delete")
        reference = self.preview(editable_ids=[])
        self.assertFalse(reference.keep_button.isEnabled())
        reference.decide("delete")
        self.assertEqual(self.store.asset(self.ids["1"])["decision"], "pending")

    def test_individual_card_decisions_survive_adopting_remaining_suggestions(self):
        self.window.review_cards[self.ids["2"]].keep_button.click()
        self.window.accept_group()
        self.assertEqual(self.store.asset(self.ids["2"])["decision"], "keep")
        self.assertEqual(self.store.asset(self.ids["3"])["decision"], "delete")
        self.assertNotEqual(self.store.asset(self.ids["4"])["decision"], "delete")
        self.window.review_cards[self.ids["3"]].pending_button.click()
        self.assertEqual(self.store.asset(self.ids["3"])["decision"], "pending")
        self.window.review_cards[self.ids["1"]].delete_button.click()
        self.assertFalse(self.window.adopt_button.isEnabled())
        self.assertFalse(self.window.review_cards[self.ids["4"]].delete_button.isEnabled())

    def test_review_navigation_scope_and_gallery_navigation_crosses_thumbnail_pages(self):
        with patch("boyo.gui.PhotoPreviewDialog") as preview:
            self.window.open_review_preview(self.ids["2"])
            args, kwargs = preview.call_args
            self.assertEqual(args[3], [self.ids[n] for n in ("1", "2", "3", "4")])
            self.assertEqual(kwargs["context"], self.group["id"])
        self.window.settings["page_size"] = 1; self.window.refresh_gallery()
        self.assertEqual(self.window.gallery.count(), 4)
        with patch("boyo.gui.PhotoPreviewDialog") as preview:
            self.window.preview(self.window.gallery.item(0))
            self.assertEqual(len(preview.call_args.args[3]), 4)

    def test_visual_plan_rescue_removes_raw_bundle_and_executes_only_remaining_photo(self):
        dialog = self.plan()
        self.assertEqual(dialog.tabs.tabText(0), "看图复核")
        self.assertIsNotNone(dialog.reference_card)
        self.assertEqual(len(dialog.original_plan["actions"]), 4)
        dialog.agree.setChecked(True)
        dialog.candidate_card.keep_button.click()
        self.assertFalse(dialog.agree.isChecked())
        remaining = dialog.remaining_actions()
        self.assertEqual([a["src"] for a in remaining], ["拍摄组/jpg/3.JPG"])
        self.assertTrue((self.album / "jpg/2.JPG").exists())
        self.assertTrue((self.album / "raw/2.ARW").exists())
        self.assertFalse((self.root / "已删除备份").exists())
        dialog.agree.setChecked(True); dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertNotEqual(dialog.plan["id"], dialog.original_plan["id"])
        service = Operations(self.store, self.root)
        with self.assertRaises(ValueError): service.execute(dialog.original_plan["id"])
        service.execute(dialog.plan["id"])
        self.assertTrue((self.album / "jpg/2.JPG").exists())
        self.assertTrue((self.album / "raw/2.ARW").exists())
        self.assertTrue((self.album / "raw/2.xmp").exists())
        self.assertTrue((self.root / "已删除备份/拍摄组/3.JPG").exists())

    def test_plan_can_retract_rescue_or_cancel_and_cannot_execute_empty(self):
        dialog = self.plan()
        dialog.candidate_card.keep_button.click()
        dialog.candidate_card.delete_button.click()
        self.assertEqual(len(dialog.remaining_actions()), 4)
        dialog.candidate_card.keep_button.click()
        dialog.next_photo.click()
        dialog.candidate_card.pending_button.click()
        self.assertEqual(dialog.remaining_actions(), [])
        dialog.agree.setChecked(True)
        self.assertFalse(dialog.execute_button.isEnabled())
        dialog.accept()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.reject()
        self.assertTrue((self.album / "jpg/2.JPG").exists())
        self.assertTrue((self.album / "jpg/3.JPG").exists())
        self.assertEqual(self.store.one("SELECT COUNT(*) AS n FROM journal")["n"], 0)

    def test_reduced_plan_api_cannot_add_unlisted_photos(self):
        dialog = self.plan()
        with self.assertRaises(ValueError):
            Operations(self.store, self.root).exclude_from_deletion(dialog.plan["id"], [self.ids["1"]])
        self.assertEqual(Operations(self.store, self.root).get_plan(dialog.plan["id"])[0]["state"], "preview")

    def test_mixed_decision_batch_rolls_back_if_any_photo_is_protected(self):
        with self.assertRaises(ValueError):
            self.store.decide_many({self.ids["1"]: "keep", self.ids["4"]: "delete"}, self.group["id"])
        self.assertEqual(self.store.asset(self.ids["1"])["decision"], "pending")
        self.assertIsNone(self.store.one("SELECT * FROM reviews WHERE asset_id=?", (self.ids["1"],)))

    def test_marking_a_photo_keeps_widgets_and_does_not_reload_any_images(self):
        from boyo import photo_widgets
        card = self.window.review_cards[self.ids["2"]]
        with patch.object(self.window, "refresh", wraps=self.window.refresh) as refresh, \
                patch("boyo.photo_widgets.asset_thumbnail", wraps=photo_widgets.asset_thumbnail) as thumbnail:
            card.delete_button.click(); card.keep_button.click(); card.pending_button.click()
            self.assertEqual(refresh.call_count, 0)
            self.assertEqual(thumbnail.call_count, 0)
        self.assertIs(self.window.review_cards[self.ids["2"]], card)
        self.assertIn("未决定", card.state_label.text())

    def test_review_list_uses_one_album_snapshot_not_one_database_connection_per_photo(self):
        with self.store.connect() as db:
            db.executemany("INSERT INTO groups VALUES (?,?,?)", [
                (f"extra-{n}", self.album.name, json.dumps({**self.group, "id": f"extra-{n}"})) for n in range(100)
            ])
        with patch.object(self.store, "asset", wraps=self.store.asset) as single_photo_query:
            self.window.refresh()
            self.assertEqual(single_photo_query.call_count, 0)
        self.assertEqual(self.window.group_list.count(), 101)

    def test_plan_rescue_updates_existing_cards_without_loading_thumbnails_again(self):
        from boyo import photo_widgets
        dialog = self.plan(); card = dialog.candidate_card
        with patch("boyo.photo_widgets.asset_thumbnail", wraps=photo_widgets.asset_thumbnail) as thumbnail:
            card.keep_button.click()
            self.assertEqual(thumbnail.call_count, 0)
        self.assertIs(dialog.candidate_card, card)
        self.assertIn("已从本次清单移出", dialog.bundle.text())

    def test_decoded_cache_evicts_by_memory_and_does_not_cache_oversize_images(self):
        cache = ImageCache(800)
        image = QImage(10, 10, QImage.Format.Format_RGB32)
        for key in (1, 2): cache.put(key, image)
        self.assertIsNotNone(cache.get(1))
        cache.put(3, image)
        self.assertIsNone(cache.get(2))
        cache.put(4, QImage(100, 100, QImage.Format.Format_RGB32))
        self.assertIsNone(cache.get(4)); self.assertLessEqual(cache.bytes, 800)

    def test_slow_decode_keeps_ui_responsive_and_discards_intermediate_requests(self):
        first, last = self.root / "slow-first.jpg", self.root / "slow-last.jpg"
        first.write_bytes(b"test"); last.write_bytes(b"last")
        loader = PreviewQueue()
        started, release = threading.Event(), threading.Event()
        calls, received = [], []
        main_thread = threading.get_ident()
        def slow_decode(path, limit):
            calls.append(Path(path).name); started.set()
            release.wait(2)
            image = QImage(20, 10, QImage.Format.Format_RGB32); image.fill(Qt.GlobalColor.green)
            return image
        loader.ready.connect(lambda token, image, error: received.append((token, threading.get_ident())))
        with patch("boyo.photo_widgets.read_image", side_effect=slow_decode):
            try:
                loader.request(loader.token(), first, QSize(100, 100))
                self.assertTrue(started.wait(1))
                for n in range(30):
                    final_token = loader.token(); loader.request(final_token, last, QSize(100, 100))
                # A queued Qt click still executes while image decoding is blocked.
                dialog = self.preview(); dialog.load_timer.stop()
                dialog.next_button.click()
                self.assertEqual(dialog.current_id, self.ids["2"])
                release.set()
                deadline = time.monotonic()+3
                while loader.active is not None and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertIsNone(loader.active)
                self.assertEqual(calls, [first.name, last.name])
                self.assertEqual([r[0] for r in received], [1, final_token])
                self.assertTrue(all(thread == main_thread for _, thread in received))
            finally:
                release.set(); loader.pool.waitForDone(3000); self.app.processEvents()

    def test_old_preview_results_cannot_replace_the_current_photo(self):
        dialog = self.preview(); dialog.load_timer.stop(); dialog.load_token = 999
        image = QImage(20, 10, QImage.Format.Format_RGB32); image.fill(Qt.GlobalColor.red)
        original = dialog.view.item.pixmap().cacheKey()
        dialog.image_ready(998, image, "")
        self.assertEqual(dialog.view.item.pixmap().cacheKey(), original)
        dialog.image_ready(999, image, "")
        self.assertEqual(dialog.view.item.pixmap().toImage().pixelColor(1, 1).name(), "#ff0000")

    def test_hidden_pending_gallery_is_refreshed_only_when_shown(self):
        self.window.filter.setCurrentText("待删 JPG")
        self.window.tabs.setCurrentIndex(1)
        with patch.object(self.window, "refresh_gallery", wraps=self.window.refresh_gallery) as refresh:
            self.window.review_cards[self.ids["2"]].delete_button.click()
            self.assertEqual(refresh.call_count, 0)
            self.window.tabs.setCurrentIndex(0)
            self.assertEqual(refresh.call_count, 1)
            self.assertEqual(self.window.gallery.count(), 1)

    def test_hidden_or_closed_preview_does_not_keep_a_decode_timer_running(self):
        dialog = self.preview()
        self.assertFalse(dialog.load_timer.isActive())
        dialog.show(); self.app.processEvents()
        dialog.hide()
        self.assertFalse(dialog.load_timer.isActive())
        self.assertIsNone(dialog.load_token)


if __name__ == "__main__":
    unittest.main()
