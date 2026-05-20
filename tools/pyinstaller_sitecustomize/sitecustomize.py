import platform

platform.system = lambda: "Windows"
platform.machine = lambda: "AMD64"
platform.win32_ver = lambda: ("10", "", "", "")
platform.platform = lambda *args, **kwargs: "Windows-10-AMD64"
