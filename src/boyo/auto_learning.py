"""Event-driven batching; this module never imports a training/vision library."""
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject,QProcess,QProcessEnvironment,QTimer,Signal


class AutoLearning(QObject):
    updated=Signal(str)
    activityChanged=Signal()
    IDLE_MS=12000
    MIN_INTERVAL=120.

    def __init__(self,owner):
        super().__init__(owner)
        self.owner=owner; self.process=None; self.result_path=None
        self.blocked=False; self.closed=False; self.dirty=True; self.last_started=-self.MIN_INTERVAL
        self.reconsider=False;self.last_error='';self.last_notice='';self._activity_signature=None;self.stop_for_foreground=False
        self.paused=False
        self.timer=QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.maybe_start)
        self.deadline=QTimer(self); self.deadline.setSingleShot(True); self.deadline.setInterval(180000)
        self.deadline.timeout.connect(self.abort)
        owner.destroyed.connect(self.close)
        self.timer.start(15000)

    def activity(self):
        if self.closed:code,label,message='closed','已停止','关闭程序后停止后台学习。'
        elif self.blocked:code,label,message='blocked','任务优先','扫描、分析或文件整理优先，后台学习等待。'
        elif self.process is not None:
            code,label,message=('paused','已暂停','正在筛图，学习已暂停；空闲后继续同一个进程。') if self.paused else ('working','后台验证中','在独立低优先级进程中验证，筛图可继续。')
        elif self.last_error:code,label,message='error','本轮未完成',self.last_error
        elif self.dirty and self.timer.isActive():code,label,message='waiting','等待空闲','积累选择后等待空闲，并遵守两次启动至少间隔两分钟的限制。'
        else:code,label,message='collecting','积累中','正常保留、待删和撤销会自动记录；不会持续占用 CPU 训练。'
        return dict(code=code,label=label,message=message)

    def notify_activity(self):
        signature=(self.closed,self.blocked,self.paused,self.process is not None,self.dirty,self.timer.isActive(),self.last_error)
        if signature!=self._activity_signature:self._activity_signature=signature;self.activityChanged.emit()

    def changed(self,count=1,source='manual'):
        if self.closed or not count: return
        if source=='manual': self.dirty=True
        if self.process is not None and not self.paused:
            self.process.write(b'pause\n'); self.paused=True; self.deadline.stop()
        if not self.blocked and (source=='manual' or self.paused): self.timer.start(self.IDLE_MS)
        self.notify_activity()

    def maybe_start(self,force=False):
        if self.closed or self.blocked: return
        if self.process is not None:
            if self.paused:
                self.process.write(b'resume\n'); self.paused=False; self.deadline.start()
                self.notify_activity()
            return
        if not self.dirty and not force: return
        delay=self.MIN_INTERVAL-(time.monotonic()-self.last_started)
        if delay>0 and not force:
            self.timer.start(int(delay*1000)+1);self.notify_activity();return
        store=self.owner.store
        if not force:
            state=store.learning_state()
            count=store.one("SELECT COUNT(*) AS n FROM feedback_events WHERE source='manual' AND id>?",(state.get('revision',0),))['n']
            if count<12 and not self.reconsider:self.dirty=False;self.notify_activity();return
            if self.reconsider:
                self.reconsider=False
                if not store.one('SELECT 1 AS found FROM feedback LIMIT 1'):self.dirty=False;self.notify_activity();return
                force=True  # Feature availability changed; worker deduplicates its input signature.
        self.dirty=False; self.last_started=time.monotonic();self.last_error='';self.stop_for_foreground=False
        runtime=self.owner.runtime
        result=runtime.home/'tmp'/('learning-'+uuid.uuid4().hex+'.json')
        self.result_path=result
        process=QProcess(self); self.process=process; self.paused=False
        env=QProcessEnvironment.systemEnvironment()
        values={'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'BOYO_LOCAL':str(runtime.home),
                'TEMP':str(runtime.home/'tmp'),'TMP':str(runtime.home/'tmp'),
                'PYTHONPYCACHEPREFIX':str(sys.pycache_prefix or runtime.home/'cache'/'pycache'),
                'HF_HOME':str(runtime.home/'cache'/'huggingface'),'TORCH_HOME':str(runtime.home/'cache'/'torch'),
                'HF_HUB_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1','OMP_NUM_THREADS':'1',
                'OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','NUMEXPR_NUM_THREADS':'1'}
        for key,value in values.items(): env.insert(key,value)
        process.setProcessEnvironment(env)
        process.setProgram(sys.executable)
        arguments=['-m','boyo.learning_worker','--database',str(store.path),'--home',str(runtime.home),
                   '--root',str(self.owner.root),'--result',str(result)]
        if force: arguments.append('--force')
        process.setArguments(arguments)
        process.finished.connect(lambda code,status:self.finished(process,result,code))
        process.errorOccurred.connect(lambda error:self.failed_to_start(process,result,error))
        process.start(); self.deadline.start()
        self.updated.emit('后台正在批量验证个人习惯，筛图可继续。')
        self.notify_activity()

    def failed_to_start(self,process,result,error):
        if error==QProcess.ProcessError.FailedToStart and self.process is process:
            logging.error('学习进程启动失败：%s',process.errorString())
            self.finished(process,result,-1)

    def finished(self,process,result,code):
        if self.process is not process: return
        self.process=None; self.result_path=None; self.paused=False; self.deadline.stop()
        message=None
        try:
            if code==0 and result.exists():
                data=json.loads(result.read_text(encoding='utf-8')); message=data.get('message')
                if data.get('stale'): self.dirty=True
                if data.get('error'):self.last_error=message or str(data['error'])
            elif code!=0:
                logging.info('后台学习进程已停止：%s',code)
                if not self.blocked and not self.closed and not self.stop_for_foreground:self.last_error='本轮学习已停止，未启用本轮结果；后续正常筛图后会再次尝试。'
        except (OSError,ValueError):
            logging.exception('读取后台学习结果失败');self.last_error='本轮学习结果读取失败，未启用本轮结果。'
        result.unlink(missing_ok=True); process.deleteLater();self.stop_for_foreground=False
        if message:self.last_notice=message
        if message and not self.blocked and not self.closed: self.updated.emit(message)
        if self.dirty and not self.blocked and not self.closed: self.timer.start(self.IDLE_MS)
        self.notify_activity()

    def abort(self):
        if self.process is not None: self.process.kill()

    def suspend(self):
        self.blocked=True; self.timer.stop()
        if self.process is not None: self.dirty=True;self.stop_for_foreground=True;self.abort()
        self.notify_activity()

    def resume(self):
        self.blocked=False
        if self.dirty and not self.closed: self.timer.start(self.IDLE_MS)
        self.notify_activity()

    def data_ready(self):
        self.reconsider=True; self.dirty=True
        if not self.blocked and not self.closed: self.timer.start(self.IDLE_MS)
        self.notify_activity()

    def close(self):
        self.closed=True; self.timer.stop(); self.deadline.stop()
        if self.process is not None:
            process=self.process; process.kill(); process.waitForFinished(1000)
