param(
    [string]$PythonVersion = "3.14"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$buildRoot = Join-Path $root "build\pyinstaller-headless"
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
    "RingPingHeadless"
    "--distpath"
    $root
    "--workpath"
    $buildRoot
    "--specpath"
    $specRoot
    "ringping\headless.py"
)

& py @arguments

if (-not (Test-Path (Join-Path $root "RingPingHeadless.exe"))) {
    throw "Build completed without creating RingPingHeadless.exe"
}

Write-Host "Built RingPingHeadless.exe"
