from __future__ import annotations

from pathlib import Path

from ringping.config import AppSettings
from ringping.git_ops import GitError, GitWorktreeManager
from ringping.models import IncomingRequest, ProjectSnapshot, RequestRecord, RequestStatus
from ringping.ringcentral import RingCentralClient
from ringping.storage import Storage


class AppController:
    def __init__(
        self,
        settings: AppSettings,
        storage: Storage,
        git_manager: GitWorktreeManager,
        ringcentral_client: RingCentralClient,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.git_manager = git_manager
        self.ringcentral_client = ringcentral_client

    def list_project_snapshots(self, limit_per_project: int = 20) -> list[ProjectSnapshot]:
        return self.storage.list_project_snapshots(limit_per_project)

    def create_manual_request(self, project_slug: str, title: str, prompt: str) -> RequestRecord:
        return self.storage.create_request(
            IncomingRequest(project_slug=project_slug, title=title.strip(), prompt=prompt.strip(), source="manual")
        )

    def ingest_ringcentral_payload(self, payload: dict) -> RequestRecord | None:
        projects = self.storage.list_projects()
        incoming = self.ringcentral_client.extract_incoming_request(
            payload,
            projects,
            command_prefix=self.settings.ringcentral_command_prefix,
        )
        if incoming is None:
            return None
        request, created = self.storage.create_request_result(incoming)
        if created:
            self._post_status_update(request, "Got it, Gimme a quick sec")
        return request

    def set_project_auto_push(self, project_slug: str, enabled: bool) -> None:
        self.storage.set_project_auto_push(project_slug, enabled)

    def open_review_target(self, request_id: int) -> None:
        request = self.storage.get_request(request_id)
        if not request.worktree_path:
            raise GitError("This request does not have a worktree yet.")
        project = self.storage.get_project(request.project_slug)
        self.git_manager.run_review_command(project, Path(request.worktree_path))

    def push_request(self, request_id: int) -> str:
        request = self.storage.get_request(request_id)
        if request.status != RequestStatus.READY:
            raise GitError(f"Request is not ready to push. Current status: {request.status.value}")
        project = self.storage.get_project(request.project_slug)
        commit_sha, release_version = self.git_manager.commit_and_push(
            project,
            request,
            skip_guardrails=bool(request.manual_review_reason),
        )
        summary = (request.codex_summary or "").strip()
        summary = (summary + f"\n\nPushed commit: {commit_sha}").strip()
        if release_version:
            summary = (summary + f"\nRelease requested: v{release_version}").strip()
        diff_summary = request.diff_summary or ""
        self.storage.mark_request_pushed(request.id, commit_sha, summary, diff_summary, release_version=release_version)

        if self.settings.post_status_updates and self.ringcentral_client.is_configured and request.source_thread_id:
            if release_version:
                text = "Ok that update is building now, I'll let you know when it's ready."
            else:
                text = f"RingPing pushed '{request.title}' for {project.name}. Commit: {commit_sha[:7]} Branch: {request.branch_name}"
            self.ringcentral_client.post_chat_message(request.source_thread_id, text)
        if release_version:
            return f"Pushed {commit_sha[:7]} and requested release v{release_version}."
        return commit_sha

    def retry_request(self, request_id: int) -> None:
        request = self.storage.get_request(request_id)
        project = self.storage.get_project(request.project_slug)
        if request.worktree_path or request.branch_name:
            self.git_manager.reset_request_workspace(project, request)
        self.storage.reset_request_for_retry(request_id)

    def get_request_detail_text(self, request_id: int) -> str:
        request = self.storage.get_request(request_id)
        parts = [
            f"Title: {request.title}",
            f"Status: {request.status.value}",
            f"Project: {request.project_slug}",
            f"Created: {request.created_at}",
        ]
        if request.attachments:
            parts.append("Attachments: " + ", ".join(attachment.name for attachment in request.attachments))
        if request.branch_name:
            parts.append(f"Branch: {request.branch_name}")
        if request.worktree_path:
            parts.append(f"Worktree: {request.worktree_path}")
        if request.commit_sha:
            parts.append(f"Commit: {request.commit_sha}")
        if request.release_version:
            parts.append(f"Release version: {request.release_version}")
        if request.release_ready_notified_at:
            parts.append(f"Release ready notified: {request.release_ready_notified_at}")
        if request.manual_review_reason:
            parts.extend(["", "Manual review required:", request.manual_review_reason.strip()])
        parts.extend(["", "Prompt:", request.prompt.strip()])
        if request.codex_summary:
            parts.extend(["", "Codex summary:", request.codex_summary.strip()])
        if request.error_text:
            parts.extend(["", "Error:", request.error_text.strip()])
        if request.diff_summary:
            parts.extend(["", "Diff summary:", request.diff_summary.strip()])
        return "\n".join(parts).strip()

    def get_request_diff_text(self, request_id: int) -> str:
        request = self.storage.get_request(request_id)
        if request.worktree_path:
            return self.git_manager.read_full_diff(Path(request.worktree_path))
        return request.diff_summary or "No diff available yet."

    def webhook_banner(self) -> str:
        return f"Webhook: {self.settings.local_webhook_url} | Public target: {self.settings.public_webhook_url}"

    def _post_status_update(self, request: RequestRecord, text: str) -> None:
        if not (self.settings.post_status_updates and self.ringcentral_client.is_configured and request.source_thread_id):
            return
        self.ringcentral_client.post_chat_message(request.source_thread_id, text)
