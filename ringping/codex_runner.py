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
from typing import Callable

from ringping.config import AppSettings
from ringping.models import CodexRunResult, ProjectConfig, RequestAttachment, RequestRecord
from ringping.utils import LOG_PREFIX, tail_text


class CodexRunner:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._active_processes: dict[int, str] = {}
        self._active_processes_lock = threading.Lock()
        self._last_codex_shell_self_test_result: CodexRunResult | None = None

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
            self.settings.codex_root_flags,
            self.settings.codex_flags,
            prompt,
            worktree_path,
            live_log_path,
            timeout_seconds=self.settings.codex_timeout_seconds,
        )

    def run_read_only(self, prompt: str, cwd: Path, live_log_path: Path | None = None) -> CodexRunResult:
        return self._run_with_fallback(
            self.settings.codex_command,
            self.settings.codex_ask_root_flags,
            self.settings.codex_ask_flags,
            prompt,
            cwd,
            live_log_path,
            timeout_seconds=self.settings.codex_ask_timeout_seconds,
        )

    def run_training(
        self,
        prompt: str,
        cwd: Path,
        live_log_path: Path | None = None,
        *,
        cancel_event: threading.Event | None = None,
        event_callback: Callable[[dict], None] | None = None,
        activity_callback: Callable[[], None] | None = None,
    ) -> CodexRunResult:
        return self._run_with_fallback(
            self.settings.codex_command,
            self.settings.codex_root_flags,
            self.settings.codex_flags,
            prompt,
            cwd,
            live_log_path,
            timeout_seconds=self.settings.codex_timeout_seconds,
            cancel_event=cancel_event,
            event_callback=event_callback,
            activity_callback=activity_callback,
        )

    def _is_claude_command(self, command: str) -> bool:
        return Path(command).stem.lower() == "claude"

    def _run_command(
        self,
        command: str,
        root_flags: list[str],
        flags: list[str],
        prompt: str,
        worktree_path: Path,
        live_log_path: Path | None,
        *,
        timeout_seconds: int,
        cancel_event: threading.Event | None = None,
        event_callback: Callable[[dict], None] | None = None,
        activity_callback: Callable[[], None] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> CodexRunResult:
        resolved_command = self._resolve_command(command)
        if resolved_command != command:
            self._append_live_log_line(live_log_path, f"{LOG_PREFIX} Resolved command '{command}' to '{resolved_command}'.")
            self._append_raw_log_line(live_log_path, f"{LOG_PREFIX} Resolved command '{command}' to '{resolved_command}'.")

        creationflags = self._creationflags_for_command(resolved_command)

        if self._is_claude_command(resolved_command):
            return self._run_claude(
                resolved_command,
                flags,
                prompt,
                worktree_path,
                creationflags,
                live_log_path,
                timeout_seconds,
                cancel_event=cancel_event,
                event_callback=event_callback,
                activity_callback=activity_callback,
                extra_env=extra_env,
            )
        return self._run_codex(
            resolved_command,
            root_flags,
            flags,
            prompt,
            worktree_path,
            creationflags,
            live_log_path,
            timeout_seconds,
            cancel_event=cancel_event,
            event_callback=event_callback,
            activity_callback=activity_callback,
            extra_env=extra_env,
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

    def _creationflags_for_command(self, command: str) -> int:
        if os.name != "nt":
            return 0
        if self._is_codex_command_name(command):
            return getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)

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
        root_flags: list[str],
        flags: list[str],
        prompt: str,
        worktree_path: Path,
        live_log_path: Path | None,
        *,
        timeout_seconds: int,
        cancel_event: threading.Event | None = None,
        event_callback: Callable[[dict], None] | None = None,
        activity_callback: Callable[[], None] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> CodexRunResult:
        active_extra_env = extra_env
        if self._is_codex_command_name(command):
            shell_ready = self._codex_shell_self_test(
                command,
                root_flags,
                flags,
                worktree_path,
                live_log_path,
                event_callback=event_callback,
                extra_env=active_extra_env,
            )
            fallback_env = (
                self._fallback_codex_home_env(live_log_path)
                if not shell_ready
                and self._last_codex_shell_self_test_result is not None
                and self._is_rate_limited(self._last_codex_shell_self_test_result)
                else None
            )
            if fallback_env:
                active_extra_env = fallback_env
                fallback_home = fallback_env["CODEX_HOME"]
                self._append_log_line(
                    live_log_path,
                    f"{LOG_PREFIX} Primary Codex account hit a usage limit during shell self-test. Retrying self-test with fallback CODEX_HOME: {fallback_home}",
                )
                self._emit_monitor_event(
                    event_callback,
                    "Primary Codex account hit a usage limit during shell self-test. Retrying with the fallback Codex account.",
                    event_type="warning",
                )
                shell_ready = self._codex_shell_self_test(
                    command,
                    root_flags,
                    flags,
                    worktree_path,
                    live_log_path,
                    event_callback=event_callback,
                    extra_env=active_extra_env,
                )
            if not shell_ready:
                self._recover_ringping_codex_processes(live_log_path, event_callback=event_callback)
                shell_ready = self._codex_shell_self_test(
                    command,
                    root_flags,
                    flags,
                    worktree_path,
                    live_log_path,
                    event_callback=event_callback,
                    extra_env=active_extra_env,
                )
            if not shell_ready:
                return CodexRunResult(
                    exit_code=1,
                    last_message=(
                        "Codex could start, but its internal shell runner could not execute a "
                        "minimal PowerShell command after recovery. The real job was not started."
                    ),
                    stdout_tail="",
                    stderr_tail="Codex internal shell self-test failed.",
                    command_display=f"{command} exec <shell-self-test>",
                )

        result = self._run_command(
            command,
            root_flags,
            flags,
            prompt,
            worktree_path,
            live_log_path,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
            event_callback=event_callback,
            activity_callback=activity_callback,
            extra_env=active_extra_env,
        )
        if result.timeout_reason == "cancelled":
            return result
        if self._is_codex_command_name(command) and self._is_shell_blocked_result(result):
            self._append_log_line(
                live_log_path,
                f"{LOG_PREFIX} Codex reported internal shell startup failure despite exit 0. Retrying once.",
            )
            self._emit_monitor_event(
                event_callback,
                "Codex internal shell startup failed. Retrying once.",
                event_type="warning",
            )
            self._recover_ringping_codex_processes(live_log_path, event_callback=event_callback)
            if not self._codex_shell_self_test(
                command,
                root_flags,
                flags,
                worktree_path,
                live_log_path,
                event_callback=event_callback,
                extra_env=active_extra_env,
            ):
                return CodexRunResult(
                    exit_code=1,
                    last_message=(
                        "Codex reported internal shell startup failure and the recovery self-test "
                        "still could not run PowerShell. No reliable job retry was started."
                    ),
                    stdout_tail=result.stdout_tail,
                    stderr_tail=(result.stderr_tail + "\nCodex internal shell recovery self-test failed.").strip(),
                    command_display=result.command_display,
                )
            result = self._run_command(
                command,
                root_flags,
                flags,
                prompt,
                worktree_path,
                live_log_path,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
                event_callback=event_callback,
                activity_callback=activity_callback,
                extra_env=active_extra_env,
            )

        fallback_env = self._fallback_codex_home_env(live_log_path) if self._is_rate_limited(result) else None
        if fallback_env:
            fallback_home = fallback_env["CODEX_HOME"]
            self._append_log_line(live_log_path, "")
            self._append_log_line(
                live_log_path,
                f"{LOG_PREFIX} Primary Codex account hit a usage limit. Retrying with fallback CODEX_HOME: {fallback_home}",
            )
            self._emit_monitor_event(
                event_callback,
                "Primary Codex account hit a usage limit. Retrying with the fallback Codex account.",
                event_type="warning",
            )
            fallback_result = self._run_command(
                command,
                root_flags,
                flags,
                prompt,
                worktree_path,
                live_log_path,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
                event_callback=event_callback,
                activity_callback=activity_callback,
                extra_env=fallback_env,
            )
            if not self._is_rate_limited(fallback_result):
                return fallback_result
            result = fallback_result

        fallback_command = self.settings.codex_fallback_command.strip()
        if self._is_rate_limited(result) and fallback_command and fallback_command != command:
            self._append_log_line(live_log_path, "")
            self._append_log_line(live_log_path, f"{LOG_PREFIX} Primary command hit a rate limit. Falling back to {fallback_command}.")
            return self._run_command(
                fallback_command,
                [],
                self.settings.codex_fallback_flags,
                prompt,
                worktree_path,
                live_log_path,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
                event_callback=event_callback,
                activity_callback=activity_callback,
            )
        return result

    def _is_codex_command_name(self, command: str) -> bool:
        return Path(command.strip()).stem.lower() == "codex"

    def _codex_shell_self_test(
        self,
        command: str,
        root_flags: list[str],
        flags: list[str],
        worktree_path: Path,
        live_log_path: Path | None,
        *,
        event_callback: Callable[[dict], None] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> bool:
        self._append_log_line(live_log_path, f"{LOG_PREFIX} Verifying Codex can start its internal shell before real work.")
        self._emit_monitor_event(event_callback, "Verifying Codex can start its internal shell before real work.")
        result = self._run_command(
            command,
            root_flags,
            flags,
            (
                "Run exactly one shell command in the already-open Codex shell: "
                "Get-Location. Then reply with the command exit code and output. "
                "Do not launch powershell.exe or pwsh.exe. Do not edit files."
            ),
            worktree_path,
            live_log_path,
            timeout_seconds=120,
            event_callback=event_callback,
            extra_env=extra_env,
        )
        self._last_codex_shell_self_test_result = result
        if result.exit_code == 0 and not self._is_shell_blocked_result(result) and not self._has_shell_startup_failure(result):
            self._append_log_line(live_log_path, f"{LOG_PREFIX} Codex internal shell self-test passed.")
            self._emit_monitor_event(event_callback, "Codex internal shell self-test passed.")
            return True
        self._append_log_line(
            live_log_path,
            f"{LOG_PREFIX} Codex internal shell self-test failed: exit {result.exit_code}; {result.last_message}",
        )
        self._emit_monitor_event(
            event_callback,
            "Codex internal shell self-test failed.",
            event_type="warning",
        )
        return False

    def _recover_ringping_codex_processes(
        self,
        live_log_path: Path | None,
        *,
        event_callback: Callable[[dict], None] | None = None,
    ) -> None:
        if os.name != "nt":
            return
        with self._active_processes_lock:
            active_codex_pids = [
                pid
                for pid, command_name in self._active_processes.items()
                if Path(command_name).stem.lower() == "codex"
            ]
        stale_codex_pids = self._find_stale_ringping_codex_pids(live_log_path)
        codex_pids = sorted(set(active_codex_pids + stale_codex_pids))
        if not codex_pids:
            self._append_log_line(
                live_log_path,
                f"{LOG_PREFIX} No RingPing-launched Codex process is still running; skipping global codex.exe restart.",
            )
            self._emit_monitor_event(
                event_callback,
                "No RingPing-launched Codex process is still running; skipping global Codex restart.",
                event_type="warning",
            )
            return

        self._append_log_line(live_log_path, f"{LOG_PREFIX} Stopping RingPing-launched Codex process tree(s): {codex_pids}")
        self._emit_monitor_event(event_callback, "Stopping RingPing-launched Codex process tree before retrying.", event_type="warning")
        for pid in codex_pids:
            self._terminate_pid_tree(pid, live_log_path)
        time.sleep(2)

    def _find_stale_ringping_codex_pids(self, live_log_path: Path | None) -> list[int]:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            return []
        script = (
            "Get-CimInstance Win32_Process "
            "-Filter \"Name = 'codex.exe'\" | "
            "Where-Object { $_.CommandLine -like '*ringping-codex-*' } | "
            "ForEach-Object { $_.ProcessId }"
        )
        try:
            result = subprocess.run(
                [powershell, "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            self._append_log_line(live_log_path, f"{LOG_PREFIX} Could not inspect stale RingPing Codex processes: {exc}")
            return []
        pids: list[int] = []
        for line in (result.stdout or "").splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                continue
        return pids

    def _terminate_pid_tree(self, pid: int, live_log_path: Path | None) -> None:
        if os.name != "nt":
            return
        try:
            result = subprocess.run(
                ["taskkill.exe", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            self._append_log_line(live_log_path, f"{LOG_PREFIX} Could not stop process tree {pid}: {exc}")
            return
        output = " ".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part and part.strip()
        )
        if output:
            self._append_log_line(live_log_path, f"{LOG_PREFIX} taskkill PID {pid}: {output}")

    def _is_shell_blocked_result(self, result: CodexRunResult) -> bool:
        combined = "\n".join(
            part for part in (result.last_message, result.stdout_tail, result.stderr_tail) if part
        ).lower()
        blocked_markers = (
            "blocked by the execution environment",
            "blocked by the runner",
            "blocked before i could",
            "internal shell",
            "shell startup",
            "shell launch failure",
        )
        shell_markers = (
            "-1073741502",
            "0xc0000142",
            "powershell cannot start",
            "powershell is failing",
            "could not start powershell",
            "every attempted command",
            "all failed immediately",
            "command failed before execution",
        )
        return any(marker in combined for marker in blocked_markers) and any(
            marker in combined for marker in shell_markers
        )

    def _has_shell_startup_failure(self, result: CodexRunResult) -> bool:
        combined = "\n".join(
            part for part in (result.last_message, result.stdout_tail, result.stderr_tail) if part
        ).lower()
        return any(
            marker in combined
            for marker in (
                "-1073741502",
                "0xc0000142",
                "exit code: `-1073741502`",
                "exit -1073741502",
            )
        )

    def _fallback_codex_home_env(self, live_log_path: Path | None) -> dict[str, str] | None:
        fallback_home_raw = self.settings.codex_fallback_home.strip()
        if not fallback_home_raw:
            return None
        fallback_home = Path(fallback_home_raw).expanduser()
        default_home = Path.home() / ".codex"
        try:
            if fallback_home.resolve() == default_home.resolve():
                return None
        except OSError:
            if fallback_home == default_home:
                return None
        auth_path = fallback_home / "auth.json"
        if not auth_path.exists():
            self._append_log_line(
                live_log_path,
                f"{LOG_PREFIX} Fallback CODEX_HOME is configured but not logged in yet: {fallback_home}",
            )
            return None
        return {"CODEX_HOME": str(fallback_home)}

    def _run_codex(
        self,
        command: str,
        root_flags: list[str],
        flags: list[str],
        prompt: str,
        worktree_path: Path,
        creationflags: int,
        live_log_path: Path | None,
        timeout_seconds: int,
        cancel_event: threading.Event | None = None,
        event_callback: Callable[[dict], None] | None = None,
        activity_callback: Callable[[], None] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> CodexRunResult:
        with tempfile.TemporaryDirectory(prefix="ringping-codex-") as temp_dir:
            last_message_path = Path(temp_dir) / "last-message.txt"
            cmd = [
                command,
                *root_flags,
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
                cancel_event=cancel_event,
                event_callback=event_callback,
                activity_callback=activity_callback,
                extra_env=extra_env,
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
        cancel_event: threading.Event | None = None,
        event_callback: Callable[[dict], None] | None = None,
        activity_callback: Callable[[], None] | None = None,
        extra_env: dict[str, str] | None = None,
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
            cancel_event=cancel_event,
            event_callback=event_callback,
            activity_callback=activity_callback,
            extra_env=extra_env,
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
        cancel_event: threading.Event | None = None,
        event_callback: Callable[[dict], None] | None = None,
        activity_callback: Callable[[], None] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[str, str, int, bool, str | None]:
        self._append_live_log_line(live_log_path, "")
        self._append_live_log_line(live_log_path, f"{LOG_PREFIX} Starting command:")
        self._append_live_log_line(live_log_path, subprocess.list2cmdline(cmd))
        self._append_raw_log_line(live_log_path, "")
        self._append_raw_log_line(live_log_path, f"{LOG_PREFIX} Starting command:")
        self._append_raw_log_line(live_log_path, subprocess.list2cmdline(cmd))
        self._emit_monitor_event(
            event_callback,
            f"Starting local command: {self._format_monitor_command(subprocess.list2cmdline(cmd))}",
        )
        if activity_callback is not None:
            activity_callback()
        process_env = os.environ.copy()
        if extra_env:
            process_env.update(extra_env)
        process = subprocess.Popen(
            cmd,
            cwd=worktree_path,
            env=process_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )
        with self._active_processes_lock:
            self._active_processes[process.pid] = cmd[0]
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        last_activity = [time.monotonic()]
        codex_queue_full_warnings = [0]

        def consume_stdout() -> None:
            if process.stdout is None:
                return
            for line in iter(process.stdout.readline, ""):
                last_activity[0] = time.monotonic()
                if activity_callback is not None:
                    activity_callback()
                stdout_parts.append(line)
                self._append_raw_log_line(live_log_path, f"[stdout] {line.rstrip()}")
                if json_stdout:
                    self._handle_codex_stdout_line(live_log_path, line, event_callback=event_callback)
                else:
                    self._append_live_log_line(live_log_path, f"[stdout] {line.rstrip()}")
            process.stdout.close()

        def consume_stderr() -> None:
            if process.stderr is None:
                return
            for line in iter(process.stderr.readline, ""):
                stderr_parts.append(line)
                if self._is_codex_queue_full_warning(line):
                    codex_queue_full_warnings[0] += 1
                if self._stderr_line_counts_as_activity(line):
                    last_activity[0] = time.monotonic()
                    if activity_callback is not None:
                        activity_callback()
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
            if cancel_event is not None and cancel_event.is_set():
                timed_out = True
                timeout_reason = "cancelled"
                self._terminate_process(process, live_log_path)
                break
            if timeout_seconds > 0 and (now - started_at) >= timeout_seconds:
                timed_out = True
                timeout_reason = "overall_timeout"
                self._terminate_process(process, live_log_path)
                break
            if codex_queue_full_warnings[0] >= 25:
                timed_out = True
                timeout_reason = "codex_queue_full"
                self._terminate_process(process, live_log_path)
                break
            if (
                self.settings.codex_idle_after_changes_seconds > 0
                and (now - last_activity[0]) >= self.settings.codex_idle_after_changes_seconds
                and self._tracked_changes_exist(worktree_path)
            ):
                timed_out = True
                timeout_reason = "idle_after_changes"
                self._terminate_process(process, live_log_path)
                break
            time.sleep(1)
        if timed_out:
            process.wait(timeout=5)
            if timeout_reason == "cancelled":
                timeout_message = f"{LOG_PREFIX} Command was stopped by an online command."
                monitor_message = "Local command was stopped by an online command."
            elif timeout_reason == "idle_after_changes":
                timeout_message = (
                    f"{LOG_PREFIX} Command went idle after producing local changes. "
                    "Stopping Codex and salvaging the existing diff."
                )
                monitor_message = "Local command went idle after producing changes; Scuba Steve is salvaging the existing diff."
            elif timeout_reason == "codex_queue_full":
                timeout_message = (
                    f"{LOG_PREFIX} Codex stopped responding after its in-process notification queue filled. "
                    "Stopping the local command so this training job can fail cleanly instead of hanging."
                )
                monitor_message = "Codex stopped responding because its local notification queue filled; Scuba Steve stopped the command."
            else:
                timeout_message = f"{LOG_PREFIX} Command timed out after {timeout_seconds} seconds."
                monitor_message = f"Local command timed out after {timeout_seconds} seconds."
            self._append_live_log_line(live_log_path, timeout_message)
            self._append_raw_log_line(live_log_path, timeout_message)
            self._emit_monitor_event(event_callback, monitor_message, event_type="warning")
        stdout_thread.join()
        stderr_thread.join()
        if exit_code is None:
            exit_code = process.returncode if process.returncode is not None else -1
        self._append_live_log_line(live_log_path, f"{LOG_PREFIX} Command exited with code {exit_code}.")
        self._append_raw_log_line(live_log_path, f"{LOG_PREFIX} Command exited with code {exit_code}.")
        self._emit_monitor_event(event_callback, f"Local command exited with code {exit_code}.")
        with self._active_processes_lock:
            self._active_processes.pop(process.pid, None)
        return "".join(stdout_parts), "".join(stderr_parts), exit_code, timed_out, timeout_reason

    def _terminate_process(self, process: subprocess.Popen[str], live_log_path: Path | None) -> None:
        if os.name == "nt":
            self._terminate_pid_tree(process.pid, live_log_path)
            return
        process.kill()

    def _stderr_line_counts_as_activity(self, line: str) -> bool:
        lowered = line.lower()
        ignored_fragments = (
            "dropping in-process server notification",
            "ignoring interface.defaultprompt",
            "codex_core_skills::loader: ignoring interface.icon",
        )
        return not any(fragment in lowered for fragment in ignored_fragments)

    def _is_codex_queue_full_warning(self, line: str) -> bool:
        return "dropping in-process server notification" in line.lower()

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

    def _handle_codex_stdout_line(
        self,
        live_log_path: Path | None,
        line: str,
        *,
        event_callback: Callable[[dict], None] | None = None,
    ) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            training_event = self._parse_training_event(stripped)
            if training_event is not None:
                self._append_live_log_line(live_log_path, f"[Training] {training_event.get('text', '').strip()}")
                if event_callback is not None:
                    event_callback(training_event)
                return
            self._append_live_log_line(live_log_path, f"[stdout] {stripped}")
            return

        event_type = str(payload.get("type") or "")
        if event_type == "thread.started":
            self._append_live_log_line(live_log_path, "[Codex] Session started.")
            self._emit_monitor_event(event_callback, "Codex session started.")
            return
        if event_type == "turn.started":
            self._append_live_log_line(live_log_path, "[Codex] Analyzing the request and choosing actions.")
            self._emit_monitor_event(event_callback, "Codex is analyzing the training request and choosing actions.")
            return
        if event_type == "turn.completed":
            self._append_live_log_line(live_log_path, "[Codex] Finished this run.")
            self._emit_monitor_event(event_callback, "Codex finished this run.")
            return
        if event_type in {"error", "turn.failed"}:
            message = str(payload.get("message") or "")
            if not message and isinstance(payload.get("error"), dict):
                message = str(payload["error"].get("message") or "")
            if message:
                self._append_live_log_line(live_log_path, f"[Codex] Error: {message}")
                self._emit_monitor_event(event_callback, f"Codex error: {message}", event_type="error")
            return

        item = payload.get("item")
        if not isinstance(item, dict):
            return

        item_type = str(item.get("type") or "")
        if item_type == "command_execution":
            command = self._format_monitor_command(str(item.get("command") or ""))
            if event_type == "item.started":
                self._append_live_log_line(live_log_path, f"[Codex] Running command: {command}")
                self._emit_monitor_event(event_callback, f"Codex is running command: {command}")
                return
            if event_type == "item.completed":
                exit_code = item.get("exit_code")
                message = f"Command finished with exit {exit_code}: {command}"
                self._append_live_log_line(live_log_path, f"[Codex] {message}")
                self._emit_monitor_event(event_callback, message)
                output_summary = self._summarize_command_output_for_monitor(command, str(item.get("aggregated_output") or ""))
                if output_summary:
                    self._append_live_log_line(live_log_path, f"[Codex] {output_summary}")
                    self._emit_monitor_event(event_callback, output_summary)
                return

        if item_type == "agent_message" and event_type == "item.completed":
            text = str(item.get("text") or "").strip()
            if not text:
                return
            for paragraph in text.splitlines():
                paragraph = paragraph.strip()
                if paragraph:
                    training_event = self._parse_training_event(paragraph)
                    if training_event is not None:
                        self._append_live_log_line(live_log_path, f"[Training] {training_event.get('text', '').strip()}")
                        if event_callback is not None:
                            event_callback(training_event)
                        continue
                    self._append_live_log_line(live_log_path, f"[Codex] {paragraph}")
                    self._emit_monitor_event(event_callback, paragraph)

    def _emit_monitor_event(
        self,
        event_callback: Callable[[dict], None] | None,
        text: str,
        *,
        event_type: str = "monitor",
    ) -> None:
        if event_callback is None:
            return
        cleaned = re.sub(r'\s+', ' ', str(text or '')).strip()
        if not cleaned:
            return
        event_callback({"type": event_type, "text": cleaned})

    def _parse_training_event(self, text: str) -> dict | None:
        prefix = "TRAINING_EVENT"
        if not text.startswith(prefix):
            return None
        raw_payload = text[len(prefix):].strip()
        if not raw_payload:
            return None
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            return {"type": "note", "text": raw_payload}
        if not isinstance(payload, dict):
            return None
        event_text = str(payload.get("text") or "").strip()
        if not event_text:
            return None
        return {
            "type": str(payload.get("type") or "note").strip() or "note",
            "text": event_text,
            **{key: value for key, value in payload.items() if key not in {"type", "text"}},
        }

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
