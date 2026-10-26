"""Behavioral regressions for continuous review and work scoped to its source."""
import json
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import Qt,QPoint,QSize
from PySide6.QtWidgets import QApplication,QLineEdit,QPushButton,QListWidget,QStyle,QStyleOptionViewItem,QWidget,QLabel
from PySide6.QtTest import QTest
from boyo.browser_widgets import visible_rows,QuietItemDelegate

from boyo.config import Runtime
from boyo.collections import Collections
from boyo.fs import sha256
from boyo.gui import MainWindow
from boyo.operations import Operations
from boyo.plan_dialog import PlanDialog
from boyo.photo_widgets import PhotoPreviewDialog
from boyo.scanner import Scanner
from boyo.store import Store
from boyo.tasks import Control


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='boyo-workspaces-');self.root=Path(self.tmp.name)
        self.runtime=Runtime(self.root/'tools/Boyo/local');self.runtime.prepare();self.store=Store(self.runtime.database(self.root))
        for album in ('A','B'):
            for sub in ('jpg','raw','成片','精选/jpg'):(self.root/album/sub).mkdir(parents=True)
            for index in range(1,6):Image.new('RGB',(90+index,60),(index*30,80,100)).save(self.root/album/f'jpg/{index}.JPG')
            Image.new('RGB',(100,70),'cyan').save(self.root/album/'成片/cover.JPG')
            Scanner(self.store,self.root).scan(self.root/album)
        self.ids={Path(a['path']).stem:a['id'] for a in self.store.assets('A') if a['role']=='ordinary_jpeg'}
        self.group={'id':'original','keep':self.ids['1'],'candidates':[{'id':self.ids[n],'phash_distance':2,'dhash_distance':2} for n in ('2','3')]}
        self.store.execute('INSERT INTO groups VALUES (?,?,?)',('original','A',json.dumps(self.group)))
        self.window=MainWindow(self.store,self.runtime,self.root,self.runtime.settings());self.release=threading.Event();self.dialogs=[]

    def wait(self,condition,timeout=8):
        deadline=time.monotonic()+timeout
        while not condition() and time.monotonic()<deadline:self.app.processEvents();time.sleep(.005)
        self.assertTrue(condition())

    def tearDown(self):
        self.release.set();self.window.clear_queued_tasks()
        if self.window.thread:self.window.cancel();self.wait(lambda:self.window.thread is None)
        for dialog in self.dialogs:dialog.close();dialog.deleteLater()
        self.window.close();self.window.deleteLater();self.app.processEvents();self.tmp.cleanup()

    def blocking(self,progress,control):
        while not self.release.wait(.01):
            if control.cancel.is_set():return {'cancelled':True}
        return {'complete':True}

    def seed_features(self):
        rows=[]
        for index,asset in enumerate(self.store.assets('A')):
            if asset['role'] in {'ordinary_jpeg','finished'}:
                rows.append((asset,{'quality':index/10,'sha256':sha256(self.root/asset['path']),'phash':f'{index:016x}',
                                    'dhash':f'{index:016x}','vector':[0.,index/10,.2]}))
        self.store.save_features(rows,'test')

    def seed_journal(self,count):
        with self.store.connect() as db:
            db.execute("INSERT INTO plans VALUES ('history','A','delete',1,'done','{}')")
            db.executemany("INSERT INTO journal(plan_id,seq,src,dst,action,sha256,state) VALUES ('history',?,?,?,?,?,'done')",
                [(i,f'A/jpg/{i}.JPG',f'已删除备份/A/{i}.JPG','move' if i%2 else 'copy','test') for i in range(count)])

    def test_three_workspaces_and_user_defaults(self):
        self.assertEqual(self.window.workspaces.count(),3)
        self.assertFalse(self.window.tabs.tabBar().isVisible())
        self.window.photo_navigation.setCurrentRow(1);self.assertEqual(self.window.review_stack.currentIndex(),1)
        self.window.photo_navigation.setCurrentRow(2);self.assertEqual(self.window.quality_stack.currentIndex(),1)
        self.assertFalse(self.window.settings['auto_advance'])

    def test_history_viewport_headers_and_real_rows_are_visible(self):
        self.seed_journal(2);window=self.window;window.show();window.workspaces.setCurrentIndex(1);window.file_navigation.setCurrentRow(2);self.app.processEvents()
        self.assertTrue(window.journal.isVisible());self.assertTrue(window.journal.viewport().isVisible());self.assertTrue(window.journal.horizontalHeader().isVisible())
        self.assertEqual(window.journal.rowCount(),2);self.assertIn('2/2 条',window.history_summary.text())
        window.journal.setCurrentCell(0,0);self.assertTrue(window.history_restore.isEnabled())
        window.journal.setCurrentCell(1,0);self.assertFalse(window.history_restore.isEnabled())

    def test_history_loads_older_records_and_resets_range_on_album_switch(self):
        self.seed_journal(625);window=self.window;window.show();window.workspaces.setCurrentIndex(1);window.file_navigation.setCurrentRow(2);self.app.processEvents()
        self.assertEqual(window.journal.rowCount(),300);self.assertIn('300/625 条',window.history_summary.text());window.journal.setCurrentCell(7,0);selected=window.journal.item(7,0).text()
        window.history_more.click();self.assertEqual(window.journal.rowCount(),600);self.assertEqual(window.journal.item(window.journal.currentRow(),0).text(),selected)
        window.history_more.click();self.assertEqual(window.journal.rowCount(),625);self.assertTrue(window.history_more.isHidden())
        window.album_picker.setCurrentText('B');self.app.processEvents();self.assertEqual(window.journal.rowCount(),0);self.assertEqual(window.history_stack.currentIndex(),1)
        self.assertIn('0/0 条',window.history_summary.text());self.assertFalse(window.history_restore.isEnabled())

    def test_preference_entry_opens_visible_learning_details_with_real_counts(self):
        window=self.window;window.apply_decisions({self.ids['2']:'keep',self.ids['3']:'delete'},'manual');window.show();self.app.processEvents()
        QTest.mouseClick(window.learning_status,Qt.MouseButton.LeftButton);self.app.processEvents()
        self.assertTrue(window.settings_dialog.isVisible());self.assertTrue(window.learning_page.isVisible());self.assertTrue(window.learning.isVisible())
        self.assertIn('2</span>',window.learning_panel.metrics[0].text());self.assertIn('至少需要 2',window.learning_panel.reason.text())
        self.assertIsNone(window.auto_learning.process)

    def test_hidden_learning_view_does_not_read_reports_for_every_choice(self):
        window=self.window;window.auto_learning.dirty=False
        with patch('boyo.learning_report.learning_report') as report:
            for _ in range(100):window.auto_learning.changed()
            report.assert_not_called()
        self.assertFalse(window.learning_refresh_timer.isActive());self.assertIn('等待空闲',window.learning_status.text())

    def test_all_photo_tasks_use_scroll_thumbnails_without_navigation_buttons(self):
        self.window.show();self.app.processEvents()
        for row in (0,1,2,3):
            self.window.photo_navigation.setCurrentRow(row);self.app.processEvents()
            self.assertTrue((self.window.gallery if row in (0,3) else self.window.compare if row==1 else self.window.quality_grid).isVisible())
            visible=[button.text() for button in self.window.findChildren(QPushButton) if button.isVisible()]
            self.assertFalse(any(word in text for text in visible for word in ('上一页','下一页','上一张','下一张','适应窗口','检查原尺寸')))

    def test_review_thumbnail_double_click_opens_viewer_and_single_click_only_selects(self):
        window=self.window;window.show();window.photo_navigation.setCurrentRow(1);self.app.processEvents()
        card=window.review_cards[self.ids['2']]
        with patch.object(window,'open_review_preview') as preview:
            # Rebind this card to the observed handler without opening a modal test window.
            card.previewRequested.disconnect();card.previewRequested.connect(preview)
            QTest.mouseClick(card.image_button,Qt.MouseButton.LeftButton);self.assertEqual(preview.call_count,0)
            QTest.mouseDClick(card.image_button,Qt.MouseButton.LeftButton);self.assertEqual(preview.call_args.args[0],self.ids['2'])

    def test_thumbnail_footer_buttons_change_gallery_state_without_opening_a_viewer(self):
        window=self.window;window.show();self.app.processEvents()
        item=next(window.gallery.item(n) for n in range(window.gallery.count()) if window.gallery.item(n).data(Qt.ItemDataRole.UserRole)==self.ids['2'])
        QTest.mouseClick(window.gallery.viewport(),Qt.MouseButton.LeftButton,pos=window.gallery.visualItemRect(item).center())
        with patch.object(window,'open_preview',side_effect=AssertionError('thumbnail decisions must not open a viewer')):
            for text,state in (('保留','keep'),('标记待删','delete'),('撤销决定','pending')):
                button=next(b for b in window.native_gallery_footer.findChildren(QPushButton) if b.text()==text)
                QTest.mouseClick(button,Qt.MouseButton.LeftButton)
                self.assertEqual(self.store.asset(self.ids['2'])['decision'],state)
                self.assertEqual(window.gallery.currentItem(),item)
        self.assertTrue((self.root/'A/jpg/2.JPG').exists())
        window.gallery.clearSelection()
        next(b for b in window.native_gallery_footer.findChildren(QPushButton) if b.text()=='保留').click()
        self.assertIn('先选中',window.status.text())

    def test_similar_thumbnail_selection_buttons_act_only_on_selected_photos(self):
        window=self.window;window.show();window.photo_navigation.setCurrentRow(1);self.app.processEvents()
        card=window.review_cards[self.ids['2']]
        QTest.mouseClick(card.image_button,Qt.MouseButton.LeftButton)
        QTest.mouseClick(window.review_keep_selection,Qt.MouseButton.LeftButton)
        self.assertEqual(self.store.asset(self.ids['2'])['decision'],'keep')
        QTest.mouseClick(window.review_delete_selection,Qt.MouseButton.LeftButton)
        self.assertEqual(self.store.asset(self.ids['2'])['decision'],'delete')
        self.assertEqual(self.store.asset(self.ids['3'])['decision'],'pending')
        self.assertEqual(self.store.asset(self.ids['1'])['decision'],'pending')

    def test_quality_thumbnail_footer_buttons_use_the_same_decisions_and_keep_the_current_photo(self):
        window=self.window;window.show();window.photo_navigation.setCurrentRow(2)
        a=self.store.asset(self.ids['2']);row={'id':a['id'],'size':a['size'],'mtime_ns':a['mtime_ns'],'path':a['path'],
            'severity':'可疑问题','metrics':{},'issues':[{'code':'motion_blur','label':'疑似运动模糊 / 手抖拖影','detail':'测试拖影证据'}]}
        window.quality_results=[row];window.quality_entries={a['id']:row};window.refresh_quality_grid();self.app.processEvents()
        item=window.quality_grid.item(0)
        QTest.mouseClick(window.quality_grid.viewport(),Qt.MouseButton.LeftButton,pos=window.quality_grid.visualItemRect(item).center())
        page=window.tabs.widget(5)
        for text,state in (('保留','keep'),('标记待删','delete'),('撤销决定','pending')):
            button=next(b for b in page.findChildren(QPushButton) if b.isVisible() and b.text()==text)
            QTest.mouseClick(button,Qt.MouseButton.LeftButton)
            self.assertEqual(self.store.asset(a['id'])['decision'],state)
            self.assertEqual(window.quality_grid.currentItem(),item)
        window.issue_filter.setCurrentIndex(4);self.assertEqual(window.quality_grid.count(),1)

    def test_large_album_scrolling_loads_only_viewport_thumbnails(self):
        for index in range(6,106):Image.new('RGB',(90,60),(index,30,60)).save(self.root/'A/jpg'/f'{index:03d}.JPG')
        Scanner(self.store,self.root).scan(self.root/'A');self.window.refresh();self.window.show();self.app.processEvents()
        self.assertEqual(self.window.gallery.count(),105)
        self.window.load_visible_thumbnails();self.assertLess(len(visible_rows(self.window.gallery)),40)
        owner=self.window.gallery_thumb_owner
        pending=[request for request in self.window.thumbnail_loader.requests.values() if any(o==owner for o,fn in request['callbacks'])]
        self.assertLess(len(pending),40)
        self.window.gallery.scrollToBottom();self.app.processEvents();self.window.load_visible_thumbnails()
        self.assertIn(104,visible_rows(self.window.gallery))

    def test_modal_viewer_has_keyboard_navigation_and_no_navigation_toolbar(self):
        dialog=PhotoPreviewDialog(self.store,self.runtime,self.root,[self.ids[str(i)] for i in range(1,6)],self.ids['1'],parent=self.window);self.dialogs.append(dialog)
        dialog.show();dialog.activateWindow();self.app.processEvents()
        self.assertFalse(dialog.previous_button.isVisible());self.assertFalse(dialog.next_button.isVisible())
        QTest.keyClick(dialog,Qt.Key.Key_Right);self.assertEqual(dialog.current_id,self.ids['2'])
        QTest.keyClick(dialog,Qt.Key.Key_Left);self.assertEqual(dialog.current_id,self.ids['1'])
        self.assertFalse(any(button.text() in {'适应窗口','原始分辨率','放大 +','缩小 −'} for button in dialog.findChildren(QPushButton) if button.isVisible()))

    def test_selected_navigation_uses_focus_free_delegate(self):
        self.assertIsInstance(self.window.photo_navigation.itemDelegate(),QuietItemDelegate)
        self.assertEqual(self.window.workspaces.focusPolicy(),Qt.FocusPolicy.NoFocus)

    def test_review_columns_adapt_to_window_width_without_clipping_actions(self):
        ids=list(self.ids.values());group={**self.group,'members':ids,'candidates':[{'id':aid} for aid in ids[1:]]}
        self.store.execute('UPDATE groups SET data=? WHERE id=?',(json.dumps(group),'original'));self.window.refresh()
        self.window.photo_navigation.setCurrentRow(1);self.window.show();columns=[]
        for width in (1000,1400,1920):
            self.window.resize(width,900);self.app.processEvents();time.sleep(.07);self.app.processEvents();self.window.load_visible_thumbnails();self.app.processEvents()
            grid=self.window.compare;top=grid.visualItemRect(grid.item(0)).top()
            columns.append(sum(grid.visualItemRect(grid.item(i)).top()==top for i in range(grid.count())))
            for card in self.window.review_cards.values():
                self.assertTrue(card.compact)
                self.assertFalse(any(button.isVisible() for button in (card.keep_button,card.delete_button,card.pending_button)))
                self.assertLessEqual(card.image_button.mapTo(card,card.image_button.rect().bottomLeft()).y(),card.height())
        self.assertGreaterEqual(columns[1],3);self.assertGreater(columns[2],columns[0]);self.assertGreaterEqual(columns[2],4)

    def test_compact_navigation_keeps_full_names_and_original_font_size(self):
        self.window.show();self.app.processEvents()
        self.assertLessEqual(self.window.side_stack.width(),160)
        self.assertEqual(self.window.photo_navigation.font().pixelSize(),13)
        expected=['普通照片','相似复核','废片筛查','待删复核','精选与成片']
        for row,name in enumerate(expected):
            lines=self.window.photo_navigation.item(row).text().splitlines();self.assertEqual(lines[0],name);self.assertEqual(len(lines),2)
        self.assertEqual(self.window.photo_navigation.horizontalScrollBarPolicy(),Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def test_read_work_allows_decisions_and_completion_preserves_current_group(self):
        window=self.window;window.photo_navigation.setCurrentRow(1);current=window.current_group()
        window.compare.setCurrentItem(window.review_items[self.ids['2']])
        window.run_task(self.blocking,kind='重新分析')
        self.assertTrue(window.tabs.isEnabled());self.assertTrue(window.album_picker.isEnabled())
        window.apply_decisions({self.ids['2']:'keep'},'manual')
        self.store.execute('INSERT INTO groups VALUES (?,?,?)',('new','A',json.dumps({**self.group,'id':'new'})))
        self.release.set();self.wait(lambda:window.thread is None)
        self.assertEqual(window.current_group()['id'],current['id'])
        self.assertEqual(window.compare.currentItem().data(Qt.ItemDataRole.UserRole),self.ids['2'])
        self.assertEqual(self.store.asset(self.ids['2'])['decision'],'keep');self.assertFalse(window.deferred_results)
        self.assertFalse(window.new_results_button.isVisible());self.assertEqual(len(window.group_data),2)

    def test_task_target_does_not_change_when_user_switches_albums(self):
        window=self.window;window.run_task(self.blocking,kind='重新分析');window.album_picker.setCurrentText('B')
        before=window.gallery.item(0);self.release.set();self.wait(lambda:window.thread is None)
        self.assertEqual(window.album,'B');self.assertIs(window.gallery.item(0),before)
        self.assertTrue(window.status.text().startswith('A'));self.assertFalse(window.deferred_results)

    def test_analysis_closure_captures_store_root_and_album(self):
        with patch.object(self.window,'run_task') as queue:self.window.start_analysis()
        work=queue.call_args.args[0];self.window.album_picker.setCurrentText('B')
        with patch('boyo.analysis.analyze',return_value={}) as analyze:work(lambda *args:None,Control())
        self.assertEqual(analyze.call_args.args[3],'A');self.assertIs(analyze.call_args.args[0],self.store)

    def test_queue_coalesces_same_read_and_is_bounded(self):
        window=self.window;window.run_task(self.blocking,kind='占位')
        for _ in range(15):window.run_task(lambda progress,control:{},kind='重新分析')
        self.assertEqual(len(window.task_queue),1)
        for index in range(7):window.run_task(lambda progress,control:{},kind='其他'+str(index))
        self.assertEqual(len(window.task_queue),4);window.clear_queued_tasks()

    def test_pending_rescue_stays_in_round_queue_and_reentry_rebuilds(self):
        window=self.window;window.apply_decisions({self.ids['2']:'delete',self.ids['3']:'delete'},'manual')
        window.photo_navigation.setCurrentRow(3);self.assertEqual(window.gallery_sequence.ids,[self.ids['2'],self.ids['3']])
        current=window.gallery_sequence.current_id;window.gallery_sequence.decide('keep')
        self.assertEqual(window.gallery_sequence.current_id,current);self.assertEqual(len(window.gallery_sequence.ids),2)
        self.assertIn('已救回',window.gallery.item(0).text());self.assertIn('待删 1 张',window.summary.text())
        window.photo_navigation.setCurrentRow(0);window.photo_navigation.setCurrentRow(3)
        self.assertEqual(window.gallery_sequence.ids,[self.ids['3']])

    def test_only_involved_logical_files_are_locked(self):
        window=self.window;window.run_task(self.blocking,kind='执行文件清单',locked_ids=[self.ids['2']])
        with self.assertRaises(ValueError):window.apply_decisions({self.ids['2']:'keep'},'manual')
        window.apply_decisions({self.ids['3']:'keep'},'manual');self.assertEqual(self.store.asset(self.ids['3'])['decision'],'keep')

    def test_nested_large_preview_cannot_bypass_active_file_lock(self):
        self.window.run_task(self.blocking,kind='执行文件清单',locked_ids=[self.ids['2']])
        dialog=PhotoPreviewDialog(self.store,self.runtime,self.root,[self.ids['2']],self.ids['2'],parent=self.window);self.dialogs.append(dialog)
        self.assertFalse(dialog.delete_button.isEnabled());dialog.decide('delete')
        self.assertEqual(self.store.asset(self.ids['2'])['decision'],'pending')

    def test_skip_execution_does_not_change_decision_and_raw_follows(self):
        raw=self.root/'A/raw/2.ARW';raw.write_bytes(b'raw');raw.with_suffix('.xmp').write_text('xmp')
        self.store.decide([self.ids['2'],self.ids['3']],'delete','manual')
        plan=Operations(self.store,self.root).deletion('A');dialog=PlanDialog(plan,self.window);self.dialogs.append(dialog)
        self.assertEqual(len(plan['actions']),4);dialog.candidates.setCurrentRow(0);dialog.toggle_skip()
        self.assertEqual(self.store.asset(self.ids['2'])['decision'],'delete');self.assertEqual(len(dialog.remaining_actions()),1)
        dialog.agree.setChecked(True);dialog.accept()
        result=Operations(self.store,self.root).execute(dialog.plan['id']);self.assertEqual(result['done'],1)
        self.assertTrue(raw.exists());self.assertTrue((self.root/'A/jpg/2.JPG').exists())

    def test_repeated_raw_check_uses_hash_cache_and_invalidates_changed_raw(self):
        shutil.copy2(self.root/'A/jpg/2.JPG',self.root/'A/精选/jpg/2.JPG');raw=self.root/'A/raw/2.ARW';raw.write_bytes(b'raw')
        first=Operations(self.store,self.root).fill_selected_raws('A');self.assertEqual(len(first['actions']),1)
        with patch('boyo.operations.sha256',wraps=sha256) as hashed:
            second=Operations(self.store,self.root).fill_selected_raws('A');self.assertEqual(hashed.call_count,0)
        self.assertGreater(second['timings']['digest_cache_hits'],0)
        raw.write_bytes(b'raw changed')
        with patch('boyo.operations.sha256',wraps=sha256) as hashed:Operations(self.store,self.root).fill_selected_raws('A');self.assertEqual(hashed.call_count,1)

    def test_copy_rechecks_content_even_if_size_and_time_are_unchanged(self):
        shutil.copy2(self.root/'A/jpg/2.JPG',self.root/'A/精选/jpg/2.JPG');raw=self.root/'A/raw/2.ARW';raw.write_bytes(b'original')
        plan=Operations(self.store,self.root).fill_selected_raws('A');stat=raw.stat()
        raw.write_bytes(b'changed!');os.utime(raw,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        result=Operations(self.store,self.root).execute(plan['id'])
        self.assertEqual(result['state'],'partial');self.assertEqual(result['done'],0)
        self.assertTrue(raw.exists());self.assertFalse((self.root/'A/精选/raw/2.ARW').exists())
        self.assertEqual(list((self.root/'A/精选/raw').glob('*.pending')),[])

    def test_deletion_target_conflict_postpones_whole_item_other_items_continue(self):
        raw=self.root/'A/raw/2.ARW';raw.write_bytes(b'raw');backup=self.root/'已删除备份/A/2.JPG';backup.parent.mkdir(parents=True);backup.write_bytes(b'previous backup')
        self.store.decide([self.ids['2'],self.ids['3']],'delete','manual');plan=Operations(self.store,self.root).deletion('A')
        self.assertEqual(len(plan['actions']),1);self.assertIn('整项暂缓',' '.join(plan['warnings']))
        Operations(self.store,self.root).execute(plan['id'])
        self.assertTrue(raw.exists());self.assertTrue((self.root/'A/jpg/2.JPG').exists());self.assertFalse((self.root/'A/jpg/3.JPG').exists());self.assertEqual(backup.read_bytes(),b'previous backup')

    def test_collection_fill_preserves_all_existing_slots_replace_is_atomic(self):
        self.seed_features();service=self.window.collections;set_id=service.create('A');service.add(set_id,self.ids['2'],slot=2,locked=False)
        result=service.generate(set_id,4,replace_unlocked=False);self.assertEqual(result['count'],4)
        by_slot={a['slot']:a for a in service.items(set_id)};self.assertEqual(by_slot[2]['id'],self.ids['2'])
        other=service.create('A');service.add(other,self.ids['1'],slot=1)
        before=service.items(set_id)
        with self.assertRaises(ValueError):service.replace(set_id,2,self.ids['1'])
        self.assertEqual(before,service.items(set_id));self.assertEqual(self.store.rows('SELECT * FROM feedback'),[])

    def test_automatic_new_set_has_no_empty_record_on_missing_cover(self):
        before=self.window.collections.list('A')
        with self.assertRaises(ValueError):self.window.collections.create_and_generate('A')
        self.assertEqual(before,self.window.collections.list('A'))

    def test_collection_options_are_restored_per_set(self):
        window=self.window;service=window.collections
        first=service.create('A');second=service.create('A');service.save_options(first,5,False);service.save_options(second,12,True)
        window.refresh_sets();window.set_picker.setCurrentIndex(window.set_picker.findData(second))
        self.assertEqual(window.set_count.value(),12);self.assertTrue(window.second_cover.isChecked());self.assertEqual(window.set_grid.count(),12)
        self.assertTrue(window.fill_set_button.isEnabled());self.assertTrue(window.redo_set_button.isEnabled())
        window.set_picker.setCurrentIndex(window.set_picker.findData(first))
        self.assertEqual(window.set_count.value(),5);self.assertFalse(window.second_cover.isChecked())
        service.drop(second);self.assertIsNone(self.store.one('SELECT * FROM set_options WHERE set_id=?',(second,)))

    def test_ready_plan_uses_latest_rescues_and_missing_source_is_a_placeholder(self):
        self.store.decide([self.ids['2'],self.ids['3']],'delete','manual');plan=Operations(self.store,self.root).deletion('A')
        self.store.decide([self.ids['2']],'keep','manual');(self.root/'A/jpg/3.JPG').unlink();Scanner(self.store,self.root).scan(self.root/'A')
        dialog=PlanDialog(plan,self.window);self.dialogs.append(dialog)
        self.assertEqual(dialog.remaining_actions(),[]);self.assertFalse(dialog.execute_button.isEnabled())

    def test_file_workspaces_have_distinct_pages(self):
        window=self.window;window.workspaces.setCurrentIndex(1)
        self.assertEqual([window.workspaces.tabText(i) for i in range(3)],['照片精简','文件工具','自动图集'])
        self.assertIs(window.side_stack.currentWidget(),window.file_navigation)
        for row in range(3):window.file_navigation.setCurrentRow(row);self.assertEqual(window.file_stack.currentIndex(),row)

    def test_pending_one_click_moves_jpg_raw_and_xmp_without_a_dialog(self):
        window=self.window;raw=self.root/'A/raw/2.ARW';raw.write_bytes(b'raw two')
        sidecar=raw.with_suffix('.xmp');sidecar.write_text('edits',encoding='utf-8')
        Scanner(self.store,self.root).scan(self.root/'A');window.refresh()
        window.apply_decisions({self.ids['2']:'delete'},'manual');window.photo_navigation.setCurrentRow(3);window.show();self.app.processEvents()
        self.assertFalse(window.gallery_actions.isVisible());self.assertTrue(window.delete_entry.isVisible())
        self.assertEqual([b.text() for b in window.delete_bar.findChildren(QPushButton)],['执行删除'])
        jpg_hash=sha256(self.root/'A/jpg/2.JPG')
        with patch.object(PlanDialog,'exec',side_effect=AssertionError('unexpected modal')),patch('boyo.workspace_ui.QMessageBox.question',side_effect=AssertionError('unexpected confirmation')):
            window.delete_entry.click();window.execute_delete();self.wait(lambda:window.thread is None)
        self.assertEqual(len(window.task_queue),0)
        self.assertFalse((self.root/'A/jpg/2.JPG').exists());self.assertFalse(raw.exists());self.assertFalse(sidecar.exists())
        self.assertEqual(sha256(self.root/'已删除备份/A/2.JPG'),jpg_hash)
        self.assertEqual((self.root/'A/deleteraw/2.ARW').read_bytes(),b'raw two')
        self.assertEqual((self.root/'A/deleteraw/2.xmp').read_text(encoding='utf-8'),'edits')
        self.assertEqual(len(self.store.rows("SELECT * FROM journal WHERE state='done'")),3)
        self.assertEqual(window.gallery.count(),0);self.assertIn('3/3',window.delete_report.toPlainText())

    def test_delete_queue_keeps_click_scope_honors_rescue_and_original_album(self):
        window=self.window;window.apply_decisions({self.ids['2']:'delete',self.ids['3']:'delete'},'manual')
        window.run_task(self.blocking,kind='扫描并分析');window.execute_delete();window.execute_delete()
        self.assertEqual(len(window.task_queue),1)
        window.apply_decisions({self.ids['2']:'keep',self.ids['4']:'delete'},'manual')
        window.album_picker.setCurrentText('B');self.release.set()
        self.wait(lambda:window.thread is None and not window.task_queue)
        self.assertTrue((self.root/'A/jpg/2.JPG').exists());self.assertTrue((self.root/'A/jpg/4.JPG').exists())
        self.assertFalse((self.root/'A/jpg/3.JPG').exists());self.assertTrue((self.root/'B/jpg/3.JPG').exists())
        self.assertEqual(window.album,'B');self.assertTrue(window.delete_report.isHidden())

    def test_direct_delete_postpones_conflict_and_new_selected_protection(self):
        window=self.window;window.apply_decisions({self.ids[n]:'delete' for n in ('2','3','4')},'manual')
        raw=self.root/'A/raw/2.ARW';raw.write_bytes(b'raw two')
        target=self.root/'已删除备份/A/2.JPG';target.parent.mkdir(parents=True);target.write_bytes(b'existing backup')
        shutil.copy2(self.root/'A/jpg/3.JPG',self.root/'A/精选/jpg/3.JPG')
        window.execute_delete();self.wait(lambda:window.thread is None)
        self.assertTrue(raw.exists());self.assertTrue((self.root/'A/jpg/2.JPG').exists());self.assertTrue((self.root/'A/jpg/3.JPG').exists())
        self.assertEqual(target.read_bytes(),b'existing backup');self.assertFalse((self.root/'A/jpg/4.JPG').exists())
        report=window.delete_report.toPlainText();self.assertIn('冲突',report);self.assertIn('保护',report)

    def test_raw_tools_show_checks_and_execute_inside_each_page(self):
        window=self.window;raw=self.root/'A/raw/2.ARW';raw.write_bytes(b'raw two')
        shutil.copy2(self.root/'A/jpg/2.JPG',self.root/'A/精选/jpg/2.JPG')
        orphan=self.root/'A/raw/orphan.ARW';orphan.write_bytes(b'orphan')
        Scanner(self.store,self.root).scan(self.root/'A');window.refresh();window.workspaces.setCurrentIndex(1)
        with patch.object(PlanDialog,'exec',side_effect=AssertionError('unexpected modal')):
            panel=window.file_panels['fill_raw'];panel.check_button.click();self.wait(lambda:window.thread is None)
            self.assertEqual(panel.check_table.rowCount(),1);self.assertEqual(panel.action_table.rowCount(),1)
            self.assertTrue(panel.execute_button.isEnabled());panel.execute_button.click();self.wait(lambda:window.thread is None)
            self.assertEqual((self.root/'A/精选/raw/2.ARW').read_bytes(),raw.read_bytes());self.assertTrue(raw.exists())
            self.assertFalse(panel.execute_button.isEnabled())
            window.file_navigation.setCurrentRow(1);panel=window.file_panels['orphan'];panel.check_button.click();self.wait(lambda:window.thread is None)
            self.assertEqual(panel.action_table.rowCount(),1);panel.execute_button.click();self.wait(lambda:window.thread is None)
        self.assertFalse(orphan.exists());self.assertEqual((self.root/'A/deleteraw/orphan.ARW').read_bytes(),b'orphan')

    def test_file_operation_failure_is_inline_and_does_not_move_changed_source(self):
        raw=self.root/'A/raw/2.ARW';raw.write_bytes(b'raw two')
        shutil.copy2(self.root/'A/jpg/2.JPG',self.root/'A/精选/jpg/2.JPG')
        window=self.window;window.make_plan('fill_raw');self.wait(lambda:window.thread is None)
        raw.write_bytes(b'changed raw content')
        with self.assertLogs(level='ERROR'),patch('boyo.workspace_ui.QMessageBox.warning',side_effect=AssertionError('unexpected modal')):
            window.execute_inline_plan('fill_raw');self.wait(lambda:window.thread is None)
        panel=window.file_panels['fill_raw'];self.assertEqual(panel.record['state'],'error')
        self.assertIn('未完成',panel.summary.text());self.assertFalse((self.root/'A/精选/raw/2.ARW').exists())

    def test_repeated_inline_results_and_long_paths_do_not_widen_window(self):
        window=self.window;window.workspaces.setCurrentIndex(1);window.resize(1000,700);window.show();self.app.processEvents()
        width=window.width();minimum=window.minimumSizeHint().width();long_path='A/raw/'+'长路径'*300+'.ARW'
        for index in range(12):
            plan=dict(id=str(index),album='A',kind='fill_raw',actions=[dict(action='copy',src=long_path,dst=long_path,reason='补齐')],checks=[dict(path=long_path,status='可补齐',detail=long_path)],warnings=[])
            window.plan_ready(plan);window.task_progress(index,12,long_path);self.app.processEvents()
        self.assertEqual(window.width(),width);self.assertLessEqual(window.minimumSizeHint().width(),minimum)
        self.assertEqual(len(window.file_results),1);self.assertEqual(window.file_panels['fill_raw'].action_table.rowCount(),1)
        self.assertFalse(any('查看清单' in b.text() for b in window.findChildren(QPushButton) if b.isVisible()))

    def test_export_check_and_execute_stay_inline_and_follow_selected_set(self):
        window=self.window;cover=next(a for a in self.store.assets('A') if a['role']=='finished')
        first=window.collections.create('A');window.collections.add(first,cover['id'],slot=0);window.collections.add(first,self.ids['2'],slot=1)
        window.refresh_sets();window.workspaces.setCurrentIndex(2)
        with patch.object(PlanDialog,'exec',side_effect=AssertionError('unexpected modal')):
            window.export_set();self.wait(lambda:window.thread is None)
            self.assertEqual(window.export_panel.action_table.rowCount(),2)
            window.execute_inline_plan('export');self.wait(lambda:window.thread is None)
        plan=window.export_panel.record['plan']
        self.assertTrue(all((self.root/a['dst']).exists() for a in plan['actions']))
        second=window.collections.create('A');window.refresh_sets();window.set_picker.setCurrentIndex(window.set_picker.findData(second))
        self.assertTrue(window.export_panel.isHidden());self.assertIsNone(window.export_panel.record)

    def test_old_workspace_positions_migrate_to_named_keys(self):
        window=self.window;key=self.store.path.stem+'|A'
        window.settings['ui_positions']={key:dict(workspace=1,task='ordinary')};window.restore_work_position()
        self.assertEqual(window.workspaces.currentIndex(),2);window.remember_work_position()
        self.assertEqual(window.settings['ui_positions'][key]['workspace'],'sets')
        window.settings['ui_positions'][key]=dict(workspace=2,task='ordinary');window.restore_work_position()
        self.assertEqual(window.workspaces.currentIndex(),1)

    def test_inline_export_rejects_collection_changed_since_check(self):
        window=self.window;cover=next(a for a in self.store.assets('A') if a['role']=='finished')
        set_id=window.collections.create('A');window.collections.add(set_id,cover['id'],slot=0);window.collections.add(set_id,self.ids['2'],slot=1)
        window.refresh_sets();window.export_set();self.wait(lambda:window.thread is None)
        plan=window.export_panel.record['plan'];window.collections.remove(set_id,1)
        with self.assertLogs(level='ERROR'):
            window.execute_inline_plan('export');self.wait(lambda:window.thread is None)
        self.assertEqual(window.export_panel.record['state'],'error')
        self.assertFalse(any((self.root/a['dst']).exists() for a in plan['actions']))

    def test_export_drawer_closes_and_stays_closed_after_updates_and_reopens(self):
        window=self.window;cover=next(a for a in self.store.assets('A') if a['role']=='finished')
        set_id=window.collections.create('A');window.collections.add(set_id,cover['id'],slot=0);window.refresh_sets();window.workspaces.setCurrentIndex(2);window.show()
        self.app.processEvents();window.export_set();self.wait(lambda:window.thread is None)
        record=window.export_panel.record;self.assertTrue(window.export_panel.isVisible())
        window.export_panel.close_button.click();window.sync_file_results();window.refresh_set_items();self.app.processEvents()
        self.assertTrue(window.export_panel.isHidden());self.assertIs(window.export_panel.record,record)
        window.workspaces.setCurrentIndex(0);window.workspaces.setCurrentIndex(2);window.sync_file_results();self.assertTrue(window.export_panel.isHidden())
        window.export_set();self.wait(lambda:window.thread is None);self.assertTrue(window.export_panel.isVisible())

    def test_export_drawer_does_not_reopen_when_pending_check_finishes(self):
        window=self.window;cover=next(a for a in self.store.assets('A') if a['role']=='finished')
        set_id=window.collections.create('A');window.collections.add(set_id,cover['id'],slot=0);window.refresh_sets();window.workspaces.setCurrentIndex(2)
        original=Operations.export_set
        def slow(service,sid):self.release.wait(4);return original(service,sid)
        with patch('boyo.operations.Operations.export_set',slow):
            window.export_set();window.close_export_panel();self.release.set();self.wait(lambda:window.thread is None)
        self.assertTrue(window.export_panel.isHidden());self.assertEqual(window.export_panel.record['state'],'ready')

    def test_similar_groups_are_a_visible_separate_list_and_keep_selection(self):
        window=self.window;second={'id':'second','keep':self.ids['4'],'candidates':[{'id':self.ids['5']}]}
        self.store.execute('INSERT INTO groups VALUES (?,?,?)',('second','A',json.dumps(second)))
        window.refresh();window.show();window.photo_navigation.setCurrentRow(1);self.app.processEvents()
        self.assertTrue(window.group_list.isVisible());self.assertIs(window.group_list.parentWidget(),window.native_review_splitter)
        window.group_list.setCurrentRow(1);self.assertEqual(window.current_group()['id'],'second')
        window.refresh();self.app.processEvents();self.assertEqual(window.current_group()['id'],'second')
        self.assertLessEqual(window.group_list.geometry().right(),window.review_stack.geometry().left())

    def test_photo_statistics_use_only_current_list_and_disjoint_states(self):
        window=self.window;shutil.copy2(self.root/'A/jpg/1.JPG',self.root/'A/精选/jpg/1.JPG')
        Scanner(self.store,self.root).scan(self.root/'A');window.refresh();window.decisions_changed({self.ids['2']:'delete',self.ids['3']:'keep'})
        self.assertIn('当前列表 5 张',window.photo_stats.text());self.assertIn('保护 1',window.photo_stats.text());self.assertIn('保留 1',window.photo_stats.text());self.assertIn('待删 1',window.photo_stats.text());self.assertIn('未决定 2',window.photo_stats.text())
        window.photo_navigation.setCurrentRow(1)
        self.assertIn('当前组 3 张',window.photo_stats.text());self.assertIn('第 1/1 组',window.photo_stats.text());self.assertIn('保护 1',window.photo_stats.text());self.assertIn('未决定 0',window.photo_stats.text())
        window.photo_navigation.setCurrentRow(3);self.assertIn('本轮复核 1 张',window.photo_stats.text())
        window.decisions_changed({self.ids['2']:'keep'});self.assertIn('本轮复核 1 张',window.photo_stats.text());self.assertIn('待删 0',window.photo_stats.text())

    def test_function_introductions_do_not_take_up_layout_space(self):
        window=self.window;window.show();self.app.processEvents()
        for workspace in range(3):
            window.workspaces.setCurrentIndex(workspace);self.app.processEvents()
            self.assertFalse(any(label.isVisible() and label.objectName()=='description' for label in window.findChildren(QLabel)))

    def test_raw_check_runs_without_locking_review(self):
        def check(album):
            self.release.wait(4);return {'actions':[],'warnings':[],'album':album}
        with patch('boyo.operations.Operations.fill_selected_raws',side_effect=check):
            self.window.make_plan('fill_raw');self.assertTrue(self.window.tabs.isEnabled())
            self.window.apply_decisions({self.ids['2']:'keep'},'manual');self.release.set()
            self.wait(lambda:self.window.thread is None)
        self.assertEqual(self.store.asset(self.ids['2'])['decision'],'keep')

    def test_file_callback_error_stays_inline_without_a_blocking_dialog(self):
        window=self.window;context=window.current_context();key=window.file_result_key('fill_raw',context);context['operation_key']=key
        def broken_result(result):raise ValueError('结果展示失败')
        with patch('boyo.gui.QMessageBox.warning',side_effect=AssertionError('unexpected blocking dialog')):
            window.run_task(lambda progress,control:{},broken_result,kind='检查精选 RAW',context=context)
            self.wait(lambda:window.thread is None)
        self.assertEqual(window.file_panels['fill_raw'].record['state'],'error');self.assertIn('结果展示失败',window.file_panels['fill_raw'].summary.text())

    def test_read_completion_adds_and_removes_photos_without_manual_refresh(self):
        window=self.window;window.show();self.app.processEvents();window.gallery.setCurrentItem(next(window.gallery.item(i) for i in range(window.gallery.count()) if window.gallery.item(i).data(Qt.ItemDataRole.UserRole)==self.ids['2']))
        def scan(progress,control):
            Image.new('RGB',(100,70),'purple').save(self.root/'A/jpg/6.JPG');(self.root/'A/jpg/5.JPG').unlink()
            return Scanner(self.store,self.root).scan(self.root/'A')
        window.run_task(scan,kind='扫描并分析');self.wait(lambda:window.thread is None);self.app.processEvents()
        visible={window.gallery.item(i).data(Qt.ItemDataRole.UserRole) for i in range(window.gallery.count())}
        self.assertEqual(len(visible),5);self.assertNotIn(self.ids['5'],visible)
        self.assertEqual(window.gallery.currentItem().data(Qt.ItemDataRole.UserRole),self.ids['2'])
        self.assertFalse(window.new_results_button.isVisible());self.assertFalse(window.deferred_results)

    def test_regroup_tracks_current_photo_when_group_identity_changes(self):
        window=self.window;window.photo_navigation.setCurrentRow(1);window.compare.setCurrentItem(window.review_items[self.ids['3']])
        changed=dict(id='regrouped',keep=self.ids['3'],candidates=[dict(id=self.ids['4'])])
        def regroup(progress,control):
            self.store.execute('DELETE FROM groups WHERE album=?',('A',));self.store.execute('INSERT INTO groups VALUES (?,?,?)',('regrouped','A',json.dumps(changed)));return {}
        window.run_task(regroup,kind='重新分析');self.wait(lambda:window.thread is None);self.app.processEvents()
        self.assertEqual(window.current_group()['id'],'regrouped');self.assertEqual(window.group_list.currentRow(),0)
        self.assertEqual(window.compare.currentItem().data(Qt.ItemDataRole.UserRole),self.ids['3'])
        self.assertEqual(window.photo_title.text(),'相似复核')

    def test_live_viewer_receives_new_group_and_can_edit_added_photos(self):
        window=self.window;window.photo_navigation.setCurrentRow(1)
        dialog=PhotoPreviewDialog(self.store,self.runtime,self.root,[self.ids['1'],self.ids['2'],self.ids['3']],self.ids['2'],parent=window);dialog.review_scope='similar';self.dialogs.append(dialog);dialog.show();self.app.processEvents()
        changed=dict(id='new',keep=self.ids['2'],candidates=[dict(id=self.ids['4'])])
        self.store.execute('DELETE FROM groups WHERE album=?',('A',));self.store.execute('INSERT INTO groups VALUES (?,?,?)',('new','A',json.dumps(changed)));window.refresh()
        self.assertEqual(dialog.current_id,self.ids['2']);self.assertEqual(dialog.asset_ids,[self.ids['2'],self.ids['4']])
        dialog.navigate(1);dialog.decide('keep');self.assertEqual(self.store.asset(self.ids['4'])['decision'],'keep')
        (self.root/'A/jpg/4.JPG').unlink();Scanner(self.store,self.root).scan(self.root/'A');window.refresh()
        self.assertNotIn(self.ids['4'],dialog.asset_ids);self.assertEqual(len(window.group_data),0)

    def test_read_refresh_retains_pending_round_rescues(self):
        window=self.window;window.apply_decisions({self.ids['2']:'delete',self.ids['3']:'delete'},'manual');window.photo_navigation.setCurrentRow(3)
        window.apply_decisions({self.ids['2']:'keep'},'manual');window.run_task(lambda progress,control:{},kind='重新分析');self.wait(lambda:window.thread is None)
        self.assertEqual(window.gallery.count(),2);self.assertIn('已救回',window.gallery.item(0).text());self.assertIn('待删 1 张',window.summary.text())

    def test_file_completion_refreshes_missing_collection_member(self):
        window=self.window;cover=next(a for a in self.store.assets('A') if a['role']=='finished')
        set_id=window.collections.create('A');window.collections.add(set_id,cover['id'],slot=0);window.collections.add(set_id,self.ids['2'],slot=1)
        window.refresh_sets();window.apply_decisions({self.ids['2']:'delete'},'manual');window.execute_delete();self.wait(lambda:window.thread is None)
        self.assertNotIn(self.ids['2'],window.asset_by_id);self.assertIn('已移出 1 张',window.set_summary.text())
        self.assertEqual(window.native_set_members[1]['present'],0)

    def test_native_refresh_does_not_start_analysis_or_touch_source_photos(self):
        hashes={a['path']:sha256(self.root/a['path']) for a in self.store.assets('A')}
        with patch('boyo.analysis.analyze',side_effect=AssertionError('refresh is display only')),patch('boyo.gui.Scanner',side_effect=AssertionError('refresh must not scan')):
            self.window.refresh();self.window.refresh()
        self.assertTrue(self.window.windowTitle().startswith('Boyo'))
        self.assertFalse(self.window.windowIcon().isNull())
        self.assertEqual({path:sha256(self.root/path) for path in hashes},hashes)
        self.assertFalse(any('WebEngine' in widget.metaObject().className() for widget in self.window.findChildren(QWidget)))

    def test_secondary_adoption_can_reconsider_old_keeps_but_respects_new_confirmations_and_protection(self):
        from boyo.screening import SECONDARY
        self.store.decide([self.ids['2'],self.ids['3']],'keep','manual')
        ids=list(self.ids.values());group={**self.group,'members':ids,
            'candidates':[{'id':self.ids[n],'phash_distance':2,'dhash_distance':2} for n in ('2','3','4','5')],
            'matches':{str(aid):[self.ids['1']] for aid in ids},'screening_mode':SECONDARY,
            'review_stamps':self.store.review_stamps('A')}
        self.store.execute('UPDATE groups SET data=? WHERE id=?',(json.dumps(group),'original'))
        # Reaffirming an unchanged keep must count as a fresh review, without
        # inventing another learning sample. A newly selected image is protected.
        events=len(self.store.rows('SELECT * FROM feedback_events'))
        self.store.decide([self.ids['3']],'keep','manual')
        self.assertEqual(len(self.store.rows('SELECT * FROM feedback_events')),events)
        shutil.copy2(self.root/'A/jpg/5.JPG',self.root/'A/精选/jpg/5.JPG')
        Scanner(self.store,self.root).scan(self.root/'A');self.window.refresh();self.window.photo_navigation.setCurrentRow(1)
        self.window.accept_group()
        self.assertEqual(self.store.asset(self.ids['2'])['decision'],'delete')
        self.assertEqual(self.store.asset(self.ids['4'])['decision'],'delete')
        self.assertEqual(self.store.asset(self.ids['3'])['decision'],'keep')
        self.assertTrue(self.store.asset(self.ids['5'])['protected'])
        self.assertNotEqual(self.store.asset(self.ids['5'])['decision'],'delete')
        self.assertEqual(len(list((self.root/'A/jpg').glob('*.JPG'))),5)

    def test_secondary_entry_finishes_refreshes_both_views_and_does_not_change_normal_settings(self):
        from boyo.screening import SECONDARY
        settings=dict(self.window.settings)
        errors=[]
        with patch.object(self.window,'show_error',side_effect=errors.append):
            self.window.start_secondary_screening()
            self.assertTrue(self.window.scan_button.isEnabled());self.assertTrue(self.window.album_picker.isEnabled())
            self.wait(lambda:self.window.thread is None,timeout=30)
        self.assertEqual(errors,[])
        result=json.loads(self.store.one('SELECT summary FROM analysis_runs WHERE album=?',('A',))['summary'])
        self.assertEqual(result['screening_mode'],SECONDARY)
        self.assertEqual(self.window.quality_screening_mode,SECONDARY)
        self.assertEqual(self.window.settings.get('strength'),settings.get('strength'))
        self.assertEqual(self.window.settings.get('quality_sensitivity'),settings.get('quality_sensitivity'))
        self.assertNotIn('screening_mode',self.window.settings)
        self.assertIn('二次筛查',self.window.photo_title.text())
        self.assertIn('高强度二次筛查完成',self.window.status.text())

    def test_secondary_adoption_rejects_a_reference_changed_on_disk_without_a_rescan(self):
        from boyo.screening import SECONDARY
        group={**self.group,'screening_mode':SECONDARY,'review_stamps':self.store.review_stamps('A')}
        self.store.execute('UPDATE groups SET data=? WHERE id=?',(json.dumps(group),'original'))
        Image.new('RGB',(150,90),'orange').save(self.root/'A/jpg/1.JPG')
        self.window.refresh();self.window.accept_group()
        self.assertTrue(all(self.store.asset(self.ids[n])['decision']=='pending' for n in ('2','3')))

    def test_secondary_group_with_all_keeps_has_enabled_adoption_and_reaffirmation_still_wins(self):
        from boyo.screening import SECONDARY
        self.store.decide([self.ids[n] for n in ('1','2','3')],'keep','manual')
        group={**self.group,'screening_mode':SECONDARY,'review_stamps':self.store.review_stamps('A')}
        self.store.execute('UPDATE groups SET data=? WHERE id=?',(json.dumps(group),'original'))
        self.window.refresh();self.window.photo_navigation.setCurrentRow(1)
        self.assertTrue(self.window.adopt_button.isEnabled())
        self.window.review_photo_decide(self.ids['2'],'keep')
        self.window.adopt_button.click()
        self.assertEqual(self.store.asset(self.ids['2'])['decision'],'keep')
        self.assertEqual(self.store.asset(self.ids['3'])['decision'],'delete')
        self.assertFalse(self.window.adopt_button.isEnabled())

    def test_secondary_completion_does_not_take_focus_from_another_workspace(self):
        errors=[]
        def work(*args):
            self.blocking(args[-2],args[-1])
            return {'cancelled':False,'groups':0,'candidates':0,'quality_candidates':0}
        with patch('boyo.analysis.analyze',side_effect=work),patch.object(self.window,'show_error',side_effect=errors.append):
            self.window.start_secondary_screening()
            self.window.workspaces.setCurrentIndex(2)
            self.release.set();self.wait(lambda:self.window.thread is None)
        self.assertEqual(errors,[])
        self.assertEqual(self.window.workspaces.currentIndex(),2)

    def test_native_cards_are_uniform_and_sidebar_has_no_extra_allocation(self):
        window=self.window;Image.new('RGB',(60,160),'purple').save(self.root/'A/jpg/3.JPG');Scanner(self.store,self.root).scan(self.root/'A');window.refresh()
        window.resize(1000,760);window.show();self.app.processEvents();window.load_visible_thumbnails();self.app.processEvents()
        self.assertEqual(window.native_content.x(),window.native_sidebar.width())
        sizes={(window.gallery.visualItemRect(window.gallery.item(i)).width(),window.gallery.visualItemRect(window.gallery.item(i)).height()) for i in range(window.gallery.count())}
        self.assertEqual(len(sizes),1);self.assertGreater(next(iter(sizes))[0],150)

    def test_review_footer_group_actions_keep_protection_and_only_mark_decisions(self):
        window=self.window;window.show();window.photo_navigation.setCurrentRow(1);self.app.processEvents()
        self.assertEqual([b.text() for b in window.native_review_footer.findChildren(QPushButton) if b.isVisible()],['选中保留','选中待删','采纳本组有效建议','整组保留','整组待删'])
        window.adopt_button.click();self.assertEqual(self.store.asset(self.ids['1'])['decision'],'keep');self.assertEqual(self.store.asset(self.ids['2'])['decision'],'delete')
        window.group_keep_button.click();self.assertTrue(all(self.store.asset(self.ids[n])['decision']=='keep' for n in ('1','2','3')))
        shutil.copy2(self.root/'A/jpg/2.JPG',self.root/'A/精选/jpg/2.JPG');Scanner(self.store,self.root).scan(self.root/'A');window.refresh()
        current_group=window.current_group()['id'];window.group_delete_button.click()
        self.assertEqual(self.store.asset(self.ids['2'])['decision'],'keep');self.assertTrue(self.store.asset(self.ids['2'])['protected'])
        self.assertEqual([self.store.asset(self.ids[n])['decision'] for n in ('1','3')],['delete','delete'])
        self.assertEqual(self.store.asset(self.ids['4'])['decision'],'pending');self.assertEqual(window.current_group()['id'],current_group)
        self.assertTrue(all((self.root/f'A/jpg/{n}.JPG').exists() for n in range(1,6)));self.assertFalse((self.root/'已删除备份').exists())

    def test_operating_instructions_are_not_visible_in_footers_or_viewer(self):
        window=self.window;window.show();self.app.processEvents()
        for workspace in range(3):
            window.workspaces.setCurrentIndex(workspace)
            if workspace==0:window.photo_navigation.setCurrentRow(1)
            if workspace==1:window.file_navigation.setCurrentRow(2)
            self.app.processEvents()
            visible=[label.text() for label in window.findChildren(QLabel) if label.isVisible()]
            self.assertFalse(any(word in text for text in visible for word in ('原图留在本机','双击大图','← → 切图','移动项目可恢复')))
        dialog=PhotoPreviewDialog(self.store,self.runtime,self.root,[self.ids['1']],self.ids['1'],parent=window);self.dialogs.append(dialog);dialog.show();self.app.processEvents()
        self.assertFalse(any('滚轮缩放' in label.text() or '← → 切图' in label.text() for label in dialog.findChildren(QLabel) if label.isVisible()))

    def test_zoom_fixes_all_card_bounds_and_newly_visible_cards_use_current_size(self):
        window=self.window
        for index in range(6,65):Image.new('RGB',(60,90) if index%2 else (180,45),(index*3%255,50,80)).save(self.root/'A/jpg'/f'{index:03d}.JPG')
        Image.new('RGB',(35,400),'purple').save(self.root/'A/jpg/3.JPG');Image.new('RGB',(400,35),'cyan').save(self.root/'A/jpg/4.JPG')
        Image.new('RGB',(100,90),'blue').save(self.root/'A/jpg'/('很长的照片文件名'*5+'.JPG'))
        Scanner(self.store,self.root).scan(self.root/'A');ids=[a['id'] for a in self.store.assets('A') if a['role']=='ordinary_jpeg']
        group={**self.group,'members':ids,'candidates':[{'id':aid} for aid in ids if aid!=self.group['keep']]}
        self.store.execute('UPDATE groups SET data=? WHERE id=?',(json.dumps(group),'original'));window.refresh();window.show();window.resize(1000,760);window.photo_navigation.setCurrentRow(1);self.app.processEvents()
        for zoom in (170,350,190,320,230):
            window.thumbnail_slider.setValue(zoom);QTest.qWait(80);window.load_visible_thumbnails();self.app.processEvents()
            grid=window.compare;size=grid.gridSize()
            self.assertEqual(size.width(),zoom+16);self.assertTrue(grid.uniformItemSizes());self.assertFalse(grid.wordWrap())
            self.assertEqual({(grid.visualItemRect(grid.item(i)).width(),grid.visualItemRect(grid.item(i)).height()) for i in range(grid.count())},{(size.width(),size.height())})
            for card in window.review_cards.values():
                self.assertEqual(card.size(),QSize(zoom,size.height()-16));self.assertLessEqual(card.hint_label.mapTo(card,card.hint_label.rect().bottomRight()).y(),card.height()-6)
            grid.scrollToBottom();self.app.processEvents();window.load_visible_thumbnails();self.app.processEvents()
            self.assertTrue(window.review_cards);self.assertEqual({card.width() for card in window.review_cards.values()},{zoom})
            for row in visible_rows(grid,False):
                rect=grid.visualItemRect(grid.item(row));self.assertLessEqual(rect.right(),grid.viewport().width())
        window.photo_navigation.setCurrentRow(0);self.app.processEvents();window.load_visible_thumbnails()
        self.assertEqual(window.gallery.gridSize(),window.compare.gridSize())
        old_size=window.gallery.gridSize();window.resize(1500,900);self.app.processEvents();window.load_visible_thumbnails()
        self.assertEqual(window.gallery.gridSize(),old_size)

    def test_quality_command_selects_matching_navigation_and_header(self):
        with patch.object(self.window,'run_task'):
            self.window.workspaces.setCurrentIndex(1);self.window.start_quality()
        self.assertEqual(self.window.workspaces.currentIndex(),0);self.assertEqual(self.window.last_photo_task,'quality')
        self.assertEqual(self.window.photo_title.text(),'废片筛查');self.assertEqual(self.window.tabs.currentIndex(),5)

    def test_auto_refresh_preserves_an_unselected_current_photo(self):
        window=self.window;window.gallery.setCurrentRow(1);current=window.gallery.currentItem().data(Qt.ItemDataRole.UserRole);window.gallery.clearSelection()
        window.run_task(lambda progress,control:{},kind='重新分析');self.wait(lambda:window.thread is None);self.app.processEvents()
        self.assertEqual(window.gallery.currentItem().data(Qt.ItemDataRole.UserRole),current);self.assertEqual(window.gallery.selectedItems(),[])

    def test_native_set_card_remove_is_one_click_and_keeps_original(self):
        window=self.window;cover=next(a for a in self.store.assets('A') if a['role']=='finished');set_id=window.collections.create('A')
        window.collections.add(set_id,cover['id'],slot=0);window.collections.add(set_id,self.ids['2'],slot=1);window.refresh_sets();window.workspaces.setCurrentIndex(2);window.show();self.app.processEvents();window.load_visible_thumbnails();self.app.processEvents()
        item=window.set_grid.item(1);rect=window.set_grid.visualItemRect(item);card_width=rect.width()-16;top=rect.y()+13+(card_width-14)*2/3+7
        QTest.mouseClick(window.set_grid.viewport(),Qt.MouseButton.LeftButton,pos=QPoint(rect.right()-26,round(top+10)))
        self.assertEqual([a['slot'] for a in window.collections.items(set_id)],[0]);self.assertTrue((self.root/'A/jpg/2.JPG').exists())

    def test_grid_return_tracks_current_photo_and_text_does_not_trigger_actions(self):
        window=self.window;window.gallery.setCurrentRow(0);aid=self.ids['4']
        with patch('boyo.gui.PhotoPreviewDialog') as preview:
            preview.return_value.current_id=aid;window.expand_gallery(window.gallery.item(0))
        self.assertEqual(window.gallery.currentItem().data(Qt.ItemDataRole.UserRole),aid)
        edit=QLineEdit(window);edit.show();window.show();edit.setFocus();self.app.processEvents()
        called=[];window.shortcut_action(lambda:called.append(True));self.assertEqual(called,[])


if __name__=='__main__':unittest.main()
