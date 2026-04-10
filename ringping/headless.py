from __future__ import annotations

import signal
import threading

from ringping.app import build_runtime, get_workspace_dir
from ringping.single_instance import SingleInstanceGuard


def main() -> None:
    workspace_dir = get_workspace_dir()
    instance_guard = SingleInstanceGuard(workspace_dir)
    if not instance_guard.acquire():
        return
    runtime = build_runtime(workspace_dir)
    stop_event = threading.Event()

    def handle_stop(signum, frame) -> None:  # noqa: ARG001
        stop_event.set()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    main()
