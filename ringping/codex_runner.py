from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ringping.config import AppSettings
from ringping.models import CodexRunResult, ProjectConfig, RequestAttachment, RequestRecord
from ringping.utils import tail_text


class CodexRunner:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def run(
        self,
        project: ProjectConfig,
        request: RequestRecord,
        worktree_path: Path,
        downloaded_attachments: list[tuple[RequestAttachment, Path]] | None = None,
    ) -> CodexRunResult:
        if shutil.which(self.settings.codex_command) is None:
            raise RuntimeError(f"Codex command not found on PATH: {self.settings.codex_command}")

        prompt = self._build_prompt(project, request, downloaded_attachments or [])
        with tempfile.TemporaryDirectory(prefix="ringping-codex-") as temp_dir:
            last_message_path = Path(temp_dir) / "last-message.txt"
            command = [
                self.settings.codex_command,
                "exec",
                *self.settings.codex_flags,
                "--cd",
                str(worktree_path),
                "--output-last-message",
                str(last_message_path),
                "-",
            ]
            result = subprocess.run(
                command,
                input=prompt,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=self.settings.codex_timeout_seconds,
            )
            last_message = last_message_path.read_text(encoding="utf-8").strip() if last_message_path.exists() else ""
            return CodexRunResult(
                exit_code=result.returncode,
                last_message=last_message,
                stdout_tail=tail_text(result.stdout or "", 6000),
                stderr_tail=tail_text(result.stderr or "", 6000),
                command_display=subprocess.list2cmdline(command),
            )

    def _build_prompt(
        self,
        project: ProjectConfig,
        request: RequestRecord,
        downloaded_attachments: list[tuple[RequestAttachment, Path]],
    ) -> str:
        parts = []
        if project.codex_prompt_prefix:
            parts.append(project.codex_prompt_prefix)
        parts.extend(
            [
                f"Project: {project.name}",
                f"Base branch: {project.base_branch}",
                f"Request title: {request.title}",
                "",
                "Requested change:",
                request.prompt.strip(),
                "",
                "Constraints:",
                "- Work only in the current repository.",
                "- Do not push to git.",
                "- Do not commit to git.",
                "- Leave the working tree in a reviewable state for a human.",
            ]
        )
        parts.extend(self._guardrail_lines(project))
        if project.test_command:
            parts.append(f"- Prefer to run this validation command if it is relevant: {project.test_command}")
        if downloaded_attachments:
            parts.extend(
                [
                    "",
                    "Downloaded request attachments:",
                ]
            )
            for attachment, path in downloaded_attachments:
                parts.append(f"- {attachment.name}: {path}")
            parts.extend(
                [
                    "",
                    "Use the downloaded attachments as evidence for reproducing and fixing the parser problem if they are relevant.",
                ]
            )
        return "\n".join(parts).strip()

    def _guardrail_lines(self, project: ProjectConfig) -> list[str]:
        guardrails = project.guardrails
        lines: list[str] = []
        if guardrails.block_deletions:
            lines.append("- Do not delete, rename, or move files.")
        for rule in guardrails.prompt_rules:
            lines.append(f"- {rule}")
        if guardrails.allowed_paths:
            lines.append("- Only modify files that match these paths:")
            lines.extend(f"  - {pattern}" for pattern in guardrails.allowed_paths)
        if guardrails.blocked_paths:
            lines.append("- Never modify these protected paths:")
            lines.extend(f"  - {pattern}" for pattern in guardrails.blocked_paths)
        return lines
