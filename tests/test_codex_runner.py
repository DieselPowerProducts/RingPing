from __future__ import annotations

import tempfile
import sys
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
        codex_root_flags=[],
        codex_flags=["--full-auto"],
        codex_ask_root_flags=[],
        codex_ask_flags=["--full-auto", "--sandbox", "read-only", "--ephemeral"],
        codex_fallback_command="claude",
        codex_fallback_flags=["--dangerously-skip-permissions"],
        codex_fallback_home="",
        codex_timeout_seconds=0,
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
        ringcentral_legacy_requests_enabled=True,
        ringcentral_online_training_url="https://invoice-extractor-online.vercel.app/",
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

        with patch.object(runner, "_codex_shell_self_test", return_value=True), patch.object(
            runner,
            "_run_command",
            side_effect=[primary, fallback],
        ) as run_command:
            result = runner.run_read_only("Please inspect the files.", Path("C:/repo"))

        self.assertEqual(result.last_message, "Attachments look good.")
        self.assertEqual(run_command.call_count, 2)
        self.assertEqual(run_command.call_args_list[0].args[0], "codex")
        self.assertEqual(run_command.call_args_list[0].args[1], [])
        self.assertEqual(run_command.call_args_list[0].args[2], ["--full-auto", "--sandbox", "read-only", "--ephemeral"])
        self.assertEqual(run_command.call_args_list[0].kwargs["timeout_seconds"], 180)
        self.assertEqual(run_command.call_args_list[1].args[0], "claude")
        self.assertEqual(run_command.call_args_list[1].args[1], [])
        self.assertEqual(run_command.call_args_list[1].args[2], ["--dangerously-skip-permissions"])
        self.assertEqual(run_command.call_args_list[1].kwargs["timeout_seconds"], 180)

    def test_run_training_retries_with_fallback_codex_home_after_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fallback_home = Path(temp_dir) / ".codex-secondary"
            fallback_home.mkdir()
            (fallback_home / "auth.json").write_text("{}", encoding="utf-8")
            settings = build_settings()
            settings.codex_fallback_command = ""
            settings.codex_fallback_home = str(fallback_home)
            runner = CodexRunner(settings)
            primary = CodexRunResult(
                exit_code=1,
                last_message="",
                stdout_tail="You've hit your usage limit.",
                stderr_tail="",
                command_display="codex exec --full-auto",
            )
            fallback = CodexRunResult(
                exit_code=0,
                last_message="Training finished.",
                stdout_tail="",
                stderr_tail="",
                command_display="codex exec --full-auto",
            )

            with patch.object(runner, "_codex_shell_self_test", return_value=True), patch.object(
                runner,
                "_run_command",
                side_effect=[primary, fallback],
            ) as run_command:
                result = runner.run_training("Train vendor.", Path("C:/repo"))

            self.assertEqual(result.last_message, "Training finished.")
            self.assertEqual(run_command.call_count, 2)
            self.assertIsNone(run_command.call_args_list[0].kwargs["extra_env"])
            self.assertEqual(
                run_command.call_args_list[1].kwargs["extra_env"],
                {"CODEX_HOME": str(fallback_home)},
            )

    def test_run_training_does_not_retry_fallback_after_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fallback_home = Path(temp_dir) / ".codex-secondary"
            fallback_home.mkdir()
            (fallback_home / "auth.json").write_text("{}", encoding="utf-8")
            settings = build_settings()
            settings.codex_fallback_command = ""
            settings.codex_fallback_home = str(fallback_home)
            runner = CodexRunner(settings)
            cancelled = CodexRunResult(
                exit_code=1,
                last_message="",
                stdout_tail="",
                stderr_tail="You've hit your usage limit.",
                command_display="codex exec --full-auto",
                timed_out=True,
                timeout_reason="cancelled",
            )

            with patch.object(runner, "_codex_shell_self_test", return_value=True), patch.object(
                runner,
                "_run_command",
                return_value=cancelled,
            ) as run_command:
                result = runner.run_training("Train vendor.", Path("C:/repo"))

        self.assertEqual(result.timeout_reason, "cancelled")
        self.assertEqual(run_command.call_count, 1)

    def test_run_training_uses_fallback_codex_home_when_shell_self_test_rate_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fallback_home = Path(temp_dir) / ".codex-secondary"
            fallback_home.mkdir()
            (fallback_home / "auth.json").write_text("{}", encoding="utf-8")
            settings = build_settings()
            settings.codex_fallback_command = ""
            settings.codex_fallback_home = str(fallback_home)
            runner = CodexRunner(settings)
            rate_limited_self_test = CodexRunResult(
                exit_code=1,
                last_message="",
                stdout_tail="You've hit your usage limit.",
                stderr_tail="",
                command_display="codex exec --full-auto",
            )
            successful_self_test = CodexRunResult(
                exit_code=0,
                last_message="Exit code: `0`\n\nOutput: C:/repo",
                stdout_tail="",
                stderr_tail="",
                command_display="codex exec --full-auto",
            )
            successful_job = CodexRunResult(
                exit_code=0,
                last_message="Training finished.",
                stdout_tail="",
                stderr_tail="",
                command_display="codex exec --full-auto",
            )

            with patch.object(
                runner,
                "_run_command",
                side_effect=[rate_limited_self_test, successful_self_test, successful_job],
            ) as run_command:
                result = runner.run_training("Train vendor.", Path("C:/repo"))

            self.assertEqual(result.last_message, "Training finished.")
            self.assertIsNone(run_command.call_args_list[0].kwargs["extra_env"])
            self.assertEqual(
                run_command.call_args_list[1].kwargs["extra_env"],
                {"CODEX_HOME": str(fallback_home)},
            )
            self.assertEqual(
                run_command.call_args_list[2].kwargs["extra_env"],
                {"CODEX_HOME": str(fallback_home)},
            )

    def test_run_training_restarts_codex_after_shell_blocked_zero_exit(self) -> None:
        runner = CodexRunner(build_settings())
        blocked = CodexRunResult(
            exit_code=0,
            last_message=(
                "I'm blocked by the execution environment. Every attempted command failed "
                "immediately with exit code -1073741502."
            ),
            stdout_tail="",
            stderr_tail="",
            command_display="codex exec --full-auto",
        )
        success = CodexRunResult(
            exit_code=0,
            last_message="Training finished.",
            stdout_tail="",
            stderr_tail="",
            command_display="codex exec --full-auto",
        )

        with patch.object(runner, "_codex_shell_self_test", return_value=True) as self_test, patch.object(
            runner,
            "_recover_ringping_codex_processes",
        ) as recover, patch.object(runner, "_run_command", side_effect=[blocked, success]) as run_command:
            result = runner.run_training("Train vendor.", Path("C:/repo"))

        self.assertEqual(result.last_message, "Training finished.")
        recover.assert_called_once()
        self.assertEqual(self_test.call_count, 2)
        self.assertEqual(run_command.call_count, 2)

    def test_run_training_does_not_start_real_job_when_shell_self_test_fails_after_recovery(self) -> None:
        runner = CodexRunner(build_settings())

        with patch.object(runner, "_codex_shell_self_test", return_value=False) as self_test, patch.object(
            runner,
            "_recover_ringping_codex_processes",
        ) as recover, patch.object(runner, "_run_command") as run_command:
            result = runner.run_training("Train vendor.", Path("C:/repo"))

        self.assertEqual(result.exit_code, 1)
        self.assertIn("real job was not started", result.last_message)
        recover.assert_called_once()
        self.assertEqual(self_test.call_count, 2)
        run_command.assert_not_called()

    def test_codex_shell_self_test_fails_when_inner_shell_command_exits_negative(self) -> None:
        runner = CodexRunner(build_settings())
        failed_inner_shell = CodexRunResult(
            exit_code=0,
            last_message="Exit code: `-1073741502`\n\nOutput: empty",
            stdout_tail="",
            stderr_tail="",
            command_display="codex exec --full-auto",
        )

        with patch.object(runner, "_run_command", return_value=failed_inner_shell):
            result = runner._codex_shell_self_test("codex", [], ["--full-auto"], Path("C:/repo"), None)

        self.assertFalse(result)

    def test_codex_shell_self_test_uses_existing_shell_without_nested_powershell(self) -> None:
        runner = CodexRunner(build_settings())
        successful_self_test = CodexRunResult(
            exit_code=0,
            last_message="Exit code: `0`\n\nOutput: C:/repo",
            stdout_tail="",
            stderr_tail="",
            command_display="codex exec --full-auto",
        )

        with patch.object(runner, "_run_command", return_value=successful_self_test) as run_command:
            result = runner._codex_shell_self_test("codex", [], ["--full-auto"], Path("C:/repo"), None)

        self.assertTrue(result)
        self.assertIn("Get-Location", run_command.call_args.args[3])
        self.assertIn("already-open Codex shell", run_command.call_args.args[3])
        self.assertNotIn("powershell.exe -NoProfile", run_command.call_args.args[3])

    def test_codex_root_flags_are_placed_before_exec_subcommand(self) -> None:
        settings = build_settings()
        settings.codex_root_flags = ["--dangerously-bypass-approvals-and-sandbox"]
        runner = CodexRunner(settings)

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            runner,
            "_run_process",
            return_value=("", "", 0, False, None),
        ):
            result = runner._run_codex(
                "codex",
                settings.codex_root_flags,
                settings.codex_flags,
                "prompt",
                Path(temp_dir),
                0,
                None,
                30,
            )

        self.assertIn("codex --dangerously-bypass-approvals-and-sandbox exec --full-auto", result.command_display)

    def test_run_process_reports_raw_output_activity(self) -> None:
        runner = CodexRunner(build_settings())
        with tempfile.TemporaryDirectory() as temp_dir:
            activities = []

            stdout_text, stderr_text, exit_code, timed_out, timeout_reason = runner._run_process(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('stdout activity'); print('stderr activity', file=sys.stderr)",
                ],
                "",
                Path(temp_dir),
                0,
                None,
                10,
                activity_callback=lambda: activities.append("activity"),
            )

        self.assertEqual(exit_code, 0)
        self.assertFalse(timed_out)
        self.assertIsNone(timeout_reason)
        self.assertIn("stdout activity", stdout_text)
        self.assertIn("stderr activity", stderr_text)
        self.assertGreaterEqual(len(activities), 2)

    def test_run_process_stops_when_codex_notification_queue_fills(self) -> None:
        runner = CodexRunner(build_settings())
        with tempfile.TemporaryDirectory() as temp_dir:
            stdout_text, stderr_text, exit_code, timed_out, timeout_reason = runner._run_process(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys, time\n"
                        "for _ in range(30):\n"
                        "    print('WARN codex_app_server::in_process: dropping in-process server notification (queue full)', file=sys.stderr, flush=True)\n"
                        "time.sleep(30)\n"
                    ),
                ],
                "",
                Path(temp_dir),
                0,
                None,
                60,
                json_stdout=True,
            )

        self.assertEqual(stdout_text, "")
        self.assertIn("dropping in-process server notification", stderr_text)
        self.assertTrue(timed_out)
        self.assertEqual(timeout_reason, "codex_queue_full")
        self.assertNotEqual(exit_code, 0)

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
            events = []
            runner._handle_codex_stdout_line(
                log_path,
                '{"type":"item.started","item":{"type":"command_execution","command":"python -m unittest test_file.py"}}',
                event_callback=events.append,
            )
            runner._handle_codex_stdout_line(
                log_path,
                '{"type":"item.completed","item":{"type":"command_execution","command":"python -m unittest test_file.py","exit_code":0,"aggregated_output":"Ran 3 tests\\nOK\\n"}}',
                event_callback=events.append,
            )
            runner._handle_codex_stdout_line(
                log_path,
                '{"type":"item.completed","item":{"type":"agent_message","text":"Updated the parser and added a test."}}',
                event_callback=events.append,
            )

            log_text = log_path.read_text(encoding="utf-8")

        self.assertIn("[Codex] Running command: python -m unittest test_file.py", log_text)
        self.assertIn("[Codex] Command finished with exit 0: python -m unittest test_file.py", log_text)
        self.assertIn("[Codex] Test output: Ran 3 tests | OK", log_text)
        self.assertIn("[Codex] Updated the parser and added a test.", log_text)
        self.assertIn(
            {"type": "monitor", "text": "Codex is running command: python -m unittest test_file.py"},
            events,
        )
        self.assertIn(
            {"type": "monitor", "text": "Command finished with exit 0: python -m unittest test_file.py"},
            events,
        )
        self.assertIn(
            {"type": "monitor", "text": "Updated the parser and added a test."},
            events,
        )

    def test_handle_codex_stdout_line_posts_usage_limit_error_event(self) -> None:
        runner = CodexRunner(build_settings())
        events = []

        runner._handle_codex_stdout_line(
            None,
            '{"type":"error","message":"You have hit your usage limit."}',
            event_callback=events.append,
        )

        self.assertEqual(
            events,
            [{"type": "error", "text": "Codex error: You have hit your usage limit."}],
        )

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
        self.assertFalse(
            runner._stderr_line_counts_as_activity(
                "WARN codex_core_skills::loader: ignoring interface.icon_small: icon path must not contain '..'"
            )
        )
        self.assertTrue(runner._stderr_line_counts_as_activity("Traceback: real command failure"))


if __name__ == "__main__":
    unittest.main()
