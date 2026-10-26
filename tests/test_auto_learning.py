import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QObject,QProcess
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from boyo.auto_learning import AutoLearning
from boyo.config import Runtime
from boyo.store import Store


class AutomaticLearningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='boyo-auto-')
        self.owner=QObject(); self.owner.root=Path(self.tmp.name)
        self.owner.runtime=Runtime(self.owner.root/'local'); self.owner.runtime.prepare()
        self.owner.store=Store(self.owner.runtime.db_path)
        self.learner=AutoLearning(self.owner); self.learner.timer.stop()

    def tearDown(self):
        self.learner.close(); self.owner.deleteLater(); self.app.processEvents(); self.tmp.cleanup()

    def test_actions_are_coalesced_and_bulk_adoption_does_not_schedule_training(self):
        self.learner.dirty=False
        self.learner.changed(100,'adopt')
        self.assertFalse(self.learner.timer.isActive())
        with patch.object(self.learner,'maybe_start') as start:
            for _ in range(100): self.learner.changed()
            start.assert_not_called()
        self.assertTrue(self.learner.timer.isSingleShot()); self.assertTrue(self.learner.dirty)
        self.assertIsNone(self.learner.process)

    def test_few_actions_do_not_even_launch_a_child_and_idle_has_no_poll(self):
        with patch('boyo.auto_learning.QProcess') as child:
            self.learner.maybe_start()
            child.assert_not_called()
        self.assertFalse(self.learner.dirty); self.assertFalse(self.learner.timer.isActive())

    def test_worker_is_separate_and_result_does_not_change_photo_state(self):
        messages=[]; self.learner.updated.connect(messages.append)
        with patch('boyo.preferences.train',side_effect=AssertionError('training in GUI process')):
            self.learner.maybe_start(force=True)
            self.assertIsNotNone(self.learner.process)
            started=self.learner.process
            self.assertEqual(started.program(),sys.executable)
            self.assertEqual(started.processEnvironment().value('OMP_NUM_THREADS'),'1')
            deadline=time.monotonic()+30
            ticks=0
            while self.learner.process is not None and time.monotonic()<deadline:
                QTest.qWait(10); ticks+=1
            self.assertIsNone(self.learner.process)
            self.assertGreater(ticks,0); self.assertTrue(any('已记录' in m for m in messages))
        self.assertEqual(self.owner.store.rows('SELECT * FROM assets'),[])

    def test_rate_limit_requeues_once_and_foreground_cancels_the_wait(self):
        self.learner.last_started=time.monotonic(); self.learner.maybe_start()
        self.assertTrue(self.learner.timer.isActive()); self.assertGreater(self.learner.timer.remainingTime(),100000)
        self.learner.suspend(); self.assertFalse(self.learner.timer.isActive())
        self.learner.resume(); self.assertTrue(self.learner.timer.isActive())

    def test_foreground_and_close_stop_only_the_managed_learning_process(self):
        process=QProcess(self.learner)
        process.start(sys.executable,['-c','import time; time.sleep(10)'])
        self.assertTrue(process.waitForStarted(3000)); self.learner.process=process
        process.finished.connect(lambda code,status:self.learner.finished(process,self.owner.runtime.home/'tmp/test.json',code))
        self.learner.suspend(); self.assertTrue(process.waitForFinished(3000))
        self.assertTrue(self.learner.blocked); self.assertIsNone(self.learner.process)

    def test_new_analysis_reconsiders_previously_unavailable_features_without_extra_user_actions(self):
        store=self.owner.store
        aid=store.execute('INSERT INTO assets(album,path,stem,role,logical_key,size,mtime_ns) VALUES (?,?,?,?,?,?,?)',
                          ('A','A/jpg/1.jpg','1','ordinary_jpeg','A|1',1,1))
        store.decide([aid],'keep','manual')
        self.learner.dirty=False; self.learner.data_ready()
        self.learner.maybe_start()
        self.assertIsNotNone(self.learner.process)
        self.assertIn('--force',self.learner.process.arguments())
        self.learner.close()

    def test_marking_pauses_once_and_idle_resumes_the_same_process(self):
        with patch('boyo.auto_learning.QProcess') as cls:
            process=cls.return_value; self.learner.process=process
            self.learner.changed(); self.learner.changed(); self.learner.changed(20,'adopt')
            process.write.assert_called_once_with(b'pause\n')
            self.assertTrue(self.learner.paused); self.assertFalse(self.learner.deadline.isActive())
            self.learner.maybe_start()
            self.assertEqual(process.write.call_args.args,(b'resume\n',))
            self.assertIs(self.learner.process,process); self.assertFalse(self.learner.paused)
            self.learner.process=None; self.learner.deadline.stop()

    def test_activity_changes_are_event_driven_and_do_not_flood_while_marking(self):
        changes=[];self.learner.activityChanged.connect(lambda:changes.append(self.learner.activity()['code']))
        self.learner.dirty=False
        for _ in range(100):self.learner.changed()
        self.assertEqual(changes,['waiting']);self.assertTrue(self.learner.timer.isSingleShot())
        with patch('boyo.auto_learning.QProcess') as cls:
            self.learner.process=cls.return_value;self.learner.paused=False;self.learner.notify_activity()
            self.learner.changed();self.learner.changed();self.assertEqual(self.learner.activity()['code'],'paused')
            self.learner.maybe_start();self.assertEqual(self.learner.activity()['code'],'working')
            self.learner.process=None;self.learner.deadline.stop()
        self.learner.suspend();self.assertEqual(self.learner.activity()['code'],'blocked')
        self.learner.resume();self.assertEqual(self.learner.activity()['code'],'waiting')

    def test_foreground_interruption_is_not_misreported_as_training_failure(self):
        with patch('boyo.auto_learning.QProcess') as cls:
            process=cls.return_value;self.learner.process=process
            self.learner.suspend();self.learner.resume()
            self.learner.finished(process,self.owner.runtime.home/'tmp/interrupted.json',1)
        self.assertEqual(self.learner.last_error,'');self.assertEqual(self.learner.activity()['code'],'waiting')

    def test_pause_protocol_works_with_windows_gui_python_and_does_not_spin(self):
        from PySide6.QtCore import QProcessEnvironment
        process=QProcess(self.owner); env=QProcessEnvironment.systemEnvironment()
        env.insert('PYTHONPATH',str(Path(__file__).resolve().parents[1]/'src')); process.setProcessEnvironment(env)
        executable=Path(sys.executable).with_name('pythonw.exe')
        if not executable.exists(): executable=Path(sys.executable)
        result=self.owner.runtime.home/'tmp/protocol.json'
        # A background CRT stdin read used to deadlock libraries querying isatty.
        code="import time,sys; from pathlib import Path; from boyo.learning_worker import Budget; b=Budget(2); time.sleep(.3); sys.stdin.isatty(); Path(sys.argv[1]).write_text(str(b.checkpoint()))"
        process.start(str(executable),['-c',code,str(result)])
        try:
            self.assertTrue(process.waitForStarted(3000)); process.write(b'pause\n')
            QTest.qWait(800); self.assertFalse(result.exists())
            process.write(b'resume\n')
            deadline=time.monotonic()+10
            while process.state()!=QProcess.ProcessState.NotRunning and time.monotonic()<deadline: QTest.qWait(10)
            self.assertEqual(process.exitCode(),0)
            self.assertEqual(result.read_text(),'True')
        finally:
            if process.state()!=QProcess.ProcessState.NotRunning: process.kill(); process.waitForFinished(1000)


if __name__=='__main__': unittest.main()
