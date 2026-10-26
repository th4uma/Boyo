import tempfile
import unittest
from pathlib import Path
import json
from unittest.mock import patch

from boyo.config import Runtime
from boyo.imaging import FEATURE_VERSION
from boyo.preferences import personal_score, train, feedback_data, _examples, model_path, load_model, retain_both
from boyo.store import Store
from boyo.learning_report import learning_report


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boyo-learning-")
        self.runtime = Runtime(Path(self.tmp.name)); self.runtime.prepare()
        self.store = Store(self.runtime.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def photo(self,album,name,decision=None,context='manual',source='manual',value=.5):
        rel=f'{album}/jpg/{name}.jpg'
        aid=self.store.execute('INSERT INTO assets(album,path,stem,role,logical_key,size,mtime_ns) VALUES (?,?,?,?,?,?,?)',
            (album,rel,name,'ordinary_jpeg',rel,1,1))
        self.store.save_feature(self.store.asset(aid),FEATURE_VERSION,{'quality':.5,'vector':[value]*9})
        if decision: self.store.decide([aid],decision,context,source=source)
        return aid

    def test_insufficient_evidence_does_not_claim_learning(self):
        result = train(self.store, self.runtime)
        self.assertFalse(result["enabled"])
        self.assertFalse((self.runtime.home / "models/preference.json").exists())

    def test_report_separates_manual_adopted_and_last_validation_without_training(self):
        self.photo('A','one','keep');self.photo('A','two','delete');self.photo('A','bulk','keep',source='adopt')
        state={'photos':2,'pairs':132,'enabled':False,'updated':1,'revision':1}
        self.store.execute("INSERT INTO learning_state VALUES ('automatic',?)",(json.dumps(state),))
        with patch('boyo.preferences.train',side_effect=AssertionError('view started training')):
            report=learning_report(self.store,self.runtime)
        self.assertEqual((report['manual'],report['keep'],report['delete'],report['adopted'],report['albums']),(2,1,1,1,1))
        self.assertEqual(report['pairs'],132);self.assertIn('至少需要 2',report['reason']);self.assertFalse(report['enabled'])
        self.assertIn('暂无可靠的提升数字',report['details']);self.assertNotIn('验证一致率',report['details'])
        self.assertEqual(self.store.learning_state(),state)

    def test_report_checks_deployed_model_and_only_shows_real_validation_scores(self):
        self.photo('A','a','keep');self.photo('B','b','delete')
        payload={'version':'behavior-v1','feature_version':FEATURE_VERSION,'enabled':True,'updated':1,
                 'rank':{'coef':[1.],'mean':[0.],'scale':[1.],'accuracy':.9,'baseline':.7},'retention':None,'quality':{}}
        path=model_path(self.runtime,self.store.path);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(payload))
        self.store.execute("INSERT INTO learning_state VALUES ('automatic',?)",(json.dumps(payload),))
        report=learning_report(self.store,self.runtime)
        self.assertTrue(report['enabled']);self.assertIn('同构图排序',report['enabled_items']);self.assertIn('90.0%',report['details']);self.assertIn('70.0%',report['details'])
        path.unlink() # Test-owned model only.
        report=learning_report(self.store,self.runtime)
        self.assertFalse(report['enabled']);self.assertIn('文件缺失',report['reason']);self.assertNotIn('90.0%',report['details'])

    def test_single_album_validation_records_specific_reason_even_with_many_pairs(self):
        for index in range(32):
            self.photo('A',f'keep{index}','keep',context=f'group{index}',value=.9)
            self.photo('A',f'drop{index}','delete',context=f'group{index}',value=.1)
        result=train(self.store,self.runtime)
        self.assertEqual(result['pairs'],32);self.assertFalse(result['enabled'])
        self.assertEqual(result['diagnostics']['rank']['albums'],1);self.assertIn('至少需要 2',result['diagnostics']['rank']['reason'])

    def test_group_holdout_can_enable_improving_small_model(self):
        for album in ("A", "B"):
            for group in range(15):
                for decision, value, quality in (("keep", 0.9, 0.2), ("delete", 0.1, 0.8)):
                    rel = f"{album}/jpg/{group}-{decision}.JPG"
                    asset_id = self.store.execute("""INSERT INTO assets(album,path,stem,role,logical_key,size,mtime_ns)
                                                   VALUES (?,?,?,?,?,?,?)""",
                                                  (album, rel, rel, "ordinary_jpeg", rel, 1, 1))
                    a = self.store.asset(asset_id)
                    self.store.save_feature(a, FEATURE_VERSION, {"quality": quality,
                                            "vector": [value, 0, 0, 0, 0, 0, 0, 0, 0], "error": None})
                    self.store.decide([asset_id], decision, f"{album}-group-{group}")
        result = train(self.store, self.runtime)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["pairs"], 30)
        self.assertGreater(result["accuracy"], result["baseline"])
        a = {"quality": 0.5, "vector": [0.9, 0, 0, 0, 0, 0, 0, 0, 0]}
        b = {"quality": 0.5, "vector": [0.1, 0, 0, 0, 0, 0, 0, 0, 0]}
        self.assertGreater(personal_score(self.runtime, a), personal_score(self.runtime, b))
        # Verify the actual deployed blend, not just a classifier's abstract accuracy.
        a['quality']=.2; b['quality']=.8
        self.assertGreater(personal_score(self.runtime,a),personal_score(self.runtime,b))

    def test_adoption_alone_is_not_training_and_manual_confirmation_upgrades_signal(self):
        keep=self.photo('A','keep','keep','group','adopt')
        drop=self.photo('A','drop','delete','group','adopt')
        self.assertEqual(train(self.store,self.runtime)['pairs'],0)
        self.store.decide([keep],'keep','group')
        self.assertEqual(train(self.store,self.runtime)['pairs'],1)
        self.assertEqual(self.store.one('SELECT source FROM feedback WHERE asset_id=?',(keep,))['source'],'manual')
        count=self.store.one('SELECT COUNT(*) AS n FROM feedback_events')['n']
        self.store.decide([keep],'keep','group',source='adopt')
        self.assertEqual(self.store.one('SELECT COUNT(*) AS n FROM feedback_events')['n'],count)
        self.assertEqual(self.store.one('SELECT source FROM feedback WHERE asset_id=?',(keep,))['source'],'manual')

    def test_undo_withdraws_training_label_and_preserves_action_history(self):
        aid=self.photo('A','one','delete','group')
        self.store.decide([aid],'keep','group')
        self.store.decide([aid],'pending','group')
        self.assertFalse(feedback_data(self.store))
        events=self.store.rows('SELECT previous_decision,decision FROM feedback_events ORDER BY id')
        self.assertEqual([(r['previous_decision'],r['decision']) for r in events],
                         [('pending','delete'),('delete','keep'),('keep','pending')])

    def test_same_state_repeated_click_does_not_inflate_training(self):
        aid=self.photo('A','one','keep','group')
        for _ in range(20): self.store.decide([aid],'keep','group')
        self.assertEqual(self.store.one('SELECT COUNT(*) AS n FROM feedback_events')['n'],1)

    def test_changed_file_invalidates_labels_and_folder_protection_is_not_a_label(self):
        changed=self.photo('A','changed','keep','group')
        protected=self.photo('A','protected')
        self.store.execute('UPDATE assets SET mtime_ns=2 WHERE id=?',(changed,))
        self.store.execute("UPDATE assets SET protected=1,decision='keep' WHERE id=?",(protected,))
        self.assertEqual(feedback_data(self.store),{})

    def test_old_reviews_without_provenance_do_not_become_manual_labels(self):
        aid=self.photo('A','old')
        self.store.execute("UPDATE assets SET decision='keep' WHERE id=?",(aid,))
        self.store.execute("INSERT INTO reviews VALUES (?,?,?,?)",(aid,'group','keep',1))
        self.assertEqual(feedback_data(self.store),{})

    def test_gallery_actions_use_direct_matching_links_and_never_cross_broad_merge(self):
        one=self.photo('A','one','keep'); two=self.photo('A','two','delete')
        three=self.photo('A','three','keep'); four=self.photo('A','four','delete')
        data={'id':'merged','kind':'duplicate','keep':one,'members':[one,two,three,four],
              'candidates':[],'matches':{str(one):[two],str(two):[one],str(three):[four],str(four):[three]}}
        self.store.execute('INSERT INTO groups VALUES (?,?,?)',('merged','A',json.dumps(data)))
        rank,_=_examples(self.store,feedback_data(self.store))
        self.assertEqual(len(rank)//2,2)

    def test_manual_gallery_keeps_do_not_treat_all_other_photos_as_negatives(self):
        self.photo('A','keep','keep'); self.photo('A','pending'); self.photo('A','drop','delete')
        self.assertEqual(train(self.store,self.runtime)['pairs'],0)

    def test_automatic_batch_guard_does_no_encoding_or_fitting_for_few_actions(self):
        self.photo('A','one','keep')
        with patch('boyo.preferences.ensure_embeddings') as encoder,patch('boyo.preferences._fit') as fit:
            self.assertTrue(train(self.store,self.runtime,root=Path(self.tmp.name),force=False)['skipped'])
        encoder.assert_not_called(); fit.assert_not_called()

    def test_stale_training_is_not_published(self):
        self.photo('A','one','keep')
        with patch.object(self.store,'feedback_revision',side_effect=[1,2]):
            self.assertTrue(train(self.store,self.runtime)['stale'])
        self.assertFalse(model_path(self.runtime).exists())

    def test_models_are_separate_for_each_workspace(self):
        other=Store(self.runtime.home/'data'/'library-other.sqlite3')
        self.assertNotEqual(model_path(self.runtime,self.store.path),model_path(self.runtime,other.path))
        model_path(self.runtime,self.store.path).parent.mkdir(parents=True,exist_ok=True)
        model_path(self.runtime,self.store.path).write_text('{broken',encoding='utf-8')
        self.assertIsNone(load_model(self.runtime,self.store.path))

    def test_same_inputs_reuse_validation_instead_of_fitting_again(self):
        self.photo('A','one','keep')
        train(self.store,self.runtime)
        with patch('boyo.preferences._fit') as fit:
            self.assertTrue(train(self.store,self.runtime)['skipped'])
            fit.assert_not_called()

    def test_new_matching_evidence_invalidates_prior_validation_without_more_clicks(self):
        one=self.photo('A','one','keep'); two=self.photo('A','two','delete')
        self.assertEqual(train(self.store,self.runtime)['pairs'],0)
        group={'id':'new','kind':'duplicate','keep':one,'candidates':[],
               'matches':{str(one):[two],str(two):[one]}}
        self.store.execute('INSERT INTO groups VALUES (?,?,?)',('new','A',json.dumps(group)))
        result=train(self.store,self.runtime)
        self.assertFalse(result.get('skipped')); self.assertEqual(result['pairs'],1)

    def test_partial_batch_failure_rolls_back_feedback_history_as_well_as_decisions(self):
        one=self.photo('A','one'); protected=self.photo('A','protected')
        self.store.execute('UPDATE assets SET protected=1 WHERE id=?',(protected,))
        with self.assertRaises(ValueError): self.store.decide_many({one:'keep',protected:'delete'},'group')
        self.assertEqual(self.store.asset(one)['decision'],'pending')
        self.assertEqual(self.store.rows('SELECT * FROM feedback_events'),[])
        self.assertEqual(self.store.rows('SELECT * FROM feedback'),[])

    def test_learning_cache_io_policy_does_not_reduce_user_decision_or_journal_durability(self):
        cache=Store(self.store.path,regenerable=True)
        with cache.connect() as db:
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0],1)
            self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0],100)
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0],2)

    def test_retention_model_can_only_filter_strict_suggestions(self):
        from boyo.analysis import direct_suggestions
        assets=[{'id':i,'path':str(i),'protected':0,'decision':'pending'} for i in range(1,4)]
        edges={1:{2:(1,2)},2:{1:(1,2)}}
        groups=direct_suggestions(assets,edges,{1:1,2:.5,3:0},retain=lambda *args:True)
        self.assertEqual(groups,[])
        self.assertFalse(retain_both(None,{'vector':[.1]*9},{'vector':[.1]*9},(1,2)))

    def test_quality_only_screening_labels_train_without_full_photo_analysis(self):
        from boyo.quality import QUALITY_VERSION
        from boyo.preferences import quality_hint
        for album in ('A','B'):
            for n in range(12):
                decision='keep' if n%2 else 'delete'
                aid=self.photo(album,str(n),decision,'quality:standalone')
                self.store.execute('DELETE FROM features WHERE path=?',(self.store.asset(aid)['path'],))
                self.store.execute('UPDATE assets SET metadata=? WHERE id=?',(json.dumps({'camera':'new-camera'}),aid))
                metrics={'acutance':1. if decision=='keep' else .1,'detail_acutance':1. if decision=='keep' else .1}
                self.store.execute('INSERT INTO quality_cache VALUES (?,?,?,?,?)',
                    (self.store.asset(aid)['path'],1,1,QUALITY_VERSION,json.dumps(metrics)))
        result=train(self.store,self.runtime)
        self.assertIn('new-camera',result['quality'])
        self.assertEqual(result['pairs'],0)
        self.assertIsNone(quality_hint(result,{'metadata':json.dumps({'camera':'old-camera'})},{'metrics':{}}))


if __name__ == "__main__":
    unittest.main()
