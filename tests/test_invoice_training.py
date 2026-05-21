from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ringping.config import AppSettings
from ringping.invoice_training import InvoiceTrainingWorker
from ringping.models import CodexRunResult, ProjectConfig


def build_settings(workspace_dir: Path) -> AppSettings:
    return AppSettings(
        workspace_dir=workspace_dir,
        db_path=workspace_dir / "data" / "ringping.db",
        worktrees_dir=workspace_dir / "data" / "worktrees",
        request_logs_dir=workspace_dir / "data" / "request-logs",
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
        codex_fallback_command="",
        codex_fallback_flags=[],
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
        ringcentral_legacy_requests_enabled=False,
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
        training_worker_enabled=True,
        training_worker_base_url="https://invoice-extractor-online.vercel.app",
        training_worker_token="secret",
        training_worker_id="test-worker",
        training_worker_project_slug="invoice-extractor-online",
        training_worker_active_command_poll_seconds=1,
        training_worker_plain_event_timeout_seconds=180,
    )


class InvoiceTrainingWorkerTests(unittest.TestCase):
    def test_training_worker_checks_queue_once_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            worker.api_client = Mock()
            worker.api_client.is_configured = True
            worker.api_client.next_job.return_value = None
            worker._post_ringcentral_status = Mock()
            worker._process_job = Mock()

            worker._check_once()

        worker.api_client.next_job.assert_called_once()
        worker._process_job.assert_not_called()

    def test_training_worker_processes_single_claimed_job_without_polling_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            worker.api_client = Mock()
            worker.api_client.is_configured = True
            worker.api_client.next_job.return_value = {"jobId": "job-1"}
            worker._post_ringcentral_status = Mock()
            worker._process_job = Mock()

            worker._check_once()

        worker.api_client.next_job.assert_called_once()
        worker._process_job.assert_called_once_with({"jobId": "job-1"})

    def test_training_worker_wake_checks_queue_again_without_polling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            worker.api_client = Mock()
            worker.api_client.is_configured = True
            worker.api_client.next_job.return_value = None
            worker._post_ringcentral_status = Mock()
            worker._process_job = Mock()

            worker.start()
            deadline = time.monotonic() + 2
            while worker.api_client.next_job.call_count < 1 and time.monotonic() < deadline:
                time.sleep(0.01)

            worker.wake()
            deadline = time.monotonic() + 2
            while worker.api_client.next_job.call_count < 2 and time.monotonic() < deadline:
                time.sleep(0.01)

            worker.stop()
            worker.join(timeout=2)

        self.assertEqual(worker.api_client.next_job.call_count, 2)
        worker._process_job.assert_not_called()

    def test_build_training_prompt_requires_plain_english_training_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            project = ProjectConfig(
                slug="invoice-extractor-online",
                name="InvoiceExtractorOnline",
                repo_path=str(Path(temp_dir) / "repo"),
                codex_prompt_prefix="Run shell commands sequentially.",
                test_command="pytest",
            )

            prompt = worker._build_training_prompt(
                {
                    "jobId": "job-1",
                    "vendorName": "ACME",
                    "prompt": "Train ACME from the uploaded invoice.",
                    "files": [{"name": "invoice.pdf", "driveFileId": "file-1"}],
                },
                project,
                "ringping/training/job-1",
            )

        self.assertIn("TRAINING_EVENT", prompt)
        self.assertIn("InvoiceExtractorOnline", prompt)
        self.assertIn("Run shell commands sequentially.", prompt)
        self.assertIn("Vendor: ACME", prompt)
        self.assertIn("driveFileId", prompt)
        self.assertIn("Run this validation command if relevant: pytest", prompt)
        self.assertIn("training_output.xlsx", prompt)
        self.assertIn("first `Bill No.` cell must hyperlink", prompt)

    def test_find_training_output_prefers_root_review_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            worktree = Path(temp_dir) / "worktree"
            worktree.mkdir()
            nested = worktree / "training_output_attempt.xlsx"
            nested.write_text("old", encoding="utf-8")
            root = worktree / "training_output.xlsx"
            root.write_text("current", encoding="utf-8")

            self.assertEqual(worker._find_training_output(worktree), root)

    def test_watch_commands_sets_cancel_event_for_stop_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = build_settings(Path(temp_dir))
            worker = InvoiceTrainingWorker(settings, Mock(), Mock(), Mock(), Mock())
            cancel_event = threading.Event()
            worktree = Path(temp_dir) / "worktree"
            worktree.mkdir()
            command_state = {"interrupted_by_message": False}
            worker.api_client = Mock()
            worker.api_client.list_commands.return_value = [{"id": "cmd-1", "type": "stop"}]

            worker._watch_commands("job-1", cancel_event, worktree, command_state)

        self.assertTrue(cancel_event.is_set())
        worker.api_client.acknowledge_command.assert_called_once()

    def test_watch_commands_interrupts_for_message_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = build_settings(Path(temp_dir))
            worker = InvoiceTrainingWorker(settings, Mock(), Mock(), Mock(), Mock())
            cancel_event = threading.Event()
            worktree = Path(temp_dir) / "worktree"
            worktree.mkdir()
            command_state = {"interrupted_by_message": False}
            worker.api_client = Mock()
            worker.api_client.list_commands.return_value = [
                {"id": "cmd-1", "type": "message", "payload": {"message": "Use the attached master."}}
            ]

            worker._watch_commands("job-1", cancel_event, worktree, command_state)

        self.assertTrue(cancel_event.is_set())
        self.assertTrue(command_state["interrupted_by_message"])
        worker.api_client.acknowledge_command.assert_called_once()

    def test_training_watchdog_tracks_active_local_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            state = {
                "last_event_at": 0.0,
                "active_command_started_at": None,
                "active_command_text": "",
                "last_silent_warning_at": 0.0,
            }

            worker._update_training_watchdog_state(
                state,
                {"type": "monitor", "text": 'Codex is running command: "Get-Content api\\python_parser\\invoice_parser.py"'},
            )

            self.assertIsNotNone(state["active_command_started_at"])
            self.assertIn("Get-Content", state["active_command_text"])

            worker._update_training_watchdog_state(
                state,
                {"type": "monitor", "text": 'Command finished with exit 0: "Get-Content api\\python_parser\\invoice_parser.py"'},
            )

            self.assertIsNone(state["active_command_started_at"])
            self.assertEqual(state["active_command_text"], "")

    def test_training_silence_timeout_stops_when_no_local_command_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            worker.api_client = Mock()
            cancel_event = threading.Event()
            now = time.monotonic()
            state = {
                "last_event_at": now - 181,
                "active_command_started_at": None,
                "active_command_text": "",
                "last_silent_warning_at": 0.0,
            }

            stopped = worker._handle_training_silence_timeout("job-1", cancel_event, state, 180, now)

            self.assertTrue(stopped)
            self.assertTrue(cancel_event.is_set())
            worker.api_client.post_event.assert_called_once()
            args, kwargs = worker.api_client.post_event.call_args
            self.assertEqual(args[1], "needs_input")
            self.assertIn("no local command is running", args[2])
            self.assertEqual(kwargs["status"], "stopped")

    def test_training_silence_timeout_warns_when_local_command_has_no_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            worker.api_client = Mock()
            cancel_event = threading.Event()
            now = time.monotonic()
            state = {
                "last_event_at": now - 181,
                "active_command_started_at": now - 181,
                "active_command_text": 'Get-Content api\\python_parser\\invoice_parser.py | Select-Object -Skip 1390 -First 50',
                "last_silent_warning_at": 0.0,
            }

            stopped = worker._handle_training_silence_timeout("job-1", cancel_event, state, 180, now)

            self.assertFalse(stopped)
            self.assertFalse(cancel_event.is_set())
            worker.api_client.post_event.assert_called_once()
            args, kwargs = worker.api_client.post_event.call_args
            self.assertEqual(args[1], "warning")
            self.assertIn("local command is still active", args[2])
            self.assertEqual(kwargs["status"], "running")
            self.assertEqual(state["last_silent_warning_at"], now)

    def test_training_silence_timeout_throttles_active_command_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            worker.api_client = Mock()
            cancel_event = threading.Event()
            now = time.monotonic()
            state = {
                "last_event_at": now - 181,
                "active_command_started_at": now - 181,
                "active_command_text": 'Get-Content api\\python_parser\\invoice_parser.py',
                "last_silent_warning_at": now - 20,
            }

            stopped = worker._handle_training_silence_timeout("job-1", cancel_event, state, 180, now)

            self.assertFalse(stopped)
            self.assertFalse(cancel_event.is_set())
            worker.api_client.post_event.assert_not_called()

    def test_blocked_agent_result_detects_command_startup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            result = type(
                "Result",
                (),
                {
                    "last_message": "Blocked before I could inspect the parser. No files were changed.",
                    "stdout_tail": "",
                    "stderr_tail": "Exit code: -1073741502",
                },
            )()

        self.assertTrue(worker._is_blocked_agent_result(result))

    def test_blocked_agent_result_detects_every_shell_invocation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            result = type(
                "Result",
                (),
                {
                    "last_message": (
                        "Blocked: every shell invocation is failing immediately with exit code "
                        "`-1073741502`, including a bare `Get-Location`."
                    ),
                    "stdout_tail": "",
                    "stderr_tail": "",
                },
            )()

        self.assertTrue(worker._is_blocked_agent_result(result))

    def test_process_job_does_not_generate_output_after_blocked_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            worktree = workspace / "worktree"
            worktree.mkdir()
            settings = build_settings(workspace)
            project = ProjectConfig(
                slug="invoice-extractor-online",
                name="InvoiceExtractorOnline",
                repo_path=str(workspace / "repo"),
            )
            storage = Mock()
            storage.get_project.return_value = project
            git_manager = Mock()
            git_manager.create_or_reuse_worktree.return_value = ("ringping/training/job-1", worktree)
            git_manager.collect_diff_summary.return_value = ""
            codex_runner = Mock()
            blocked = CodexRunResult(
                exit_code=0,
                last_message=(
                    "I'm blocked by the execution environment. Every attempted command exits "
                    "immediately with exit code -1073741502."
                ),
                stdout_tail="",
                stderr_tail="",
                command_display="codex exec --full-auto",
            )
            codex_runner.run_training.side_effect = [blocked, blocked]
            worker = InvoiceTrainingWorker(settings, storage, git_manager, codex_runner, Mock())
            worker.api_client = Mock()
            worker.api_client.list_commands.return_value = []
            worker._validate_online_training_project = Mock()
            worker._ensure_codex_shell_ready = Mock()
            worker._recover_windows_process_startup = Mock()
            worker._watch_commands = Mock()
            worker._watch_plain_event_timeout = Mock()
            worker._generate_training_output = Mock()

            worker._process_job({"jobId": "job-1", "vendorName": "ACME", "prompt": "Train ACME."})

        self.assertEqual(codex_runner.run_training.call_count, 2)
        worker._recover_windows_process_startup.assert_called_once_with("job-1")
        worker._generate_training_output.assert_not_called()
        failure_events = [
            call.args
            for call in worker.api_client.post_event.call_args_list
            if len(call.args) >= 3 and call.args[1] == "error"
        ]
        self.assertTrue(failure_events)
        self.assertIn("blocked by local command execution", failure_events[-1][2])

    def test_no_change_result_is_not_command_startup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            result = type(
                "Result",
                (),
                {
                    "last_message": "Codex finished. No files were changed because the parser already handled this vendor.",
                    "stdout_tail": "",
                    "stderr_tail": "",
                },
            )()

        self.assertFalse(worker._is_blocked_agent_result(result))

    def test_recover_windows_process_startup_does_not_kill_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            worker.api_client = Mock()

            with patch("ringping.invoice_training.shutil.which", return_value="taskkill.exe"), patch(
                "ringping.invoice_training.subprocess.run"
            ) as run:
                run.return_value.returncode = 0
                worker._recover_windows_process_startup("job-1")

        killed_names = [call.args[0][-1] for call in run.call_args_list]
        self.assertNotIn("codex.exe", killed_names)
        self.assertIn("Dell.TechHub.Diagnostics.SubAgent.exe", killed_names)

    def test_validate_online_training_project_rejects_desktop_project_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            repo = Path(temp_dir) / "desktop"
            repo.mkdir()
            project = ProjectConfig(
                slug="invoice-extractor",
                name="InvoiceExtractor",
                repo_path=str(repo),
            )

            with self.assertRaisesRegex(RuntimeError, "misconfigured"):
                worker._validate_online_training_project(project)

    def test_validate_online_training_project_accepts_online_repo_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = InvoiceTrainingWorker(build_settings(Path(temp_dir)), Mock(), Mock(), Mock(), Mock())
            repo = Path(temp_dir) / "InvoiceExtractorOnline"
            (repo / "src" / "app").mkdir(parents=True)
            (repo / "api" / "python_parser").mkdir(parents=True)
            (repo / "package.json").write_text("{}", encoding="utf-8")
            (repo / "api" / "python_parser" / "invoice_parser.py").write_text("", encoding="utf-8")
            project = ProjectConfig(
                slug="invoice-extractor-online",
                name="InvoiceExtractorOnline",
                repo_path=str(repo),
            )

            worker._validate_online_training_project(project)


if __name__ == "__main__":
    unittest.main()
