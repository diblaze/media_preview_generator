<#
.SYNOPSIS
    Stop and remove the Plex database node for this Windows user.

.DESCRIPTION
    Stops the node, removes its logon entry and its program folder, and removes its start-at-boot task and firewall
    rule when there are any (asking for elevation once; if that doesn't run, nothing is removed). The node's own
    folder (%LOCALAPPDATA%\MediaPreviewGenerator\node: its id, key, log and loudness undo record) stays, so a
    reinstall keeps the key the app already has.

    -RemoveData deletes the id, key and log. The loudness undo record (loudness-writes.jsonl) stays unless you add
    -RemoveUndoRecord: it is the only way to put back the Plex audio tracks the node wrote loudness to.
#>
[CmdletBinding()]
param(
    [switch]$RemoveData,
    [switch]$RemoveUndoRecord
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\MediaPreviewGenerator\PlexDbNode"
$NodeDir = Join-Path $env:LOCALAPPDATA "MediaPreviewGenerator\node"
$UndoRecord = "loudness-writes.jsonl"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValue = "MediaPreviewGeneratorPlexDbNode"
# Per user: each user's node is a program of its own, and one user mustn't replace or remove another user's.
$RuleName = "Media Preview Generator Plex database node ($env:USERNAME)"
$TaskName = "Media Preview Generator Plex database node ($env:USERNAME)"
$TaskMarker = Join-Path $InstallDir "starts-at-boot"

function ConvertTo-Literal([string]$Value) {
    # A single-quoted PowerShell string. PowerShell also ends one at a typographic quote (U+2018 to U+201B),
    # so this escapes those too, not only the plain quote.
    "'" + [Management.Automation.Language.CodeGeneration]::EscapeSingleQuotedStringContent($Value) + "'"
}

function Invoke-Elevated([string]$Command, [string]$LogFile) {
    # Encoded: the elevated PowerShell gets the command intact, whatever spaces and quotes the names hold. Its window
    # closes when it ends, so a failure is written to $LogFile and shown here.
    $wrapped = "`$ErrorActionPreference = 'Stop'; try { $Command } catch { " +
        "`$_ | Out-File -Encoding utf8 -LiteralPath $(ConvertTo-Literal $LogFile); exit 1 }"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($wrapped))
    $arguments = "-NoProfile -NonInteractive -EncodedCommand $encoded"
    Remove-Item -LiteralPath $LogFile -ErrorAction SilentlyContinue
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $process = Start-Process -FilePath "powershell.exe" -Wait -PassThru -WindowStyle Hidden -ArgumentList $arguments
    } else {
        $process = Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -PassThru -ArgumentList $arguments
    }
    if ($process.ExitCode) {
        $why = if (Test-Path -LiteralPath $LogFile) { (Get-Content -Raw -LiteralPath $LogFile).Trim() } else { "" }
        throw "The administrator step failed (exit $($process.ExitCode)). $why"
    }
}

function Wait-TaskStopped {
    # Stop-ScheduledTask returns before the process has gone, and a normal window can't even read that process's path.
    for ($i = 0; $i -lt 20; $i++) {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task -or $task.State -ne "Running") { return }
        Start-Sleep -Milliseconds 500
    }
    # Its files would stay locked, and only Task Scheduler can stop it from here.
    throw "The node's boot task didn't stop within 10 seconds, so nothing was changed. Run this again."
}

function Get-OwnNode {
    # Only this user's copy: another Windows user's node isn't ours to stop, and its path can't even be read here.
    Get-Process -Name "PlexDbNode", "PlexDbNode-debug" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith("$InstallDir\", [StringComparison]::OrdinalIgnoreCase) }
}

# This script may run from the program folder it deletes, and Windows can't delete a process's working folder.
Set-Location $env:USERPROFILE
[Environment]::CurrentDirectory = $env:USERPROFILE

$elevated = @()
# A task registered from an elevated prompt may be hidden from this user; install.ps1 leaves a marker for it.
if ((Test-Path $TaskMarker) -or (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    $elevated += "Unregister-ScheduledTask -TaskName $(ConvertTo-Literal $TaskName) -Confirm:`$false " +
        "-ErrorAction SilentlyContinue"
}
# Get-NetFirewallRule is "Access is denied" in a normal window; the firewall's COM object still lists the rules.
if ((New-Object -ComObject HNetCfg.FwPolicy2).Rules | Where-Object { $_.Name -eq $RuleName }) {
    $elevated += "Remove-NetFirewallRule -DisplayName $(ConvertTo-Literal $RuleName)"
}

# The task's copy may run with administrator rights a normal window can't stop; Task Scheduler stops it for its user.
# Stopped before the task is unregistered: after that, nothing here could stop it.
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Wait-TaskStopped

if ($elevated) {
    New-Item -ItemType Directory -Force -Path $NodeDir | Out-Null
    try {
        Invoke-Elevated ($elevated -join "; ") (Join-Path $NodeDir "uninstall-elevated.log")
    } catch {
        # Nothing removed yet, so running this again retries all of it; the task's node starts again meanwhile.
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        throw "The administrator step didn't run, so nothing was removed: $($_.Exception.Message)"
    }
}

Get-OwnNode | Stop-Process -Force
Start-Sleep -Milliseconds 500
Remove-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue
# A stopped node's files stay locked for a moment after it exits.
for ($i = 0; (Test-Path $InstallDir) -and $i -lt 20; $i++) {
    Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
    if (Test-Path $InstallDir) { Start-Sleep -Milliseconds 500 }
}
if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }

if (Test-Path $NodeDir) {
    Get-ChildItem -Force $NodeDir |
        Where-Object { if ($_.Name -eq $UndoRecord) { $RemoveUndoRecord } else { $RemoveData } } |
        Remove-Item -Recurse -Force
    if ($RemoveData -and -not (Get-ChildItem -Force $NodeDir)) { Remove-Item -Force $NodeDir }
}

$record = Join-Path $NodeDir $UndoRecord
if ($RemoveData -and (Test-Path $record)) {
    $kept = " Its loudness undo record stays: $record"
} elseif (Test-Path $record) {
    $kept = " Its key and loudness undo record stay in $NodeDir."
} elseif (-not $RemoveData -and (Test-Path $NodeDir)) {
    $kept = " Its key stays in $NodeDir."
} else {
    $kept = ""
}
"The Plex database node is removed." + $kept
