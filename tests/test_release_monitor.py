from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ringping.config import AppSettings
from ringping.release_monitor import ReleaseMonitor


def build_settings(workspace_dir: Path) -> AppSettings:
    logs_dir = workspace_dir / "data" / "request-logs"
    return AppSettings(
        workspace_dir=workspace_dir,
        db_path=workspace_dir / "data" / "ringping.db",
        worktrees_dir=workspace_dir / "data" / "worktrees",
        request_logs_dir=logs_dir,
        projects_file=workspace_dir / "config" / "projects.json",
        webhook_host="127.0.0.1",
        webhook_port=8765,
        webhook_public_base_url="",
        poll_interval_seconds=2,
        ringcentral_poll_seconds=20,
        release_poll_seconds=20,
        codex_command="codex",
        codex_root_flags=[],
        codex_flags=["--full-auto"],
        codex_ask_root_flags=[],
        codex_ask_flags=["--full-auto", "--sandbox", "read-only", "--ephemeral"],
        codex_fallback_command="claude",
        codex_fallback_flags=["--dangerously-skip-permissions"],
        codex_timeout_seconds=3600,
        codex_ask_timeout_seconds=180,
        codex_idle_after_changes_seconds=180,
        ringcentral_server_url="https://platform.ringcentral.com",
        ringcentral_client_id="",
        ringcentral_client_secret="",
        ringcentral_jwt="",
        ringcentral_verification_token="",
        ringcentral_validation_token="",
        ringcentral_command_prefix="fix:",
        ringcentral_ask_prefix="ask:",
        request_console_enabled=False,
        post_status_updates=False,
        review_email_enabled=False,
        review_email_mode="outlook",
        review_email_to="",
        review_email_subject="Code Review",
        review_email_smtp_host="",
        review_email_smtp_port=587,
        review_email_smtp_username="",
        review_email_smtp_password="",
        review_email_smtp_from="",
        review_email_smtp_use_tls=True,
    )


class ReleaseMonitorTests(unittest.TestCase):
    def test_append_request_log_status_updates_live_and_raw_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)
            live_log = settings.request_logs_dir / "request-18.log"
            raw_log = settings.request_logs_dir / "request-18.raw.log"
            live_log.write_text("[RingPing] Existing live line\n", encoding="utf-8")
            raw_log.write_text("[RingPing] Existing raw line\n", encoding="utf-8")
            monitor = ReleaseMonitor(settings, None, None)

            monitor._append_request_log_status(18, "Release ready. Update is available.")

            self.assertIn("Release ready. Update is available.", live_log.read_text(encoding="utf-8"))
            self.assertIn("Release ready. Update is available.", raw_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
