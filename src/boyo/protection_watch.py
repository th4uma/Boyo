"""Operation-scoped notifications; no thread, refresh timer or polling loop.

Only local NTFS volumes use this shortcut. Any change, error or unsupported
filesystem permanently falls back to the full protection walk for this operation.
"""
import os
from pathlib import Path


class ProtectionWatch:
    def __init__(self, album):
        self.paths = [Path(album), Path(album)/'精选', Path(album)/'成片']
        self.handles = []
        self.active = False
        if os.name != 'nt': return
        try:
            import ctypes
            from ctypes import wintypes
            api = self.api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR,wintypes.LPWSTR,wintypes.DWORD]
            api.GetVolumePathNameW.restype = wintypes.BOOL
            api.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
            api.GetDriveTypeW.restype = wintypes.UINT
            api.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR,wintypes.LPWSTR,wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),ctypes.POINTER(wintypes.DWORD),ctypes.POINTER(wintypes.DWORD),wintypes.LPWSTR,wintypes.DWORD]
            api.GetVolumeInformationW.restype = wintypes.BOOL
            api.FindFirstChangeNotificationW.argtypes = [wintypes.LPCWSTR,wintypes.BOOL,wintypes.DWORD]
            api.FindFirstChangeNotificationW.restype = wintypes.HANDLE
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
            api.WaitForSingleObject.restype = wintypes.DWORD
            api.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
            api.FindCloseChangeNotification.restype = wintypes.BOOL
            volume = ctypes.create_unicode_buffer(32768)
            filesystem = ctypes.create_unicode_buffer(32)
            if not api.GetVolumePathNameW(str(self.paths[0]),volume,len(volume)): return
            if api.GetDriveTypeW(volume.value) != 3: return
            if not api.GetVolumeInformationW(volume.value,None,0,None,None,None,filesystem,len(filesystem)): return
            if filesystem.value.upper() != 'NTFS': return
            self.identities = [self._identity(p) for p in self.paths]
            if self.identities[0] is None: return
            for path,identity in zip(self.paths[1:],self.identities[1:]):
                if identity is None: continue
                handle = api.FindFirstChangeNotificationW(str(path),True,0x11f)
                if handle in (None,ctypes.c_void_p(-1).value):
                    self.close();return
                self.handles.append(handle)
            # Opening the handles must not conceal a replaced protection folder.
            if self.identities != [self._identity(p) for p in self.paths]:
                self.close();return
            self.active = True
        except (OSError,ValueError,AttributeError):
            self.close()

    @staticmethod
    def _identity(path):
        try: stat = path.stat(follow_symlinks=False)
        except FileNotFoundError: return None
        attributes = getattr(stat,'st_file_attributes',0)
        if attributes & 0x400: raise ValueError('Protection path became a reparse point')
        return stat.st_dev,stat.st_ino,stat.st_mode,attributes

    def unchanged(self):
        if not self.active: return False
        try:
            if self.identities != [self._identity(p) for p in self.paths]:
                self.close();return False
            # A timeout means no notification. A signaled/error handle is never
            # rearmed: subsequent files use the original full validation path.
            if any(self.api.WaitForSingleObject(handle,0) != 258 for handle in self.handles):
                self.close();return False
            return True
        except (OSError,ValueError):
            self.close();return False

    def close(self):
        for handle in self.handles:
            self.api.FindCloseChangeNotification(handle)
        self.handles.clear()
        self.active = False

    def __enter__(self): return self
    def __exit__(self,*args): self.close()
