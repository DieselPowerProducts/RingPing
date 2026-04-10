from __future__ import annotations

import threading
from pathlib import Path

from ringping.codex_runner import CodexRunner
from ringping.config import AppSettings
from ringping.email_notifier import ReviewEmailError, ReviewEmailNotifier
from ringping.git_ops import GitWorktreeManager, GuardrailError
from ringping.models import ProjectConfig, RequestRecord
from ringping.ringcentral import RingCentralClient
from ringping.storage import Storage
from ringping.utils import detect_codex_reset_time, format_local_time


class RequestWorker(threading.Thread):
    def __init__(
        self,
        settings: AppSettings,
        storage: Storage,
        git_manager: GitWorktreeManager,
        codex_runner: CodexRunner,
        ringcentral_client: RingCentralClient,
        review_email_notifier: ReviewEmailNotifier,
    ) -> None:
        super().__init__(daemon=True, name="ringping-worker")
        self.settings = settings
        self.storage = storage
        self.git_manager = git_manager
        self.codex_runner = codex_runner
        self.ringcentral_client = ringcentral_client
        self.review_email_notifier = review_email_notifier
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            request = self.storage.claim_next_pending_request()
            if request is None:
                self._stop_event.wait(self.settings.poll_interval_seconds)
                continue
            self._process_request(request)

    def _process_request(self, request: RequestRecord) -> None:
        project = self.storage.get_project(request.project_slug)
        try:
            branch_name, worktree_path = self.git_manager.create_or_reuse_worktree(project, request)
            self.storage.update_request_workspace(request.id, branch_name, str(worktree_path))
            downloaded_attachments = self._download_request_attachments(request, worktree_path)

            codex_result = self.codex_runner.run(project, request, worktree_path, downloaded_attachments)
            summary_parts = []
            if downloaded_attachments:
                summary_parts.append(
                    "Downloaded attachments:\n"
                    + "\n".join(f"{attachment.name} -> {path}" for attachment, path in downloaded_attachments)
                )
            if codex_result.last_message:
                summary_parts.append("Last agent message:\n" + codex_result.last_message.strip())
            if codex_result.stdout_tail:
                summary_parts.append("Stdout tail:\n" + codex_result.stdout_tail.strip())
            if codex_result.stderr_tail:
                summary_parts.append("Stderr tail:\n" + codex_result.stderr_tail.strip())
            summary_parts.append("Command:\n" + codex_result.command_display)

            validation_note = self._run_validation(project, worktree_path)
            if validation_note:
                summary_parts.append(validation_note)

            diff_summary = self.git_manager.collect_diff_summary(worktree_path)
            summary = "\n\n".join(part for part in summary_parts if part).strip()

            if codex_result.exit_code != 0:
                rate_limit_text = self._build_rate_limit_message(codex_result)
                error_text = "Codex exited with a non-zero status."
                status_text = f"RingPing hit an error while working on '{request.title}'. Review is needed before retrying."
                if rate_limit_text:
                    error_text = rate_limit_text
                    status_text = rate_limit_text
                self.storage.mark_request_error(request.id, error_text, summary, diff_summary)
                self._maybe_post_status(request, status_text)
                return

            if not self.git_manager.worktree_has_changes(worktree_path):
                no_change_summary = summary or "Codex completed but left no local diff."
                self.storage.mark_request_no_changes(request.id, no_change_summary, diff_summary)
                self._maybe_post_status(
                    request,
                    f"RingPing reviewed '{request.title}' but did not produce a code change.",
                )
                return

            fresh_request = self.storage.get_request(request.id)
            try:
                self.git_manager.validate_guardrails(project, worktree_path)
            except GuardrailError as exc:
                review_reason = str(exc).strip()
                held_summary = summary
                if review_reason:
                    held_summary = (held_summary + "\n\nManual review required:\n" + review_reason).strip()
                self.storage.mark_request_ready(
                    request.id,
                    held_summary,
                    diff_summary,
                    manual_review_reason=review_reason,
                )
                held_request = self.storage.get_request(request.id)
                self._maybe_post_status(
                    request,
                    "This looks like it might affect more of the base code than we want, lets contact REAL Mike to make sure the fix is safe. Contacting him now",
                )
                try:
                    self.review_email_notifier.send_manual_review_email(project, held_request, review_reason)
                except ReviewEmailError as exc:
                    updated_summary = (held_request.codex_summary or "").strip()
                    updated_summary = (updated_summary + f"\n\nReview email alert failed:\n{exc}").strip()
                    self.storage.mark_request_ready(
                        request.id,
                        updated_summary,
                        diff_summary,
                        manual_review_reason=review_reason,
                    )
                return

            if project.auto_push:
                commit_sha, release_version = self.git_manager.commit_and_push(project, fresh_request)
                pushed_summary = (summary + f"\n\nPushed commit: {commit_sha}").strip()
                if release_version:
                    pushed_summary = (pushed_summary + f"\nRelease requested: v{release_version}").strip()
                self.storage.mark_request_pushed(
                    request.id,
                    commit_sha,
                    pushed_summary,
                    diff_summary,
                    release_version=release_version,
                )
                if self.settings.post_status_updates and self.ringcentral_client.is_configured and fresh_request.source_thread_id:
                    if release_version:
                        text = "Ok that update is building now, I'll let you know when it's ready."
                    else:
                        text = f"RingPing auto-pushed '{fresh_request.title}' for {project.name}. Commit: {commit_sha[:7]} Branch: {branch_name}"
                    self.ringcentral_client.post_chat_message(fresh_request.source_thread_id, text)
                return

            self.storage.mark_request_ready(request.id, summary, diff_summary)
            self._maybe_post_status(
                request,
                f"RingPing prepared a fix for '{request.title}'. It is ready for review and push.",
            )
        except Exception as exc:  # noqa: BLE001
            self.storage.mark_request_error(request.id, str(exc))
            self._maybe_post_status(
                request,
                f"RingPing failed while processing '{request.title}'. Review is needed before retrying.",
            )

    def _run_validation(self, project: ProjectConfig, worktree_path: Path) -> str:
        if not project.test_command:
            return ""
        exit_code, output = self.git_manager.run_shell_command(project.test_command, worktree_path)
        heading = f"Validation command `{project.test_command}` exited with {exit_code}:"
        if output:
            return heading + "\n" + output
        return heading

    def _maybe_post_status(self, request: RequestRecord, text: str) -> None:
        if not (self.settings.post_status_updates and self.ringcentral_client.is_configured and request.source_thread_id):
            return
        self.ringcentral_client.post_chat_message(request.source_thread_id, text)

    def _download_request_attachments(self, request: RequestRecord, worktree_path: Path):
        if not request.attachments:
            return []
        artifacts_root = worktree_path / ".ringping_artifacts" / f"request-{request.id}"
        self.git_manager.ensure_excluded(worktree_path, ".ringping_artifacts/")
        downloaded = []
        for attachment in request.attachments:
            local_path = self.ringcentral_client.download_attachment(attachment, artifacts_root)
            downloaded.append((attachment, local_path))
        return downloaded

    def _build_rate_limit_message(self, codex_result) -> str | None:
        combined = "\n".join(
            part for part in (codex_result.last_message, codex_result.stdout_tail, codex_result.stderr_tail) if part
        )
        lowered = combined.lower()
        if not any(token in lowered for token in ("credit", "limit", "quota", "rate limit")):
            return None
        reset_time = detect_codex_reset_time(combined)
        if reset_time is None:
            return "Im super tired and going to take a nap for a bit, please send your fix again later."
        return (
            f"Im super tired and going to take a nap until {format_local_time(reset_time)}, "
            "please send your fix again after that time."
        )
