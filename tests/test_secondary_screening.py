"""Secondary screening reconsiders decisions; it never resets or moves photos."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boyo.analysis import analyze
from boyo.config import Runtime
from boyo.fs import sha256
from boyo.imaging import thumbnail_path
from boyo.matching import find_neighbors
from boyo.quality import assess,screen_quality
from boyo.scanner import Scanner
from boyo.screening import SECONDARY,SECONDARY_STRENGTH
from boyo.store import Store
from boyo.tasks import Control
from boyo.vision import Matcher,is_duplicate
from test_culling import scene


class SecondaryScreeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='boyo-secondary-')
        self.root=Path(self.tmp.name);self.album=self.root/'A'
        (self.album/'jpg').mkdir(parents=True)
        self.runtime=Runtime(self.root/'tools/Boyo/local');self.runtime.prepare()
        self.store=Store(self.runtime.database(self.root))
        self.settings={**self.runtime.settings(),'strength':'较强','analysis_workers':2}

    def tearDown(self):self.tmp.cleanup()

    def seed(self):
        image=scene(9);image.save(self.album/'jpg/1.JPG')
        for name in ('2','3'):shutil.copy2(self.album/'jpg/1.JPG',self.album/'jpg'/f'{name}.JPG')
        Scanner(self.store,self.root).scan(self.album)
        return {Path(a['path']).stem:a['id'] for a in self.store.assets('A')}

    def run_analysis(self,secondary=False,control=None,progress=None):
        settings=dict(self.settings)
        if secondary:settings['screening_mode']=SECONDARY
        return analyze(self.store,self.runtime,self.root,'A',settings,progress or (lambda *a:None),control or Control())

    def test_previously_kept_duplicates_are_reconsidered_without_resetting_labels_or_feedback(self):
        ids=self.seed();self.store.decide(ids.values(),'keep','manual')
        before=self.store.review_stamps('A');events=self.store.rows('SELECT * FROM feedback_events')
        hashes={p.name:sha256(p) for p in (self.album/'jpg').iterdir()}
        self.assertEqual(self.run_analysis()['candidates'],0)
        with patch('boyo.analysis.extract',side_effect=AssertionError('reuse image measurements')):
            result=self.run_analysis(True)
        self.assertEqual(result['candidates'],2)
        self.assertEqual(self.store.review_stamps('A'),before)
        self.assertEqual(self.store.rows('SELECT * FROM feedback_events'),events)
        self.assertEqual({p.name:sha256(p) for p in (self.album/'jpg').iterdir()},hashes)
        self.assertTrue(all(g['screening_mode']==SECONDARY for g in self.store.groups('A')))
        with patch.object(Matcher,'compare',side_effect=AssertionError('reuse pair evidence')):
            self.assertTrue(self.run_analysis(True)['reused_groups'])
            self.assertEqual(self.run_analysis()['candidates'],0)
        self.assertTrue(all(a['decision']=='keep' for a in self.store.assets('A')))

    def test_selected_reference_is_protected_and_existing_delete_is_outside_the_second_pass(self):
        ids=self.seed();self.store.decide([ids['2']],'keep','manual');self.store.decide([ids['3']],'delete','manual')
        selected=self.album/'精选/jpg';selected.mkdir(parents=True)
        shutil.copy2(self.album/'jpg/1.JPG',selected/'1.JPG')
        Scanner(self.store,self.root).scan(self.album)
        result=self.run_analysis(True)
        self.assertEqual(result['candidates'],1)
        group=self.store.groups('A')[0]
        self.assertEqual(group['keep'],ids['1'])
        self.assertEqual([a['id'] for a in group['candidates']],[ids['2']])
        self.assertNotIn(ids['3'],group['members'])
        self.assertEqual(result['quality']['total'],1)
        self.assertEqual(self.store.asset(ids['3'])['decision'],'delete')

    def test_quality_profile_finds_milder_cases_using_the_existing_measurement_cache(self):
        ids=self.seed()
        from boyo.quality import QUALITY_VERSION
        metrics={'acutance':.88,'detail_acutance':.82,'detail_tiles':16,'textured_tiles':20,
                 'focus_scale':1800,'brightness':.5,'clipping':.05,'quality_version':QUALITY_VERSION}
        self.run_analysis()
        with self.store.connect() as db:
            for a in self.store.assets('A'):
                db.execute('INSERT OR REPLACE INTO quality_cache VALUES (?,?,?,?,?)',
                           (a['path'],a['size'],a['mtime_ns'],QUALITY_VERSION,json.dumps(metrics)))
            db.execute('DELETE FROM quality_runs')
        self.store.decide([ids['1']],'keep','manual');self.store.decide([ids['3']],'delete','manual')
        with patch('boyo.quality.extract_quality',side_effect=AssertionError('profile changes reuse measurements')):
            normal=screen_quality(self.store,self.runtime,self.root,'A',dict(self.settings,quality_sensitivity='多找可疑'),lambda *a:None,Control())
            high=screen_quality(self.store,self.runtime,self.root,'A',dict(self.settings,screening_mode=SECONDARY),lambda *a:None,Control())
        self.assertEqual(normal['candidates'],0)
        self.assertEqual(high['candidates'],2)
        self.assertEqual({a['id'] for a in self.store.quality_report('A')['results']},{ids['1'],ids['2']})
        self.assertTrue(all(a['severity']=='可疑问题' for a in self.store.quality_report('A')['results']))

    def test_secondary_quality_remains_a_superset_and_flat_or_sharp_images_are_not_bad(self):
        for blur in (.2,.5,.7,.88,1.4):
            metrics={'acutance':blur,'detail_acutance':blur,'detail_tiles':16,'textured_tiles':20,'focus_scale':1800}
            if assess(metrics,'多找可疑'):self.assertTrue(assess(metrics,secondary=True))
        self.assertFalse(assess({'textured_tiles':0,'detail_tiles':0},secondary=True))
        self.assertFalse(assess({'acutance':1.4,'detail_acutance':1.3,'textured_tiles':20,'detail_tiles':16,'focus_scale':1800},secondary=True))
        self.assertEqual(assess({'brightness':.1,'clipping':.7},secondary=True)[0]['severity'],'可疑问题')

    def test_wider_retrieval_finds_a_distant_crop_but_still_requires_direct_geometric_evidence(self):
        assets=[{'id':n,'path':f'A/jpg/{n}.JPG','size':1,'mtime_ns':1,'decision':'pending','protected':0} for n in range(22)]
        features={a['id']:{'phash':'ffffffffffffffff','dhash':'0000000000000000'} for a in assets}
        features[0]['phash']='0000000000000000';features[21]['phash']='000000000fffffff'
        target={str(thumbnail_path(self.runtime,assets[n])) for n in (0,21)}
        evidence={'overlap':.55,'spread':.12,'correlation':.61,'inliers':24,'inlier_ratio':.6}
        def compare(left,right):return evidence if {str(left),str(right)}==target else None
        with patch.object(Matcher,'compare',side_effect=compare):
            normal=find_neighbors(self.store,self.runtime,'A',assets,features,'较强',lambda *a:None,Control())
            high=find_neighbors(self.store,self.runtime,'A',assets,features,SECONDARY_STRENGTH,lambda *a:None,Control())
        self.assertNotIn(21,normal[0]);self.assertIn(21,high[0])
        for invalid in ({'spread':.01},{'inliers':8},{'correlation':.1},{'overlap':.2}):
            self.assertFalse(is_duplicate({**evidence,**invalid},SECONDARY_STRENGTH))

    def test_cancellation_preserves_labels_feedback_and_previous_groups(self):
        ids=self.seed();self.store.decide(ids.values(),'keep','manual');self.run_analysis()
        prior={'id':'previous','keep':ids['1'],'candidates':[{'id':ids['2']}]}
        self.store.execute('INSERT INTO groups VALUES (?,?,?)',('previous','A',json.dumps(prior)))
        before=self.store.review_stamps('A');events=self.store.rows('SELECT * FROM feedback_events')
        control=Control();control.cancel.set()
        self.assertTrue(self.run_analysis(True,control)['cancelled'])
        self.assertEqual(self.store.groups('A'),[prior])
        self.assertEqual(self.store.review_stamps('A'),before)
        self.assertEqual(self.store.rows('SELECT * FROM feedback_events'),events)


if __name__=='__main__':unittest.main()
