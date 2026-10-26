import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from boyo.config import Runtime
from boyo.store import Store
from boyo.imaging import thumbnail_path
from boyo.embeddings import ensure_embeddings,weights_path,EMBEDDING_VERSION


class EmbeddingCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='boyo-embeddings-')
        self.runtime=Runtime(Path(self.tmp.name)); self.runtime.prepare(); self.store=Store(self.runtime.db_path)
        self.assets=[{'id':n,'path':f'A/jpg/{n}.jpg','size':1,'mtime_ns':1} for n in (1,2)]
        for asset in self.assets:
            path=thumbnail_path(self.runtime,asset); path.parent.mkdir(parents=True,exist_ok=True)
            Image.new('RGB',(80,60),'green').save(path,'WEBP')
        path=weights_path(self.runtime); path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(b'fixture')
        self.features={n:{'sha256':'identical-content'} for n in (1,2)}

    def tearDown(self): self.tmp.cleanup()

    def test_exact_content_encodes_once_and_warm_cache_does_no_work(self):
        with patch('boyo.embeddings.FrozenEncoder') as encoder:
            encoder.return_value.encode.return_value=[.1]*384
            first=ensure_embeddings(self.store,self.runtime,self.assets,self.features)
            self.assertEqual(set(first),{1,2}); self.assertEqual(encoder.return_value.encode.call_count,1)
        with patch('boyo.embeddings.FrozenEncoder') as encoder:
            self.assertEqual(ensure_embeddings(self.store,self.runtime,self.assets,self.features),first)
            encoder.assert_not_called()

    def test_missing_weights_stays_offline_and_failed_initialization_does_not_retry_each_photo(self):
        with patch('boyo.embeddings.FrozenEncoder',side_effect=RuntimeError('fixture broken')) as encoder,self.assertLogs(level='ERROR'):
            self.assertEqual(ensure_embeddings(self.store,self.runtime,self.assets,self.features),{})
            self.assertEqual(encoder.call_count,1)
        weights_path(self.runtime).unlink()
        with patch('boyo.embeddings.FrozenEncoder') as encoder:
            self.assertEqual(ensure_embeddings(self.store,self.runtime,self.assets,self.features),{})
            encoder.assert_not_called()

    def test_cancel_keeps_completed_cache_and_stale_version_is_not_reused(self):
        self.features[2]={'sha256':'different-content'}
        from boyo.tasks import Control
        control=Control()
        def encoded_then_cancel(path):
            control.cancel.set(); return [.1]*384
        with patch('boyo.embeddings.FrozenEncoder') as encoder:
            encoder.return_value.encode.side_effect=encoded_then_cancel
            self.assertEqual(set(ensure_embeddings(self.store,self.runtime,self.assets,self.features,control)),{1})
        self.assertEqual(self.store.one('SELECT COUNT(*) AS n FROM embeddings')['n'],1)
        self.store.execute("UPDATE embeddings SET version='old'")
        with patch('boyo.embeddings.FrozenEncoder') as encoder:
            encoder.return_value.encode.return_value=[.2]*384
            ensure_embeddings(self.store,self.runtime,self.assets,self.features)
            self.assertEqual(encoder.return_value.encode.call_count,2)


if __name__=='__main__': unittest.main()
