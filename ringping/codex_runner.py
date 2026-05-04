from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
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
        live_log_path: Path | None = None,
    ) -> CodexRunResult:
        prompt = self._build_prompt(project, request, downloaded_attachments or [])
        return self._run_with_fallback(
            self.settings.codex_command,
            self.settings.codex_flags,
            prompt,
            worktree_path,
            live_log_path,
            timeout_seconds=self.settings.codex_timeout_seconds,
        )

    def run_read_only(self, prompt: str, cwd: Path, live_log_path: Path | None = None) -> CodexRunResult:
        return self._run_with_fallback(
            self.settings.codex_command,
            self.settings.codex_ask_flags,
            prompt,
            cwd,
            live_log_path,
            timeout_seconds=self.settings.codex_ask_timeout_seconds,
        )

    def _is_claude_command(self, command: str) -> bool:
        return Path(command).stem.lower() == "claude"

    def _run_command(
        self,
        command: str,
        flags: list[str],
        prompt: str,
        worktree_path: Path,
        live_log_path: Path | None,
        *,
        timeout_seconds: int,
    ) -> CodexRunResult:
        resolved_command = self._resolve_command(command)
        if resolved_command != command:
            self._append_live_log_line(live_log_path, f"[RingPing] Resolved command '{command}' to '{resolved_command}'.")
            self._append_raw_log_line(live_log_path, f"[RingPing] Resolved command '{command}' to '{resolved_command}'.")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

        if self._is_claude_command(resolved_command):
            return self._run_claude(
                resolved_command,
                flags,
                prompt,
                worktree_path,
                creationflags,
                live_log_path,
                timeout_seconds,
            )
        return self._run_codex(
            resolved_command,
            flags,
            prompt,
            worktree_path,
            creationflags,
            live_log_path,
            timeout_seconds,
        )

    def _resolve_command(self, command: str) -> str:
        command = command.strip()
        if not command:
            raise RuntimeError("Command is blank.")

        command_path = Path(command)
        if command_path.exists():
            return command

        resolved = shutil.which(command)
        if resolved:
            return resolved

        fallback_names: list[str] = []
        if command_path.name and command_path.name != command:
            fallback_names.append(command_path.name)
            if command_path.stem and command_path.stem != command_path.name:
                fallback_names.append(command_path.stem)

        for fallback_name in fallback_names:
            resolved = shutil.which(fallback_name)
            if resolved:
                return resolved

        if command_path.name.lower() in {"codex", "codex.exe"}:
            resolved = self._resolve_vscode_codex_command(command_path)
            if resolved:
                return resolved

        raise RuntimeError(f"Command not found on PATH: {command}")

    def _resolve_vscode_codex_command(self, configured_path: Path) -> str | None:
        candidates: list[Path] = []
        candidates.extend(self._vscode_codex_candidates(configured_path))

        user_profile = Path.home()
        for extensions_dir in (
            user_profile / ".vscode" / "extensions",
            user_profile / ".vscode-insiders" / "extensions",
        ):
            candidates.extend(self._vscode_codex_candidates(extensions_dir))

        existing = [path for path in candidates if path.exists()]
        if not existing:
            return None
        return str(max(existing, key=self._codex_candidate_sort_key))

    def _vscode_codex_candidates(self, root: Path) -> list[Path]:
        roots: list[Path] = []
        parts = root.parts
        for index, part in enumerate(parts):
            if part.lower() == "extensions":
                roots.append(Path(*parts[: index + 1]))
                break
        if root.name.lower() == "extensions":
            roots.append(root)

        candidates: list[Path] = []
        for extensions_dir in roots:
            if not extensions_dir.exists():
                continue
            for extension_dir in extensions_dir.glob("openai.chatgpt-*"):
                candidates.append(extension_dir / "bin" / "windows-x86_64" / "codex.exe")
        return candidates

    def _codex_candidate_sort_key(self, path: Path) -> tuple[tuple[int, ...], float, str]:
        version: tuple[int, ...] = ()
        for parent in path.parents:
            match = re.match(r"openai\.chatgpt-([0-9.]+)", parent.name, flags=re.IGNORECASE)
            if match:
                version = tuple(int(part) for part in match.group(1).split(".") if part.isdigit())
                break
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            modified_at = 0.0
        return version, modified_at, str(path)

    def _run_with_fallback(
        self,
        command: str,
        flags: list[str],
        prompt: str,
        worktree_path: Path,
        live_log_path: Path | None,
        *,
        timeout_seconds: int,
    ) -> CodexRunResult:
        result = self._run_command(
            command,
            flags,
            prompt,
            worktree_path,
            live_log_path,
            timeout_seconds=timeout_seconds,
        )
        fallback_command = self.settings.codex_fallback_command.strip()
        if self._is_rate_limited(result) and fallback_command and fallback_command != command:
            self._append_log_line(live_log_path, "")
            self._append_log_line(live_log_path, f"[RingPing] Primary command hit a rate limit. Falling back to {fallback_command}.")
            return self._run_command(
                fallback_command,
                self.settings.codex_fallback_flags,
                prompt,
                worktree_path,
                live_log_path,
                timeout_seconds=timeout_seconds,
            )
        return result

    def _run_codex(
        self,
        command: str,
        flags: list[str],
        prompt: str,
        worktree_path: Path,
        creationflags: int,
        live_log_path: Path | None,
        timeout_seconds: int,
    ) -> CodexRunResult:
        with tempfile.TemporaryDirectory(prefix="ringping-codex-") as temp_dir:
            last_message_path = Path(temp_dir) / "last-message.txt"
            cmd = [
                command,
                "exec",
                *flags,
                "--json",
                "--cd",
                str(worktree_path),
                "--output-last-message",
                str(last_message_path),
                "-",
            ]
            stdout_text, stderr_text, exit_code, timed_out, timeout_reason = self._run_process(
                cmd,
                prompt,
                worktree_path,
                creationflags,
                live_log_path,
                timeout_seconds,
                json_stdout=True,
            )
            last_message = last_message_path.read_text(encoding="utf-8").strip() if last_message_path.exists() else ""
            return CodexRunResult(
                exit_code=exit_code,
                last_message=last_message,
                stdout_tail=tail_text(stdout_text, 6000),
                stderr_tail=tail_text(stderr_text, 6000),
                command_display=subprocess.list2cmdline(cmd),
                timed_out=timed_out,
                timeout_reason=timeout_reason,
            )

    def _run_claude(
        self,
        command: str,
        flags: list[str],
        prompt: str,
        worktree_path: Path,
        creationflags: int,
        live_log_path: Path | None,
        timeout_seconds: int,
    ) -> CodexRunResult:
        import json as _json
        cmd = [
            command,
            "-p",
            *flags,
            "--output-format", "json",
        ]
        stdout_text, stderr_text, exit_code, timed_out, timeout_reason = self._run_process(
            cmd,
            prompt,
            worktree_path,
            creationflags,
            live_log_path,
            timeout_seconds,
        )
        last_message = ""
        if stdout_text:
            try:
                data = _json.loads(stdout_text)
                last_message = str(data.get("result") or "").strip()
            except (_json.JSONDecodeError, AttributeError):
                last_message = tail_text(stdout_text, 6000)
        return CodexRunResult(
            exit_code=exit_code,
            last_message=last_message,
            stdout_tail=tail_text(stdout_text, 6000),
            stderr_tail=tail_text(stderr_text, 6000),
            command_display=subprocess.list2cmdline(cmd),
            timed_out=timed_out,
            timeout_reason=timeout_reason,
        )

    def _run_process(
        self,
        cmd: list[str],
        prompt: str,
        worktree_path: Path,
        creationflags: int,
        live_log_path: Path | None,
        timeout_seconds: int,
        json_stdout: bool = False,
    ) -> tuple[str, str, int, bool, str | None]:
        self._append_live_log_line(live_log_path, "")
        self._append_live_log_line(live_log_path, "[RingPing] Starting command:")
        self._append_live_log_line(live_log_path, subprocess.list2cmdline(cmd))
        self._append_raw_log_line(live_log_path, "")
        self._append_raw_log_line(live_log_path, "[RingPing] Starting command:")
        self._append_raw_log_line(live_log_path, subprocess.list2cmdline(cmd))
        process = subprocess.Popen(
            cmd,
            cwd=worktree_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        last_activity = [time.monotonic()]

        def consume_stdout() -> None:
            if process.stdout is None:
                return
            for line in iter(process.stdout.readline, ""):
                last_activity[0] = time.monotonic()
                stdout_parts.append(line)
                self._append_raw_log_line(live_log_path, f"[stdout] {line.rstrip()}")
                if json_stdout:
                    self._handle_codex_stdout_line(live_log_path, line)
                else:
                    self._append_live_log_line(live_log_path, f"[stdout] {line.rstrip()}")
            process.stdout.close()

        def consume_stderr() -> None:
            if process.stderr is None:
                return
            for line in iter(process.stderr.readline, ""):
                stderr_parts.append(line)
                if self._stderr_line_counts_as_activity(line):
                    last_activity[0] = time.monotonic()
                self._append_raw_log_line(live_log_path, f"[stderr] {line.rstrip()}")
                if not json_stdout and line.rstrip():
                    self._append_live_log_line(live_log_path, f"[stderr] {line.rstrip()}")
            process.stderr.close()

        stdout_thread = threading.Thread(target=consume_stdout, daemon=True)
        stderr_thread = threading.Thread(target=consume_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        if process.stdin is not None:
            process.stdin.write(prompt)
            process.stdin.close()
        started_at = time.monotonic()
        timed_out = False
        timeout_reason = None
        exit_code = None
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                break
            now = time.monotonic()
            if timeout_seconds > 0 and (now - started_at) >= timeout_seconds:
                timed_out = True
                timeout_reason = "overall_timeout"
                process.kill()
                break
            if (
                self.settings.codex_idle_after_changes_seconds > 0
                and (now - last_activity[0]) >= self.settings.codex_idle_after_changes_seconds
                and self._tracked_changes_exist(worktree_path)
            ):
                timed_out = True
                timeout_reason = "idle_after_changes"
                process.kill()
                break
            time.sleep(1)
        if timed_out:
            process.wait(timeout=5)
            if timeout_reason == "idle_after_changes":
                timeout_message = (
                    "[RingPing] Command went idle after producing local changes. "
                    "Stopping Codex and salvaging the existing diff."
                )
            else:
                timeout_message = f"[RingPing] Command timed out after {timeout_seconds} seconds."
            self._append_live_log_line(live_log_path, timeout_message)
            self._append_raw_log_line(live_log_path, timeout_message)
        stdout_thread.join()
        stderr_thread.join()
        if exit_code is None:
            exit_code = process.returncode if process.returncode is not None else -1
        self._append_live_log_line(live_log_path, f"[RingPing] Command exited with code {exit_code}.")
        self._append_raw_log_line(live_log_path, f"[RingPing] Command exited with code {exit_code}.")
        return "".join(stdout_parts), "".join(stderr_parts), exit_code, timed_out, timeout_reason

    def _stderr_line_counts_as_activity(self, line: str) -> bool:
        lowered = line.lower()
        ignored_fragments = (
            "dropping in-process server notification",
            "ignoring interface.defaultprompt",
        )
        return not any(fragment in lowered for fragment in ignored_fragments)

    def _tracked_changes_exist(self, worktree_path: Path) -> bool:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "status", "--short", "--untracked-files=no"],
            capture_output=True,
            text=True,
            creationflags=creationflags,
        )
        return bool((result.stdout or "").strip())

    def _append_log_line(self, live_log_path: Path | None, text: str) -> None:
        if live_log_path is None:
            return
        live_log_path.parent.mkdir(parents=True, exist_ok=True)
        with live_log_path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(text + "\n")

    def _append_live_log_line(self, live_log_path: Path | None, text: str) -> None:
        self._append_log_line(live_log_path, text)

    def _append_raw_log_line(self, live_log_path: Path | None, text: str) -> None:
        raw_log_path = self._raw_log_path(live_log_path)
        self._append_log_line(raw_log_path, text)

    def _raw_log_path(self, live_log_path: Path | None) -> Path | None:
        if live_log_path is None:
            return None
        return live_log_path.with_name(f"{live_log_path.stem}.raw{live_log_path.suffix}")

    def _handle_codex_stdout_line(self, live_log_path: Path | None, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            self._append_live_log_line(live_log_path, f"[stdout] {stripped}")
            return

        event_type = str(payload.get("type") or "")
        if event_type == "thread.started":
            self._append_live_log_line(live_log_path, "[Codex] Session started.")
            return
        if event_type == "turn.started":
            self._append_live_log_line(live_log_path, "[Codex] Analyzing the request and choosing actions.")
            return
        if event_type == "turn.completed":
            self._append_live_log_line(live_log_path, "[Codex] Finished this run.")
            return

        item = payload.get("item")
        if not isinstance(item, dict):
            return

        item_type = str(item.get("type") or "")
        if item_type == "command_execution":
            command = self._format_monitor_command(str(item.get("command") or ""))
            if event_type == "item.started":
                self._append_live_log_line(live_log_path, f"[Codex] Running command: {command}")
                return
            if event_type == "item.completed":
                exit_code = item.get("exit_code")
                self._append_live_log_line(
                    live_log_path,
                    f"[Codex] Command finished with exit {exit_code}: {command}",
                )
                output_summary = self._summarize_command_output_for_monitor(command, str(item.get("aggregated_output") or ""))
                if output_summary:
                    self._append_live_log_line(live_log_path, f"[Codex] {output_summary}")
                return

        if item_type == "agent_message" and event_type == "item.completed":
            text = str(item.get("text") or "").strip()
            if not text:
                return
            for paragraph in text.splitlines():
                paragraph = paragraph.strip()
                if paragraph:
                    self._append_live_log_line(live_log_path, f"[Codex] {paragraph}")

    def _format_monitor_command(self, command: str, limit: int = 220) -> str:
        command = " ".join(command.split())
        if len(command) <= limit:
            return command
        return command[: limit - 3] + "..."

    def _summarize_command_output_for_monitor(self, command: str, output: str) -> str:
        if not output:
            return ""
        lowered = command.lower()
        if "pytest" not in lowered and "unittest" not in lowered:
            return ""
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        interesting = [
            line
            for line in lines
            if line == "OK"
            or line.startswith("Ran ")
            or "failed" in line.lower()
            or "passed" in line.lower()
            or "error" in line.lower()
        ]
        if not interesting:
            return ""
        summary = " | ".join(interesting[:3])
        if len(summary) <= 220:
            return f"Test output: {summary}"
        return f"Test output: {summary[:217]}..."

    def _is_rate_limited(self, result: CodexRunResult) -> bool:
        if result.exit_code == 0:
            return False
        combined = "\n".join(
            part for part in (result.last_message, result.stdout_tail, result.stderr_tail) if part
        )
        lowered = combined.lower()
        return any(token in lowered for token in ("credit", "limit", "quota", "rate limit"))

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
