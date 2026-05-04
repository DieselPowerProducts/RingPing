from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ringping.codex_runner import CodexRunner
from ringping.config import AppSettings
from ringping.models import CodexRunResult


def build_settings() -> AppSettings:
    return AppSettings(
        workspace_dir=Path("C:/workspace"),
        db_path=Path("C:/workspace/data/ringping.db"),
        worktrees_dir=Path("C:/workspace/data/worktrees"),
        request_logs_dir=Path("C:/workspace/data/request-logs"),
        projects_file=Path("C:/workspace/config/projects.json"),
        webhook_host="127.0.0.1",
        webhook_port=8765,
        webhook_public_base_url="",
        poll_interval_seconds=2,
        ringcentral_poll_seconds=20,
        release_poll_seconds=20,
        codex_command="codex",
        codex_flags=["--full-auto"],
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


class CodexRunnerTests(unittest.TestCase):
    def test_run_read_only_falls_back_after_rate_limit(self) -> None:
        runner = CodexRunner(build_settings())
        primary = CodexRunResult(
            exit_code=1,
            last_message="",
            stdout_tail="",
            stderr_tail="You've hit your usage limit.",
            command_display="codex exec --full-auto",
        )
        fallback = CodexRunResult(
            exit_code=0,
            last_message="Attachments look good.",
            stdout_tail="",
            stderr_tail="",
            command_display="claude -p --dangerously-skip-permissions",
        )

        with patch.object(runner, "_run_command", side_effect=[primary, fallback]) as run_command:
            result = runner.run_read_only("Please inspect the files.", Path("C:/repo"))

        self.assertEqual(result.last_message, "Attachments look good.")
        self.assertEqual(run_command.call_count, 2)
        self.assertEqual(run_command.call_args_list[0].args[0], "codex")
        self.assertEqual(
            run_command.call_args_list[0].args[1],
            ["--full-auto", "--sandbox", "read-only", "--ephemeral"],
        )
        self.assertEqual(run_command.call_args_list[0].kwargs["timeout_seconds"], 180)
        self.assertEqual(run_command.call_args_list[1].args[0], "claude")
        self.assertEqual(run_command.call_args_list[1].args[1], ["--dangerously-skip-permissions"])
        self.assertEqual(run_command.call_args_list[1].kwargs["timeout_seconds"], 180)

    def test_resolve_command_uses_path_when_configured_absolute_path_is_stale(self) -> None:
        runner = CodexRunner(build_settings())
        stale_command = "C:/Users/Mike/.vscode/extensions/openai.chatgpt-old/bin/windows-x86_64/codex.exe"

        def fake_which(command: str) -> str | None:
            if command == "codex.exe":
                return "C:/Users/Mike/.vscode/extensions/openai.chatgpt-new/bin/windows-x86_64/codex.exe"
            return None

        with patch("ringping.codex_runner.shutil.which", side_effect=fake_which):
            resolved = runner._resolve_command(stale_command)

        self.assertEqual(
            resolved,
            "C:/Users/Mike/.vscode/extensions/openai.chatgpt-new/bin/windows-x86_64/codex.exe",
        )

    def test_resolve_command_finds_newest_vscode_codex_when_path_entry_is_unavailable(self) -> None:
        runner = CodexRunner(build_settings())
        with tempfile.TemporaryDirectory() as temp_dir:
            extensions_dir = Path(temp_dir) / "extensions"
            old_codex = extensions_dir / "openai.chatgpt-26.409.20454-win32-x64" / "bin" / "windows-x86_64" / "codex.exe"
            new_codex = extensions_dir / "openai.chatgpt-26.422.21459-win32-x64" / "bin" / "windows-x86_64" / "codex.exe"
            old_codex.parent.mkdir(parents=True)
            new_codex.parent.mkdir(parents=True)
            old_codex.write_text("", encoding="utf-8")
            new_codex.write_text("", encoding="utf-8")
            stale_command = str(old_codex).replace("26.409.20454", "26.408.11111")

            with patch("ringping.codex_runner.shutil.which", return_value=None), patch(
                "ringping.codex_runner.Path.home",
                return_value=Path(temp_dir),
            ):
                resolved = runner._resolve_command(stale_command)

        self.assertEqual(resolved, str(new_codex))

    def test_handle_codex_stdout_line_writes_monitor_friendly_entries(self) -> None:
        runner = CodexRunner(build_settings())
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "request-1.log"
            runner._handle_codex_stdout_line(
                log_path,
                '{"type":"item.started","item":{"type":"command_execution","command":"python -m unittest test_file.py"}}',
            )
            runner._handle_codex_stdout_line(
                log_path,
                '{"type":"item.completed","item":{"type":"command_execution","command":"python -m unittest test_file.py","exit_code":0,"aggregated_output":"Ran 3 tests\\nOK\\n"}}',
            )
            runner._handle_codex_stdout_line(
                log_path,
                '{"type":"item.completed","item":{"type":"agent_message","text":"Updated the parser and added a test."}}',
            )

            log_text = log_path.read_text(encoding="utf-8")

        self.assertIn("[Codex] Running command: python -m unittest test_file.py", log_text)
        self.assertIn("[Codex] Command finished with exit 0: python -m unittest test_file.py", log_text)
        self.assertIn("[Codex] Test output: Ran 3 tests | OK", log_text)
        self.assertIn("[Codex] Updated the parser and added a test.", log_text)

    def test_noisy_codex_server_warnings_do_not_count_as_activity(self) -> None:
        runner = CodexRunner(build_settings())

        self.assertFalse(
            runner._stderr_line_counts_as_activity(
                "WARN codex_app_server::in_process: dropping in-process server notification (queue full)"
            )
        )
        self.assertFalse(
            runner._stderr_line_counts_as_activity(
                "WARN codex_core_plugins::manifest: ignoring interface.defaultPrompt"
            )
        )
        self.assertTrue(runner._stderr_line_counts_as_activity("Traceback: real command failure"))


if __name__ == "__main__":
    unittest.main()
