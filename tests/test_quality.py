import json
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import shutil
import tempfile
import time
import unittest
import numpy as np
from pathlib import Path
from unittest.mock import patch

from PIL import Image,ImageFilter

from boyo.config import Runtime
from boyo.quality import assess,focus_measurements,screen_quality
from boyo.scanner import Scanner
from boyo.store import Store
from boyo.tasks import Control
from test_culling import scene


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='boyo-quality-')
        self.root=Path(self.tmp.name); self.album=self.root/'quality-test'
        (self.album/'jpg').mkdir(parents=True)
        self.runtime=Runtime(self.root/'tools/Boyo/local'); self.runtime.prepare()
        self.store=Store(self.runtime.database(self.root)); self.settings={**self.runtime.settings(),'analysis_workers':2}

    def tearDown(self): self.tmp.cleanup()

    def screen(self,control=None,progress=None):
        Scanner(self.store,self.root).scan(self.album)
        return screen_quality(self.store,self.runtime,self.root,self.album.name,self.settings,progress or (lambda *args:None),control or Control())

    def test_independent_blur_screen_does_not_match_compute_hashes_or_change_decisions(self):
        path=self.album/'jpg/unique-blur.jpg'; scene().filter(ImageFilter.GaussianBlur(4)).save(path)
        with patch('boyo.matching.find_neighbors',side_effect=AssertionError('no pair matching')),patch('boyo.imaging.sha256',side_effect=AssertionError('no image hashing')):
            result=self.screen()
        self.assertEqual(result['candidates'],1)
        self.assertEqual(self.store.groups(self.album.name),[])
        self.assertEqual(self.store.assets(self.album.name)[0]['decision'],'pending')
        self.assertTrue(path.exists())
        with patch('boyo.quality.extract_quality',side_effect=AssertionError('warm cache must avoid decoding')):
            self.assertTrue(self.screen()['reused_results'])
            self.settings['quality_sensitivity']='多找可疑'
            again=self.screen()
            self.assertEqual(again['cached'],1); self.assertFalse(again['reused_results'])

    def test_blur_and_exposure_are_independent_and_flat_gray_is_not_bad(self):
        for name,color in [('black','black'),('white','white'),('flat','gray')]:
            Image.new('RGB',(1200,900),color).save(self.album/'jpg'/f'{name}.jpg')
        result=self.screen()
        self.assertEqual(result['candidates'],2)
        names={Path(r['path']).stem for r in self.store.quality_report(self.album.name)['results']}
        self.assertEqual(names,{'black','white'})

    def test_native_detail_check_detects_soft_large_photo_without_comparison(self):
        image=scene(17).resize((3600,2700),Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(3))
        metrics=focus_measurements(image)
        self.assertEqual(metrics['detail_tiles'],16)
        self.assertLess(metrics['detail_acutance'],metrics['acutance'])
        self.assertTrue(any(i['code'] in {'blur','soft_detail'} for i in assess(metrics,'标准')),metrics)

    def test_sensitivity_is_monotonic_and_keeps_uncertain_cases_distinct(self):
        metrics={'acutance':.65,'detail_acutance':.9,'focus_scale':1800,'textured_tiles':20,'detail_tiles':16}
        self.assertFalse(assess(metrics,'仅明显问题')); self.assertFalse(assess(metrics,'标准'))
        self.assertEqual(assess(metrics,'多找可疑')[0]['severity'],'可疑问题')
        clear={**metrics,'acutance':1.4,'detail_acutance':1.3}
        for sensitivity in ('仅明显问题','标准','多找可疑'): self.assertFalse(assess(clear,sensitivity))

    def test_directional_motion_blur_is_found_when_overall_sharpness_would_pass(self):
        import cv2
        source=np.asarray(scene(13))
        for angle in (0,45,90):
            with self.subTest(angle=angle):
                length=15;kernel=np.zeros((length,length),np.float32)
                c=(length-1)/2;theta=np.deg2rad(angle)
                cv2.line(kernel,(round(c-c*np.cos(theta)),round(c-c*np.sin(theta))),
                         (round(c+c*np.cos(theta)),round(c+c*np.sin(theta))),1,1)
                kernel/=kernel.sum()
                image=Image.fromarray(cv2.filter2D(source,-1,kernel))
                metrics=focus_measurements(image)
                self.assertGreater(metrics['acutance'],.75)
                self.assertIn('motion_blur',{i['code'] for i in assess(metrics)},metrics)
        sharp=focus_measurements(Image.fromarray(source))
        self.assertNotIn('motion_blur',{i['code'] for i in assess(sharp,secondary=True)})

    def test_motion_detection_rejects_flat_fields_and_isolated_parallel_lines(self):
        from PIL import ImageDraw
        image=Image.new('RGB',(1200,900),'gray');draw=ImageDraw.Draw(image)
        for x in range(20,1200,100):draw.line((x,0,x,899),fill='white',width=3)
        for sample in (image,Image.new('RGB',(1200,900),'gray')):
            self.assertNotIn('motion_blur',{i['code'] for i in assess(focus_measurements(sample),secondary=True)})

    def test_selected_original_is_excluded_even_when_it_is_very_blurry(self):
        path=self.album/'jpg/blur.jpg'; scene(2).filter(ImageFilter.GaussianBlur(5)).save(path)
        selected=self.album/'精选/jpg'; selected.mkdir(parents=True); shutil.copy2(path,selected/'blur.jpg')
        with patch('boyo.quality.extract_quality') as extraction:
            result=self.screen(); extraction.assert_not_called()
        self.assertEqual(result['total'],0); self.assertEqual(result['protected'],1)
        self.assertEqual(result['candidates'],0)

    def test_cancel_keeps_prior_results_and_stops_a_bounded_queue(self):
        for i in range(8): scene(i).save(self.album/'jpg'/f'{i}.jpg')
        prior={'summary':{'failures':[]},'results':[{'id':999}]}
        self.store.execute('INSERT INTO quality_runs VALUES (?,?,?)',(self.album.name,'old',json.dumps(prior)))
        from boyo.quality import extract_quality
        control=Control()
        def progress(n,total,label):
            if label.startswith('筛查模糊'): control.cancel.set()
        def slow(*args): time.sleep(.05); return extract_quality(*args)
        with patch('boyo.quality.extract_quality',side_effect=slow) as extraction:
            result=self.screen(control,progress)
            self.assertTrue(result['cancelled']); self.assertLessEqual(extraction.call_count,2)
        self.assertEqual(self.store.quality_report(self.album.name),prior)
        self.assertEqual(len(list((self.album/'jpg').iterdir())),8)

    def test_quality_button_runs_without_duplicate_analysis(self):
        from PySide6.QtWidgets import QApplication
        from boyo.gui import MainWindow
        app=QApplication.instance() or QApplication([])
        scene().filter(ImageFilter.GaussianBlur(4)).save(self.album/'jpg/blur.jpg')
        window=MainWindow(self.store,self.runtime,self.root,self.settings)
        try:
            with patch('boyo.analysis.analyze',side_effect=AssertionError('independent feature')):
                window.start_quality()
                deadline=time.monotonic()+30
                while window.thread is not None and time.monotonic()<deadline:
                    app.processEvents(); time.sleep(.01)
                self.assertIsNone(window.thread)
            self.assertEqual(window.tabs.currentWidget(),window.quality_pane)
            self.assertEqual(window.quality_grid.count(),1)
            self.assertIn('废片筛查完成',window.status.text())
        finally: window.close(); window.deleteLater(); app.processEvents()


if __name__=='__main__': unittest.main()
