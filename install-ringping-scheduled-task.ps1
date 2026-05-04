param(
    [string]$TaskName = "Wake RingPing",
    [string]$WorkspaceDir = (Split-Path -Parent $PSCommandPath),
    [string]$PythonExecutable = "",
    [string]$UserName = "",
    [string]$Password = ""
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-PythonExecutable {
    param([string]$ConfiguredPath)

    if ($ConfiguredPath) {
        return $ConfiguredPath
    }

    $resolved = (& py -3.14 -c "import sys; print(sys.executable)" 2>$null).Trim()
    if ($resolved) {
        $pythonwCandidate = Join-Path (Split-Path -Parent $resolved) "pythonw.exe"
        if (Test-Path $pythonwCandidate) {
            return $pythonwCandidate
        }
        return $resolved
    }

    $pyw = Get-Command pyw.exe -ErrorAction SilentlyContinue
    if ($pyw) {
        return $pyw.Source
    }

    throw "Unable to resolve a Python executable for RingPing."
}

function Resolve-EffectiveUserName {
    param(
        [string]$ConfiguredUser,
        $ExistingTask
    )

    $candidate = $ConfiguredUser
    if (-not $candidate -and $ExistingTask) {
        $candidate = $ExistingTask.Principal.UserId
    }
    if (-not $candidate) {
        $candidate = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    }
    if ($candidate -match "[\\@]") {
        return $candidate
    }
    return "$env:COMPUTERNAME\$candidate"
}

function New-RingPingAction {
    param(
        [string]$LauncherPath
    )

    New-ScheduledTaskAction `
        -Execute $LauncherPath
}

function Write-RingPingLauncher {
    param(
        [string]$Directory,
        [string]$PythonPath
    )

    $launcherPath = Join-Path $Directory "run-ringping-watchdog-task.cmd"
    $contents = @"
@echo off
cd /d "$Directory"
"$PythonPath" -m ringping.watchdog
"@
    Set-Content -Path $launcherPath -Value $contents -Encoding Ascii
    return $launcherPath
}

function New-RingPingSettings {
    New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -MultipleInstances IgnoreNew `
        -RestartCount 10 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable `
        -WakeToRun
}

$invocationPath = (Resolve-Path $PSCommandPath).Path
if (-not (Test-IsAdministrator)) {
    $argumentList = @(
        "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $invocationPath),
        "-TaskName", ('"{0}"' -f $TaskName),
        "-WorkspaceDir", ('"{0}"' -f $WorkspaceDir)
    )
    if ($PythonExecutable) {
        $argumentList += @("-PythonExecutable", ('"{0}"' -f $PythonExecutable))
    }
    if ($UserName) {
        $argumentList += @("-UserName", ('"{0}"' -f $UserName))
    }
    if ($Password) {
        $argumentList += @("-Password", ('"{0}"' -f $Password))
    }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argumentList | Out-Null
    Write-Host "Requested elevation. Approve the UAC prompt to update the RingPing scheduled task."
    exit 0
}

$workspacePath = (Resolve-Path $WorkspaceDir).Path
$pythonPath = Resolve-PythonExecutable $PythonExecutable
$launcherPath = Write-RingPingLauncher -Directory $workspacePath -PythonPath $pythonPath
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$effectiveUser = Resolve-EffectiveUserName -ConfiguredUser $UserName -ExistingTask $existingTask
$taskPassword = $Password
if (-not $taskPassword) {
    $credential = Get-Credential -UserName $effectiveUser -Message "Enter the Windows password for the RingPing scheduled task."
    $effectiveUser = $credential.UserName
    $taskPassword = $credential.GetNetworkCredential().Password
}

$action = New-RingPingAction -LauncherPath $launcherPath
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn -User $effectiveUser)
)
$settings = New-RingPingSettings

$principal = New-ScheduledTaskPrincipal -UserId $effectiveUser -LogonType Password -RunLevel Limited
$task = New-ScheduledTask `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Starts the RingPing watchdog at boot and logon."

if ($existingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Register-ScheduledTask `
        -TaskName $TaskName `
        -InputObject $task `
        -User $effectiveUser `
        -Password $taskPassword | Out-Null
    $result = "Updated existing scheduled task '$TaskName'."
} else {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -InputObject $task `
        -User $effectiveUser `
        -Password $taskPassword | Out-Null
    $result = "Created scheduled task '$TaskName'."
}

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName

Write-Host $result
Write-Host "Task name: $($task.TaskName)"
Write-Host "Run as: $($task.Principal.UserId)"
Write-Host "Execute: $($task.Actions.Execute)"
Write-Host "Arguments: $($task.Actions.Arguments)"
Write-Host "Working directory: $($task.Actions.WorkingDirectory)"
Write-Host "Launcher path: $launcherPath"
Write-Host "State: $($task.State)"
Write-Host "Last result: $($info.LastTaskResult)"
