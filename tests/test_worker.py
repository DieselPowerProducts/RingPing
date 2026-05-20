from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ringping.config import AppSettings
from ringping.models import CodexRunResult, ProjectConfig, ProjectGuardrails, RequestAttachment, RequestRecord, RequestStatus
from ringping.ringcentral import RingCentralError
from ringping.worker import RequestWorker


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


class WorkerLiveLogTests(unittest.TestCase):
    def test_prepare_live_log_writes_prompt_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)
            worker = RequestWorker(settings, None, None, None, None, None)
            request = type(
                "RequestStub",
                (),
                {"id": 42, "title": "Sample request", "prompt": "fix the parser", "is_ask": False},
            )()
            project = type("ProjectStub", (), {"name": "InvoiceExtractor"})()

            log_path = worker._prepare_live_log(request, project, workspace_dir)

            self.assertEqual(log_path, settings.request_logs_dir / "request-42.log")
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("[Scuba Steve] Request 42", log_text)
            self.assertIn("[Scuba Steve] Project: InvoiceExtractor", log_text)
            self.assertIn("[Scuba Steve] Title: Sample request", log_text)
            self.assertIn("[Scuba Steve] Mode: fix", log_text)
            self.assertIn("[Scuba Steve] Raw log:", log_text)
            self.assertIn("fix the parser", log_text)

    def test_prepare_live_log_opens_console_only_when_interactive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)
            settings.request_console_enabled = True
            worker = RequestWorker(settings, None, None, None, None, None)
            request = type(
                "RequestStub",
                (),
                {"id": 43, "title": "Sample request", "prompt": "fix the parser", "is_ask": False},
            )()
            project = type("ProjectStub", (), {"name": "InvoiceExtractor"})()

            with patch("ringping.worker.interactive_request_console_available", return_value=False), patch.object(
                worker, "_open_live_console"
            ) as open_console:
                worker._prepare_live_log(request, project, workspace_dir)
                open_console.assert_not_called()

            with patch("ringping.worker.interactive_request_console_available", return_value=True), patch.object(
                worker, "_open_live_console"
            ) as open_console:
                worker._prepare_live_log(request, project, workspace_dir)
                open_console.assert_called_once()

    def test_build_ask_prompt_discourages_broad_repo_scans_without_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            settings = build_settings(workspace_dir)
            worker = RequestWorker(settings, None, None, None, None, None)
            request = type("RequestStub", (), {"prompt": "how are we verifying the S&B vendor name?"})()

            prompt = worker._build_ask_prompt(request, [])

            self.assertIn("No attachments were provided for this request.", prompt)
            self.assertIn("This is a fast read-only question, not a fix request.", prompt)
            self.assertIn("Do not do broad repo sweeps", prompt)
            self.assertIn("Usually 1-4 short read-only commands or file reads should be enough.", prompt)

    def test_build_ask_prompt_requests_plain_english_business_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            settings = build_settings(workspace_dir)
            worker = RequestWorker(settings, None, None, None, None, None)
            request = type("RequestStub", (), {"prompt": "is S&B using address matching?"})()

            prompt = worker._build_ask_prompt(request, [])

            self.assertIn("Write for a non-technical business user such as accounting or operations.", prompt)
            self.assertIn("Do not mention function names, file names, test names, commands, or internal implementation details", prompt)
            self.assertIn("If the question is effectively yes/no, start the answer with a clear Yes or No.", prompt)
            self.assertIn("Do not delete downloaded ask attachments; Scuba Steve removes those temporary files", prompt)

    def test_worker_waits_without_claiming_when_codex_credits_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            settings = build_settings(workspace_dir)
            settings.codex_fallback_command = ""
            storage = Mock()
            worker = RequestWorker(settings, storage, Mock(), Mock(), Mock(), Mock())

            def stop_after_wait(seconds: float) -> None:
                self.assertEqual(seconds, 60)
                worker._stop_event.set()

            with patch("ringping.worker.codex_credits_available", return_value=False), patch.object(
                worker._stop_event,
                "wait",
                side_effect=stop_after_wait,
            ):
                worker.run()

            storage.claim_next_pending_request.assert_not_called()

    def test_process_request_salvages_timed_out_diff_and_marks_pushed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            worktree_dir = workspace_dir / "worktree"
            worktree_dir.mkdir(parents=True, exist_ok=True)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)
            settings.post_status_updates = True

            storage = Mock()
            git_manager = Mock()
            codex_runner = Mock()
            ringcentral_client = Mock()
            ringcentral_client.is_configured = True
            review_email_notifier = Mock()
            worker = RequestWorker(settings, storage, git_manager, codex_runner, ringcentral_client, review_email_notifier)

            request = RequestRecord(
                id=22,
                project_slug="invoice-extractor",
                source="ringcentral",
                source_thread_id="thread-1",
                source_message_id="message-1",
                title="Fix KC memo",
                prompt="Fix KC memo",
                attachments=[],
                status=RequestStatus.RUNNING,
                branch_name="ringping/invoice-extractor/22",
                worktree_path=str(worktree_dir),
                codex_summary=None,
                diff_summary=None,
                manual_review_reason=None,
                error_text=None,
                commit_sha=None,
                release_version=None,
                created_at="2026-04-20T17:39:29+00:00",
                updated_at="2026-04-20T17:39:29+00:00",
                started_at="2026-04-20T17:39:29+00:00",
                completed_at=None,
                pushed_at=None,
                release_ready_notified_at=None,
                is_ask=False,
            )
            project = ProjectConfig(
                slug="invoice-extractor",
                name="InvoiceExtractor",
                repo_path=str(worktree_dir),
                push_mode="direct",
                auto_push=True,
                release_on_push=True,
                release_version_strategy="patch",
                guardrails=ProjectGuardrails(
                    allowed_paths=["invoice_parser.py", "test_*invoice_parser.py"],
                    blocked_paths=["VERSION", "release_request.json"],
                ),
            )
            codex_result = CodexRunResult(
                exit_code=-1,
                last_message="",
                stdout_tail="",
                stderr_tail="",
                command_display="codex exec --full-auto",
                timed_out=True,
                timeout_reason="idle_after_changes",
            )

            storage.get_project.return_value = project
            storage.get_request.return_value = request
            git_manager.create_or_reuse_worktree.return_value = (request.branch_name, worktree_dir)
            git_manager.collect_diff_summary.return_value = "Status:\nM invoice_parser.py"
            git_manager.worktree_has_changes.return_value = True
            git_manager.commit_and_push.return_value = ("abc123", "1.2.44")
            codex_runner.run.return_value = codex_result

            worker._process_request(request)

            storage.mark_request_pushed.assert_called_once()
            storage.mark_request_error.assert_not_called()
            git_manager.commit_and_push.assert_called_once()

    def test_process_request_keeps_downloaded_attachment_summary_when_runner_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            worktree_dir = workspace_dir / "worktree"
            worktree_dir.mkdir(parents=True, exist_ok=True)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)
            settings.post_status_updates = True

            storage = Mock()
            git_manager = Mock()
            codex_runner = Mock()
            ringcentral_client = Mock()
            ringcentral_client.is_configured = True
            review_email_notifier = Mock()
            worker = RequestWorker(settings, storage, git_manager, codex_runner, ringcentral_client, review_email_notifier)

            attachment = RequestAttachment(
                id="file-1",
                name="invoice.pdf",
                content_uri="https://example.com/invoice.pdf",
            )
            request = RequestRecord(
                id=25,
                project_slug="invoice-extractor",
                source="ringcentral",
                source_thread_id="thread-1",
                source_message_id="message-1",
                title="Fix invoice",
                prompt="Fix invoice",
                attachments=[attachment],
                status=RequestStatus.RUNNING,
                branch_name=None,
                worktree_path=None,
                codex_summary=None,
                diff_summary=None,
                manual_review_reason=None,
                error_text=None,
                commit_sha=None,
                release_version=None,
                created_at="2026-04-24T16:24:42+00:00",
                updated_at="2026-04-24T16:24:42+00:00",
                started_at=None,
                completed_at=None,
                pushed_at=None,
                release_ready_notified_at=None,
                is_ask=False,
            )
            project = ProjectConfig(
                slug="invoice-extractor",
                name="InvoiceExtractor",
                repo_path=str(worktree_dir),
            )
            downloaded_path = worktree_dir / "ringping_attachments" / "request-25" / "invoice.pdf"

            storage.get_project.return_value = project
            git_manager.create_or_reuse_worktree.return_value = ("ringping/invoice-extractor/25", worktree_dir)
            ringcentral_client.download_attachment.return_value = downloaded_path
            codex_runner.run.side_effect = RuntimeError("Command not found on PATH: codex")

            worker._process_request(request)

            storage.mark_request_error.assert_called_once()
            args = storage.mark_request_error.call_args.args
            self.assertEqual(args[0], 25)
            self.assertEqual(args[1], "Command not found on PATH: codex")
            self.assertIn("Downloaded attachments:", args[2])
            self.assertIn(f"invoice.pdf -> {downloaded_path}", args[2])
            ringcentral_client.post_chat_message.assert_called_once_with(
                "thread-1",
                "Scuba Steve failed while processing 'Fix invoice'. Review is needed before retrying.",
            )

    def test_process_request_marks_blocked_zero_exit_noop_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            worktree_dir = workspace_dir / "worktree"
            worktree_dir.mkdir(parents=True, exist_ok=True)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)
            settings.post_status_updates = True

            storage = Mock()
            git_manager = Mock()
            codex_runner = Mock()
            ringcentral_client = Mock()
            ringcentral_client.is_configured = True
            review_email_notifier = Mock()
            worker = RequestWorker(settings, storage, git_manager, codex_runner, ringcentral_client, review_email_notifier)

            request = RequestRecord(
                id=36,
                project_slug="invoice-extractor",
                source="ringcentral",
                source_thread_id="thread-1",
                source_message_id="message-1",
                title="Fix stock order",
                prompt="Fix stock order",
                attachments=[],
                status=RequestStatus.RUNNING,
                branch_name=None,
                worktree_path=None,
                codex_summary=None,
                diff_summary=None,
                manual_review_reason=None,
                error_text=None,
                commit_sha=None,
                release_version=None,
                created_at="2026-05-04T17:38:00+00:00",
                updated_at="2026-05-04T17:38:00+00:00",
                started_at=None,
                completed_at=None,
                pushed_at=None,
                release_ready_notified_at=None,
                is_ask=False,
            )
            project = ProjectConfig(
                slug="invoice-extractor",
                name="InvoiceExtractor",
                repo_path=str(worktree_dir),
            )
            codex_result = CodexRunResult(
                exit_code=0,
                last_message=(
                    "Blocked before I could inspect or change the repo: every shell_command call "
                    "fails at PowerShell startup with Windows status 0xC0000142. No files were modified."
                ),
                stdout_tail="",
                stderr_tail="",
                command_display="codex exec --full-auto",
            )

            storage.get_project.return_value = project
            git_manager.create_or_reuse_worktree.return_value = ("ringping/invoice-extractor/36", worktree_dir)
            git_manager.collect_diff_summary.return_value = ""
            git_manager.worktree_has_changes.return_value = False
            codex_runner.run.return_value = codex_result

            with patch.object(worker, "_recover_windows_process_startup") as recover, \
                 patch.object(worker, "_powershell_smoke_test", return_value=(True, "")) as smoke_test, \
                 patch("ringping.worker.time.sleep"):
                worker._process_request(request)

            recover.assert_called_once()
            self.assertEqual(smoke_test.call_count, 2)
            self.assertEqual(codex_runner.run.call_count, 2)
            storage.mark_request_error.assert_called_once()
            args = storage.mark_request_error.call_args.args
            self.assertEqual(args[0], 36)
            self.assertIn("local tool/session failure", args[1])
            storage.mark_request_no_changes.assert_not_called()
            ringcentral_client.post_chat_message.assert_called_once_with(
                "thread-1",
                "Scuba Steve was blocked by a local tool/session problem while working on 'Fix stock order'. Review is needed before retrying.",
            )

    def test_process_request_retries_once_after_blocked_codex_tool_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            worktree_dir = workspace_dir / "worktree"
            worktree_dir.mkdir(parents=True, exist_ok=True)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)

            storage = Mock()
            git_manager = Mock()
            codex_runner = Mock()
            ringcentral_client = Mock()
            ringcentral_client.is_configured = True
            review_email_notifier = Mock()
            worker = RequestWorker(settings, storage, git_manager, codex_runner, ringcentral_client, review_email_notifier)

            request = RequestRecord(
                id=39,
                project_slug="invoice-extractor",
                source="ringcentral",
                source_thread_id="thread-1",
                source_message_id="message-1",
                title="Fix invoice",
                prompt="Fix invoice",
                attachments=[],
                status=RequestStatus.RUNNING,
                branch_name=None,
                worktree_path=None,
                codex_summary=None,
                diff_summary=None,
                manual_review_reason=None,
                error_text=None,
                commit_sha=None,
                release_version=None,
                created_at="2026-05-04T18:10:00+00:00",
                updated_at="2026-05-04T18:10:00+00:00",
                started_at=None,
                completed_at=None,
                pushed_at=None,
                release_ready_notified_at=None,
                is_ask=False,
            )
            project = ProjectConfig(
                slug="invoice-extractor",
                name="InvoiceExtractor",
                repo_path=str(worktree_dir),
            )
            blocked_result = CodexRunResult(
                exit_code=0,
                last_message=(
                    "I'm blocked by the runner: every shell process fails immediately "
                    "with Windows status -1073741502, including cmd /c echo hello.\n\n"
                    "I left the working tree unchanged."
                ),
                stdout_tail="",
                stderr_tail="",
                command_display="codex exec --full-auto",
            )
            success_result = CodexRunResult(
                exit_code=0,
                last_message="No code changes needed.",
                stdout_tail="",
                stderr_tail="",
                command_display="codex exec --full-auto",
            )

            storage.get_project.return_value = project
            git_manager.create_or_reuse_worktree.return_value = ("ringping/invoice-extractor/39", worktree_dir)
            git_manager.collect_diff_summary.return_value = ""
            git_manager.worktree_has_changes.return_value = False
            codex_runner.run.side_effect = [blocked_result, success_result]

            with patch.object(worker, "_recover_windows_process_startup") as recover, \
                 patch.object(worker, "_powershell_smoke_test", return_value=(True, "")) as smoke_test, \
                 patch("ringping.worker.time.sleep"):
                worker._process_request(request)

            recover.assert_called_once()
            self.assertEqual(smoke_test.call_count, 2)
            self.assertEqual(codex_runner.run.call_count, 2)
            storage.mark_request_no_changes.assert_called_once()
            storage.mark_request_error.assert_not_called()

    def test_process_request_recovers_failed_shell_preflight_before_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            worktree_dir = workspace_dir / "worktree"
            worktree_dir.mkdir(parents=True, exist_ok=True)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)

            storage = Mock()
            git_manager = Mock()
            codex_runner = Mock()
            ringcentral_client = Mock()
            ringcentral_client.is_configured = True
            review_email_notifier = Mock()
            worker = RequestWorker(settings, storage, git_manager, codex_runner, ringcentral_client, review_email_notifier)

            request = RequestRecord(
                id=37,
                project_slug="invoice-extractor",
                source="ringcentral",
                source_thread_id="thread-1",
                source_message_id="message-1",
                title="Fix invoice",
                prompt="Fix invoice",
                attachments=[],
                status=RequestStatus.RUNNING,
                branch_name=None,
                worktree_path=None,
                codex_summary=None,
                diff_summary=None,
                manual_review_reason=None,
                error_text=None,
                commit_sha=None,
                release_version=None,
                created_at="2026-05-04T17:38:00+00:00",
                updated_at="2026-05-04T17:38:00+00:00",
                started_at=None,
                completed_at=None,
                pushed_at=None,
                release_ready_notified_at=None,
                is_ask=False,
            )
            project = ProjectConfig(
                slug="invoice-extractor",
                name="InvoiceExtractor",
                repo_path=str(worktree_dir),
            )
            codex_result = CodexRunResult(
                exit_code=0,
                last_message="No code changes needed.",
                stdout_tail="",
                stderr_tail="",
                command_display="codex exec --full-auto",
            )

            storage.get_project.return_value = project
            git_manager.create_or_reuse_worktree.return_value = ("ringping/invoice-extractor/37", worktree_dir)
            git_manager.collect_diff_summary.return_value = ""
            git_manager.worktree_has_changes.return_value = False
            codex_runner.run.return_value = codex_result

            with patch.object(worker, "_powershell_smoke_test", side_effect=[(False, "0xC0000142"), (True, "")]), \
                 patch.object(worker, "_recover_windows_process_startup") as recover, \
                 patch("ringping.worker.time.sleep"):
                worker._process_request(request)

            recover.assert_called_once()
            codex_runner.run.assert_called_once()
            storage.mark_request_no_changes.assert_called_once()
            storage.mark_request_error.assert_not_called()

    def test_process_request_fails_before_codex_if_shell_preflight_cannot_recover(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            worktree_dir = workspace_dir / "worktree"
            worktree_dir.mkdir(parents=True, exist_ok=True)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)
            settings.post_status_updates = True

            storage = Mock()
            git_manager = Mock()
            codex_runner = Mock()
            ringcentral_client = Mock()
            ringcentral_client.is_configured = True
            review_email_notifier = Mock()
            worker = RequestWorker(settings, storage, git_manager, codex_runner, ringcentral_client, review_email_notifier)

            request = RequestRecord(
                id=38,
                project_slug="invoice-extractor",
                source="ringcentral",
                source_thread_id="thread-1",
                source_message_id="message-1",
                title="Fix invoice",
                prompt="Fix invoice",
                attachments=[],
                status=RequestStatus.RUNNING,
                branch_name=None,
                worktree_path=None,
                codex_summary=None,
                diff_summary=None,
                manual_review_reason=None,
                error_text=None,
                commit_sha=None,
                release_version=None,
                created_at="2026-05-04T17:38:00+00:00",
                updated_at="2026-05-04T17:38:00+00:00",
                started_at=None,
                completed_at=None,
                pushed_at=None,
                release_ready_notified_at=None,
                is_ask=False,
            )
            project = ProjectConfig(
                slug="invoice-extractor",
                name="InvoiceExtractor",
                repo_path=str(worktree_dir),
            )

            storage.get_project.return_value = project
            git_manager.create_or_reuse_worktree.return_value = ("ringping/invoice-extractor/38", worktree_dir)

            with patch.object(worker, "_powershell_smoke_test", return_value=(False, "0xC0000142")), \
                 patch.object(worker, "_recover_windows_process_startup"), \
                 patch("ringping.worker.time.sleep"):
                worker._process_request(request)

            codex_runner.run.assert_not_called()
            storage.mark_request_error.assert_called_once()
            args = storage.mark_request_error.call_args.args
            self.assertEqual(args[0], 38)
            self.assertIn("Local PowerShell startup check failed", args[1])
            ringcentral_client.post_chat_message.assert_called_once_with(
                "thread-1",
                "Scuba Steve failed while processing 'Fix invoice'. Review is needed before retrying.",
            )

    def test_download_attachments_reuses_cached_file_when_ringcentral_link_is_dead(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            settings = build_settings(workspace_dir)
            cached_dir = (
                settings.worktrees_dir
                / "invoice-extractor"
                / "26"
                / ".ringping_artifacts"
                / "request-26"
            )
            cached_dir.mkdir(parents=True, exist_ok=True)
            cached_file = cached_dir / "Invoice _1_.pdf"
            cached_file.write_text("cached invoice", encoding="utf-8")

            worktree_dir = settings.worktrees_dir / "invoice-extractor" / "28"
            worktree_dir.mkdir(parents=True, exist_ok=True)

            git_manager = Mock()
            ringcentral_client = Mock()
            ringcentral_client.download_attachment.side_effect = RingCentralError("Attachment download failed 404")
            worker = RequestWorker(settings, Mock(), git_manager, Mock(), ringcentral_client, Mock())
            attachment = RequestAttachment(
                id="file-1",
                name="Invoice (1).pdf",
                content_uri="https://dl.mvp.ringcentral.com/file/missing",
            )
            request = type(
                "RequestStub",
                (),
                {"id": 28, "project_slug": "invoice-extractor", "attachments": [attachment]},
            )()

            downloaded = worker._download_request_attachments(request, worktree_dir)

            target_file = worktree_dir / "ringping_attachments" / "request-28" / "Invoice _1_.pdf"
            self.assertEqual(downloaded, [(attachment, target_file)])
            self.assertEqual(target_file.read_text(encoding="utf-8"), "cached invoice")
            git_manager.ensure_excluded.assert_called_once_with(worktree_dir, "ringping_attachments/")

    def test_ask_attachments_use_visible_temp_folder_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            project_root = workspace_dir / "repo"
            project_root.mkdir(parents=True, exist_ok=True)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)

            git_manager = Mock()
            ringcentral_client = Mock()
            worker = RequestWorker(settings, Mock(), git_manager, Mock(), ringcentral_client, Mock())
            attachment = RequestAttachment(
                id="file-1",
                name="invoice.pdf",
                content_uri="https://example.com/invoice.pdf",
            )
            request = type(
                "RequestStub",
                (),
                {"id": 29, "project_slug": "invoice-extractor", "attachments": [attachment]},
            )()
            downloaded_path = project_root / "ringping_attachments" / "request-29" / "invoice.pdf"
            ringcentral_client.download_attachment.return_value = downloaded_path

            downloaded = worker._download_ask_attachments(request, project_root)

            self.assertEqual(downloaded, [(attachment, downloaded_path)])
            self.assertEqual(downloaded_path.parent.name, "request-29")
            self.assertEqual(downloaded_path.parent.parent.name, "ringping_attachments")

            downloaded_path.parent.mkdir(parents=True, exist_ok=True)
            downloaded_path.write_text("downloaded", encoding="utf-8")
            live_log = settings.request_logs_dir / "request-29.log"
            live_log.write_text("", encoding="utf-8")

            worker._cleanup_ask_attachments(request, downloaded, live_log)

            self.assertFalse(downloaded_path.parent.exists())
            self.assertIn("Cleaned up downloaded ask attachments.", live_log.read_text(encoding="utf-8"))

    def test_no_change_fix_response_is_sent_as_scuba_steve(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            worktree_dir = workspace_dir / "worktree"
            worktree_dir.mkdir(parents=True, exist_ok=True)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)
            settings.post_status_updates = True

            storage = Mock()
            git_manager = Mock()
            codex_runner = Mock()
            ringcentral_client = Mock()
            ringcentral_client.is_configured = True
            worker = RequestWorker(settings, storage, git_manager, codex_runner, ringcentral_client, Mock())
            request = RequestRecord(
                id=30,
                project_slug="invoice-extractor",
                source="ringcentral",
                source_thread_id="thread-1",
                source_message_id="message-1",
                title="Check invoice",
                prompt="Check invoice",
                attachments=[],
                status=RequestStatus.RUNNING,
                branch_name=None,
                worktree_path=None,
                codex_summary=None,
                diff_summary=None,
                manual_review_reason=None,
                error_text=None,
                commit_sha=None,
                release_version=None,
                created_at="2026-04-24T16:24:42+00:00",
                updated_at="2026-04-24T16:24:42+00:00",
                started_at=None,
                completed_at=None,
                pushed_at=None,
                release_ready_notified_at=None,
                is_ask=False,
            )
            project = ProjectConfig(
                slug="invoice-extractor",
                name="InvoiceExtractor",
                repo_path=str(worktree_dir),
            )
            codex_result = CodexRunResult(
                exit_code=0,
                last_message="I checked this and no code changes are needed.",
                stdout_tail="",
                stderr_tail="",
                command_display="codex exec --full-auto",
            )

            storage.get_project.return_value = project
            git_manager.create_or_reuse_worktree.return_value = ("ringping/invoice-extractor/30", worktree_dir)
            git_manager.collect_diff_summary.return_value = ""
            git_manager.worktree_has_changes.return_value = False
            codex_runner.run.return_value = codex_result

            worker._process_request(request)

            ringcentral_client.post_chat_message.assert_called_once_with(
                "thread-1",
                "Scuba Steve says: I checked this and no code changes are needed.",
            )

    def test_ask_response_is_sent_as_scuba_steve(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            project_root = workspace_dir / "repo"
            project_root.mkdir(parents=True, exist_ok=True)
            settings = build_settings(workspace_dir)
            settings.request_logs_dir.mkdir(parents=True, exist_ok=True)
            settings.post_status_updates = True

            storage = Mock()
            codex_runner = Mock()
            ringcentral_client = Mock()
            ringcentral_client.is_configured = True
            worker = RequestWorker(settings, storage, Mock(), codex_runner, ringcentral_client, Mock())
            request = RequestRecord(
                id=31,
                project_slug="invoice-extractor",
                source="ringcentral",
                source_thread_id="thread-1",
                source_message_id="message-1",
                title="Can you check this?",
                prompt="Can you check this?",
                attachments=[],
                status=RequestStatus.RUNNING,
                branch_name=None,
                worktree_path=None,
                codex_summary=None,
                diff_summary=None,
                manual_review_reason=None,
                error_text=None,
                commit_sha=None,
                release_version=None,
                created_at="2026-04-24T16:24:42+00:00",
                updated_at="2026-04-24T16:24:42+00:00",
                started_at=None,
                completed_at=None,
                pushed_at=None,
                release_ready_notified_at=None,
                is_ask=True,
            )
            project = ProjectConfig(
                slug="invoice-extractor",
                name="InvoiceExtractor",
                repo_path=str(project_root),
            )
            codex_result = CodexRunResult(
                exit_code=0,
                last_message="Yes, the attachment landed correctly.",
                stdout_tail="",
                stderr_tail="",
                command_display="codex exec --full-auto --sandbox read-only --ephemeral",
            )

            storage.get_project.return_value = project
            codex_runner.run_read_only.return_value = codex_result

            worker._process_request(request)

            ringcentral_client.post_chat_message.assert_called_once_with(
                "thread-1",
                "Scuba Steve says: Yes, the attachment landed correctly.",
            )

    def test_scuba_steve_readiness_ask_does_not_start_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            project_root = workspace_dir / "repo"
            project_root.mkdir(parents=True, exist_ok=True)
            settings = build_settings(workspace_dir)
            settings.post_status_updates = True

            storage = Mock()
            codex_runner = Mock()
            ringcentral_client = Mock()
            ringcentral_client.is_configured = True
            worker = RequestWorker(settings, storage, Mock(), codex_runner, ringcentral_client, Mock())
            request = RequestRecord(
                id=32,
                project_slug="invoice-extractor",
                source="ringcentral",
                source_thread_id="thread-1",
                source_message_id="message-1",
                title="is scuba steve ready and willing to help the team",
                prompt="is scuba steve ready and willing to help the team",
                attachments=[],
                status=RequestStatus.RUNNING,
                branch_name=None,
                worktree_path=None,
                codex_summary=None,
                diff_summary=None,
                manual_review_reason=None,
                error_text=None,
                commit_sha=None,
                release_version=None,
                created_at="2026-04-24T16:24:42+00:00",
                updated_at="2026-04-24T16:24:42+00:00",
                started_at=None,
                completed_at=None,
                pushed_at=None,
                release_ready_notified_at=None,
                is_ask=True,
            )
            project = ProjectConfig(
                slug="invoice-extractor",
                name="InvoiceExtractor",
                repo_path=str(project_root),
            )
            storage.get_project.return_value = project

            worker._process_request(request)

            codex_runner.run_read_only.assert_not_called()
            storage.mark_request_no_changes.assert_called_once_with(
                32,
                "Handled by Scuba Steve quick response.",
                "",
            )
            ringcentral_client.post_chat_message.assert_called_once_with(
                "thread-1",
                "Scuba Steve is ready and willing to help the team.",
            )

    def test_maybe_post_status_ignores_ringcentral_post_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = build_settings(Path(temp_dir))
            settings.post_status_updates = True
            ringcentral_client = Mock()
            ringcentral_client.is_configured = True
            ringcentral_client.post_chat_message.side_effect = RingCentralError("post failed")
            worker = RequestWorker(settings, Mock(), Mock(), Mock(), ringcentral_client, Mock())
            request = type("RequestStub", (), {"source_thread_id": "thread-1"})()

            worker._maybe_post_status(request, "Scuba Steve hit an error.")

            ringcentral_client.post_chat_message.assert_called_once_with("thread-1", "Scuba Steve hit an error.")


if __name__ == "__main__":
    unittest.main()
