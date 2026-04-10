from __future__ import annotations

import atexit
import ctypes
import hashlib
from pathlib import Path


ERROR_ALREADY_EXISTS = 183
KERNEL32 = ctypes.windll.kernel32


class SingleInstanceGuard:
    def __init__(self, workspace_dir: Path) -> None:
        normalized = str(workspace_dir.resolve()).lower().encode("utf-8", errors="ignore")
        digest = hashlib.sha1(normalized).hexdigest()
        self.name = f"Local\\RingPing-{digest}"
        self._handle = None

    def acquire(self) -> bool:
        handle = KERNEL32.CreateMutexW(None, True, self.name)
        if not handle:
            raise OSError("Failed to create RingPing single-instance mutex.")

        last_error = KERNEL32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            KERNEL32.CloseHandle(handle)
            return False

        self._handle = handle
        atexit.register(self.release)
        return True

    def release(self) -> None:
        if not self._handle:
            return
        try:
            KERNEL32.ReleaseMutex(self._handle)
        except Exception:
            pass
        try:
            KERNEL32.CloseHandle(self._handle)
        except Exception:
            pass
        self._handle = None
