from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from ringping.single_instance import SingleInstanceGuard


class SingleInstanceGuardTests(unittest.TestCase):
    def test_headless_shutdown_requested_clears_empty_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            guard = SingleInstanceGuard(Path(temp_dir))
            guard.state_dir.mkdir(parents=True, exist_ok=True)
            guard.switch_to_ui_path.write_text("", encoding="ascii")

            self.assertFalse(guard.headless_shutdown_requested())
            self.assertFalse(guard.switch_to_ui_path.exists())

    def test_headless_shutdown_requested_clears_stale_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            guard = SingleInstanceGuard(Path(temp_dir))
            guard.state_dir.mkdir(parents=True, exist_ok=True)
            guard.switch_to_ui_path.write_text(str(time.time() - 120), encoding="ascii")

            self.assertFalse(guard.headless_shutdown_requested())
            self.assertFalse(guard.switch_to_ui_path.exists())

    def test_headless_shutdown_requested_honors_fresh_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            guard = SingleInstanceGuard(Path(temp_dir))
            guard.state_dir.mkdir(parents=True, exist_ok=True)
            guard.switch_to_ui_path.write_text(str(time.time()), encoding="ascii")

            self.assertTrue(guard.headless_shutdown_requested())
            self.assertTrue(guard.switch_to_ui_path.exists())


if __name__ == "__main__":
    unittest.main()
