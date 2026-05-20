import os
import platform

if os.environ.get("RINGPING_PYINSTALLER_TRACEBACK_FILE"):
    import faulthandler

    traceback_file = open(os.environ["RINGPING_PYINSTALLER_TRACEBACK_FILE"], "w", encoding="utf-8")
    faulthandler.enable(file=traceback_file)
    faulthandler.dump_traceback_later(
        int(os.environ.get("RINGPING_PYINSTALLER_TRACEBACK_SECONDS", "20")),
        file=traceback_file,
    )

platform.system = lambda: "Windows"
platform.machine = lambda: "AMD64"
platform.win32_ver = lambda: ("10", "", "", "")
platform.platform = lambda *args, **kwargs: "Windows-10-AMD64"

from PyInstaller.__main__ import run

run()
