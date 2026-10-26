import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

from boyo.analysis import Control, analyze, direct_suggestions
from boyo.collections import Collections
from boyo.config import Runtime
from boyo.fs import checked, sha256
from boyo.operations import Operations
from boyo.scanner import Scanner
from boyo.store import Store


class PhotoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boyo-test-")
        self.root = Path(self.tmp.name)
        self.album = self.root / "261001测试"
        for folder in ("jpg", "raw", "精选/jpg", "精选/raw", "成片"):
            (self.album / folder).mkdir(parents=True, exist_ok=True)
        self.runtime = Runtime(self.root / "tools" / "test" / "local"); self.runtime.prepare()
        self.store = Store(self.runtime.db_path)
        self.scan = Scanner(self.store, self.root)
        self.ops = Operations(self.store, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def jpg(self, name, folder="jpg", seed=0):
        rng = np.random.default_rng(seed)
        image = Image.fromarray(rng.integers(0, 256, (240, 360, 3), dtype=np.uint8))
        draw = ImageDraw.Draw(image); draw.rectangle((40, 30, 180, 180), outline=(240, seed % 255, 80), width=12)
        path = self.album / folder / name; path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return path

    def raw(self, name):
        p = self.album / "raw" / name; p.write_bytes(b"RAW sample " + name.encode())
        return p

    def inventory(self):
        r = self.scan.scan(self.album)
        self.assertTrue(r["complete"])
        return self.store.assets(self.album.name)

    def asset(self, name, role="ordinary_jpeg"):
        return next(a for a in self.store.assets(self.album.name) if Path(a["path"]).name == name and a["role"] == role)

    def test_selected_copy_protects_original_and_raw(self):
        jpg = self.jpg("DSC1.JPG"); self.raw("DSC1.ARW")
        shutil.copy2(jpg, self.album / "精选/jpg/DSC1.JPG")
        self.inventory()
        self.assertTrue(self.asset("DSC1.JPG")["protected"])
        self.assertTrue(self.asset("DSC1.ARW", "ordinary_raw")["protected"])
        with self.assertRaises(ValueError): self.store.decide([self.asset("DSC1.JPG")["id"]], "delete", "manual")
        self.assertTrue(jpg.exists())

    def test_renamed_exact_selected_copy_binds_to_original(self):
        jpg = self.jpg("DSC1.JPG"); self.raw("DSC1.ARW")
        shutil.copy2(jpg, self.album / "精选/jpg/自己改名.JPG")
        self.inventory()
        self.assertTrue(self.asset("DSC1.JPG")["protected"])
        plan = self.ops.fill_selected_raws(self.album.name)
        self.assertEqual(len(plan["actions"]), 1)
        self.ops.execute(plan["id"])
        self.assertTrue((self.album / "精选/raw/自己改名.ARW").exists())
        self.assertEqual(self.ops.fill_selected_raws(self.album.name)["actions"], [])

    def test_same_filename_other_album_does_not_protect(self):
        jpg = self.jpg("DSC1.JPG")
        other = self.root / "另一组/精选/jpg"; other.mkdir(parents=True)
        shutil.copy2(jpg, other / "DSC1.JPG")
        self.inventory(); self.assertFalse(self.asset("DSC1.JPG")["protected"])

    def test_plan_is_read_only_and_exact_destinations(self):
        jpg = self.jpg("DSC2.JPG"); raw = self.raw("DSC2.ARW")
        xmp = raw.with_suffix(".xmp"); xmp.write_text("edit", encoding="utf-8")
        self.inventory(); self.store.decide([self.asset("DSC2.JPG")["id"]], "delete", "manual")
        plan = self.ops.deletion(self.album.name, include_raw=True)
        self.assertTrue(jpg.exists()); self.assertTrue(raw.exists())
        self.assertFalse((self.root / "已删除备份").exists())
        self.assertEqual(len(plan["actions"]), 3)
        result = self.ops.execute(plan["id"])
        self.assertEqual(result["state"], "done")
        self.assertTrue((self.root / "已删除备份" / self.album.name / "DSC2.JPG").exists())
        self.assertFalse((self.root / "已删除备份" / self.album.name / "jpg").exists())
        self.assertTrue((self.album / "deleteraw/DSC2.ARW").exists())
        self.assertTrue((self.album / "deleteraw/DSC2.xmp").exists())
        self.assertFalse(jpg.exists())
        with self.assertRaises(ValueError): self.ops.execute(plan["id"])

    def test_raw_can_be_excluded_only_with_explicit_service_option(self):
        self.jpg("DSC2.JPG"); raw = self.raw("DSC2.ARW")
        self.inventory(); self.store.decide([self.asset("DSC2.JPG")["id"]], "delete", "manual")
        plan = self.ops.deletion(self.album.name,include_raw=False)
        self.assertEqual(len(plan["actions"]), 1)
        self.ops.execute(plan["id"]); self.assertTrue(raw.exists())

    def test_added_selected_after_plan_blocks_all_moves(self):
        jpg = self.jpg("DSC2.JPG")
        self.inventory(); self.store.decide([self.asset("DSC2.JPG")["id"]], "delete", "manual")
        plan = self.ops.deletion(self.album.name)
        shutil.copy2(jpg, self.album / "精选/jpg/DSC2.JPG")
        with self.assertRaises(ValueError): self.ops.execute(plan["id"])
        self.assertTrue(jpg.exists())

    def test_source_changed_after_preview_blocks_all_moves(self):
        jpg = self.jpg("DSC2.JPG")
        self.inventory(); self.store.decide([self.asset("DSC2.JPG")["id"]], "delete", "manual")
        plan = self.ops.deletion(self.album.name)
        jpg.write_bytes(jpg.read_bytes() + b"changed")
        with self.assertRaises(ValueError): self.ops.execute(plan["id"])
        self.assertTrue(jpg.exists())

    def test_destination_collision_after_preview_never_overwrites(self):
        jpg = self.jpg("DSC2.JPG")
        self.inventory(); self.store.decide([self.asset("DSC2.JPG")["id"]], "delete", "manual")
        plan = self.ops.deletion(self.album.name)
        target = self.root / plan["actions"][0]["dst"]; target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")
        with self.assertRaises(ValueError): self.ops.execute(plan["id"])
        self.assertEqual(target.read_bytes(), b"existing"); self.assertTrue(jpg.exists())

    def test_restore_never_overwrites_and_can_restore(self):
        jpg = self.jpg("DSC2.JPG"); original = sha256(jpg)
        self.inventory(); self.store.decide([self.asset("DSC2.JPG")["id"]], "delete", "manual")
        plan = self.ops.deletion(self.album.name); self.ops.execute(plan["id"])
        j = self.store.one("SELECT * FROM journal WHERE plan_id=?", (plan["id"],))
        jpg.write_bytes(b"new existing file")
        with self.assertRaises(ValueError): self.ops.restore(j["id"])
        jpg.unlink()  # Test-owned file only.
        self.ops.restore(j["id"])
        self.assertEqual(sha256(jpg), original)
        self.assertEqual(self.asset("DSC2.JPG")["decision"], "pending")

    def test_orphan_raw_separate_and_selected_is_exception(self):
        self.raw("alone.ARW"); self.raw("selected.ARW"); self.jpg("selected.JPG", "精选/jpg")
        self.raw("hasjpg.ARW"); self.jpg("hasjpg.JPG")
        self.inventory(); plan = self.ops.orphan_raws(self.album.name)
        self.assertEqual([Path(a["src"]).name for a in plan["actions"]], ["alone.ARW"])
        self.assertTrue((self.album / "raw/alone.ARW").exists())
        self.jpg("alone.JPG")
        with self.assertRaises(ValueError): self.ops.execute(plan["id"])

    def test_raw_xmp_collision_uses_same_backup_stem(self):
        raw = self.raw("alone.ARW"); raw.with_suffix(".xmp").write_text("xmp")
        backup = self.album / "deleteraw"; backup.mkdir(); (backup / "alone.ARW").write_bytes(b"old")
        self.inventory(); plan = self.ops.orphan_raws(self.album.name)
        stems = {Path(a["dst"]).stem for a in plan["actions"]}
        self.assertEqual(stems, {"alone__备份1"})

    def test_fill_selected_is_copy_preserves_source_and_xmp(self):
        self.jpg("DSC3.JPG", "精选/jpg"); raw = self.raw("DSC3.ARW")
        raw.with_suffix(".xmp").write_text("my adjustment")
        self.inventory(); plan = self.ops.fill_selected_raws(self.album.name)
        self.assertEqual(len(plan["actions"]), 2); self.ops.execute(plan["id"])
        self.assertTrue(raw.exists()); self.assertTrue((self.album / "精选/raw/DSC3.ARW").exists())
        self.assertEqual((self.album / "精选/raw/DSC3.xmp").read_text(), "my adjustment")

    def test_cancelled_or_failed_scan_disables_operations(self):
        self.jpg("DSC1.JPG"); self.inventory()
        with patch("boyo.scanner.os.scandir", side_effect=PermissionError("denied")):
            self.assertFalse(self.scan.scan(self.album)["complete"])
            with self.assertRaises(ValueError): self.ops.orphan_raws(self.album.name)

    def test_fill_also_moves_obsolete_selected_raw_and_xmp_but_keeps_originals(self):
        original=self.raw('old.ARW');self.jpg('old.JPG')
        obsolete=self.album/'精选/raw/old.ARW';shutil.copy2(original,obsolete)
        sidecar=obsolete.with_suffix('.xmp');sidecar.write_text('selected edits')
        self.jpg('new.JPG','精选/jpg',seed=7);new_raw=self.raw('new.ARW')
        plan=self.ops.fill_selected_raws(self.album.name)
        self.assertTrue(obsolete.exists());self.assertFalse((self.album/'deleteraw').exists())
        self.assertEqual([a['action'] for a in plan['actions']],['copy','move','move'])
        result=self.ops.execute(plan['id']);self.assertEqual(result['state'],'done');self.assertEqual(result['done'],3)
        self.assertTrue(original.exists());self.assertTrue(new_raw.exists());self.assertTrue((self.album/'jpg/old.JPG').exists())
        self.assertFalse(obsolete.exists());self.assertFalse(sidecar.exists())
        self.assertEqual((self.album/'精选/raw/new.ARW').read_bytes(),new_raw.read_bytes())
        self.assertEqual((self.album/'deleteraw/old.xmp').read_text(),'selected edits')
        journal=self.store.one("SELECT * FROM journal WHERE plan_id=? AND src=?",(plan['id'],obsolete.relative_to(self.root).as_posix()))
        self.ops.restore(journal['id']);self.assertEqual(obsolete.read_bytes(),original.read_bytes())

    def test_selected_raw_cleanup_preserves_current_and_renamed_selected_matches(self):
        source=self.jpg('source.JPG');self.raw('source.ARW')
        shutil.copy2(source,self.album/'精选/jpg/renamed.JPG')
        selected_raw=self.album/'精选/raw/source.ARW';selected_raw.write_bytes(b'renamed jpg still needs this raw')
        self.jpg('keep.JPG','精选/jpg',seed=3);(self.album/'精选/raw/keep.ARW').write_bytes(b'keep')
        plan=self.ops.fill_selected_raws(self.album.name)
        self.assertFalse(any(a['action']=='move' for a in plan['actions']));self.assertTrue(selected_raw.exists())

    def test_returned_selected_jpg_invalidates_cleanup_before_any_file_changes(self):
        obsolete=self.album/'精选/raw/old.ARW';obsolete.write_bytes(b'old raw')
        self.jpg('new.JPG','精选/jpg',seed=2);self.raw('new.ARW')
        plan=self.ops.fill_selected_raws(self.album.name);self.jpg('old.JPG','精选/jpg',seed=4)
        with self.assertRaises(ValueError):self.ops.execute(plan['id'])
        self.assertTrue(obsolete.exists());self.assertFalse((self.album/'精选/raw/new.ARW').exists())

    def test_selected_raw_cleanup_conflicts_preserve_files_and_backup_never_overwrites(self):
        for name in ('ambiguous.ARW','ambiguous.NEF','old.ARW'):(self.album/'精选/raw'/name).write_bytes(name.encode())
        (self.album/'精选/raw/old.xmp').write_text('edits')
        backup=self.album/'deleteraw';backup.mkdir();(backup/'old.ARW').write_bytes(b'previous')
        plan=self.ops.fill_selected_raws(self.album.name)
        self.assertEqual({Path(a['src']).name for a in plan['actions']},{'old.ARW','old.xmp'})
        self.assertEqual({Path(a['dst']).stem for a in plan['actions']},{'old__备份1'})
        self.assertTrue(plan['warnings']);self.assertEqual(self.ops.execute(plan['id'])['state'],'done')
        self.assertEqual((backup/'old.ARW').read_bytes(),b'previous');self.assertTrue((self.album/'精选/raw/ambiguous.ARW').exists())

    def test_selected_raw_cleanup_stops_on_external_selected_change_between_moves(self):
        for name in ('old.ARW','old.xmp'):(self.album/'精选/raw'/name).write_bytes(b'old')
        plan=self.ops.fill_selected_raws(self.album.name)
        def added_selected(done,total,message):
            if done==1:self.jpg('old.JPG','精选/jpg')
        result=self.ops.execute(plan['id'],added_selected)
        self.assertEqual(result['state'],'partial');self.assertEqual(result['done'],1)
        self.assertTrue((self.album/'精选/raw/old.xmp').exists());self.assertIn('目录发生变化',result['error'])

    def test_selected_raw_cleanup_does_not_relax_original_raw_protection(self):
        self.jpg('keep.JPG','精选/jpg');self.raw('keep.ARW');self.inventory()
        original=self.asset('keep.ARW','ordinary_raw')
        action=self.ops._snapshot(original,(Path(self.album.name)/'deleteraw/keep.ARW').as_posix(),'move','invalid cleanup')
        plan=self.ops._save(self.album.name,'fill_raw',[action],[])
        with self.assertRaises(ValueError):self.ops.execute(plan['id'])
        self.assertTrue((self.album/'raw/keep.ARW').exists())

    def test_traversal_paths_are_rejected(self):
        for p in ("../outside", "C:/outside", "/outside"):
            with self.assertRaises(ValueError): checked(self.root, p)

    def test_excluded_folders_not_reanalyzed(self):
        self.jpg("live.JPG")
        self.jpg("backup.JPG", "deleteraw")
        self.jpg("export.JPG", "图集/图集001")
        names = {Path(a["path"]).name for a in self.inventory()}
        self.assertEqual(names, {"live.JPG"})

    def test_ambiguous_raw_pair_is_not_moved(self):
        self.jpg("DSC2.JPG"); self.raw("DSC2.ARW"); self.raw("DSC2.NEF")
        self.inventory(); self.store.decide([self.asset("DSC2.JPG")["id"]], "delete", "manual")
        plan = self.ops.deletion(self.album.name, True)
        self.assertEqual(plan["actions"], []); self.assertTrue(plan["warnings"])

    def test_conflicting_camera_metadata_does_not_pair_raw(self):
        self.jpg("DSC2.JPG"); self.raw("DSC2.ARW"); self.inventory()
        jpg, raw = self.asset("DSC2.JPG"), self.asset("DSC2.ARW", "ordinary_raw")
        self.store.execute("UPDATE assets SET metadata=? WHERE id=?", (json.dumps({"reader_version": 2, "camera": "相机A"}), jpg["id"]))
        self.store.execute("UPDATE assets SET metadata=? WHERE id=?", (json.dumps({"reader_version": 2, "camera": "相机B"}), raw["id"]))
        self.store.decide([jpg["id"]], "delete", "manual")
        plan = self.ops.deletion(self.album.name, True)
        self.assertEqual(plan["actions"], [])
        self.assertIn("相机/时间", plan["warnings"][0])

    def test_analysis_cache_no_file_changes_and_no_transitive_deletion(self):
        first = self.jpg("a.JPG"); shutil.copy2(first, self.album / "jpg/b.JPG")
        self.jpg("different.JPG", seed=7)
        before = {p: sha256(p) for p in self.album.rglob("*.JPG")}
        self.inventory(); settings = {"strength": "保守", "delay_ms": 0}
        result = analyze(self.store, self.runtime, self.root, self.album.name, settings, lambda *a: None, Control())
        self.assertGreaterEqual(result["candidates"], 1)
        again = analyze(self.store, self.runtime, self.root, self.album.name, settings, lambda *a: None, Control())
        self.assertEqual(again["cached"], 3)
        self.assertEqual(before, {p: sha256(p) for p in self.album.rglob("*.JPG")})
        assets = [{"id": i, "protected": 0, "path": str(i)} for i in (1, 2, 3)]
        edges = {1: {2: (2, 2)}, 2: {1: (2, 2), 3: (2, 2)}, 3: {2: (2, 2)}}
        groups = direct_suggestions(assets, edges, {1: 1, 2: 0.9, 3: 0.8})
        self.assertEqual(groups, [{"keep": 1, "candidates": [{"id": 2, "phash_distance": 2, "dhash_distance": 2}]}])

    def test_selected_copy_reuses_analysis_and_feedback_survives_backup_move(self):
        jpg = self.jpg("a.JPG"); self.jpg("b.JPG", seed=1)
        shutil.copy2(jpg, self.album / "精选/jpg/a.JPG")
        self.inventory()
        settings = {"strength": "保守", "delay_ms": 0}
        result = analyze(self.store, self.runtime, self.root, self.album.name, settings, lambda *a: None, Control())
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["cached"], 1)
        self.store.decide([self.asset("a.JPG")["id"]], "keep", "confirmed-group")
        self.store.decide([self.asset("b.JPG")["id"]], "delete", "confirmed-group")
        plan = self.ops.deletion(self.album.name)
        self.ops.execute(plan["id"])
        from boyo.preferences import train
        status = train(self.store, self.runtime)
        self.assertEqual(status["pairs"], 1)

    def test_collections_cover_unique_and_no_preference_feedback(self):
        cover = self.jpg("cover.JPG", "成片", seed=1)
        self.jpg("ordinary.JPG", seed=2)
        self.jpg("second.JPG", "成片", seed=3)
        self.inventory()
        sets = Collections(self.store, self.root); one = sets.create(self.album.name)
        normal = self.asset("ordinary.JPG")
        with self.assertRaises(ValueError): sets.add(one, normal["id"])
        sets.add(one, self.asset("cover.JPG", "finished")["id"])
        sets.add(one, normal["id"])
        two = sets.create(self.album.name)
        sets.add(two, self.asset("second.JPG", "finished")["id"])
        with self.assertRaises(ValueError): sets.add(two, normal["id"])
        self.assertEqual(self.store.one("SELECT COUNT(*) AS n FROM reviews")["n"], 0)

    def test_collection_exact_copy_different_name_is_not_reused(self):
        jpg = self.jpg("a.JPG"); shutil.copy2(jpg, self.album / "jpg/b.JPG")
        self.jpg("cover.JPG", "成片", seed=1); self.jpg("cover2.JPG", "成片", seed=2)
        self.inventory(); sets = Collections(self.store, self.root)
        one = sets.create(self.album.name); two = sets.create(self.album.name)
        sets.add(one, self.asset("cover.JPG", "finished")["id"]); sets.add(one, self.asset("a.JPG")["id"])
        sets.add(two, self.asset("cover2.JPG", "finished")["id"])
        with self.assertRaises(ValueError): sets.add(two, self.asset("b.JPG")["id"])

    def test_generate_preserves_locks_and_export_is_copy(self):
        self.jpg("cover.JPG", "成片", seed=1)
        for i in range(4): self.jpg(f"ordinary{i}.JPG", seed=i+5)
        self.inventory(); settings = {"strength": "保守", "delay_ms": 0}
        analyze(self.store, self.runtime, self.root, self.album.name, settings, lambda *a: None, Control())
        sets = Collections(self.store, self.root); set_id = sets.create(self.album.name)
        cover = self.asset("cover.JPG", "finished"); sets.add(set_id, cover["id"])
        result = sets.generate(set_id, count=4)
        self.assertEqual(result["count"], 4)
        self.assertEqual(sets.items(set_id)[0]["id"], cover["id"])
        self.assertTrue(sets.items(set_id)[0]["locked"])
        plan = self.ops.export_set(set_id)
        self.assertTrue(all(a["action"] == "copy" for a in plan["actions"]))
        self.ops.execute(plan["id"])
        self.assertTrue((self.album / "图集/图集001/01_cover.JPG").exists())
        self.assertTrue((self.album / "成片/cover.JPG").exists())


if __name__ == "__main__":
    unittest.main()
