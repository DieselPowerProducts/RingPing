from __future__ import annotations

import fnmatch
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ringping.models import ProjectConfig, RequestRecord
from ringping.utils import tail_text, utc_now_iso


class GitError(RuntimeError):
    pass


class GuardrailError(GitError):
    pass


DEFAULT_EPHEMERAL_EXCLUDES = (
    ".ringping_artifacts/",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
)


@dataclass(slots=True)
class WorktreeChange:
    status: str
    path: str
    original_path: str | None = None

    @property
    def is_destructive(self) -> bool:
        return any(flag in self.status for flag in ("D", "R", "T"))

    @property
    def paths(self) -> list[str]:
        values = []
        if self.original_path:
            values.append(self.original_path)
        if self.path:
            values.append(self.path)
        return values


class GitWorktreeManager:
    def __init__(self, worktrees_root: Path) -> None:
        self.worktrees_root = Path(worktrees_root)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

    def create_or_reuse_worktree(self, project: ProjectConfig, request: RequestRecord) -> tuple[str, Path]:
        repo_path = Path(project.repo_path)
        if not repo_path.exists():
            raise GitError(f"Repo path does not exist: {repo_path}")
        if not (repo_path / ".git").exists():
            raise GitError(f"Repo path is not a git repository: {repo_path}")
        self._refresh_remote_branch(repo_path, project)

        branch_name = request.branch_name or f"ringping/{project.slug}/{request.id}"
        worktree_path = Path(request.worktree_path) if request.worktree_path else self.worktrees_root / project.slug / str(request.id)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        if (worktree_path / ".git").exists():
            self.ensure_standard_excludes(worktree_path)
            return branch_name, worktree_path
        if worktree_path.exists() and any(worktree_path.iterdir()):
            raise GitError(f"Worktree path already exists and is not empty: {worktree_path}")

        if self._branch_exists(repo_path, branch_name):
            self._run_git(repo_path, "worktree", "add", "--force", str(worktree_path), branch_name)
            self.ensure_standard_excludes(worktree_path)
            return branch_name, worktree_path

        base_ref = self._preferred_base_ref(repo_path, project)
        try:
            self._run_git(repo_path, "worktree", "add", "--force", "-b", branch_name, str(worktree_path), base_ref)
        except GitError:
            remote_branch = f"{project.remote_name}/{project.base_branch}"
            self._run_git(repo_path, "worktree", "add", "--force", "-b", branch_name, str(worktree_path), remote_branch)
        self.ensure_standard_excludes(worktree_path)
        return branch_name, worktree_path

    def worktree_has_changes(self, worktree_path: Path) -> bool:
        return bool(self.status_porcelain(worktree_path).strip())

    def status_porcelain(self, worktree_path: Path) -> str:
        return self._run_git(worktree_path, "status", "--short").stdout.strip()

    def collect_diff_summary(self, worktree_path: Path) -> str:
        status = self.status_porcelain(worktree_path)
        diff_stat = self._run_git(worktree_path, "diff", "--stat").stdout.strip()
        staged_stat = self._run_git(worktree_path, "diff", "--cached", "--stat").stdout.strip()
        parts = []
        if status:
            parts.append("Status:\n" + status)
        if diff_stat:
            parts.append("Diff stat:\n" + diff_stat)
        if staged_stat:
            parts.append("Staged diff stat:\n" + staged_stat)
        return "\n\n".join(parts).strip()

    def read_full_diff(self, worktree_path: Path, max_chars: int = 50000) -> str:
        diff_stat = self._run_git(worktree_path, "diff", "--stat").stdout.strip()
        diff_patch = self._run_git(worktree_path, "diff").stdout
        parts = []
        if diff_stat:
            parts.append("Diff stat:\n" + diff_stat)
        if diff_patch:
            parts.append("Patch:\n" + tail_text(diff_patch, max_chars))
        return "\n\n".join(parts).strip() if parts else "No local diff."

    def run_review_command(self, project: ProjectConfig, worktree_path: Path) -> None:
        if project.review_command:
            subprocess.Popen(project.review_command, cwd=worktree_path, shell=True)
            return
        try:
            subprocess.Popen(["code", "."], cwd=worktree_path)
        except FileNotFoundError:
            subprocess.Popen(["explorer.exe", str(worktree_path)])

    def run_shell_command(self, command: str, cwd: Path, timeout_seconds: int = 1800) -> tuple[int, str]:
        result = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        output = (result.stdout or "") + ("\n" if result.stdout and result.stderr else "") + (result.stderr or "")
        return result.returncode, tail_text(output, 8000)

    def ensure_excluded(self, worktree_path: Path, pattern: str) -> None:
        git_dir = Path(self._run_git(worktree_path, "rev-parse", "--git-dir").stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (worktree_path / git_dir).resolve()
        info_dir = git_dir / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude_file = info_dir / "exclude"
        existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
        normalized_pattern = pattern.strip()
        if normalized_pattern in {line.strip() for line in existing.splitlines()}:
            return
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        exclude_file.write_text(existing + prefix + normalized_pattern + "\n", encoding="utf-8")

    def ensure_standard_excludes(self, worktree_path: Path) -> None:
        for pattern in DEFAULT_EPHEMERAL_EXCLUDES:
            self.ensure_excluded(worktree_path, pattern)

    def validate_guardrails(self, project: ProjectConfig, worktree_path: Path) -> None:
        guardrails = project.guardrails
        if not (
            guardrails.block_deletions
            or guardrails.max_changed_files > 0
            or guardrails.allowed_paths
            or guardrails.blocked_paths
        ):
            return

        changes = [
            change
            for change in self._list_worktree_changes(worktree_path)
            if any(not self._is_ephemeral_path(path) for path in change.paths)
        ]
        if not changes:
            return

        violations: list[str] = []
        if guardrails.block_deletions:
            destructive = [self._describe_change(change) for change in changes if change.is_destructive]
            if destructive:
                violations.append("destructive changes are blocked: " + ", ".join(destructive))

        changed_paths = sorted(
            {
                self._normalize_repo_path(path)
                for change in changes
                for path in change.paths
                if path and not self._is_ephemeral_path(path)
            }
        )
        if guardrails.max_changed_files > 0 and len(changed_paths) > guardrails.max_changed_files:
            violations.append(
                f"too many files changed ({len(changed_paths)} > {guardrails.max_changed_files}): "
                + ", ".join(changed_paths)
            )

        if guardrails.allowed_paths:
            disallowed = [path for path in changed_paths if not self._matches_any(path, guardrails.allowed_paths)]
            if disallowed:
                violations.append(
                    "changes outside the allowed paths: " + ", ".join(disallowed)
                )

        if guardrails.blocked_paths:
            protected = [path for path in changed_paths if self._matches_any(path, guardrails.blocked_paths)]
            if protected:
                violations.append("changes touched protected paths: " + ", ".join(protected))

        if violations:
            details = "\n".join(f"- {violation}" for violation in violations)
            raise GuardrailError("Guardrails blocked this request.\n" + details)

    def reset_request_workspace(self, project: ProjectConfig, request: RequestRecord) -> None:
        repo_path = Path(project.repo_path)
        if request.worktree_path and Path(request.worktree_path).exists():
            self._run_git(repo_path, "worktree", "remove", "--force", request.worktree_path)
        if request.branch_name and self._branch_exists(repo_path, request.branch_name):
            self._run_git(repo_path, "branch", "-D", request.branch_name)

    def commit_and_push(
        self,
        project: ProjectConfig,
        request: RequestRecord,
        *,
        skip_guardrails: bool = False,
    ) -> tuple[str, str | None]:
        if not request.worktree_path:
            raise GitError("Request has no worktree path.")
        if not request.branch_name:
            raise GitError("Request has no branch name.")

        worktree_path = Path(request.worktree_path)
        if not self.worktree_has_changes(worktree_path):
            raise GitError("There are no local changes to commit.")
        if not skip_guardrails:
            self.validate_guardrails(project, worktree_path)

        self._run_git(worktree_path, "add", "-A")
        staged_check = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", "--cached", "--quiet"],
            capture_output=True,
            text=True,
        )
        if staged_check.returncode == 0:
            raise GitError("No staged changes were produced after git add.")

        self._run_git(worktree_path, "commit", "-m", f"RingPing: {request.title}")
        commit_sha = self._run_git(worktree_path, "rev-parse", "HEAD").stdout.strip()
        release_version = None

        if project.release_on_push:
            if project.push_mode != "direct":
                raise GitError("release_on_push requires push_mode 'direct'.")
            release_version = self._prepare_release_request(project, request, worktree_path)

        if project.push_mode == "branch":
            self._run_git(worktree_path, "push", "-u", project.remote_name, request.branch_name)
        elif project.push_mode == "direct":
            self._refresh_remote_branch(worktree_path, project)
            self._run_git(worktree_path, "rebase", f"{project.remote_name}/{project.base_branch}")
            self._run_git(worktree_path, "push", project.remote_name, f"HEAD:{project.base_branch}")
        else:
            raise GitError(f"Unsupported push mode: {project.push_mode}")

        return commit_sha, release_version

    def _prepare_release_request(self, project: ProjectConfig, request: RequestRecord, worktree_path: Path) -> str:
        version_path = worktree_path / "VERSION"
        release_request_path = worktree_path / "release_request.json"
        if not version_path.exists():
            raise GitError(f"VERSION file not found at {version_path}")

        current_version = version_path.read_text(encoding="utf-8").strip()
        if not current_version:
            raise GitError("VERSION is empty.")

        if project.release_version_strategy == "patch":
            release_version = self._increment_patch_version(current_version)
            version_path.write_text(release_version + "\n", encoding="ascii")
        elif project.release_version_strategy in {"", "none"}:
            release_version = current_version
        else:
            raise GitError(f"Unsupported release version strategy: {project.release_version_strategy}")

        notes_template = project.release_notes_template or "RingPing: {title}"
        notes = notes_template.format(
            title=request.title,
            request_id=request.id,
            project=project.name,
            branch=request.branch_name or "",
        ).strip()
        payload = {
            "notes": notes,
            "requested_at": utc_now_iso(),
        }
        release_request_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        self._run_git(worktree_path, "add", "VERSION", "release_request.json")
        staged_check = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", "--cached", "--quiet"],
            capture_output=True,
            text=True,
        )
        if staged_check.returncode == 0:
            raise GitError("Release request did not stage any changes.")

        self._run_git(worktree_path, "commit", "-m", f"Request release v{release_version}")
        return release_version

    def _increment_patch_version(self, version: str) -> str:
        parts = version.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            raise GitError(f"VERSION must look like major.minor.patch, got '{version}'.")
        major, minor, patch = (int(part) for part in parts)
        return f"{major}.{minor}.{patch + 1}"

    def _preferred_base_ref(self, repo_path: Path, project: ProjectConfig) -> str:
        remote_branch = f"{project.remote_name}/{project.base_branch}"
        remote_ref_exists = subprocess.run(
            ["git", "-C", str(repo_path), "show-ref", "--verify", "--quiet", f"refs/remotes/{remote_branch}"],
            capture_output=True,
            text=True,
        )
        if remote_ref_exists.returncode == 0:
            return remote_branch
        return project.base_branch

    def _refresh_remote_branch(self, cwd: Path, project: ProjectConfig) -> None:
        self._run_git(cwd, "fetch", project.remote_name, project.base_branch)

    def _branch_exists(self, repo_path: Path, branch_name: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _list_worktree_changes(self, worktree_path: Path) -> list[WorktreeChange]:
        lines = self._run_git(worktree_path, "status", "--porcelain").stdout.splitlines()
        changes: list[WorktreeChange] = []
        for raw_line in lines:
            if len(raw_line) < 3:
                continue
            status = raw_line[:2]
            payload = raw_line[3:].strip()
            original_path = None
            current_path = payload
            if " -> " in payload:
                original_path, current_path = payload.split(" -> ", 1)
            changes.append(
                WorktreeChange(
                    status=status,
                    path=self._normalize_repo_path(current_path),
                    original_path=self._normalize_repo_path(original_path) if original_path else None,
                )
            )
        return changes

    def _describe_change(self, change: WorktreeChange) -> str:
        if change.original_path:
            return f"{change.status.strip()}: {change.original_path} -> {change.path}"
        return f"{change.status.strip()}: {change.path}"

    def _matches_any(self, path: str, patterns: list[str]) -> bool:
        normalized_path = self._normalize_repo_path(path)
        for pattern in patterns:
            normalized_pattern = self._normalize_repo_path(pattern)
            if normalized_pattern.endswith("/"):
                if normalized_path == normalized_pattern.rstrip("/") or normalized_path.startswith(normalized_pattern):
                    return True
                continue
            if fnmatch.fnmatchcase(normalized_path, normalized_pattern):
                return True
        return False

    def _normalize_repo_path(self, path: str | None) -> str:
        return str(path or "").replace("\\", "/").strip().lstrip("./")

    def _is_ephemeral_path(self, path: str) -> bool:
        normalized = self._normalize_repo_path(path)
        return self._matches_any(normalized, list(DEFAULT_EPHEMERAL_EXCLUDES))

    def _run_git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Unknown git error"
            raise GitError(detail)
        return result
