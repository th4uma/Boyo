"""One GUI per local runtime. Duplicate launches activate the existing window."""
import hashlib

from PySide6.QtCore import QObject, QLockFile, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance(QObject):
    activationRequested = Signal()

    def __init__(self, runtime):
        super().__init__()
        key = hashlib.sha256(str(runtime.home.resolve()).casefold().encode()).hexdigest()[:24]
        self.name = "boyo-" + key
        self.previous_name = "liuji-" + key  # Activate an instance opened before the upgrade.
        self.lock = QLockFile(str(runtime.home / "tmp" / "gui.lock"))
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self.activate)
        self.acquired = False

    def claim(self):
        if not self.lock.tryLock(0):
            if self.lock.error() != QLockFile.LockError.LockFailedError:
                raise OSError("无法创建本机启动锁，请检查 local/tmp 的访问权限：" + str(self.lock.error()))
            for name in (self.name, self.previous_name):
                socket = QLocalSocket()
                socket.connectToServer(name)
                if socket.waitForConnected(150):
                    socket.write(b"activate\n"); socket.waitForBytesWritten(100)
                    socket.disconnectFromServer()
                    break
            return False
        self.acquired = True
        if not self.server.listen(self.name):
            QLocalServer.removeServer(self.name)
            if not self.server.listen(self.name):
                self.close(); raise OSError("无法建立本机启动通信：" + self.server.errorString())
        return True

    def activate(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.close(); socket.deleteLater()
            self.activationRequested.emit()

    def close(self):
        if self.acquired:
            self.server.close(); self.lock.unlock(); self.acquired = False
