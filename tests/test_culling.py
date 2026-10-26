import json
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from boyo.analysis import analyze, direct_suggestions
from boyo.config import Runtime
from boyo.fs import sha256
from boyo.imaging import extract, thumbnail_path
from boyo.matching import find_neighbors
from boyo.scanner import Scanner
from boyo.store import Store
from boyo.tasks import Control
from boyo.vision import Matcher, focus_metrics, is_duplicate, quality_issues, save_descriptors


def scene(seed=1):
    rng=np.random.default_rng(seed)
    arr=rng.integers(30,220,(900,1200,3),dtype=np.uint8)
    image=Image.fromarray(arr)
    draw=ImageDraw.Draw(image)
    for y in range(0,900,100):
        for x in range(0,1200,120):
            color=tuple(int(v) for v in rng.integers(15,245,3))
            draw.rectangle((x+8,y+8,x+88,y+63),fill=color,outline='white',width=3)
            draw.text((x+15,y+20),f'{seed}:{x}:{y}',fill='black')
    return image


class CullingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='boyo-cull-')
        self.root=Path(self.temp.name); self.album=self.root/'test'
        (self.album/'jpg').mkdir(parents=True)
        self.runtime=Runtime(self.root/'tools/Boyo/local'); self.runtime.prepare()
        self.store=Store(self.runtime.database(self.root))
        self.settings={**self.runtime.settings(),'strength':'较强','analysis_workers':2}

    def tearDown(self): self.temp.cleanup()

    def run_analysis(self):
        Scanner(self.store,self.root).scan(self.album)
        return analyze(self.store,self.runtime,self.root,self.album.name,self.settings,lambda *args:None,Control())

    def test_changed_crop_matches_and_unrelated_scene_is_not_a_duplicate(self):
        image=scene()
        image.save(self.album/'jpg/a.jpg')
        image.crop((65,40,1140,850)).resize(image.size).save(self.album/'jpg/b.jpg')
        scene(999).save(self.album/'jpg/c.jpg')
        before={p:sha256(p) for p in (self.album/'jpg').iterdir()}
        result=self.run_analysis()
        assets={Path(a['path']).name:a for a in self.store.assets('test')}
        groups=[g for g in self.store.groups('test') if g['kind']=='duplicate']
        matched={frozenset([g['keep'],m['id']]) for g in groups for m in g['candidates']}
        self.assertIn(frozenset((assets['a.jpg']['id'],assets['b.jpg']['id'])),matched)
        self.assertFalse(any(assets['c.jpg']['id'] in pair for pair in matched))
        self.assertEqual(before,{p:sha256(p) for p in before})
        self.assertFalse(result['cancelled'])
        self.assertTrue(all(a['decision']=='pending' for a in assets.values()))
        with patch('boyo.analysis.extract') as extraction,patch.object(Matcher,'compare',side_effect=AssertionError('must reuse groups')):
            self.assertTrue(self.run_analysis()['reused_groups'])
            extraction.assert_not_called()
        # Decisions invalidate representative selection but reuse pair evidence.
        chosen=assets['b.jpg']['id']; self.store.decide([chosen],'keep','manual')
        with patch.object(Matcher,'compare',side_effect=AssertionError('must reuse pair cache')):
            self.run_analysis()
        self.assertEqual(self.store.groups('test')[0]['keep'],chosen)

    def test_same_global_hash_is_insufficient_and_small_shared_logo_is_insufficient(self):
        a,b=scene(3),scene(7)
        logo=scene(25).resize((180,135))
        a.paste(logo,(400,350)); b.paste(logo,(400,350))
        for name,image in [('a',a),('b',b)]: image.save(self.album/'jpg'/f'{name}.jpg')
        Scanner(self.store,self.root).scan(self.album)
        assets=self.store.assets('test'); features={}
        for asset in assets:
            f=extract(self.root/asset['path'],thumbnail_path(self.runtime,asset))
            f['phash']='0'*16; f['dhash']='0'*16
            features[asset['id']]=f
        neighbors=find_neighbors(self.store,self.runtime,'test',assets,features,'较强',lambda *args:None,Control())
        self.assertTrue(all(not n for n in neighbors.values()))

    def test_blur_is_flagged_while_flat_and_partly_sharp_images_are_not(self):
        sharp=scene(5)
        blurred=sharp.filter(ImageFilter.GaussianBlur(3))
        self.assertFalse(quality_issues({'metrics':focus_metrics(sharp)}))
        self.assertTrue(quality_issues({'metrics':focus_metrics(blurred)}))
        self.assertFalse(quality_issues({'metrics':focus_metrics(Image.new('RGB',sharp.size,'gray'))}))
        partly=blurred.copy(); partly.paste(sharp.crop((250,180,950,720)),(250,180))
        self.assertFalse(quality_issues({'metrics':focus_metrics(partly)}))

    def test_quality_only_queue_and_selected_copy_protection(self):
        path=self.album/'jpg/blur.jpg'
        scene(11).filter(ImageFilter.GaussianBlur(4)).save(path,quality=95)
        result=self.run_analysis()
        self.assertEqual(result['quality_candidates'],1)
        self.assertEqual(self.store.groups('test'),[])
        report=self.store.quality_report('test')
        self.assertEqual(len(report['results']),1)
        self.assertEqual(report['results'][0]['issues'][0]['code'],'blur')
        selected=self.album/'精选/jpg'; selected.mkdir(parents=True)
        shutil.copy2(path,selected/'blur.jpg')
        self.assertEqual(self.run_analysis()['candidates'],0)
        self.assertTrue(path.exists())

    def test_quality_ui_has_individual_controls_no_batch_delete_or_false_references(self):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
        from boyo.gui import MainWindow
        from boyo.operations import Operations
        from boyo.plan_dialog import PlanDialog
        app=QApplication.instance() or QApplication([])
        for i in range(2): scene(i+10).filter(ImageFilter.GaussianBlur(4)).save(self.album/'jpg'/f'{i}.jpg')
        Scanner(self.store,self.root).scan(self.album)
        assets=self.store.assets('test')
        from boyo.quality import screen_quality
        screen_quality(self.store,self.runtime,self.root,'test',self.settings,lambda *args:None,Control())
        window=MainWindow(self.store,self.runtime,self.root,self.settings)
        try:
            self.assertEqual(window.quality_grid.count(),2)
            self.assertEqual(set(window.quality_entries),{a['id'] for a in assets})
            items={window.quality_grid.item(i).data(Qt.ItemDataRole.UserRole):window.quality_grid.item(i) for i in range(2)}
            items[assets[0]['id']].setSelected(True); window.quality_decide('delete')
            items[assets[0]['id']].setSelected(False); items[assets[1]['id']].setSelected(True); window.quality_decide('keep')
            plan=Operations(self.store,self.root).deletion('test')
            dialog=PlanDialog(plan,window)
            try: self.assertFalse(dialog.references)
            finally: dialog.close(); dialog.deleteLater()
            for item in items.values(): item.setSelected(True)
            with patch.object(window,'refresh_quality_grid') as rebuild,patch.object(window,'load_visible_thumbnails') as image_read:
                window.quality_decide('keep')
                rebuild.assert_not_called(); image_read.assert_not_called()
            self.assertTrue(all(a['decision']=='keep' for a in self.store.assets('test')))
            self.assertEqual(len(list((self.album/'jpg').iterdir())),2)
            from boyo.preferences import train
            self.assertEqual(train(self.store,self.runtime)['pairs'],0)
        finally:
            window.close(); window.deleteLater(); app.processEvents()

    def test_explicit_delete_cannot_be_reference_and_explicit_keep_wins(self):
        assets=[{'id':n,'protected':0,'path':str(n),'decision':decision} for n,decision in [(1,'delete'),(2,'pending'),(3,'keep')]]
        edges={n:{other:(1,1) for other in (1,2,3) if other!=n} for n in (1,2,3)}
        group=direct_suggestions(assets,edges,{1:1,2:.9,3:.1})[0]
        self.assertEqual(group['keep'],3)
        self.assertEqual({m['id'] for m in group['candidates']},{1,2})

    def test_match_evidence_requires_geometry_and_photometric_agreement(self):
        good={'overlap':.95,'spread':.6,'correlation':.98}
        self.assertTrue(is_duplicate(good,'保守'))
        for key,value in [('spread',.02),('overlap',.3),('correlation',.1)]:
            self.assertFalse(is_duplicate({**good,key:value},'较强'))


if __name__=='__main__': unittest.main()
