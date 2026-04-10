from __future__ import annotations

import signal
import threading

from ringping.app import get_workspace_dir
from ringping.launcher import launch_headless
from ringping.single_instance import SingleInstanceGuard


def _has_live_instance(guard: SingleInstanceGuard) -> bool:
    state = guard.get_running_state()
    pid = state.get("pid")
    try:
        parsed_pid = int(pid)
    except (TypeError, ValueError):
        parsed_pid = 0
    if parsed_pid and guard.is_pid_running(parsed_pid):
        return True
    guard.clear_stale_state()
    return False


def main() -> None:
    workspace_dir = get_workspace_dir()
    guard = SingleInstanceGuard(workspace_dir)
    stop_event = threading.Event()

    def handle_stop(signum, frame) -> None:  # noqa: ARG001
        stop_event.set()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    while not stop_event.is_set():
        if not _has_live_instance(guard):
            launch_headless(workspace_dir)
        stop_event.wait(5)


if __name__ == "__main__":
    main()
