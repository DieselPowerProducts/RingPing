from __future__ import annotations

import os
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

from ringping.codex_runner import CodexRunner
from ringping.config import load_project_configs, load_settings
from ringping.controller import AppController
from ringping.email_notifier import ReviewEmailNotifier
from ringping.git_ops import GitWorktreeManager
from ringping.ringcentral import RingCentralClient
from ringping.release_monitor import ReleaseMonitor
from ringping.storage import Storage
from ringping.single_instance import MODE_HEADLESS, MODE_UI, SingleInstanceGuard
from ringping.ui import DashboardApp
from ringping.poller import RingCentralPoller
from ringping.webhook import WebhookServer
from ringping.worker import RequestWorker


class Runtime:
    def __init__(self, controller: AppController, worker: RequestWorker, poller: RingCentralPoller, release_monitor: ReleaseMonitor, webhook_server: WebhookServer, startup_notice: str) -> None:
        self.controller = controller
        self.worker = worker
        self.poller = poller
        self.release_monitor = release_monitor
        self.webhook_server = webhook_server
        self.startup_notice = startup_notice

    def shutdown(self) -> None:
        self.worker.stop()
        self.poller.stop()
        self.release_monitor.stop()
        self.webhook_server.stop()


def get_workspace_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def build_runtime(workspace_dir: Path) -> Runtime:
    settings = load_settings(workspace_dir)

    storage = Storage(settings.db_path)
    storage.initialize()
    storage.sync_projects(load_project_configs(settings))

    git_manager = GitWorktreeManager(settings.worktrees_dir)
    ringcentral_client = RingCentralClient(settings)
    review_email_notifier = ReviewEmailNotifier(settings)
    controller = AppController(settings, storage, git_manager, ringcentral_client)
    codex_runner = CodexRunner(settings)
    worker = RequestWorker(settings, storage, git_manager, codex_runner, ringcentral_client, review_email_notifier)
    worker.start()
    poller = RingCentralPoller(settings, storage, controller, ringcentral_client)
    poller.start()
    release_monitor = ReleaseMonitor(settings, storage, ringcentral_client)
    release_monitor.start()

    startup_notice = ""
    webhook_server = WebhookServer(
        settings.webhook_host,
        settings.webhook_port,
        controller.ingest_ringcentral_payload,
        verification_token=settings.ringcentral_verification_token,
    )
    try:
        webhook_server.start()
    except OSError as exc:
        startup_notice = f"Webhook server failed to start: {exc}"

    return Runtime(controller, worker, poller, release_monitor, webhook_server, startup_notice)


def _headless_command(workspace_dir: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(workspace_dir / "RingPingHeadless.exe")]

    python_executable = Path(sys.executable)
    if python_executable.name.lower() == "python.exe":
        pythonw_candidate = python_executable.with_name("pythonw.exe")
        if pythonw_candidate.exists():
            python_executable = pythonw_candidate

    return [str(python_executable), "-m", "ringping.headless"]


def launch_headless(workspace_dir: Path) -> None:
    command = _headless_command(workspace_dir)
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


def main() -> None:
    workspace_dir = get_workspace_dir()
    instance_guard = SingleInstanceGuard(workspace_dir)
    if not instance_guard.acquire(MODE_UI):
        running_mode = instance_guard.get_running_mode()
        if running_mode == MODE_HEADLESS and instance_guard.acquire_after_headless_shutdown():
            time.sleep(0.5)
        else:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("RingPing", "RingPing is already running.")
            root.destroy()
            return
    runtime = build_runtime(workspace_dir)
    try:
        app = DashboardApp(runtime.controller, runtime.shutdown, startup_notice=runtime.startup_notice)
        app.mainloop()
    finally:
        runtime.shutdown()
        instance_guard.release()
        time.sleep(0.25)
        launch_headless(workspace_dir)
    os._exit(0)


if __name__ == "__main__":
    main()
