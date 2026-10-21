<#
.SYNOPSIS
    Install the Plex database node for this Windows user, start it at every logon (or, with -AtStartup, when Windows
    starts), and start it now.

.DESCRIPTION
    Copies the built PlexDbNode folder and uninstall.ps1 to %LOCALAPPDATA%\Programs\MediaPreviewGenerator\PlexDbNode,
    adds a "run at logon" entry for this user only (HKCU\Software\Microsoft\Windows\CurrentVersion\Run: no admin, and
    the node runs as the same user as Plex, which is what lets it open Plex's database), starts it, and prints the
    address and key to paste into the app (Settings -> Nodes -> Add node). Run it again to update the node: its key
    stays.

    With NODE_KEY set in this session, the node uses that key, and the installer keeps it in the node's node.json: the
    logon entry starts the node without this session's environment.

    Run it as the Windows user Plex runs as, not from an elevated prompt for another account. -AtStartup and
    -AddFirewallRule ask for elevation once between them.

.PARAMETER Source
    The built PlexDbNode folder (default: the folder beside this script, else dist\PlexDbNode of the repository).

.PARAMETER Port
    The port the node listens on (default 9494).

.PARAMETER PlexConfigDir
    Plex's data folder, when it isn't the one Plex uses by default for this user.

.PARAMETER AddFirewallRule
    Add an inbound Windows Firewall rule for the node (asks for elevation once).

.PARAMETER AtStartup
    Start the node when Windows starts, before anyone signs in, still as this user: for a Plex that runs as a service
    or starts at boot. A scheduled task replaces the logon entry (asks for elevation once; no password is stored).
    Run this again without it to go back to the logon entry.
#>
[CmdletBinding()]
param(
    [string]$Source,
    [int]$Port = 9494,
    [string]$PlexConfigDir,
    [switch]$AddFirewallRule,
    [switch]$AtStartup
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\MediaPreviewGenerator\PlexDbNode"
$NodeDir = Join-Path $env:LOCALAPPDATA "MediaPreviewGenerator\node"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValue = "MediaPreviewGeneratorPlexDbNode"
$RuleName = "Media Preview Generator Plex database node"
$TaskName = "Media Preview Generator Plex database node"
# Left in the program folder by -AtStartup: a task registered from an elevated prompt may be hidden from this user.
$TaskMarker = "starts-at-boot"

function ConvertTo-Argument([string]$Value) {
    # One argument, quoted the way the program's C runtime splits its command line: backslashes are literal except
    # before a quote, so a run of them before the closing quote is doubled ("D:\Plex\" would end in an escaped quote).
    '"' + ($Value -replace '(\\+)$', '$1$1') + '"'
}

function ConvertTo-Literal([string]$Value) {
    # A single-quoted PowerShell string: a quote inside one (a user folder like O'Brien) is written twice.
    "'" + $Value.Replace("'", "''") + "'"
}

function Invoke-Elevated([string]$Command, [string]$LogFile) {
    # Encoded: the elevated PowerShell gets the command intact, whatever spaces and quotes the paths hold. Its window
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

$HadTask = (Test-Path (Join-Path $InstallDir $TaskMarker)) -or
    [bool](Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)

# A running copy holds its files open; stop it before replacing them. One the task started is this user's too.
Get-Process -Name "PlexDbNode", "PlexDbNode-debug" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

if ($Source -ne $InstallDir) {
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -Recurse -Force (Join-Path $Source "*") $InstallDir
}
# The uninstaller goes with the program, so removing the node later doesn't need this download kept.
$Uninstaller = Join-Path $PSScriptRoot "uninstall.ps1"
if ((Test-Path $Uninstaller) -and $PSScriptRoot -ne $InstallDir) { Copy-Item -Force $Uninstaller $InstallDir }
$Exe = Join-Path $InstallDir "PlexDbNode.exe"

$NodeArgs = @("--port", "$Port")
if ($PlexConfigDir) { $NodeArgs += @("--plex-config-dir", $PlexConfigDir) }
$Quoted = ($NodeArgs | ForEach-Object { ConvertTo-Argument $_ }) -join " "

$identityFile = Join-Path $NodeDir "node.json"
$key = $null
if ($env:NODE_KEY -and $env:NODE_KEY.Trim()) {
    # Neither the logon entry nor the task starts the node with this session's NODE_KEY: keep the key in node.json,
    # which the node falls back to, before it starts, or it would expect a key nobody was shown.
    $key = $env:NODE_KEY.Trim()
    if (Test-Path $identityFile) {
        $identity = Get-Content -Raw $identityFile | ConvertFrom-Json
    } else {
        New-Item -ItemType Directory -Force -Path $NodeDir | Out-Null
        $identity = [pscustomobject]@{ id = [guid]::NewGuid().ToString("N") }
    }
    $identity | Add-Member -NotePropertyName key -NotePropertyValue $key -Force
    # This script shows the key below, so the node has nothing left to announce at its next start.
    $identity | Add-Member -NotePropertyName announced -NotePropertyValue $true -Force
    # No byte order mark: Windows PowerShell's -Encoding UTF8 writes one, and the node reads plain UTF-8.
    $noBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($identityFile, ($identity | ConvertTo-Json -Compress), $noBom)
}

$elevated = @()
if ($AtStartup) {
    # One starter only: the logon entry would start a second copy that can't take the port.
    Remove-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    # S4U runs it as this user with no password stored, with no sign-in needed; it reaches only this PC's files, which
    # is all the node opens. Priority 5 is normal: the default 7 would slow the writes Plex waits on.
    $elevated += "Register-ScheduledTask -Force -TaskName $(ConvertTo-Literal $TaskName) " +
        "-Action (New-ScheduledTaskAction -Execute $(ConvertTo-Literal $Exe) -Argument $(ConvertTo-Literal $Quoted) " +
        "-WorkingDirectory $(ConvertTo-Literal $InstallDir)) -Trigger (New-ScheduledTaskTrigger -AtStartup) " +
        "-Principal (New-ScheduledTaskPrincipal -UserId $(ConvertTo-Literal $user) -LogonType S4U -RunLevel Limited) " +
        "-Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries " +
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 5) | Out-Null"
    $elevated += "Start-ScheduledTask -TaskName $(ConvertTo-Literal $TaskName)"
} else {
    # Never New-Item -Force on a key that exists: it deletes the key, and every other program's logon entry with it.
    if (-not (Test-Path $RunKey)) { New-Item -Path $RunKey | Out-Null }
    Set-ItemProperty -Path $RunKey -Name $RunValue -Value ('{0} {1}' -f (ConvertTo-Argument $Exe), $Quoted)
    if ($HadTask) {
        $elevated += "Unregister-ScheduledTask -TaskName $(ConvertTo-Literal $TaskName) -Confirm:`$false " +
            "-ErrorAction SilentlyContinue"
    }
}
if ($AddFirewallRule) {
    $elevated += "Remove-NetFirewallRule -DisplayName $(ConvertTo-Literal $RuleName) -ErrorAction SilentlyContinue"
    $elevated += "New-NetFirewallRule -DisplayName $(ConvertTo-Literal $RuleName) -Direction Inbound -Action Allow " +
        "-Program $(ConvertTo-Literal $Exe) -Protocol TCP -LocalPort $Port -Profile Private,Domain | Out-Null"
}
if ($elevated) {
    New-Item -ItemType Directory -Force -Path $NodeDir | Out-Null
    Invoke-Elevated ($elevated -join "; ") (Join-Path $NodeDir "install-elevated.log")
}

if ($AtStartup) {
    Set-Content -LiteralPath (Join-Path $InstallDir $TaskMarker) -Value $TaskName
} else {
    # One quoted string: Windows PowerShell joins an argument array with bare spaces, splitting a path that has one.
    Start-Process -FilePath $Exe -ArgumentList $Quoted -WorkingDirectory $InstallDir
}

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

if (-not $key -and (Test-Path $identityFile)) {
    $key = (Get-Content -Raw $identityFile | ConvertFrom-Json).key
}

""
if ($AtStartup) {
    "The Plex database node is running and starts when Windows starts, before anyone signs in, as $env:USERNAME."
} else {
    "The Plex database node is running and starts at every logon of $env:USERNAME."
}
"  Address: http://${address}:$Port"
if ($key) { "  Key:     $key" } else { "  Key:     see $(Join-Path $NodeDir 'node.json')" }
"Paste both into the app: Settings -> Nodes -> Add node."
if ($AtStartup -and -not $AddFirewallRule) {
    "Windows can't ask to let it through the firewall when it starts before sign-in. If the app can't reach it, run"
    "this again with -AtStartup -AddFirewallRule."
} elseif (-not $AddFirewallRule) {
    "If the app can't reach it, run this again with -AddFirewallRule (or allow PlexDbNode.exe when Windows asks)."
}
