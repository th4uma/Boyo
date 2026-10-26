"""The deletion shortcut must preserve content checks and mid-batch protection."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from boyo.config import Runtime
from boyo.fs import sha256
from boyo.operations import Operations
from boyo.protection_watch import ProtectionWatch
from boyo.scanner import Scanner
from boyo.store import Store


class ProtectionWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='boyo-protection-')
        self.root=Path(self.tmp.name);self.album=self.root/'A'
        for folder in ('jpg','精选/jpg','成片'):(self.album/folder).mkdir(parents=True)
        for n in range(3):Image.new('RGB',(90,60),(20+n*30,90,100)).save(self.album/'jpg'/f'{n}.JPG')
        runtime=Runtime(self.root/'tools/Boyo/local');runtime.prepare()
        self.store=Store(runtime.database(self.root));Scanner(self.store,self.root).scan(self.album)
        self.store.decide([a['id'] for a in self.store.assets('A')],'delete','manual')
        self.ops=Operations(self.store,self.root)

    def tearDown(self):self.tmp.cleanup()

    def test_unchanged_batch_reuses_protection_walk_but_still_hashes_every_move_twice(self):
        with ProtectionWatch(self.album) as watch:available=watch.active
        plan=self.ops.deletion('A')
        with patch('boyo.operations.sha256',wraps=sha256) as content_check:
            result=self.ops.execute(plan['id'])
        self.assertEqual(result['done'],3);self.assertEqual(result['state'],'done')
        self.assertEqual(content_check.call_count,6)
        if available:
            self.assertEqual(result['timings']['protection_walks'],1)
            self.assertEqual(result['timings']['protection_reuses'],3)
        self.assertEqual(len(self.store.rows("SELECT * FROM journal WHERE state='done'")),3)

    def test_selected_added_during_execution_stops_remaining_moves(self):
        plan=self.ops.deletion('A');target=Path(plan['actions'][1]['src'])
        def progress(n,total,label):
            if label=='完成 1/3':shutil.copy2(self.root/target,self.album/'精选/jpg'/target.name)
        result=self.ops.execute(plan['id'],progress)
        self.assertEqual(result['state'],'partial');self.assertEqual(result['done'],1)
        self.assertTrue((self.root/target).exists());self.assertIn('目录发生变化',result['error'])
        self.assertTrue((self.album/'精选/jpg'/target.name).exists())

    def test_missing_protection_folder_created_mid_batch_is_detected(self):
        shutil.rmtree(self.album/'精选')
        plan=self.ops.deletion('A');target=Path(plan['actions'][1]['src'])
        def progress(n,total,label):
            if label=='完成 1/3':
                (self.album/'精选/jpg').mkdir(parents=True)
                shutil.copy2(self.root/target,self.album/'精选/jpg'/target.name)
        result=self.ops.execute(plan['id'],progress)
        self.assertEqual(result['done'],1);self.assertEqual(result['state'],'partial')
        self.assertTrue((self.root/target).exists())

    def test_unavailable_notifications_fall_back_to_full_checks(self):
        plan=self.ops.deletion('A')
        with patch.object(ProtectionWatch,'unchanged',return_value=False):result=self.ops.execute(plan['id'])
        self.assertEqual(result['done'],3)
        self.assertEqual(result['timings']['protection_walks'],4)
        self.assertEqual(result['timings']['protection_reuses'],0)

    def test_watch_stays_invalid_after_change_and_closes_handles_on_exit(self):
        with ProtectionWatch(self.album) as watch:
            if not watch.active:self.skipTest('Local NTFS notifications unavailable')
            self.assertTrue(watch.unchanged())
            Image.new('RGB',(30,20),'red').save(self.album/'精选/jpg/new.JPG')
            self.assertFalse(watch.unchanged());self.assertFalse(watch.unchanged())
        self.assertFalse(watch.active);self.assertEqual(watch.handles,[])


if __name__=='__main__':unittest.main()
