from __future__ import annotations

import atexit
import ctypes
import hashlib
import json
import os
import time
import uuid
from pathlib import Path


ERROR_ALREADY_EXISTS = 183
KERNEL32 = ctypes.windll.kernel32
MODE_UI = "ui"
MODE_HEADLESS = "headless"


class SingleInstanceGuard:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = Path(workspace_dir).resolve()
        normalized = str(self.workspace_dir).lower().encode("utf-8", errors="ignore")
        digest = hashlib.sha1(normalized).hexdigest()
        self.name = f"Local\\RingPing-{digest}"
        self.instance_id = uuid.uuid4().hex
        self.state_dir = self.workspace_dir / "data"
        self.state_path = self.state_dir / "instance-state.json"
        self.switch_to_ui_path = self.state_dir / "switch-to-ui.flag"
        self._handle = None
        self._mode: str | None = None

    def acquire(self, mode: str) -> bool:
        handle = KERNEL32.CreateMutexW(None, True, self.name)
        if not handle:
            raise OSError("Failed to create RingPing single-instance mutex.")

        last_error = KERNEL32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            KERNEL32.CloseHandle(handle)
            return False

        self._handle = handle
        self._mode = mode
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._write_state(mode)
        if mode == MODE_UI:
            self.clear_ui_switch_request()
        atexit.register(self.release)
        return True

    def acquire_after_headless_shutdown(self, timeout_seconds: float = 20.0, poll_seconds: float = 0.25) -> bool:
        self.request_ui_takeover()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.acquire(MODE_UI):
                return True
            time.sleep(poll_seconds)
        return False

    def request_ui_takeover(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.switch_to_ui_path.write_text(str(time.time()), encoding="ascii")

    def clear_ui_switch_request(self) -> None:
        try:
            self.switch_to_ui_path.unlink()
        except FileNotFoundError:
            pass

    def headless_shutdown_requested(self) -> bool:
        return self.switch_to_ui_path.exists()

    def get_running_mode(self) -> str | None:
        state = self._read_state()
        mode = state.get("mode")
        return str(mode) if mode else None

    def release(self) -> None:
        if not self._handle:
            return
        try:
            self._clear_state_if_owned()
        except Exception:
            pass
        try:
            KERNEL32.ReleaseMutex(self._handle)
        except Exception:
            pass
        try:
            KERNEL32.CloseHandle(self._handle)
        except Exception:
            pass
        self._handle = None
        self._mode = None

    def _write_state(self, mode: str) -> None:
        payload = {
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "mode": mode,
        }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _clear_state_if_owned(self) -> None:
        state = self._read_state()
        if state.get("instance_id") != self.instance_id:
            return
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass
