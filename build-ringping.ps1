param(
    [string]$PythonVersion = "3.14"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$buildRoot = Join-Path $root "build\pyinstaller"
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
    "RingPing"
    "--distpath"
    $root
    "--workpath"
    $buildRoot
    "--specpath"
    $specRoot
    "ringping\app.py"
)

& py @arguments

if (-not (Test-Path (Join-Path $root "RingPing.exe"))) {
    throw "Build completed without creating RingPing.exe"
}

Write-Host "Built RingPing.exe"
