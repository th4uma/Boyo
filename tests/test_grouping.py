import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from boyo.analysis import direct_suggestions
from boyo.config import Runtime
from boyo.grouping import merge_review_groups,group_members,group_references
from boyo.gui import MainWindow
from boyo.imaging import FEATURE_VERSION
from boyo.operations import Operations
from boyo.plan_dialog import PlanDialog
from boyo.scanner import Scanner
from boyo.store import Store


def suggestions(assets,edges,scores):
    return [{**g,'id':f"star-{g['keep']}",'kind':'duplicate','strength':'较强'}
            for g in direct_suggestions(assets,edges,scores)]


class GroupingTests(unittest.TestCase):
    def assets(self,n):
        return [{'id':i,'path':str(i),'protected':0,'decision':'pending'} for i in range(1,n+1)]

    def edges(self,pairs):
        edges={}
        for a,b in pairs:
            edges.setdefault(a,{})[b]=(2,3); edges.setdefault(b,{})[a]=(2,3)
        return edges

    def test_connected_composition_is_one_review_group_without_chain_deletion(self):
        assets=self.assets(5); scores={i:6-i for i in range(1,6)}
        edges=self.edges([(1,2),(2,3),(3,4),(4,5)])
        original=suggestions(assets,edges,scores); self.assertEqual(len(original),2)
        merged=merge_review_groups(assets,edges,original,scores,'album')
        self.assertEqual(len(merged),1)
        group=merged[0]
        self.assertEqual(set(group_members(group)),{1,2,3,4,5})
        self.assertEqual(set(group_references(group)),{1,3,5})
        self.assertEqual({(m['id'],m['reference']) for m in group['candidates']},{(2,1),(4,3)})
        self.assertEqual(group['contexts']['2'],'star-1'); self.assertEqual(group['contexts']['4'],'star-3')
        for member in group['candidates']: self.assertIn(member['reference'],edges[member['id']])

    def test_disconnected_scenes_remain_separate(self):
        assets=self.assets(4); edges=self.edges([(1,2),(3,4)]); scores={i:5-i for i in range(1,5)}
        merged=merge_review_groups(assets,edges,suggestions(assets,edges,scores),scores,'album')
        self.assertEqual({frozenset(group_members(g)) for g in merged},{frozenset((1,2)),frozenset((3,4))})

    def test_broader_review_relation_merges_groups_but_never_becomes_deletion_evidence(self):
        assets=self.assets(4); strict=self.edges([(1,2),(3,4)]); broad=self.edges([(1,2),(2,3),(3,4)])
        scores={i:5-i for i in range(1,5)}
        merged=merge_review_groups(assets,strict,suggestions(assets,strict,scores),scores,'album',review_neighbors=broad)
        self.assertEqual(len(merged),1)
        self.assertEqual(set(group_members(merged[0])),{1,2,3,4})
        self.assertNotIn(3,merged[0]['matches']['2'])
        self.assertEqual({(m['id'],m['reference']) for m in merged[0]['candidates']},{(2,1),(4,3)})

    def test_weaker_geometry_can_only_link_review_and_cannot_authorize_a_deletion(self):
        from boyo.vision import is_related,is_duplicate
        weak={'overlap':.75,'spread':.2,'correlation':.6,'inliers':14,'inlier_ratio':.22}
        self.assertTrue(is_related(weak,'较强'))
        self.assertFalse(is_duplicate(weak,'较强'))
        self.assertFalse(is_related({**weak,'inliers':8},'较强'))

    def test_deleted_unassigned_member_is_visible_but_never_made_a_reference(self):
        assets=self.assets(3); assets[1]['decision']=assets[2]['decision']='delete'
        edges=self.edges([(1,2),(2,3)]); scores={1:1,2:.9,3:.8}
        merged=merge_review_groups(assets,edges,suggestions(assets,edges,scores),scores,'album')[0]
        self.assertEqual(set(group_members(merged)),{1,2,3})
        self.assertEqual(group_references(merged),[1])
        self.assertEqual([m['id'] for m in merged['candidates']],[2])

    def test_large_connected_sequence_uses_iterative_traversal_and_unique_members(self):
        assets=self.assets(3000); edges=self.edges([(i,i+1) for i in range(1,3000)])
        # Real star groups, without quadratic representative search in this traversal test.
        stars=[{'id':f'star-{i}','keep':i,'kind':'duplicate','candidates':[{'id':i+1,'phash_distance':2,'dhash_distance':3}]}
               for i in range(1,3000,2)]
        group=merge_review_groups(assets,edges,stars,{},'album')[0]
        self.assertEqual(len(group_members(group)),3000)
        self.assertEqual(len(group['candidates']),1500)


class GroupedReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='boyo-grouped-')
        self.root=Path(self.tmp.name); self.album=self.root/'photos'; (self.album/'jpg').mkdir(parents=True)
        for n,color in enumerate(('red','green','blue','yellow'),1): Image.new('RGB',(120,80),color).save(self.album/'jpg'/f'{n}.jpg')
        selected=self.album/'精选/jpg'; selected.mkdir(parents=True); shutil.copy2(self.album/'jpg/4.jpg',selected/'4.jpg')
        self.runtime=Runtime(self.root/'tools/Boyo/local'); self.runtime.prepare(); self.store=Store(self.runtime.database(self.root))
        Scanner(self.store,self.root).scan(self.album)
        self.assets=[a for a in self.store.assets('photos') if a['role']=='ordinary_jpeg']
        self.ids={Path(a['path']).stem:a['id'] for a in self.assets}
        one,two,three,four=(self.ids[str(i)] for i in range(1,5))
        edges={one:{two:(2,3)},two:{one:(2,3),three:(2,3)},three:{two:(2,3),four:(2,3)},four:{three:(2,3)}}
        stars=[{'id':'original-a','keep':one,'kind':'duplicate','candidates':[{'id':two,'phash_distance':2,'dhash_distance':3}]},
               {'id':'original-b','keep':four,'kind':'duplicate','candidates':[{'id':three,'phash_distance':2,'dhash_distance':3}]}]
        self.group=merge_review_groups(self.assets,edges,stars,{},'photos')[0]
        self.store.execute('INSERT INTO groups VALUES (?,?,?)',(self.group['id'],'photos',json.dumps(self.group)))
        self.window=MainWindow(self.store,self.runtime,self.root,self.runtime.settings())

    def tearDown(self):
        self.window.close(); self.window.deleteLater(); self.app.processEvents(); self.tmp.cleanup()

    def test_one_group_contains_both_references_and_preview_uses_per_photo_contexts(self):
        self.assertEqual(self.window.group_list.count(),1)
        self.assertEqual(set(self.window.review_cards),set(self.ids.values()))
        with patch('boyo.gui.PhotoPreviewDialog') as preview:
            self.window.open_review_preview(self.ids['2'])
            self.assertEqual(set(preview.call_args.args[3]),set(self.ids.values()))
            contexts=preview.call_args.kwargs['context']
            self.assertEqual(contexts[self.ids['2']],'original-a'); self.assertEqual(contexts[self.ids['3']],'original-b')

    def test_adoption_keeps_direct_references_and_does_not_cross_train_merged_groups(self):
        for a in self.assets:
            self.store.save_feature(a,FEATURE_VERSION,{'vector':[.1]*9,'quality':.5})
        self.window.review_photo_decide(self.ids['4'],'keep')  # Explicit feedback, separate from folder protection.
        self.window.accept_group()
        self.assertEqual([self.store.asset(self.ids[str(i)])['decision'] for i in range(1,5)],['keep','delete','delete','keep'])
        from boyo.preferences import train
        # The explicitly confirmed protected keeper supplies one weak mixed pair;
        # the other subgroup contains only AI adoption and cannot train itself.
        self.assertEqual(train(self.store,self.runtime)['pairs'],1)
        self.assertTrue(all((self.root/a['path']).exists() for a in self.assets))

    def test_deleting_one_reference_skips_its_candidates_but_other_subgroup_still_works(self):
        self.window.review_photo_decide(self.ids['1'],'delete')
        self.assertTrue(self.window.adopt_button.isEnabled())
        self.window.accept_group()
        self.assertEqual(self.store.asset(self.ids['2'])['decision'],'pending')
        self.assertEqual(self.store.asset(self.ids['3'])['decision'],'delete')
        self.assertEqual(self.store.asset(self.ids['4'])['decision'],'keep')
        self.assertFalse(self.window.adopt_button.isEnabled())

    def test_plan_reference_uses_actual_direct_match_instead_of_group_primary(self):
        self.window.accept_group()
        plan=Operations(self.store,self.root).deletion('photos')
        dialog=PlanDialog(plan,self.window)
        try:
            self.assertEqual(dialog.references[self.ids['2']],self.ids['1'])
            self.assertEqual(dialog.references[self.ids['3']],self.ids['4'])
            self.assertEqual(dialog.contexts[self.ids['2']],'original-a')
            self.assertEqual(dialog.contexts[self.ids['3']],'original-b')
        finally: dialog.close(); dialog.deleteLater(); self.app.processEvents()

    def test_mixed_context_batch_still_rolls_back_when_protection_rejects_a_photo(self):
        with self.assertRaises(ValueError):
            self.store.decide_many({self.ids['1']:'keep',self.ids['4']:'delete'},
                                   {self.ids['1']:'original-a',self.ids['4']:'original-b'})
        self.assertEqual(self.store.asset(self.ids['1'])['decision'],'pending')
        self.assertIsNone(self.store.one('SELECT * FROM reviews WHERE asset_id=?',(self.ids['1'],)))

    def test_large_group_scroll_keeps_full_preview_scope_and_bounded_cards(self):
        for n in range(5,56): Image.new('RGB',(120,80),'gray').save(self.album/'jpg'/f'{n}.jpg')
        Scanner(self.store,self.root).scan(self.album)
        assets=[a for a in self.store.assets('photos') if a['role']=='ordinary_jpeg']; keeper=assets[0]['id']
        ids=[a['id'] for a in assets]
        group={'id':'large','keep':keeper,'keeps':[keeper],'members':ids,'candidates':[{'id':aid,'phash_distance':2,'dhash_distance':3} for aid in ids if aid!=keeper]}
        self.store.execute('DELETE FROM groups'); self.store.execute('INSERT INTO groups VALUES (?,?,?)',('large','photos',json.dumps(group)))
        self.window.refresh()
        self.assertEqual(self.window.compare.count(),55)
        self.assertLessEqual(len(self.window.review_cards),12)
        with patch('boyo.gui.PhotoPreviewDialog') as preview:
            self.window.open_review_preview(ids[30])
            self.assertEqual(len(preview.call_args.args[3]),55)
        self.assertEqual(self.window.compare.count(),55)


if __name__=='__main__': unittest.main()
