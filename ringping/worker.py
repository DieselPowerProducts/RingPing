from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ringping.codex_runner import CodexRunner
from ringping.config import AppSettings
from ringping.email_notifier import ReviewEmailError, ReviewEmailNotifier
from ringping.git_ops import GitWorktreeManager, GuardrailError
from ringping.models import ProjectConfig, RequestRecord
from ringping.ringcentral import RingCentralClient, RingCentralError
from ringping.storage import Storage
from ringping.utils import (
    DISPLAY_NAME,
    LOG_PREFIX,
    codex_credits_available,
    detect_codex_reset_time,
    format_local_time,
    format_scuba_steve_status,
    interactive_request_console_available,
    scuba_steve_quick_reply,
)


REQUEST_ATTACHMENTS_DIR = "ringping_attachments"
LEGACY_REQUEST_ATTACHMENTS_DIR = ".ringping_artifacts"


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
        self._using_fallback = False

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            if not self.settings.codex_fallback_command and not codex_credits_available():
                self._using_fallback = True
                self._stop_event.wait(60)
                continue
            if self._using_fallback and codex_credits_available():
                self._using_fallback = False
            request = self.storage.claim_next_pending_request()
            if request is None:
                wait = 60 if self._using_fallback else self.settings.poll_interval_seconds
                self._stop_event.wait(wait)
                continue
            self._process_request(request)

    def _process_request(self, request: RequestRecord) -> None:
        if request.is_ask:
            self._process_ask_request(request)
            return
        project = self.storage.get_project(request.project_slug)
        downloaded_attachments = []
        live_log_path = None
        diff_summary = ""
        try:
            branch_name, worktree_path = self.git_manager.create_or_reuse_worktree(project, request)
            self.storage.update_request_workspace(request.id, branch_name, str(worktree_path))
            live_log_path = self._prepare_live_log(request, project, worktree_path)
            self._append_live_status(live_log_path, "Request claimed. Downloading attachments.")
            downloaded_attachments = self._download_request_attachments(request, worktree_path)
            if downloaded_attachments:
                self._append_live_status(live_log_path, self._format_downloaded_attachments(downloaded_attachments))
            self._ensure_codex_shell_ready(live_log_path)
            self._append_live_status(live_log_path, "Starting Codex.")

            codex_result = self.codex_runner.run(project, request, worktree_path, downloaded_attachments, live_log_path)
            if self._is_blocked_agent_result(codex_result):
                self._append_live_status(
                    live_log_path,
                    "Codex tool runner reported shell startup failure. Recovering local helpers and retrying once.",
                )
                self._recover_windows_process_startup(live_log_path)
                time.sleep(2)
                self._ensure_codex_shell_ready(live_log_path)
                codex_result = self.codex_runner.run(project, request, worktree_path, downloaded_attachments, live_log_path)

            summary_parts = []
            if live_log_path:
                summary_parts.append(f"Live log:\n{live_log_path}")
                summary_parts.append(f"Raw log:\n{self._raw_log_path(live_log_path)}")
            if downloaded_attachments:
                summary_parts.append(self._format_downloaded_attachments(downloaded_attachments))
            if codex_result.last_message:
                summary_parts.append("Last agent message:\n" + codex_result.last_message.strip())
            if codex_result.stdout_tail:
                summary_parts.append("Stdout tail:\n" + codex_result.stdout_tail.strip())
            if codex_result.stderr_tail:
                summary_parts.append("Stderr tail:\n" + codex_result.stderr_tail.strip())
            if codex_result.timed_out:
                if codex_result.timeout_reason == "idle_after_changes":
                    summary_parts.append(
                        f"{DISPLAY_NAME} note:\nCodex became idle after producing a local diff, so {DISPLAY_NAME} continued from the existing changes."
                    )
                else:
                    summary_parts.append(f"{DISPLAY_NAME} note:\nCodex timed out before finishing the run.")
            summary_parts.append("Command:\n" + codex_result.command_display)

            self._append_live_status(live_log_path, "Codex finished. Checking workspace changes.")
            validation_note = self._run_validation(project, worktree_path)
            if validation_note:
                summary_parts.append(validation_note)
                self._append_live_status(live_log_path, "Validation command finished.")

            diff_summary = self.git_manager.collect_diff_summary(worktree_path)
            summary = "\n\n".join(part for part in summary_parts if part).strip()
            timed_out_with_changes = codex_result.timed_out and self.git_manager.worktree_has_changes(worktree_path)

            if self.settings.codex_fallback_command and self.settings.codex_fallback_command in codex_result.command_display:
                self._using_fallback = True

            if codex_result.timed_out and not timed_out_with_changes:
                error_text = (
                    "Codex timed out after becoming idle."
                    if codex_result.timeout_reason == "idle_after_changes"
                    else "Codex timed out before completing the request."
                )
                self.storage.mark_request_error(request.id, error_text, summary, diff_summary)
                self._maybe_post_status(
                    request,
                    f"Scuba Steve is stuck on '{request.title}'. Review is needed before retrying.",
                )
                return

            if timed_out_with_changes:
                self._append_live_status(live_log_path, "Codex stalled after producing a diff. Salvaging existing changes.")

            if codex_result.exit_code != 0 and not timed_out_with_changes:
                rate_limit_text = self._build_rate_limit_message(codex_result)
                error_text = "Codex exited with a non-zero status."
                status_text = f"Scuba Steve hit an error while working on '{request.title}'. Review is needed before retrying."
                if rate_limit_text:
                    self._using_fallback = True
                    error_text = rate_limit_text
                    status_text = rate_limit_text
                self.storage.mark_request_error(request.id, error_text, summary, diff_summary)
                self._maybe_post_status(request, status_text)
                return

            if not self.git_manager.worktree_has_changes(worktree_path):
                blocked_reason = self._build_blocked_agent_noop_message(codex_result)
                if blocked_reason:
                    self.storage.mark_request_error(request.id, blocked_reason, summary, diff_summary)
                    self._maybe_post_status(
                        request,
                        f"Scuba Steve was blocked by a local tool/session problem while working on '{request.title}'. Review is needed before retrying.",
                    )
                    return
                no_change_summary = summary or "Codex completed but left no local diff."
                self.storage.mark_request_no_changes(request.id, no_change_summary, diff_summary)
                agent_reply = codex_result.last_message.strip() if codex_result.last_message else ""
                status_text = agent_reply or f"Scuba Steve reviewed '{request.title}' but did not produce a code change."
                self._maybe_post_status(request, status_text)
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
                    "Scuba Steve thinks this might affect more of the base code than we want, so he is contacting REAL Mike to make sure the fix is safe.",
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
                self._append_live_status(live_log_path, "Found a fix. Preparing push and release.")
                self._maybe_post_status(
                    request,
                    "Scuba Steve found the issue and a fix. He is working on it and will let you know when it is ready for you to update.",
                )
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
                if release_version:
                    self._append_live_status(live_log_path, "Push complete. Waiting for release readiness.")
                else:
                    self._append_live_status(live_log_path, "Push complete. Update is ready.")
                if self.settings.post_status_updates and self.ringcentral_client.is_configured and fresh_request.source_thread_id:
                    if not release_version:
                        self.ringcentral_client.post_chat_message(
                            fresh_request.source_thread_id,
                            "Scuba Steve is done. It is ready for you to update!",
                        )
                return

            self.storage.mark_request_ready(request.id, summary, diff_summary)
            self._maybe_post_status(
                request,
                f"Scuba Steve prepared a fix for '{request.title}'. It is ready for review and push.",
            )
        except Exception as exc:  # noqa: BLE001
            summary_parts = []
            if live_log_path:
                summary_parts.append(f"Live log:\n{live_log_path}")
                summary_parts.append(f"Raw log:\n{self._raw_log_path(live_log_path)}")
            if downloaded_attachments:
                summary_parts.append(self._format_downloaded_attachments(downloaded_attachments))
            self.storage.mark_request_error(request.id, str(exc), "\n\n".join(summary_parts).strip(), diff_summary)
            self._maybe_post_status(
                request,
                f"Scuba Steve failed while processing '{request.title}'. Review is needed before retrying.",
            )

    def _process_ask_request(self, request: RequestRecord) -> None:
        project = self.storage.get_project(request.project_slug)
        downloaded_attachments = []
        live_log_path = None
        try:
            quick_reply = scuba_steve_quick_reply(request.prompt)
            if quick_reply:
                self.storage.mark_request_no_changes(request.id, f"Handled by {DISPLAY_NAME} quick response.", "")
                self._maybe_post_status(request, quick_reply)
                return

            project_root = Path(project.repo_path)
            live_log_path = self._prepare_live_log(request, project, project_root)
            self._append_live_status(live_log_path, "Ask request claimed. Downloading attachments.")
            downloaded_attachments = self._download_ask_attachments(request, project_root)
            if downloaded_attachments:
                self._append_live_status(live_log_path, self._format_downloaded_attachments(downloaded_attachments))
            self._append_live_status(live_log_path, "Starting Codex.")
            prompt = self._build_ask_prompt(request, downloaded_attachments)
            codex_result = self.codex_runner.run_read_only(prompt, project_root, live_log_path)

            summary_parts = []
            if live_log_path:
                summary_parts.append(f"Live log:\n{live_log_path}")
                summary_parts.append(f"Raw log:\n{self._raw_log_path(live_log_path)}")
            if downloaded_attachments:
                summary_parts.append(self._format_downloaded_attachments(downloaded_attachments))
            if codex_result.last_message:
                summary_parts.append("Last agent message:\n" + codex_result.last_message.strip())
            if codex_result.stdout_tail:
                summary_parts.append("Stdout tail:\n" + codex_result.stdout_tail.strip())
            if codex_result.stderr_tail:
                summary_parts.append("Stderr tail:\n" + codex_result.stderr_tail.strip())
            if codex_result.timed_out:
                summary_parts.append(f"{DISPLAY_NAME} note:\nCodex timed out before finishing the ask request.")
            summary_parts.append("Command:\n" + codex_result.command_display)
            summary = "\n\n".join(part for part in summary_parts if part).strip()

            if self.settings.codex_fallback_command and self.settings.codex_fallback_command in codex_result.command_display:
                self._using_fallback = True

            if codex_result.timed_out:
                self.storage.mark_request_error(request.id, "Ask request timed out.", summary)
                self._maybe_post_status(
                    request,
                    "Scuba Steve ran into a problem because that ask request took too long to finish. Please try again.",
                )
                return

            if codex_result.exit_code != 0:
                rate_limit_text = self._build_rate_limit_message(codex_result)
                error_text = rate_limit_text or "Ask command exited with a non-zero status."
                self.storage.mark_request_error(request.id, error_text, summary)
                self._maybe_post_status(
                    request,
                    rate_limit_text or "Scuba Steve ran into a problem trying to answer that ask request.",
                )
                return

            reply = codex_result.last_message.strip() if codex_result.last_message else ""
            self.storage.mark_request_no_changes(request.id, summary or "No response from agent.", "")
            self._maybe_post_status(request, reply or "Scuba Steve was not able to get a response, please try again.")
        except Exception as exc:  # noqa: BLE001
            summary_parts = []
            if live_log_path:
                summary_parts.append(f"Live log:\n{live_log_path}")
                summary_parts.append(f"Raw log:\n{self._raw_log_path(live_log_path)}")
            if downloaded_attachments:
                summary_parts.append(self._format_downloaded_attachments(downloaded_attachments))
            self.storage.mark_request_error(request.id, str(exc), "\n\n".join(summary_parts).strip())
            self._maybe_post_status(request, f"Scuba Steve ran into a problem trying to answer your question: {exc}")
        finally:
            self._cleanup_ask_attachments(request, downloaded_attachments, live_log_path)

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
        text = self._format_scuba_steve_status(text)
        try:
            self.ringcentral_client.post_chat_message(request.source_thread_id, text)
        except Exception:
            return

    def _format_scuba_steve_status(self, text: str) -> str:
        return format_scuba_steve_status(text)

    def _download_request_attachments(self, request: RequestRecord, worktree_path: Path):
        if not request.attachments:
            return []
        artifacts_root = worktree_path / REQUEST_ATTACHMENTS_DIR / f"request-{request.id}"
        self.git_manager.ensure_excluded(worktree_path, f"{REQUEST_ATTACHMENTS_DIR}/")
        return self._download_attachments_to_directory(request, artifacts_root)

    def _download_ask_attachments(self, request: RequestRecord, project_root: Path):
        if not request.attachments:
            return []
        if not project_root.exists():
            raise RuntimeError(f"Repo path does not exist: {project_root}")
        if (project_root / ".git").exists():
            self.git_manager.ensure_excluded(project_root, f"{REQUEST_ATTACHMENTS_DIR}/")
        artifacts_root = project_root / REQUEST_ATTACHMENTS_DIR / f"request-{request.id}"
        return self._download_attachments_to_directory(request, artifacts_root)

    def _download_attachments_to_directory(self, request: RequestRecord, artifacts_root: Path):
        downloaded = []
        artifacts_root.mkdir(parents=True, exist_ok=True)
        for attachment in request.attachments:
            try:
                local_path = self.ringcentral_client.download_attachment(attachment, artifacts_root)
            except RingCentralError:
                local_path = self._copy_cached_attachment(request, attachment.name, artifacts_root)
                if local_path is None:
                    raise
            downloaded.append((attachment, local_path))
        return downloaded

    def _copy_cached_attachment(self, request: RequestRecord, attachment_name: str, artifacts_root: Path) -> Path | None:
        cached_path = self._find_cached_attachment(request, attachment_name, artifacts_root)
        if cached_path is None:
            return None
        target_path = artifacts_root / self._safe_attachment_name(attachment_name)
        if cached_path.resolve() == target_path.resolve():
            return target_path
        shutil.copy2(cached_path, target_path)
        return target_path

    def _find_cached_attachment(self, request: RequestRecord, attachment_name: str, artifacts_root: Path) -> Path | None:
        safe_name = self._safe_attachment_name(attachment_name).lower()
        project_worktrees = self.settings.worktrees_dir / request.project_slug
        if not project_worktrees.exists():
            return None
        artifacts_root = artifacts_root.resolve()
        cache_patterns = [
            f"*/{REQUEST_ATTACHMENTS_DIR}/request-*/*",
            f"*/{LEGACY_REQUEST_ATTACHMENTS_DIR}/request-*/*",
        ]
        candidates = [
            path
            for pattern in cache_patterns
            for path in project_worktrees.glob(pattern)
            if path.is_file() and path.name.lower() == safe_name and path.stat().st_size > 0
        ]
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
            resolved = path.resolve()
            if resolved == artifacts_root / path.name:
                continue
            try:
                if resolved.is_relative_to(artifacts_root):
                    continue
            except ValueError:
                pass
            return path
        return None

    def _cleanup_ask_attachments(
        self,
        request: RequestRecord,
        downloaded_attachments: list[tuple[object, Path]],
        live_log_path: Path | None,
    ) -> None:
        request_dir_name = f"request-{request.id}"
        allowed_parents = {REQUEST_ATTACHMENTS_DIR, LEGACY_REQUEST_ATTACHMENTS_DIR}
        deleted = False
        for directory in {path.parent for _, path in downloaded_attachments}:
            if directory.name != request_dir_name or directory.parent.name not in allowed_parents:
                continue
            shutil.rmtree(directory, ignore_errors=True)
            deleted = True
        if deleted:
            self._append_live_status(live_log_path, "Cleaned up downloaded ask attachments.")

    def _safe_attachment_name(self, name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", name or "attachment.bin").strip(" .")
        return cleaned or "attachment.bin"

    def _format_downloaded_attachments(self, downloaded_attachments) -> str:
        return "Downloaded attachments:\n" + "\n".join(
            f"{attachment.name} -> {path}" for attachment, path in downloaded_attachments
        )

    def _build_ask_prompt(self, request: RequestRecord, downloaded_attachments) -> str:
        prompt_parts = [request.prompt.strip()]
        if downloaded_attachments:
            prompt_parts.append("")
            prompt_parts.append("Downloaded attachments:")
            for attachment, path in downloaded_attachments:
                prompt_parts.append(f"- {attachment.name}: {path}")
            prompt_parts.extend(
                [
                    "",
                    "Read only the listed attachments unless the question explicitly asks for something else.",
                ]
            )
        else:
            prompt_parts.extend(
                [
                    "",
                    "No attachments were provided for this request.",
                ]
            )
        prompt_parts.extend(
            [
                "",
                "This is a fast read-only question, not a fix request.",
                "Answer directly and briefly from the minimum evidence needed.",
                "Write for a non-technical business user such as accounting or operations.",
                "Use plain English, not developer language.",
                "Do not mention function names, file names, test names, commands, or internal implementation details unless the user explicitly asks for technical detail.",
                "If the question is effectively yes/no, start the answer with a clear Yes or No.",
                "Lead with the practical takeaway first, then a short explanation.",
                "Keep the answer short and easy to understand.",
                "Usually 1-4 short read-only commands or file reads should be enough.",
                "Start with the most likely source file or test and stop once you can answer confidently.",
                "For binary attachments such as PDFs or spreadsheets, do not dump or read the whole raw file; use metadata, headers, or a targeted parser.",
                "If the user is testing whether attachment downloads work, verifying that the listed files exist, are non-empty, and have the expected file signatures is enough.",
                "Do not do broad repo sweeps or scan `ringping_attachments`, `.ringping_artifacts`, `training`, or `required` unless attachments were provided or the question explicitly requires those directories.",
                "Do not modify repository files.",
                "Do not commit or push anything.",
                f"Do not delete downloaded ask attachments; {DISPLAY_NAME} removes those temporary files after the answer.",
                "If there are attachments, report whether they landed correctly only if that is relevant to the question.",
            ]
        )
        return "\n".join(prompt_parts).strip()

    def _prepare_live_log(self, request: RequestRecord, project: ProjectConfig, cwd: Path) -> Path | None:
        log_path = self.settings.request_logs_dir / f"request-{request.id}.log"
        raw_log_path = self._raw_log_path(log_path)
        log_lines = [
            f"{LOG_PREFIX} Request {request.id}",
            f"{LOG_PREFIX} Project: {project.name}",
            f"{LOG_PREFIX} Title: {request.title}",
            f"{LOG_PREFIX} Mode: {'ask' if request.is_ask else 'fix'}",
            f"{LOG_PREFIX} Working directory: {cwd}",
            f"{LOG_PREFIX} Raw log: {raw_log_path}",
            "",
            f"{LOG_PREFIX} Prompt:",
            request.prompt.strip(),
            "",
        ]
        log_path.write_text("\n".join(log_lines), encoding="utf-8")
        raw_log_path.write_text("\n".join(log_lines), encoding="utf-8")
        if self.settings.request_console_enabled and interactive_request_console_available():
            self._open_live_console(request, log_path)
        return log_path

    def _open_live_console(self, request: RequestRecord, log_path: Path) -> None:
        if os.name != "nt":
            return
        title = f"RingPing Request {request.id}"
        escaped_log_path = str(log_path).replace("'", "''")
        script = (
            f"$Host.UI.RawUI.WindowTitle = '{title}'; "
            f"Write-Host 'Tailing {escaped_log_path}'; "
            f"Get-Content -LiteralPath '{escaped_log_path}' -Wait"
        )
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen(
            ["powershell.exe", "-NoExit", "-Command", script],
            cwd=self.settings.workspace_dir,
            creationflags=creationflags,
        )

    def _append_live_status(self, log_path: Path | None, text: str) -> None:
        if log_path is None:
            return
        with log_path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"{LOG_PREFIX} {text}\n")

    def _raw_log_path(self, log_path: Path) -> Path:
        return log_path.with_name(f"{log_path.stem}.raw{log_path.suffix}")

    def _ensure_codex_shell_ready(self, live_log_path: Path | None) -> None:
        """Verify Windows can start PowerShell before handing the request to Codex."""
        ok, detail = self._powershell_smoke_test()
        if ok:
            return

        self._append_live_status(
            live_log_path,
            f"PowerShell startup check failed before Codex launch: {detail}",
        )
        self._append_live_status(
            live_log_path,
            "Attempting local recovery by stopping known high-handle Dell helper processes.",
        )
        self._recover_windows_process_startup(live_log_path)

        for attempt in range(1, 4):
            time.sleep(2)
            ok, detail = self._powershell_smoke_test()
            if ok:
                self._append_live_status(live_log_path, "PowerShell startup recovered. Continuing with Codex.")
                return
            self._append_live_status(
                live_log_path,
                f"PowerShell startup retry {attempt}/3 failed: {detail}",
            )

        raise RuntimeError(
            "Local PowerShell startup check failed before launching Codex. "
            f"{DISPLAY_NAME} stopped known Dell helper processes but Windows still could not start PowerShell."
        )

    def _powershell_smoke_test(self) -> tuple[bool, str]:
        if os.name != "nt":
            return True, ""
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "$PSVersionTable.PSVersion.ToString()",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=creationflags,
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        if result.returncode == 0:
            return True, ""
        return False, (
            f"exit {self._format_windows_exit_code(result.returncode)}; "
            f"stdout={result.stdout.strip()!r}; stderr={result.stderr.strip()!r}"
        )

    def _format_windows_exit_code(self, exit_code: int) -> str:
        if exit_code < 0:
            unsigned = exit_code + (1 << 32)
            return f"{exit_code} (0x{unsigned:08X})"
        if exit_code > 0x7FFFFFFF:
            signed = exit_code - (1 << 32)
            return f"{signed} (0x{exit_code:08X})"
        return str(exit_code)

    def _recover_windows_process_startup(self, live_log_path: Path | None) -> None:
        if os.name != "nt":
            return
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process_names = (
            "codex.exe",
            "Dell.TechHub.Diagnostics.SubAgent.exe",
            "Dell.TechHub.Instrumentation.SubAgent.exe",
            "DPM.exe",
        )
        for process_name in process_names:
            result = subprocess.run(
                ["taskkill.exe", "/F", "/IM", process_name],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=creationflags,
            )
            output = " ".join(
                part.strip()
                for part in (result.stdout, result.stderr)
                if part and part.strip()
            )
            if result.returncode == 0:
                self._append_live_status(live_log_path, f"Stopped {process_name}.")
            elif output:
                self._append_live_status(live_log_path, f"{process_name}: {output}")

    def _build_rate_limit_message(self, codex_result) -> str | None:
        combined = "\n".join(
            part for part in (codex_result.last_message, codex_result.stdout_tail, codex_result.stderr_tail) if part
        )
        lowered = combined.lower()
        if not any(token in lowered for token in ("credit", "limit", "quota", "rate limit")):
            return None
        reset_time = detect_codex_reset_time(combined)
        if reset_time is None:
            return "Scuba Steve is super tired and going to take a nap for a bit. Please send your fix again later."
        return (
            f"Scuba Steve is super tired and going to take a nap until {format_local_time(reset_time)}, "
            "please send your fix again after that time."
        )

    def _build_blocked_agent_noop_message(self, codex_result) -> str | None:
        if not self._is_blocked_agent_result(codex_result):
            return None
        return "Codex reported it was blocked by a local tool/session failure and left no code changes."

    def _is_blocked_agent_result(self, codex_result) -> bool:
        combined = "\n".join(
            part for part in (codex_result.last_message, codex_result.stdout_tail, codex_result.stderr_tail) if part
        )
        lowered = combined.lower()
        blocked_markers = (
            "blocked before i could inspect",
            "blocked before i could",
            "blocked by the runner",
            "left the working tree unchanged",
            "no files were modified",
            "shell_command",
            "shell process fails",
            "shell execution is failing",
            "powershell startup",
            "0xc0000142",
            "-1073741502",
            "command_execution",
            "cmd /c echo hello",
        )
        if "blocked" not in lowered:
            return False
        return any(marker in lowered for marker in blocked_markers)
