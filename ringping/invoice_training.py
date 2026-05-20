from __future__ import annotations

import json
import mimetypes
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

from ringping.codex_runner import CodexRunner
from ringping.config import AppSettings
from ringping.git_ops import GitError, GitWorktreeManager, GuardrailError
from ringping.models import ProjectConfig, RequestRecord, RequestStatus
from ringping.ringcentral import RingCentralClient
from ringping.storage import Storage
from ringping.utils import DISPLAY_NAME, LOG_PREFIX, utc_now_iso


class InvoiceTrainingApiError(RuntimeError):
    pass


class InvoiceTrainingApiClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.training_worker_base_url and self.settings.training_worker_token)

    def next_job(self) -> dict | None:
        payload = self._request_json(
            "POST",
            "/api/training/worker/jobs/next",
            {
                "workerId": self.settings.training_worker_id,
                "projectSlug": self.settings.training_worker_project_slug,
            },
        )
        if not isinstance(payload, dict):
            return None
        job = payload.get("job") or payload.get("trainingJob")
        if isinstance(job, dict):
            return job
        if self._job_id(payload):
            return payload
        return None

    def post_event(
        self,
        job_id: str,
        event_type: str,
        text: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        latest_output: dict | None = None,
        **extra: Any,
    ) -> None:
        payload = {
            "workerId": self.settings.training_worker_id,
            "event": {
                "type": self._normalize_event_type(event_type),
                "text": text,
            },
            **extra,
        }
        if status:
            payload["status"] = status
        if progress is not None:
            payload["progress"] = progress
        if latest_output is not None:
            payload["latestOutput"] = latest_output
        self._request_json("POST", f"/api/training/worker/jobs/{self._quote_path(job_id)}/events", payload)

    def list_commands(self, job_id: str) -> list[dict]:
        payload = self._request_json("GET", f"/api/training/worker/jobs/{self._quote_path(job_id)}/commands")
        if isinstance(payload, dict):
            commands = payload.get("commands") or payload.get("items") or []
        else:
            commands = payload
        return [item for item in commands if isinstance(item, dict)] if isinstance(commands, list) else []

    def acknowledge_command(self, job_id: str, command: dict) -> None:
        command_id = str(command.get("id") or command.get("commandId") or "").strip()
        payload = {
            "workerId": self.settings.training_worker_id,
            "commandIds": [command_id] if command_id else [],
        }
        try:
            self._request_json("POST", f"/api/training/worker/jobs/{self._quote_path(job_id)}/commands", payload)
        except InvoiceTrainingApiError:
            return

    def download_file(self, file_id: str, destination: Path) -> None:
        base_url = self.settings.training_worker_base_url.rstrip("/")
        url = f"{base_url}/api/training/worker/files/{self._quote_path(file_id)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.settings.training_worker_token}",
                "X-Training-Worker-Token": self.settings.training_worker_token,
                "X-Training-Worker-Id": self.settings.training_worker_id,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise InvoiceTrainingApiError(f"download {file_id} failed {exc.code}: {detail}") from exc
        except OSError as exc:
            raise InvoiceTrainingApiError(f"download {file_id} failed: {exc}") from exc

    def upload_output_file(self, job_id: str, file_path: Path) -> dict:
        base_url = self.settings.training_worker_base_url.rstrip("/")
        url = f"{base_url}/api/training/worker/jobs/{self._quote_path(job_id)}/output"
        boundary = f"----RingPingTrainingOutput{int(time.time() * 1000)}"
        filename = file_path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        file_bytes = file_path.read_bytes()
        body = b"".join(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    'Content-Disposition: form-data; name="file"; '
                    f'filename="{self._multipart_filename(filename)}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                file_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode("utf-8"),
            ]
        )
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.settings.training_worker_token}",
                "X-Training-Worker-Token": self.settings.training_worker_token,
                "X-Training-Worker-Id": self.settings.training_worker_id,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise InvoiceTrainingApiError(f"upload output failed {exc.code}: {detail}") from exc
        except OSError as exc:
            raise InvoiceTrainingApiError(f"upload output failed: {exc}") from exc
        payload = json.loads(response_body) if response_body else {}
        output = payload.get("output") if isinstance(payload, dict) else None
        return output if isinstance(output, dict) else {}

    def _request_json(self, method: str, path: str, payload: dict | None = None) -> Any:
        base_url = self.settings.training_worker_base_url.rstrip("/")
        url = f"{base_url}{path}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.settings.training_worker_token}",
            "X-Training-Worker-Token": self.settings.training_worker_token,
            "X-Training-Worker-Id": self.settings.training_worker_id,
            "X-Training-Worker-Protocol": "ringping-one-shot-v2",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise InvoiceTrainingApiError(f"{method} {path} failed {exc.code}: {detail}") from exc
        except OSError as exc:
            raise InvoiceTrainingApiError(f"{method} {path} failed: {exc}") from exc
        return json.loads(body) if body else {}

    def _quote_path(self, value: str) -> str:
        return urllib.parse.quote(value, safe="")

    def _multipart_filename(self, value: str) -> str:
        return str(value or "training_output.xlsx").replace("\\", "_").replace('"', "_")

    def _job_id(self, payload: dict) -> str:
        return str(payload.get("jobId") or payload.get("id") or "").strip()

    def _normalize_event_type(self, event_type: str) -> str:
        normalized = str(event_type or "").strip().lower()
        allowed = {
            "status",
            "decision",
            "assumption",
            "change",
            "warning",
            "output_ready",
            "message",
            "stop",
        }
        if normalized in allowed:
            return normalized
        if normalized in {"error", "failed", "needs_review", "needs_input"}:
            return "warning"
        if normalized in {"complete", "completed"}:
            return "output_ready"
        if normalized in {"stopped", "cancelled", "canceled"}:
            return "stop"
        return "status"


class InvoiceTrainingWorker(threading.Thread):
    def __init__(
        self,
        settings: AppSettings,
        storage: Storage,
        git_manager: GitWorktreeManager,
        codex_runner: CodexRunner,
        ringcentral_client: RingCentralClient,
    ) -> None:
        super().__init__(daemon=True, name="ringping-invoice-training-worker")
        self.settings = settings
        self.storage = storage
        self.git_manager = git_manager
        self.codex_runner = codex_runner
        self.ringcentral_client = ringcentral_client
        self.api_client = InvoiceTrainingApiClient(settings)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def wake(self) -> None:
        self._wake_event.set()

    def run(self) -> None:
        if not (self.settings.training_worker_enabled and self.api_client.is_configured):
            return

        self._post_ringcentral_status(f"{DISPLAY_NAME} is online and waiting for queued InvoiceExtractor training jobs.")
        self.wake()
        while not self._stop_event.is_set():
            self._wake_event.wait()
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self._check_once()

    def _check_once(self) -> None:
        try:
            job = self.api_client.next_job()
            if job is not None and not self._stop_event.is_set():
                self._process_job(job)
        except Exception as exc:  # noqa: BLE001
            self._post_ringcentral_status(f"{DISPLAY_NAME} could not check the InvoiceExtractor training queue: {exc}")

    def _process_job(self, job: dict) -> None:
        job_id = self._job_id(job)
        if not job_id:
            return

        cancel_event = threading.Event()
        watchdog_state: dict[str, Any] = {
            "last_event_at": time.monotonic(),
            "active_command_started_at": None,
            "active_command_text": "",
            "last_silent_warning_at": 0.0,
        }
        command_thread = threading.Thread(
            target=self._watch_commands,
            args=(job_id, cancel_event),
            daemon=True,
            name=f"ringping-training-commands-{self._safe_id(job_id)}",
        )
        command_thread.start()

        project = self.storage.get_project(self.settings.training_worker_project_slug)
        request = self._request_record_for_job(job, project)
        live_log_path = self._prepare_live_log(job_id, job, project)
        worktree_path = None
        try:
            self.api_client.post_event(
                job_id,
                "status",
                f"{DISPLAY_NAME} picked up the queued training job.",
                status="running",
                progress=10,
            )
            self._validate_online_training_project(project)
            approval_command = self._approval_command(job)
            if approval_command is not None:
                self._process_approval(job_id, project, request, approval_command)
                return
            self._post_ringcentral_status(f"Queued InvoiceExtractor training job {job_id} has started on {DISPLAY_NAME}.")
            branch_name, worktree_path = self.git_manager.create_or_reuse_worktree(project, request)
            self.api_client.post_event(
                job_id,
                "workspace",
                f"Created local training workspace on branch {branch_name}.",
                status="running",
                progress=15,
                branchName=branch_name,
                worktreePath=str(worktree_path),
            )
            downloaded_files = self._download_training_files(job, worktree_path, job_id)
            if downloaded_files:
                self.api_client.post_event(
                    job_id,
                    "status",
                    f"Downloaded {len(downloaded_files)} training file(s) for local Codex.",
                    status="running",
                    progress=20,
                )
            manifest_path = self._write_training_manifest(job_id, downloaded_files, worktree_path)
            prompt = self._build_training_prompt(job, project, branch_name, downloaded_files)
            self._ensure_codex_shell_ready(job_id)

            def mark_training_activity() -> None:
                watchdog_state["last_event_at"] = time.monotonic()

            def on_training_event(event: dict) -> None:
                self._update_training_watchdog_state(watchdog_state, event)
                self.api_client.post_event(
                    job_id,
                    str(event.get("type") or "note"),
                    str(event.get("text") or ""),
                    status="running",
                    payload=event,
                )

            watchdog_thread = threading.Thread(
                target=self._watch_plain_event_timeout,
                args=(job_id, cancel_event, watchdog_state),
                daemon=True,
                name=f"ringping-training-watchdog-{self._safe_id(job_id)}",
            )
            watchdog_thread.start()
            result = self.codex_runner.run_training(
                prompt,
                worktree_path,
                live_log_path,
                cancel_event=cancel_event,
                event_callback=on_training_event,
                activity_callback=mark_training_activity,
            )
            if self._is_blocked_agent_result(result):
                self.api_client.post_event(
                    job_id,
                    "warning",
                    f"Codex reported local command execution was blocked. {DISPLAY_NAME} is recovering local helper processes and retrying once.",
                    status="running",
                )
                self._recover_windows_process_startup(job_id)
                time.sleep(2)
                self._ensure_codex_shell_ready(job_id)
                result = self.codex_runner.run_training(
                    prompt,
                    worktree_path,
                    live_log_path,
                    cancel_event=cancel_event,
                    event_callback=on_training_event,
                    activity_callback=mark_training_activity,
                )
            if self._is_blocked_agent_result(result):
                diff_summary = self.git_manager.collect_diff_summary(worktree_path)
                self.api_client.post_event(
                    job_id,
                    "error",
                    "Codex was blocked by local command execution and did not change the parser.",
                    status="failed",
                    progress=100,
                    lastMessage=result.last_message,
                    stderrTail=result.stderr_tail,
                    diffSummary=diff_summary,
                )
                self._post_ringcentral_status(
                    f"InvoiceExtractor training job {job_id} failed because local Codex command execution is blocked."
                )
                return
            if result.timeout_reason == "cancelled":
                diff_summary = self.git_manager.collect_diff_summary(worktree_path)
                self.api_client.post_event(
                    job_id,
                    "stopped",
                    f"{DISPLAY_NAME} stopped Codex for this training job.",
                    status="stopped",
                    progress=100,
                    diffSummary=diff_summary,
                )
                self._post_ringcentral_status(f"InvoiceExtractor training job {job_id} was stopped.")
                return
            if result.exit_code != 0:
                diff_summary = self.git_manager.collect_diff_summary(worktree_path)
                self.api_client.post_event(
                    job_id,
                    "error",
                    "Codex exited with an error while running training.",
                    status="failed",
                    progress=100,
                    lastMessage=result.last_message,
                    stderrTail=result.stderr_tail,
                    diffSummary=diff_summary,
                )
                self._post_ringcentral_status(f"InvoiceExtractor training job {job_id} needs review because Codex hit an error.")
                return
            output_path = self._generate_training_output(job_id, worktree_path, manifest_path)
            diff_summary = self.git_manager.collect_diff_summary(worktree_path)
            if not self.git_manager.worktree_has_changes(worktree_path):
                if output_path is not None:
                    output = self.api_client.upload_output_file(job_id, output_path)
                    self.api_client.post_event(
                        job_id,
                        "output_ready",
                        f"Uploaded {output.get('name') or output_path.name} for review. Codex did not leave parser code changes.",
                        status="waiting_for_review",
                        progress=100,
                        latest_output=output or None,
                        lastMessage=result.last_message,
                        diffSummary=diff_summary,
                        branchName=branch_name,
                        worktreePath=str(worktree_path),
                    )
                    self._post_ringcentral_status(
                        f"InvoiceExtractor training job {job_id} produced review output but no parser code changes."
                    )
                    return
                self.api_client.post_event(
                    job_id,
                    "needs_review",
                    "Codex finished but did not leave any parser changes.",
                    status="needs_fix",
                    progress=100,
                    lastMessage=result.last_message,
                    diffSummary=diff_summary,
                )
                self._post_ringcentral_status(f"InvoiceExtractor training job {job_id} needs another pass because no parser changes were produced.")
                return
            try:
                self.git_manager.validate_guardrails(project, worktree_path)
            except GuardrailError as exc:
                self.api_client.post_event(
                    job_id,
                    "needs_review",
                    str(exc),
                    status="waiting_for_review",
                    progress=95,
                    diffSummary=diff_summary,
                )
                self._post_ringcentral_status(f"InvoiceExtractor training job {job_id} needs review before anything is pushed.")
                return
            if output_path is None:
                self.api_client.post_event(
                    job_id,
                    "needs_review",
                    f"Codex finished parser changes but did not create training_output.xlsx. Send a correction so {DISPLAY_NAME} can retry with output generation.",
                    status="needs_fix",
                    progress=95,
                    diffSummary=diff_summary,
                    branchName=branch_name,
                    worktreePath=str(worktree_path),
                )
                self._post_ringcentral_status(
                    f"InvoiceExtractor training job {job_id} needs another pass because training_output.xlsx was not generated."
                )
                return
            output = self.api_client.upload_output_file(job_id, output_path)
            self.api_client.post_event(
                job_id,
                "output_ready",
                f"Uploaded {output.get('name') or output_path.name} for review.",
                status="waiting_for_review",
                progress=99,
                latest_output=output or None,
            )
            self.api_client.post_event(
                job_id,
                "complete",
                f"{DISPLAY_NAME} finished the training job and left the local workspace ready for review.",
                status="waiting_for_review",
                progress=100,
                lastMessage=result.last_message,
                diffSummary=diff_summary,
                branchName=branch_name,
                worktreePath=str(worktree_path),
            )
            self._post_ringcentral_status(f"InvoiceExtractor training job {job_id} is ready for review.")
        except Exception as exc:  # noqa: BLE001
            try:
                self.api_client.post_event(
                    job_id,
                    "error",
                    f"{DISPLAY_NAME} failed this training job: {exc}",
                    status="failed",
                    progress=100,
                )
            except Exception:
                pass
            self._post_ringcentral_status(f"InvoiceExtractor training job {job_id} failed in {DISPLAY_NAME}: {exc}")
        finally:
            cancel_event.set()
            command_thread.join(timeout=2)

    def _approval_command(self, job: dict) -> dict | None:
        commands = job.get("commands") if isinstance(job.get("commands"), list) else []
        for command in commands:
            if not isinstance(command, dict):
                continue
            if command.get("handledAt"):
                continue
            command_type = str(command.get("type") or command.get("command") or "").strip().lower()
            if command_type == "approve":
                return command
        return None

    def _process_approval(
        self,
        job_id: str,
        project: ProjectConfig,
        request: RequestRecord,
        approval_command: dict,
    ) -> None:
        worktree_path = Path(request.worktree_path or "")
        try:
            if not (worktree_path / ".git").exists():
                raise GitError(f"Training workspace is not available for approval: {worktree_path}")

            self.git_manager.ensure_standard_excludes(worktree_path)
            self.api_client.post_event(
                job_id,
                "status",
                "Approval received. Validating parser changes before merge.",
                status="approving",
                progress=25,
                branchName=request.branch_name,
                worktreePath=str(worktree_path),
            )
            self.git_manager.validate_guardrails(project, worktree_path)
            diff_summary = self.git_manager.collect_diff_summary(worktree_path)
            self.api_client.post_event(
                job_id,
                "status",
                "Guardrails passed. Committing and pushing the approved parser fix.",
                status="approving",
                progress=55,
                diffSummary=diff_summary,
            )
            commit_sha, _release_version = self.git_manager.commit_and_push(project, request)
            self.api_client.acknowledge_command(job_id, approval_command)
            self.api_client.post_event(
                job_id,
                "status",
                f"Approved and merged parser fix {commit_sha[:12]} to {project.base_branch}.",
                status="approved",
                progress=100,
                diffSummary=diff_summary,
                branchName=request.branch_name,
                worktreePath=str(worktree_path),
                commitSha=commit_sha,
            )
            self._post_ringcentral_status(f"InvoiceExtractor training job {job_id} was approved and merged.")
        except Exception as exc:  # noqa: BLE001
            try:
                self.api_client.post_event(
                    job_id,
                    "warning",
                    f"Approval failed: {exc}",
                    status="waiting_for_review",
                    progress=100,
                    branchName=request.branch_name,
                    worktreePath=str(worktree_path),
                )
            finally:
                self.api_client.acknowledge_command(job_id, approval_command)
            self._post_ringcentral_status(f"InvoiceExtractor training job {job_id} approval failed: {exc}")

    def _watch_commands(self, job_id: str, cancel_event: threading.Event) -> None:
        seen_command_ids: set[str] = set()
        while not self._stop_event.is_set() and not cancel_event.is_set():
            try:
                commands = self.api_client.list_commands(job_id)
            except Exception:
                self._stop_event.wait(max(self.settings.training_worker_active_command_poll_seconds, 1))
                continue
            for command in commands:
                command_id = str(command.get("id") or command.get("commandId") or json.dumps(command, sort_keys=True))
                if command_id in seen_command_ids:
                    continue
                seen_command_ids.add(command_id)
                command_type = str(command.get("type") or command.get("command") or "").strip().lower()
                if command_type in {"stop", "cancel", "abort"}:
                    cancel_event.set()
                    self.api_client.acknowledge_command(job_id, command)
                    return
                if command_type == "message":
                    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
                    text = str(payload.get("text") or payload.get("message") or "").strip()
                    if text:
                        self.api_client.post_event(
                            job_id,
                            "message",
                            f"{DISPLAY_NAME} received this online message while Codex was running: {text}",
                            status="running",
                        )
                    self.api_client.acknowledge_command(job_id, command)
            self._stop_event.wait(max(self.settings.training_worker_active_command_poll_seconds, 1))

    def _update_training_watchdog_state(self, state: dict[str, Any], event: dict) -> None:
        now = time.monotonic()
        state["last_event_at"] = now
        event_type = str(event.get("type") or "").strip().lower()
        text = str(event.get("text") or "").strip()
        if event_type != "monitor":
            return
        if text.startswith("Codex is running command:"):
            state["active_command_started_at"] = now
            state["active_command_text"] = text.replace("Codex is running command:", "", 1).strip()
            state["last_silent_warning_at"] = 0.0
            return
        if (
            text.startswith("Command finished with exit")
            or text.startswith("Codex finished this run")
            or text.startswith("Local command exited")
        ):
            state["active_command_started_at"] = None
            state["active_command_text"] = ""
            state["last_silent_warning_at"] = 0.0

    def _watch_plain_event_timeout(self, job_id: str, cancel_event: threading.Event, watchdog_state: dict[str, Any]) -> None:
        timeout_seconds = self.settings.training_worker_plain_event_timeout_seconds
        if timeout_seconds <= 0:
            return
        while not self._stop_event.is_set() and not cancel_event.is_set():
            if self._handle_training_silence_timeout(job_id, cancel_event, watchdog_state, timeout_seconds, time.monotonic()):
                return
            self._stop_event.wait(5)

    def _handle_training_silence_timeout(
        self,
        job_id: str,
        cancel_event: threading.Event,
        watchdog_state: dict[str, Any],
        timeout_seconds: int,
        now: float,
    ) -> bool:
        last_event_at = float(watchdog_state.get("last_event_at") or now)
        if now - last_event_at < timeout_seconds:
            return False
        active_command_started_at = watchdog_state.get("active_command_started_at")
        if active_command_started_at is not None:
            command_text = str(watchdog_state.get("active_command_text") or "the current local command").strip()
            if len(command_text) > 220:
                command_text = f"{command_text[:217]}..."
            last_warning_at = float(watchdog_state.get("last_silent_warning_at") or 0.0)
            if now - last_warning_at >= timeout_seconds:
                watchdog_state["last_silent_warning_at"] = now
                self.api_client.post_event(
                    job_id,
                    "warning",
                    (
                        "Codex has not produced useful local output recently, but a local command is still active. "
                        f"{DISPLAY_NAME} is leaving Codex running and waiting for the command to finish: {command_text}"
                    ),
                    status="running",
                )
            return False
        else:
            message = (
                "Codex has not produced useful local output recently and no local command is running, "
                f"so {DISPLAY_NAME} stopped it before it could run away."
            )
        self.api_client.post_event(job_id, "needs_input", message, status="stopped", progress=100)
        cancel_event.set()
        return True

    def _download_training_files(self, job: dict, worktree_path: Path, job_id: str) -> list[tuple[dict, Path]]:
        downloaded: list[tuple[dict, Path]] = []
        for vendor in self._job_vendors(job):
            vendor_name = str(vendor.get("vendorName") or "vendor").strip()
            vendor_dir = worktree_path / "ringping_attachments" / f"training-{self._safe_id(job_id)}" / self._safe_id(vendor_name)
            for file_payload in self._vendor_files(vendor):
                file_id = str(file_payload.get("id") or file_payload.get("driveFileId") or "").strip()
                if not file_id:
                    continue
                filename = self._safe_filename(str(file_payload.get("name") or file_payload.get("filename") or f"{file_id}.pdf"))
                destination = vendor_dir / filename
                self.api_client.download_file(file_id, destination)
                downloaded.append((file_payload, destination))
        return downloaded

    def _write_training_manifest(self, job_id: str, downloaded_files: list[tuple[dict, Path]], worktree_path: Path) -> Path | None:
        if not downloaded_files:
            return None
        manifest_path = worktree_path / ".ringping_artifacts" / f"training-files-{self._safe_id(job_id)}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        files = []
        for file_payload, path in downloaded_files:
            files.append(
                {
                    "path": str(path),
                    "name": str(file_payload.get("name") or file_payload.get("filename") or path.name),
                    "sourceUrl": str(file_payload.get("webViewLink") or file_payload.get("webContentLink") or "").strip(),
                    "driveFileId": str(file_payload.get("id") or file_payload.get("driveFileId") or "").strip(),
                }
            )
        manifest_path.write_text(json.dumps({"files": files}, indent=2), encoding="utf-8")
        return manifest_path

    def _generate_training_output(self, job_id: str, worktree_path: Path, manifest_path: Path | None) -> Path | None:
        script_path = worktree_path / "scripts" / "generate_training_output.py"
        if not script_path.is_file():
            return self._find_training_output(worktree_path)

        command = ["py", str(script_path), "--output", "training_output.xlsx"]
        if manifest_path is not None:
            command.extend(["--manifest", str(manifest_path)])
        else:
            command.extend(["--input", str(worktree_path / "ringping_attachments" / f"training-{self._safe_id(job_id)}")])

        self.api_client.post_event(
            job_id,
            "status",
            "Generating fresh training_output.xlsx from the updated online parser.",
            status="running",
            progress=90,
        )
        try:
            result = subprocess.run(
                command,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            self.api_client.post_event(
                job_id,
                "warning",
                f"Could not generate training_output.xlsx automatically: {exc}",
                status="running",
            )
            return self._find_training_output(worktree_path)

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            self.api_client.post_event(
                job_id,
                "warning",
                "Automatic training_output.xlsx generation failed: " + detail[-1200:],
                status="running",
            )
            return self._find_training_output(worktree_path)

        output_path = self._find_training_output(worktree_path)
        if output_path is not None:
            self.api_client.post_event(
                job_id,
                "status",
                f"Generated {output_path.name} from the updated online parser.",
                status="running",
                progress=95,
            )
        return output_path

    def _build_training_prompt(
        self,
        job: dict,
        project: ProjectConfig,
        branch_name: str,
        downloaded_files: list[tuple[dict, Path]] | None = None,
    ) -> str:
        vendor_name = str(job.get("vendorName") or job.get("vendor") or "").strip()
        user_prompt = str(job.get("prompt") or job.get("message") or job.get("instructions") or "").strip()
        files = job.get("files") or job.get("attachments") or job.get("documents") or []
        vendors = self._job_vendors(job)
        lines = [
            "You are working on InvoiceExtractorOnline vendor training from the online app.",
            "This job must improve the online parser repository, not the desktop InvoiceExtractor app.",
            f"Project: {project.name}",
            f"Repository path: {project.repo_path}",
            f"Base branch: {project.base_branch}",
            f"Training branch: {branch_name}",
            f"Online job id: {self._job_id(job)}",
        ]
        if project.codex_prompt_prefix:
            lines.extend(["", "Project-specific instructions:", project.codex_prompt_prefix])
        lines.extend([
            "",
            "Plain-English event requirement:",
            '- Emit progress lines exactly like `TRAINING_EVENT {"type":"decision","text":"..."}` '
            + "whenever you choose a field source, reject an assumption, change parser behavior, create output, or hit a blocker.",
            "- Do this before and after meaningful commands so the online page can show the team what is happening.",
            "",
            "Training task:",
        ])
        if vendor_name:
            lines.append(f"- Vendor: {vendor_name}")
        if vendors:
            lines.append("- Vendors in this training job:")
            for vendor in vendors:
                name = str(vendor.get("vendorName") or "").strip()
                terms = str(vendor.get("terms") or "").strip()
                if terms:
                    lines.append(f"  - {name} (terms: {terms})")
                elif name:
                    lines.append(f"  - {name}")
        if user_prompt:
            lines.append(user_prompt)
        else:
            lines.append("Train or repair the vendor parser using the online job data below.")
        if isinstance(files, list) and files:
            lines.extend(["", "Online file references:"])
            for item in files:
                if isinstance(item, dict):
                    lines.append("- " + json.dumps(item, sort_keys=True))
                else:
                    lines.append(f"- {item}")
        if downloaded_files:
            lines.extend(["", "Downloaded local training files:"])
            for file_payload, path in downloaded_files:
                name = str(file_payload.get("name") or file_payload.get("filename") or path.name)
                source_url = str(file_payload.get("webViewLink") or file_payload.get("webContentLink") or "").strip()
                if source_url:
                    lines.append(f"- {name}: {path} (source PDF URL: {source_url})")
                else:
                    lines.append(f"- {name}: {path}")
        lines.extend(
            [
                "",
                "Constraints:",
                "- On this Windows runner, run exactly one shell command at a time; wait for it to finish before starting another.",
                "- Keep parser changes tightly scoped to this vendor training job.",
                "- Prefer vendor-specific parser rules and training output over generic parser rewrites.",
                "- Mandatory final output step: after parser changes and validation, parse every downloaded training PDF again with the updated parser and write a fresh QuickBooks review workbook named exactly `training_output.xlsx` at the repository root.",
                "- In `training_output.xlsx`, each invoice's first `Bill No.` cell must hyperlink to that invoice's source PDF URL when an online file reference includes `webViewLink` or `webContentLink`.",
                "- Do not finish until `training_output.xlsx` exists and reflects the latest parser behavior, including stock-order collapsing/highlighting rules.",
                "- Do not commit or push.",
                "- Leave the workspace reviewable.",
            ]
        )
        if project.test_command:
            lines.append(f"- Run this validation command if relevant: {project.test_command}")
        return "\n".join(lines).strip()

    def _validate_online_training_project(self, project: ProjectConfig) -> None:
        expected_slug = "invoice-extractor-online"
        if project.slug != expected_slug:
            raise RuntimeError(
                "Online training worker is misconfigured: "
                f"project slug is {project.slug!r}, expected {expected_slug!r}. "
                "Refusing to train against the desktop InvoiceExtractor project."
            )

        repo_path = Path(project.repo_path).resolve()
        required_paths = [
            repo_path / "package.json",
            repo_path / "src" / "app",
            repo_path / "api" / "python_parser" / "invoice_parser.py",
        ]
        missing = [str(path) for path in required_paths if not path.exists()]
        if missing:
            raise RuntimeError(
                "Online training worker is pointed at the wrong repository. "
                "Missing online extractor file(s): " + ", ".join(missing)
            )

        desktop_markers = [
            repo_path / "invoice_extractor_gui.py",
            repo_path / "InvoiceExtractor.spec",
        ]
        present_desktop_markers = [str(path) for path in desktop_markers if path.exists()]
        if present_desktop_markers:
            raise RuntimeError(
                "Online training worker is pointed at the desktop InvoiceExtractor app. "
                "Refusing to run online parser training there: " + ", ".join(present_desktop_markers)
            )

    def _job_vendors(self, job: dict) -> list[dict]:
        vendors = job.get("vendors")
        if isinstance(vendors, list):
            return [vendor for vendor in vendors if isinstance(vendor, dict)]
        return [job]

    def _vendor_files(self, vendor: dict) -> list[dict]:
        files = vendor.get("files") or vendor.get("attachments") or vendor.get("documents") or []
        return [file for file in files if isinstance(file, dict)] if isinstance(files, list) else []

    def _find_training_output(self, worktree_path: Path) -> Path | None:
        preferred = worktree_path / "training_output.xlsx"
        if preferred.is_file():
            return preferred
        candidates = [
            path
            for path in worktree_path.glob("training_output*.xlsx")
            if path.is_file() and not path.name.lower().startswith("training_output_backup")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _request_record_for_job(self, job: dict, project: ProjectConfig) -> RequestRecord:
        job_id = self._job_id(job)
        numeric_id = zlib.crc32(job_id.encode("utf-8")) & 0x7FFFFFFF
        safe_id = self._safe_id(job_id)
        title = str(job.get("title") or job.get("vendorName") or f"Training {job_id}").strip()
        worktree_path = self.settings.worktrees_dir / project.slug / f"training-{safe_id}"
        return RequestRecord(
            id=numeric_id,
            project_slug=project.slug,
            source="invoice-online",
            source_thread_id=None,
            source_message_id=job_id,
            title=title[:120],
            prompt=str(job.get("prompt") or job.get("message") or title),
            attachments=[],
            status=RequestStatus.RUNNING,
            branch_name=f"ringping/training/{safe_id}",
            worktree_path=str(worktree_path),
            codex_summary=None,
            diff_summary=None,
            manual_review_reason=None,
            error_text=None,
            commit_sha=None,
            release_version=None,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            started_at=utc_now_iso(),
            completed_at=None,
            pushed_at=None,
            release_ready_notified_at=None,
        )

    def _prepare_live_log(self, job_id: str, job: dict, project: ProjectConfig) -> Path:
        self.settings.request_logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.settings.request_logs_dir / f"training-{self._safe_id(job_id)}.log"
        lines = [
            f"{LOG_PREFIX} Training job {job_id}",
            f"{LOG_PREFIX} Project: {project.name}",
            f"{LOG_PREFIX} Online worker: {self.settings.training_worker_id}",
            "",
            f"{LOG_PREFIX} Job payload:",
            json.dumps(job, indent=2, sort_keys=True),
            "",
        ]
        log_path.write_text("\n".join(lines), encoding="utf-8")
        log_path.with_name(f"{log_path.stem}.raw{log_path.suffix}").write_text("\n".join(lines), encoding="utf-8")
        return log_path

    def _post_ringcentral_status(self, text: str) -> None:
        chat_id = self.settings.training_worker_ringcentral_chat_id
        if not (chat_id and self.settings.post_status_updates and self.ringcentral_client.is_configured):
            return
        try:
            self.ringcentral_client.post_chat_message(chat_id, text)
        except Exception:
            return

    def _ensure_codex_shell_ready(self, job_id: str) -> None:
        ok, detail = self._powershell_smoke_test()
        if ok:
            return
        self.api_client.post_event(
            job_id,
            "warning",
            f"PowerShell startup check failed before Codex launch: {detail}. {DISPLAY_NAME} is attempting local recovery.",
            status="running",
        )
        self._recover_windows_process_startup(job_id)
        for attempt in range(1, 4):
            time.sleep(2)
            ok, detail = self._powershell_smoke_test()
            if ok:
                self.api_client.post_event(
                    job_id,
                    "status",
                    f"PowerShell startup recovered. {DISPLAY_NAME} is starting Codex.",
                    status="running",
                )
                return
            self.api_client.post_event(
                job_id,
                "warning",
                f"PowerShell startup retry {attempt}/3 failed: {detail}",
                status="running",
            )
        raise RuntimeError("Local PowerShell startup check failed before launching Codex.")

    def _powershell_smoke_test(self) -> tuple[bool, str]:
        if not shutil.which("powershell.exe"):
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

    def _recover_windows_process_startup(self, job_id: str) -> None:
        if not shutil.which("taskkill.exe"):
            return
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process_names = (
            "Dell.TechHub.Diagnostics.SubAgent.exe",
            "Dell.TechHub.Instrumentation.SubAgent.exe",
            "DPM.exe",
        )
        stopped: list[str] = []
        for process_name in process_names:
            try:
                result = subprocess.run(
                    ["taskkill.exe", "/F", "/IM", process_name],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    creationflags=creationflags,
                )
            except Exception:
                continue
            if result.returncode == 0:
                stopped.append(process_name)
        if stopped:
            self.api_client.post_event(
                job_id,
                "status",
                "Stopped local helper process(es) before retrying Codex: " + ", ".join(stopped),
                status="running",
            )

    def _format_windows_exit_code(self, exit_code: int) -> str:
        if exit_code < 0:
            unsigned = exit_code + (1 << 32)
            return f"{exit_code} (0x{unsigned:08X})"
        if exit_code > 0x7FFFFFFF:
            signed = exit_code - (1 << 32)
            return f"{signed} (0x{exit_code:08X})"
        return str(exit_code)

    def _job_id(self, job: dict) -> str:
        return str(job.get("jobId") or job.get("id") or "").strip()

    def _is_blocked_agent_result(self, result) -> bool:
        combined = "\n".join(
            part for part in (result.last_message, result.stdout_tail, result.stderr_tail) if part
        ).lower()
        blocked_markers = (
            "blocked:",
            "blocked by the execution environment",
            "blocked before i could",
            "blocked by the runner",
            "i'm blocked by the runner",
            "i'm blocked by the execution environment",
            "i’m blocked by the execution environment",
            "local command execution was blocked",
        )
        shell_markers = (
            "command execution is failing",
            "command failed before execution",
            "every shell process fails",
            "every shell invocation",
            "every shell_command call",
            "process startup",
            "powershell startup",
            "powershell is failing to initialize",
            "shell_command",
            "shell process fails",
            "shell execution is failing",
            "-1073741502",
            "0xc0000142",
        )
        return any(marker in combined for marker in blocked_markers) and any(
            marker in combined for marker in shell_markers
        )

    def _safe_id(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip("-._")
        return cleaned[:80] or "job"

    def _safe_filename(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", value).strip(" .")
        return cleaned or "training-file.bin"
