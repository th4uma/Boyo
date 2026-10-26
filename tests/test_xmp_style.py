import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from PIL import Image,ImageDraw

from boyo.colour_match import fit_to_reference,apply_match,sample_lab,moments
from boyo.collections import Collections
from boyo.config import Runtime
from boyo.fs import sha256
from boyo.operations import Operations
from boyo.scanner import Scanner
from boyo.store import Store
from boyo.tasks import Control
from boyo.xmp_style import XmpStyle,read_xmp,write_global_xmp,CRS,RDF


XMP='''<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/">
<rdf:RDF><rdf:Description crs:ProcessVersion="15.4" crs:Exposure2012="0.75" crs:Temperature="5450" crs:HasCrop="True" crs:CropTop="0.2">
<crs:ToneCurvePV2012><rdf:Seq><rdf:li>0, 0</rdf:li><rdf:li>255, 255</rdf:li></rdf:Seq></crs:ToneCurvePV2012>
<crs:MaskGroupBasedCorrections><rdf:Seq/></crs:MaskGroupBasedCorrections>
</rdf:Description></rdf:RDF></x:xmpmeta>'''


class XmpStyleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='boyo-xmp-');self.root=Path(self.tmp.name);self.album=self.root/'A'
        for folder in ('jpg','raw','成片'):(self.album/folder).mkdir(parents=True)
        image=Image.new('RGB',(320,240),(60,90,120));ImageDraw.Draw(image).rectangle((20,20,120,100),fill=(150,100,80));image.save(self.album/'jpg/1.JPG')
        image=Image.new('RGB',(320,240),(100,95,80));ImageDraw.Draw(image).rectangle((20,20,120,100),fill=(190,120,70));image.save(self.album/'成片/cover.JPG')
        self.xmp=self.album/'成片/cover.xmp';self.xmp.write_text(XMP,encoding='utf-8')
        self.runtime=Runtime(self.root/'tools/Boyo/local');self.runtime.prepare();self.store=Store(self.runtime.database(self.root))
        Scanner(self.store,self.root).scan(self.album)
        self.collection=Collections(self.store,self.root);self.set_id=self.collection.create('A')
        assets=self.store.assets('A');self.cover=next(a for a in assets if a['role']=='finished');self.source=next(a for a in assets if a['role']=='ordinary_jpeg')
        self.collection.add(self.set_id,self.cover['id'],slot=0);self.collection.add(self.set_id,self.source['id'],slot=1)
        self.service=XmpStyle(self.store,self.runtime,self.root);self.service.cache_root.mkdir(parents=True)
        self.job=self.service.cache_root/'job-test';self.job.mkdir()

    def tearDown(self):self.tmp.cleanup()

    def prepare(self):return self.service.prepare(self.set_id,self.xmp,self.job,lambda *a:None,Control())

    def test_xmp_globals_are_reused_without_transplanting_crop_or_local_masks(self):
        output=self.job/'source.xmp';count=write_global_xmp(self.xmp,output,'source.ARW')
        desc=read_xmp(output).find('.//{'+RDF+'}Description')
        self.assertEqual(desc.attrib['{'+CRS+'}Exposure2012'],'0.75')
        self.assertEqual(desc.attrib['{'+CRS+'}Temperature'],'5450')
        self.assertEqual(desc.attrib['{'+CRS+'}HasCrop'],'False')
        self.assertNotIn('{'+CRS+'}CropTop',desc.attrib)
        self.assertIsNone(desc.find('{'+CRS+'}MaskGroupBasedCorrections'))
        self.assertIsNotNone(desc.find('{'+CRS+'}ToneCurvePV2012'))
        self.assertGreater(count,0)

    def test_missing_raw_fallback_is_explicit_and_export_preserves_every_original(self):
        hashes={a['path']:sha256(self.root/a['path']) for a in self.store.assets('A')}
        self.assertEqual(self.service.reference(self.set_id),self.xmp)
        result=self.prepare();self.assertEqual(len(result['warnings']),1)
        self.assertFalse(result['parameters'][str(self.source['id'])]['developed_from_xmp'])
        operations=Operations(self.store,self.root,render_cache_root=self.service.cache_root)
        plan=operations.export_styled_set(self.set_id,result)
        completed=operations.execute(plan['id']);self.assertEqual(completed['done'],2)
        self.assertEqual({p:sha256(self.root/p) for p in hashes},hashes)
        self.assertEqual(self.store.rows('SELECT * FROM feedback_events'),[])
        copied=self.root/plan['actions'][0]['dst']
        self.assertEqual(sha256(copied),hashes[self.cover['path']])
        with Image.open(self.root/plan['actions'][1]['dst']) as exported:self.assertEqual(exported.size,(320,240));self.assertIn('icc_profile',exported.info)
        second=operations.export_styled_set(self.set_id,result)
        self.assertNotEqual(plan['output_folder'],second['output_folder'])

    def test_raw_renderer_receives_only_a_copy_and_the_filtered_xmp(self):
        raw=self.album/'raw/1.ARW';raw.write_bytes(b'fake RAW input for renderer contract test')
        Scanner(self.store,self.root).scan(self.album)
        digest=sha256(raw)
        def render(source,destination,job,control):
            self.assertNotEqual(source,raw)
            self.assertEqual(sha256(source),digest)
            desc=read_xmp(source.with_suffix('.xmp')).find('.//{'+RDF+'}Description')
            self.assertEqual(float(desc.attrib['{'+CRS+'}Exposure2012']),.75)
            Image.new('RGB',(400,300),(100,90,80)).save(destination)
        with patch('boyo.xmp_style.render_raw',side_effect=render) as renderer:
            result=self.prepare()
        self.assertEqual(renderer.call_count,1)
        self.assertTrue(result['parameters'][str(self.source['id'])]['developed_from_xmp'])
        self.assertEqual(sha256(raw),digest)
        self.assertFalse((self.job/'working').exists())

    def test_stale_collection_and_tampered_cache_are_rejected_before_export(self):
        result=self.prepare()
        operations=Operations(self.store,self.root,render_cache_root=self.service.cache_root)
        cache=self.service.cache_root/result['outputs'][self.source['id']]['cache'];cache.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'缓存已改变'):operations.export_styled_set(self.set_id,result)
        self.assertFalse((self.album/'图集').exists())

    def test_reference_parser_rejects_entities_and_invalid_xml(self):
        for text in ('<!DOCTYPE x [<!ENTITY e "text">]><x/>','<broken>'):
            self.xmp.write_text(text,encoding='utf-8')
            with self.assertRaises(ValueError):read_xmp(self.xmp)

    def test_raw_exposure_adaptation_is_finite_and_changes_only_the_temporary_xmp(self):
        (self.album/'raw/1.ARW').write_bytes(b'RAW renderer adaptive exposure contract')
        Scanner(self.store,self.root).scan(self.album)
        original=sha256(self.xmp);exposures=[]
        def render(source,destination,job,control):
            desc=read_xmp(source.with_suffix('.xmp')).find('.//{'+RDF+'}Description')
            exposures.append(float(desc.get('{'+CRS+'}Exposure2012')))
            brightness=230 if len(exposures)==1 else 110
            Image.new('RGB',(100,80),(brightness,brightness,brightness)).save(destination)
        with patch('boyo.xmp_style.render_raw',side_effect=render):result=self.prepare()
        self.assertGreaterEqual(len(exposures),2);self.assertLessEqual(len(exposures),3)
        self.assertLess(exposures[1],exposures[0])
        self.assertEqual(sha256(self.xmp),original)
        self.assertLessEqual(result['parameters'][str(self.source['id'])]['raw_renders'],3)

    def test_reconsidered_delete_and_changed_collection_block_styled_export(self):
        prepared=self.prepare();operations=Operations(self.store,self.root,render_cache_root=self.service.cache_root)
        plan=operations.export_styled_set(self.set_id,prepared)
        self.store.decide([self.source['id']],'delete','manual')
        with self.assertRaisesRegex(ValueError,'待删'):operations.execute(plan['id'])
        self.assertFalse((self.album/'图集').exists())

    def test_matching_is_bounded_improves_reference_distance_and_preserves_dimensions(self):
        image=Image.new('RGB',(130,600),(50,80,110));reference=Image.new('RGB',(130,600),(100,100,90))
        params=fit_to_reference(image,moments(sample_lab(reference)),developed=False)
        self.assertLessEqual(params['rounds'],3)
        self.assertLess(params['final_distance'],params['initial_distance'])
        self.assertTrue(all(abs(v)<=15 for v in params['offset']))
        output=apply_match(image,params)
        self.assertEqual(output.size,image.size)
        array=np.asarray(output)
        self.assertTrue(np.array_equal(array[255],array[256]))


if __name__=='__main__':unittest.main()
