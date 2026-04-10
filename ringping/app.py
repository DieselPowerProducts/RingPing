from __future__ import annotations

import sys
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
from ringping.single_instance import SingleInstanceGuard
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


def main() -> None:
    workspace_dir = get_workspace_dir()
    instance_guard = SingleInstanceGuard(workspace_dir)
    if not instance_guard.acquire():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("RingPing", "RingPing is already running.")
        root.destroy()
        return
    runtime = build_runtime(workspace_dir)
    app = DashboardApp(runtime.controller, runtime.shutdown, startup_notice=runtime.startup_notice)
    app.mainloop()


if __name__ == "__main__":
    main()
