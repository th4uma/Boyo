"""Isolated low-priority learner; no GUI, photo moves, or network requests."""
import argparse
import json
import os
import sys
import time
import threading
from pathlib import Path


class Budget:
    """Blocking command reader, not a polling loop; pause time consumes no budget."""
    def __init__(self,seconds=150,stream=None):
        self.deadline=time.monotonic()+seconds
        self.ready=threading.Event(); self.ready.set()
        self.paused_at=None; self.cancelled=False; self.lock=threading.Lock()
        self.reader=threading.Thread(target=self.read_commands,args=(stream or sys.stdin,),daemon=True)
        self.reader.start()
        self.trace('learner ready')

    def trace(self,label):
        if os.environ.get('BOYO_LEARNING_DEBUG')=='1':
            try: os.write(1,f'{time.monotonic():.1f} {label}\n'.encode())
            except OSError: pass

    def command(self,value):
        with self.lock:
            if value=='pause' and self.ready.is_set():
                self.paused_at=time.monotonic(); self.ready.clear()
                self.trace('paused')
            elif value=='resume' and not self.ready.is_set():
                self.deadline+=time.monotonic()-self.paused_at
                self.paused_at=None; self.ready.set()
                self.trace('resumed')

    def read_commands(self,stream):
        if stream is None: return
        try:
            if os.name=='nt':
                # CRT _read holds stdin's descriptor lock while waiting. Library
                # imports that query isatty(0) would then deadlock. Read the pipe
                # handle directly so no Python/CRT stdio lock is held.
                import ctypes,msvcrt
                handle=msvcrt.get_osfhandle(stream.fileno())
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.ReadFile.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,
                                          ctypes.POINTER(ctypes.c_uint),ctypes.c_void_p]
                buffer=ctypes.create_string_buffer(128); received=ctypes.c_uint()
                def read():
                    ok=kernel.ReadFile(handle,buffer,128,ctypes.byref(received),None)
                    return buffer.raw[:received.value] if ok else b''
            else:
                def read(): return os.read(stream.fileno(),128)
            pending=b''
            while True:
                block=read()
                if not block: break
                pending+=block
                while b'\n' in pending:
                    line,pending=pending.split(b'\n',1)
                    self.command(line.decode('ascii',errors='ignore').strip())
                if len(pending)>128: pending=b''
        finally:
            # Parent exited: release a blocked checkpoint and stop the job.
            self.cancelled=True; self.ready.set()

    def checkpoint(self):
        if not self.ready.is_set(): self.trace('waiting for idle')
        self.ready.wait()
        return not self.cancelled and time.monotonic()<self.deadline


def main():
    parser=argparse.ArgumentParser()
    for name in ('database','home','root','result'): parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--force',action='store_true')
    args=parser.parse_args()
    # Set limits before importing numpy/sklearn/torch.
    for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[name]='1'
    os.environ['HF_HUB_OFFLINE']='1'
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    sys.pycache_prefix=os.environ.get('PYTHONPYCACHEPREFIX',str(args.home/'cache'/'pycache'))
    if os.name=='nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetCurrentProcess.restype=ctypes.c_void_p
        kernel.SetPriorityClass.argtypes=[ctypes.c_void_p,ctypes.c_uint]
        handle=kernel.GetCurrentProcess()
        kernel.SetPriorityClass(handle,0x4000)
    else:
        try: os.nice(10)
        except OSError: pass
    from .config import Runtime
    from .store import Store
    from .preferences import train
    from .settings_io import atomic_json
    try:
        result=train(Store(args.database,regenerable=True),Runtime(args.home),args.root,Budget(),force=args.force)
        # Coefficients stay in the model file; only compact status crosses processes.
        result={k:v for k,v in result.items() if k not in {'rank','retention','quality'}}
    except Exception as exc:
        import traceback
        log=args.home/'logs'/'learning-error.log'; log.parent.mkdir(parents=True,exist_ok=True)
        log.write_text(traceback.format_exc(),encoding='utf-8')
        result={'error':str(exc),'message':'后台学习本轮未完成，筛图可继续；错误已记录。'}
    atomic_json(args.result,result)


if __name__=='__main__': main()
