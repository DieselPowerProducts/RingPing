param(
    [string]$PythonVersion = "3.14"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$buildRoot = Join-Path $root "build\pyinstaller-watchdog"
$specRoot = Join-Path $buildRoot "spec"

if (Test-Path $buildRoot) {
    Remove-Item -LiteralPath $buildRoot -Recurse -Force
}

$arguments = @(
    "-$PythonVersion"
    "-m"
    "PyInstaller"
    "--noconfirm"
    "--clean"
    "--onefile"
    "--windowed"
    "--name"
    "RingPingWatchdog"
    "--distpath"
    $root
    "--workpath"
    $buildRoot
    "--specpath"
    $specRoot
    "ringping\watchdog.py"
)

& py @arguments

if (-not (Test-Path (Join-Path $root "RingPingWatchdog.exe"))) {
    throw "Build completed without creating RingPingWatchdog.exe"
}

Write-Host "Built RingPingWatchdog.exe"
