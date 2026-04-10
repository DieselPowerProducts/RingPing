from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class RequestStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    PUSHED = "pushed"
    ERROR = "error"
    NO_CHANGES = "no_changes"


@dataclass(slots=True)
class RequestAttachment:
    id: str
    name: str
    content_uri: str
    type: str = "File"

    @classmethod
    def from_dict(cls, payload: dict) -> "RequestAttachment":
        return cls(
            id=str(payload.get("id") or "").strip(),
            name=str(payload.get("name") or "attachment").strip(),
            content_uri=str(payload.get("contentUri") or "").strip(),
            type=str(payload.get("type") or "File").strip(),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "contentUri": self.content_uri,
            "type": self.type,
        }


@dataclass(slots=True)
class ProjectGuardrails:
    block_deletions: bool = False
    max_changed_files: int = 0
    allowed_paths: list[str] = field(default_factory=list)
    blocked_paths: list[str] = field(default_factory=list)
    prompt_rules: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict | None) -> "ProjectGuardrails":
        payload = payload or {}
        return cls(
            block_deletions=bool(payload.get("block_deletions", False)),
            max_changed_files=int(payload.get("max_changed_files", 0) or 0),
            allowed_paths=[str(item).strip() for item in payload.get("allowed_paths", []) if str(item).strip()],
            blocked_paths=[str(item).strip() for item in payload.get("blocked_paths", []) if str(item).strip()],
            prompt_rules=[str(item).strip() for item in payload.get("prompt_rules", []) if str(item).strip()],
        )

    def to_dict(self) -> dict:
        return {
            "block_deletions": self.block_deletions,
            "max_changed_files": self.max_changed_files,
            "allowed_paths": list(self.allowed_paths),
            "blocked_paths": list(self.blocked_paths),
            "prompt_rules": list(self.prompt_rules),
        }


@dataclass(slots=True)
class ProjectConfig:
    slug: str
    name: str
    repo_path: str
    base_branch: str = "main"
    remote_name: str = "origin"
    push_mode: str = "branch"
    auto_push: bool = False
    release_on_push: bool = False
    release_version_strategy: str = "none"
    release_notes_template: str = ""
    release_manifest_url: str = ""
    ringcentral_chat_ids: list[str] = field(default_factory=list)
    codex_prompt_prefix: str = ""
    test_command: str = ""
    review_command: str = ""
    guardrails: ProjectGuardrails = field(default_factory=ProjectGuardrails)

    @classmethod
    def from_dict(cls, payload: dict) -> "ProjectConfig":
        return cls(
            slug=str(payload["slug"]).strip(),
            name=str(payload.get("name") or payload["slug"]).strip(),
            repo_path=str(payload["repo_path"]).strip(),
            base_branch=str(payload.get("base_branch", "main")).strip(),
            remote_name=str(payload.get("remote_name", "origin")).strip(),
            push_mode=str(payload.get("push_mode", "branch")).strip(),
            auto_push=bool(payload.get("auto_push", False)),
            release_on_push=bool(payload.get("release_on_push", False)),
            release_version_strategy=str(payload.get("release_version_strategy", "none")).strip(),
            release_notes_template=str(payload.get("release_notes_template", "")).strip(),
            release_manifest_url=str(payload.get("release_manifest_url", "")).strip(),
            ringcentral_chat_ids=[str(item).strip() for item in payload.get("ringcentral_chat_ids", []) if str(item).strip()],
            codex_prompt_prefix=str(payload.get("codex_prompt_prefix", "")).strip(),
            test_command=str(payload.get("test_command", "")).strip(),
            review_command=str(payload.get("review_command", "")).strip(),
            guardrails=ProjectGuardrails.from_dict(payload.get("guardrails")),
        )


@dataclass(slots=True)
class RequestRecord:
    id: int
    project_slug: str
    source: str
    source_thread_id: str | None
    source_message_id: str | None
    title: str
    prompt: str
    attachments: list[RequestAttachment]
    status: RequestStatus
    branch_name: str | None
    worktree_path: str | None
    codex_summary: str | None
    diff_summary: str | None
    manual_review_reason: str | None
    error_text: str | None
    commit_sha: str | None
    release_version: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None
    pushed_at: str | None
    release_ready_notified_at: str | None

    @property
    def can_push(self) -> bool:
        return self.status == RequestStatus.READY

    @property
    def can_retry(self) -> bool:
        return self.status in {RequestStatus.ERROR, RequestStatus.NO_CHANGES, RequestStatus.READY}


@dataclass(slots=True)
class IncomingRequest:
    project_slug: str
    title: str
    prompt: str
    attachments: list[RequestAttachment] = field(default_factory=list)
    source: str = "manual"
    source_thread_id: str | None = None
    source_message_id: str | None = None


@dataclass(slots=True)
class CodexRunResult:
    exit_code: int
    last_message: str
    stdout_tail: str
    stderr_tail: str
    command_display: str


@dataclass(slots=True)
class ProjectSnapshot:
    project: ProjectConfig
    requests: list[RequestRecord]
