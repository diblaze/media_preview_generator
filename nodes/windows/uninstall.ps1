<#
.SYNOPSIS
    Stop and remove the Plex database node for this Windows user.

.DESCRIPTION
    Stops the node, removes its logon entry and its program folder, and removes its firewall rule when there is one
    (asking for elevation once). The node's own folder (%LOCALAPPDATA%\MediaPreviewGenerator\node: its id, key and
    log) stays, so a reinstall keeps the key the app already has; -RemoveData deletes it too.
#>
[CmdletBinding()]
param(
    [switch]$RemoveData
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\MediaPreviewGenerator\PlexDbNode"
$NodeDir = Join-Path $env:LOCALAPPDATA "MediaPreviewGenerator\node"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValue = "MediaPreviewGeneratorPlexDbNode"
$RuleName = "Media Preview Generator Plex database node"

Get-Process -Name "PlexDbNode", "PlexDbNode-debug" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
Remove-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue
if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }

if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
    $command = "Remove-NetFirewallRule -DisplayName '$RuleName'"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -ArgumentList "-NoProfile -EncodedCommand $encoded"
}

if ($RemoveData -and (Test-Path $NodeDir)) { Remove-Item -Recurse -Force $NodeDir }

"The Plex database node is removed." + $(if (-not $RemoveData -and (Test-Path $NodeDir)) { " Its key stays in $NodeDir." } else { "" })
