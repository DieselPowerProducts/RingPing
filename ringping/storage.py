from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from ringping.models import (
    IncomingRequest,
    ProjectConfig,
    ProjectGuardrails,
    ProjectSnapshot,
    RequestAttachment,
    RequestRecord,
    RequestStatus,
)
from ringping.utils import utc_now_iso


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    slug TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    base_branch TEXT NOT NULL,
                    remote_name TEXT NOT NULL,
                    push_mode TEXT NOT NULL,
                    auto_push INTEGER NOT NULL,
                    release_on_push INTEGER NOT NULL DEFAULT 0,
                    release_version_strategy TEXT NOT NULL DEFAULT 'none',
                    release_notes_template TEXT NOT NULL DEFAULT '',
                    release_manifest_url TEXT NOT NULL DEFAULT '',
                    ringcentral_chat_ids TEXT NOT NULL,
                    codex_prompt_prefix TEXT NOT NULL,
                    test_command TEXT NOT NULL,
                    review_command TEXT NOT NULL,
                    guardrails_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            project_columns = {row["name"] for row in connection.execute("PRAGMA table_info(projects)").fetchall()}
            if "release_on_push" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN release_on_push INTEGER NOT NULL DEFAULT 0")
            if "release_version_strategy" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN release_version_strategy TEXT NOT NULL DEFAULT 'none'")
            if "release_notes_template" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN release_notes_template TEXT NOT NULL DEFAULT ''")
            if "release_manifest_url" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN release_manifest_url TEXT NOT NULL DEFAULT ''")
            if "guardrails_json" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN guardrails_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_slug TEXT NOT NULL REFERENCES projects(slug),
                    source TEXT NOT NULL,
                    source_thread_id TEXT,
                    source_message_id TEXT,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    branch_name TEXT,
                    worktree_path TEXT,
                    codex_summary TEXT,
                    diff_summary TEXT,
                    manual_review_reason TEXT,
                    error_text TEXT,
                    commit_sha TEXT,
                    release_version TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    pushed_at TEXT,
                    release_ready_notified_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_source_message ON requests(source, source_message_id)"
            )
            request_columns = {row["name"] for row in connection.execute("PRAGMA table_info(requests)").fetchall()}
            if "attachments_json" not in request_columns:
                connection.execute("ALTER TABLE requests ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
            if "release_version" not in request_columns:
                connection.execute("ALTER TABLE requests ADD COLUMN release_version TEXT")
            if "release_ready_notified_at" not in request_columns:
                connection.execute("ALTER TABLE requests ADD COLUMN release_ready_notified_at TEXT")
            if "manual_review_reason" not in request_columns:
                connection.execute("ALTER TABLE requests ADD COLUMN manual_review_reason TEXT")

    def sync_projects(self, projects: list[ProjectConfig]) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            for project in projects:
                existing = connection.execute(
                    "SELECT auto_push FROM projects WHERE slug = ?",
                    (project.slug,),
                ).fetchone()
                if existing:
                    connection.execute(
                        """
                        UPDATE projects
                        SET name = ?, repo_path = ?, base_branch = ?, remote_name = ?, push_mode = ?,
                            release_on_push = ?, release_version_strategy = ?, release_notes_template = ?,
                            release_manifest_url = ?,
                            ringcentral_chat_ids = ?, codex_prompt_prefix = ?, test_command = ?,
                            review_command = ?, guardrails_json = ?, updated_at = ?
                        WHERE slug = ?
                        """,
                        (
                            project.name,
                            project.repo_path,
                            project.base_branch,
                            project.remote_name,
                            project.push_mode,
                            1 if project.release_on_push else 0,
                            project.release_version_strategy,
                            project.release_notes_template,
                            project.release_manifest_url,
                            json.dumps(project.ringcentral_chat_ids),
                            project.codex_prompt_prefix,
                            project.test_command,
                            project.review_command,
                            json.dumps(project.guardrails.to_dict()),
                            now,
                            project.slug,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO projects (
                            slug, name, repo_path, base_branch, remote_name, push_mode, auto_push,
                            release_on_push, release_version_strategy, release_notes_template, release_manifest_url,
                            ringcentral_chat_ids, codex_prompt_prefix, test_command, review_command, guardrails_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            project.slug,
                            project.name,
                            project.repo_path,
                            project.base_branch,
                            project.remote_name,
                            project.push_mode,
                            1 if project.auto_push else 0,
                            1 if project.release_on_push else 0,
                            project.release_version_strategy,
                            project.release_notes_template,
                            project.release_manifest_url,
                            json.dumps(project.ringcentral_chat_ids),
                            project.codex_prompt_prefix,
                            project.test_command,
                            project.review_command,
                            json.dumps(project.guardrails.to_dict()),
                            now,
                            now,
                        ),
                    )

    def list_projects(self) -> list[ProjectConfig]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE").fetchall()
        return [self._row_to_project(row) for row in rows]

    def get_project(self, slug: str) -> ProjectConfig:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown project slug: {slug}")
        return self._row_to_project(row)

    def set_project_auto_push(self, slug: str, enabled: bool) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE projects SET auto_push = ?, updated_at = ? WHERE slug = ?",
                (1 if enabled else 0, now, slug),
            )

    def create_request(self, incoming: IncomingRequest) -> RequestRecord:
        request, _created = self.create_request_result(incoming)
        return request

    def create_request_result(self, incoming: IncomingRequest) -> tuple[RequestRecord, bool]:
        now = utc_now_iso()
        title = incoming.title.strip() or incoming.prompt.strip().splitlines()[0][:80]
        with self._lock, self._connect() as connection:
            if incoming.source_message_id:
                existing = connection.execute(
                    "SELECT * FROM requests WHERE source = ? AND source_message_id = ?",
                    (incoming.source, incoming.source_message_id),
                ).fetchone()
                if existing is not None:
                    return self._row_to_request(existing), False

            cursor = connection.execute(
                """
                INSERT INTO requests (
                    project_slug, source, source_thread_id, source_message_id, title, prompt,
                    attachments_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incoming.project_slug,
                    incoming.source,
                    incoming.source_thread_id,
                    incoming.source_message_id,
                    title,
                    incoming.prompt.strip(),
                    json.dumps([attachment.to_dict() for attachment in incoming.attachments]),
                    RequestStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM requests WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._row_to_request(row), True

    def list_requests_for_project(self, project_slug: str, limit: int = 25) -> list[RequestRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE project_slug = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (project_slug, limit),
            ).fetchall()
        return [self._row_to_request(row) for row in rows]

    def list_project_snapshots(self, limit_per_project: int = 20) -> list[ProjectSnapshot]:
        return [
            ProjectSnapshot(project=project, requests=self.list_requests_for_project(project.slug, limit_per_project))
            for project in self.list_projects()
        ]

    def get_request(self, request_id: int) -> RequestRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown request id: {request_id}")
        return self._row_to_request(row)

    def claim_next_pending_request(self) -> RequestRecord | None:
        now = utc_now_iso()
        with self._lock:
            connection = self._connect()
            try:
                connection.isolation_level = None
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM requests WHERE status = ? ORDER BY id ASC LIMIT 1",
                    (RequestStatus.PENDING.value,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                connection.execute(
                    """
                    UPDATE requests
                    SET status = ?, started_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (RequestStatus.RUNNING.value, now, now, row["id"]),
                )
                connection.execute("COMMIT")
                claimed = connection.execute("SELECT * FROM requests WHERE id = ?", (row["id"],)).fetchone()
                return self._row_to_request(claimed)
            finally:
                connection.close()

    def update_request_workspace(self, request_id: int, branch_name: str, worktree_path: str) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE requests SET branch_name = ?, worktree_path = ?, updated_at = ? WHERE id = ?",
                (branch_name, worktree_path, now, request_id),
            )

    def mark_request_ready(
        self,
        request_id: int,
        codex_summary: str,
        diff_summary: str,
        manual_review_reason: str | None = None,
    ) -> None:
        self._finalize_request(
            request_id,
            RequestStatus.READY,
            codex_summary,
            diff_summary,
            None,
            None,
            manual_review_reason=manual_review_reason,
        )

    def mark_request_no_changes(self, request_id: int, codex_summary: str, diff_summary: str) -> None:
        self._finalize_request(request_id, RequestStatus.NO_CHANGES, codex_summary, diff_summary, None, None)

    def mark_request_error(self, request_id: int, error_text: str, codex_summary: str = "", diff_summary: str = "") -> None:
        self._finalize_request(request_id, RequestStatus.ERROR, codex_summary, diff_summary, error_text, None)

    def mark_request_pushed(
        self,
        request_id: int,
        commit_sha: str,
        codex_summary: str,
        diff_summary: str,
        release_version: str | None = None,
    ) -> None:
        self._finalize_request(request_id, RequestStatus.PUSHED, codex_summary, diff_summary, None, commit_sha, release_version)

    def mark_request_pending(self, request_id: int) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE requests
                SET status = ?, error_text = NULL, updated_at = ?, completed_at = NULL
                WHERE id = ?
                """,
                (RequestStatus.PENDING.value, now, request_id),
            )

    def reset_request_for_retry(self, request_id: int) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE requests
                SET status = ?, branch_name = NULL, worktree_path = NULL, commit_sha = NULL,
                    release_version = NULL, error_text = NULL, codex_summary = NULL, diff_summary = NULL,
                    manual_review_reason = NULL,
                    updated_at = ?, started_at = NULL, completed_at = NULL, pushed_at = NULL,
                    release_ready_notified_at = NULL
                WHERE id = ?
                """,
                (RequestStatus.PENDING.value, now, request_id),
            )

    def _finalize_request(
        self,
        request_id: int,
        status: RequestStatus,
        codex_summary: str,
        diff_summary: str,
        error_text: str | None,
        commit_sha: str | None,
        release_version: str | None = None,
        manual_review_reason: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE requests
                SET status = ?, codex_summary = ?, diff_summary = ?, manual_review_reason = ?, error_text = ?, commit_sha = ?, release_version = ?,
                    updated_at = ?, completed_at = ?,
                    pushed_at = CASE WHEN ? = ? THEN ? ELSE pushed_at END
                WHERE id = ?
                """,
                (
                    status.value,
                    codex_summary,
                    diff_summary,
                    manual_review_reason,
                    error_text,
                    commit_sha,
                    release_version,
                    now,
                    now,
                    status.value,
                    RequestStatus.PUSHED.value,
                    now,
                    request_id,
                ),
            )

    def list_pending_release_notifications(self) -> list[RequestRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status = ? AND release_version IS NOT NULL AND release_ready_notified_at IS NULL
                ORDER BY id ASC
                """
                ,
                (RequestStatus.PUSHED.value,),
            ).fetchall()
        return [self._row_to_request(row) for row in rows]

    def mark_release_ready_notified(self, request_id: int) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE requests SET release_ready_notified_at = ?, updated_at = ? WHERE id = ?",
                (now, now, request_id),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _row_to_project(self, row: sqlite3.Row) -> ProjectConfig:
        return ProjectConfig(
            slug=row["slug"],
            name=row["name"],
            repo_path=row["repo_path"],
            base_branch=row["base_branch"],
            remote_name=row["remote_name"],
            push_mode=row["push_mode"],
            auto_push=bool(row["auto_push"]),
            release_on_push=bool(row["release_on_push"]),
            release_version_strategy=row["release_version_strategy"],
            release_notes_template=row["release_notes_template"],
            release_manifest_url=row["release_manifest_url"],
            ringcentral_chat_ids=json.loads(row["ringcentral_chat_ids"]),
            codex_prompt_prefix=row["codex_prompt_prefix"],
            test_command=row["test_command"],
            review_command=row["review_command"],
            guardrails=ProjectGuardrails.from_dict(json.loads(row["guardrails_json"] or "{}")),
        )

    def _row_to_request(self, row: sqlite3.Row) -> RequestRecord:
        return RequestRecord(
            id=row["id"],
            project_slug=row["project_slug"],
            source=row["source"],
            source_thread_id=row["source_thread_id"],
            source_message_id=row["source_message_id"],
            title=row["title"],
            prompt=row["prompt"],
            attachments=[RequestAttachment.from_dict(item) for item in json.loads(row["attachments_json"] or "[]")],
            status=RequestStatus(row["status"]),
            branch_name=row["branch_name"],
            worktree_path=row["worktree_path"],
            codex_summary=row["codex_summary"],
            diff_summary=row["diff_summary"],
            manual_review_reason=row["manual_review_reason"],
            error_text=row["error_text"],
            commit_sha=row["commit_sha"],
            release_version=row["release_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            pushed_at=row["pushed_at"],
            release_ready_notified_at=row["release_ready_notified_at"],
        )
