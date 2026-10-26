import json
import shutil
import tempfile
import unittest
from pathlib import Path

from boyo.config import Runtime
from boyo.portability import bind_workspace, repair_workspace
from boyo.store import Store


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'original'
        self.runtime = Runtime(self.root/'tools/Boyo/local')
        self.runtime.prepare()
        self.photo = self.root/'album/jpg/photo.jpg'
        self.photo.parent.mkdir(parents=True)
        self.photo.write_bytes(b'unchanged photograph')
        self.store = Store(self.runtime.database(self.root))
        stat = self.photo.stat()
        self.store.execute("INSERT INTO albums VALUES ('album',0,1,'[]')")
        self.store.execute("INSERT INTO assets(album,path,stem,role,logical_key,size,mtime_ns,decision) "
                           "VALUES (?,?,?,?,?,?,?,'keep')",
                           ('album','album/jpg/photo.jpg','photo','ordinary_jpeg','album/photo',stat.st_size,stat.st_mtime_ns))
        self.runtime.save_settings({'workspace':str(self.root),'last_album':'album'})
        bind_workspace(self.runtime,self.root,self.store.path)

    def copy(self, source, name):
        target = self.base/name
        shutil.copytree(source,target)
        return target,Runtime(target/'tools/Boyo/local')

    def test_whole_workspace_move_preserves_identity_and_decisions(self):
        target,runtime = self.copy(self.root,'new drive')
        self.assertEqual(Path(runtime.settings()['workspace']),target)
        self.assertEqual(runtime.database(target).name,self.store.path.name)
        self.assertEqual(Store(runtime.database(target)).one('SELECT decision FROM assets')['decision'],'keep')
        self.assertNotEqual(runtime.database(self.base/'unrelated').name,self.store.path.name)

    def test_repeated_moves_and_copy_with_old_source_present(self):
        source=self.root
        for number in range(4):
            source,runtime=self.copy(source,'move-'+str(number))
            self.assertEqual(Path(runtime.settings()['workspace']),source)
            self.assertEqual(runtime.database(source).name,self.store.path.name)
        self.assertTrue(self.photo.is_file())

    def test_only_tool_move_keeps_original_photo_root(self):
        moved=self.base/'tool-only/local'
        shutil.copytree(self.runtime.home,moved)
        runtime=Runtime(moved)
        self.assertEqual(Path(runtime.settings()['workspace']),self.root)
        self.assertEqual(runtime.database(self.root).name,self.store.path.name)

    def test_same_size_changed_photo_does_not_rebind(self):
        target,runtime=self.copy(self.root,'different')
        path=target/'album/jpg/photo.jpg'
        path.write_bytes(b'x'*len(self.photo.read_bytes()))
        self.assertEqual(Path(runtime.settings()['workspace']),self.root)

    def test_explicit_repair_retains_model_filename(self):
        target=self.base/'elsewhere'
        target.mkdir()
        shutil.copytree(self.root/'album',target/'album')
        result=repair_workspace(self.runtime,target)
        self.assertEqual(result['database'],self.store.path.name)
        self.assertEqual(self.runtime.database(target),self.store.path)

    def test_corrupt_or_traversing_binding_is_ignored(self):
        path=self.runtime.home/'settings/workspace-binding.json'
        for value in ({'version':1,'database':'../other.sqlite3'},[],{'version':1,'database':'library-1.sqlite3'}):
            path.write_text(json.dumps(value),encoding='utf-8')
            self.assertEqual(Path(self.runtime.settings()['workspace']),self.root)

    def test_probe_traversal_does_not_rebind(self):
        target,runtime=self.copy(self.root,'traversal')
        path=runtime.home/'settings/workspace-binding.json'
        value=json.loads(path.read_text(encoding='utf-8'))
        value['probes'][0]['path']='../outside.jpg'
        path.write_text(json.dumps(value),encoding='utf-8')
        self.assertEqual(Path(runtime.settings()['workspace']),self.root)

    def test_missing_original_database_is_not_replaced_by_empty_library(self):
        self.store.path.unlink()
        with self.assertRaises(ValueError): repair_workspace(self.runtime,self.root)

    def test_explicit_repair_rejects_unrelated_photographs(self):
        other=self.base/'unrelated-root'
        (other/'album/jpg').mkdir(parents=True)
        (other/'album/jpg/photo.jpg').write_bytes(b'x'*self.photo.stat().st_size)
        with self.assertRaises(ValueError): repair_workspace(self.runtime,other)

    def test_interrupted_settings_write_recovers_stable_database(self):
        target,runtime=self.copy(self.root,'interrupted')
        path=runtime.home/'settings/workspace-binding.json'
        value=json.loads(path.read_text(encoding='utf-8'))
        value.update(root=str(target),runtime_home=str(runtime.home),pending_settings_root=str(self.root))
        path.write_text(json.dumps(value),encoding='utf-8')
        self.assertEqual(Path(runtime.settings()['workspace']),target)
        self.assertEqual(runtime.database(target).name,self.store.path.name)

    def test_removed_static_probe_can_use_other_cached_photo_identities(self):
        import hashlib
        for number in range(3):
            relative=f'album/jpg/new-{number}.jpg'
            path=self.root/relative
            path.write_bytes(f'new photo {number}'.encode())
            stat=path.stat()
            self.store.execute("INSERT INTO assets(album,path,stem,role,logical_key,size,mtime_ns) VALUES (?,?,?,?,?,?,?)",
                               ('album',relative,str(number),'ordinary_jpeg',relative,stat.st_size,stat.st_mtime_ns))
            self.store.execute('INSERT INTO digests VALUES (?,?,?,?)',
                               (relative,stat.st_size,stat.st_mtime_ns,hashlib.sha256(path.read_bytes()).hexdigest()))
        self.photo.unlink()
        target,runtime=self.copy(self.root,'after-culling')
        self.assertEqual(Path(runtime.settings()['workspace']),target)
        self.assertEqual(runtime.database(target).name,self.store.path.name)


if __name__=='__main__': unittest.main()
