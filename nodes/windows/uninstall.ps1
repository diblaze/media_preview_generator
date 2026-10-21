<#
.SYNOPSIS
    Stop and remove the Plex database node for this Windows user.

.DESCRIPTION
    Stops the node, removes its logon entry and its program folder, and removes its start-at-boot task and firewall
    rule when there are any (asking for elevation once). The node's own folder (%LOCALAPPDATA%\MediaPreviewGenerator\node: its id, key, log
    and loudness undo record) stays, so a reinstall keeps the key the app already has.

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
$RuleName = "Media Preview Generator Plex database node"
$TaskName = "Media Preview Generator Plex database node"
$TaskMarker = Join-Path $InstallDir "starts-at-boot"

function ConvertTo-Literal([string]$Value) {
    # A single-quoted PowerShell string: a quote inside one (a user folder like O'Brien) is written twice.
    "'" + $Value.Replace("'", "''") + "'"
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
if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
    $elevated += "Remove-NetFirewallRule -DisplayName $(ConvertTo-Literal $RuleName)"
}

Get-Process -Name "PlexDbNode", "PlexDbNode-debug" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
Remove-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue
if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }

if ($elevated) {
    # Encoded: the elevated PowerShell gets the command intact, whatever spaces and quotes the names hold.
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($elevated -join "; "))
    $arguments = "-NoProfile -NonInteractive -EncodedCommand $encoded"
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Start-Process -FilePath "powershell.exe" -Wait -WindowStyle Hidden -ArgumentList $arguments
    } else {
        Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -ArgumentList $arguments
    }
}

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
