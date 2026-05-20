param(
    [string]$PythonVersion = "3.14"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = (Join-Path $root "tools\pyinstaller_sitecustomize")
if ($previousPythonPath) {
    $env:PYTHONPATH = "$env:PYTHONPATH;$previousPythonPath"
}

$buildRoot = Join-Path $root "build\pyinstaller-watchdog"
$specRoot = Join-Path $buildRoot "spec"

if (Test-Path $buildRoot) {
    Remove-Item -LiteralPath $buildRoot -Recurse -Force
}

$arguments = @(
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

try {
    & py "-$PythonVersion" "tools\pyinstaller_bootstrap.py" @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

if (-not (Test-Path (Join-Path $root "RingPingWatchdog.exe"))) {
    throw "Build completed without creating RingPingWatchdog.exe"
}

Write-Host "Built RingPingWatchdog.exe"
