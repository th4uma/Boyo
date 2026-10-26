"""Lightweight task controls. Importing the GUI never imports analysis libraries."""
import os
import threading
import time


class Control:
    def __init__(self):
        self.cancel = threading.Event(); self.pause = threading.Event()

    def checkpoint(self):
        while self.pause.is_set():
            if self.cancel.wait(0.1): return False
        return not self.cancel.is_set()


def worker_count(settings):
    requested = int(settings.get("analysis_workers", 0))
    return min(8, max(1, requested)) if requested else max(1, min(4, (os.cpu_count() or 2)//2))


class ProgressReporter:
    def __init__(self, callback, interval=0.05):
        self.callback, self.interval = callback, interval
        self.last = 0.0

    def __call__(self, current, total, label):
        now = time.monotonic()
        if now-self.last >= self.interval or (total > 0 and current == total):
            self.last = now; self.callback(current, total, label)
