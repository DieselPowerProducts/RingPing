from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def headless_command(workspace_dir: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(workspace_dir / "RingPingHeadless.exe")]

    python_executable = Path(sys.executable)
    if python_executable.name.lower() == "python.exe":
        pythonw_candidate = python_executable.with_name("pythonw.exe")
        if pythonw_candidate.exists():
            python_executable = pythonw_candidate

    return [str(python_executable), "-m", "ringping.headless"]


def launch_headless(workspace_dir: Path) -> None:
    command = headless_command(workspace_dir)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    subprocess.Popen(
        command,
        cwd=workspace_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
