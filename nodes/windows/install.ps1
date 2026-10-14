<#
.SYNOPSIS
    Install the Plex database node for this Windows user, start it at every logon, and start it now.

.DESCRIPTION
    Copies the built PlexDbNode folder to %LOCALAPPDATA%\Programs\MediaPreviewGenerator\PlexDbNode, adds a
    "run at logon" entry for this user only (HKCU\Software\Microsoft\Windows\CurrentVersion\Run: no admin, and the
    node runs as the same user as Plex, which is what lets it open Plex's database), starts it, and prints the address
    and key to paste into the app (Settings -> Nodes -> Add node).

    Run it as the Windows user Plex runs as, not from an elevated prompt for another account. -AddFirewallRule asks
    for elevation once, to let other machines on private and domain networks reach the node's port.

.PARAMETER Source
    The built PlexDbNode folder (default: the folder beside this script, else dist\PlexDbNode of the repository).

.PARAMETER Port
    The port the node listens on (default 9494).

.PARAMETER PlexConfigDir
    Plex's data folder, when it isn't the one Plex uses by default for this user.

.PARAMETER AddFirewallRule
    Add an inbound Windows Firewall rule for the node (asks for elevation once).
#>
[CmdletBinding()]
param(
    [string]$Source,
    [int]$Port = 9494,
    [string]$PlexConfigDir,
    [switch]$AddFirewallRule
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\MediaPreviewGenerator\PlexDbNode"
$NodeDir = Join-Path $env:LOCALAPPDATA "MediaPreviewGenerator\node"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValue = "MediaPreviewGeneratorPlexDbNode"
$RuleName = "Media Preview Generator Plex database node"

if (-not $Source) {
    $candidates = @(
        $PSScriptRoot,
        (Join-Path $PSScriptRoot "PlexDbNode"),
        (Join-Path $PSScriptRoot "..\..\dist\PlexDbNode")
    )
    $Source = $candidates | Where-Object { Test-Path (Join-Path $_ "PlexDbNode.exe") } | Select-Object -First 1
    if (-not $Source) { throw "No built PlexDbNode folder found; run build.ps1 first or pass -Source." }
}
$Source = (Resolve-Path $Source).Path
if (-not (Test-Path (Join-Path $Source "PlexDbNode.exe"))) { throw "$Source has no PlexDbNode.exe." }

# A running copy holds its files open; stop it before replacing them.
Get-Process -Name "PlexDbNode", "PlexDbNode-debug" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

if ($Source -ne $InstallDir) {
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -Recurse -Force (Join-Path $Source "*") $InstallDir
}
$Exe = Join-Path $InstallDir "PlexDbNode.exe"

$NodeArgs = @("--port", "$Port")
if ($PlexConfigDir) { $NodeArgs += @("--plex-config-dir", $PlexConfigDir) }
$Quoted = ($NodeArgs | ForEach-Object { '"{0}"' -f $_ }) -join " "
New-Item -Path $RunKey -Force | Out-Null
Set-ItemProperty -Path $RunKey -Name $RunValue -Value ('"{0}" {1}' -f $Exe, $Quoted)

if ($AddFirewallRule) {
    $command = "Remove-NetFirewallRule -DisplayName '$RuleName' -ErrorAction SilentlyContinue; " +
        "New-NetFirewallRule -DisplayName '$RuleName' -Direction Inbound -Action Allow -Program '$Exe' " +
        "-Protocol TCP -LocalPort $Port -Profile Private,Domain | Out-Null"
    # Encoded: the elevated PowerShell gets the command intact, whatever spaces and quotes the paths hold.
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -ArgumentList "-NoProfile -EncodedCommand $encoded"
}

# One quoted string: Windows PowerShell joins an argument array with bare spaces, splitting a path that has one.
Start-Process -FilePath $Exe -ArgumentList $Quoted -WorkingDirectory $InstallDir

$healthUrl = "http://127.0.0.1:$Port/v1/health"
$up = $false
for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2 | Out-Null
        $up = $true
    } catch { }
}
if (-not $up) {
    throw "The node didn't answer on $healthUrl; see $(Join-Path $NodeDir 'logs\node.log')."
}

$address = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -First 1).IPAddress
if (-not $address) { $address = $env:COMPUTERNAME }

$key = $env:NODE_KEY
if (-not $key -and (Test-Path (Join-Path $NodeDir "node.json"))) {
    $key = (Get-Content -Raw (Join-Path $NodeDir "node.json") | ConvertFrom-Json).key
}

""
"The Plex database node is running and starts at every logon of $env:USERNAME."
"  Address: http://${address}:$Port"
if ($key) { "  Key:     $key" } else { "  Key:     see $(Join-Path $NodeDir 'node.json')" }
"Paste both into the app: Settings -> Nodes -> Add node."
if (-not $AddFirewallRule) {
    "If the app can't reach it, run this again with -AddFirewallRule (or allow PlexDbNode.exe when Windows asks)."
}
