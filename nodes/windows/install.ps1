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
# Per user: each user's node is a program of its own, and one user mustn't replace or remove another user's.
$RuleName = "Media Preview Generator Plex database node ($env:USERNAME)"
$TaskName = "Media Preview Generator Plex database node ($env:USERNAME)"
# Left in the program folder by -AtStartup: a task registered from an elevated prompt may be hidden from this user.
$TaskMarker = "starts-at-boot"

function ConvertTo-Argument([string]$Value) {
    # One argument, quoted the way the program's C runtime splits its command line: backslashes are literal except
    # before a quote, so a run of them before the closing quote is doubled ("D:\Plex\" would end in an escaped quote).
    '"' + ($Value -replace '(\\+)$', '$1$1') + '"'
}

function ConvertTo-Literal([string]$Value) {
    # A single-quoted PowerShell string. PowerShell also ends one at a typographic quote (U+2018 to U+201B),
    # so this escapes those too, not only the plain quote.
    "'" + [Management.Automation.Language.CodeGeneration]::EscapeSingleQuotedStringContent($Value) + "'"
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

function Test-FirewallRule {
    # Get-NetFirewallRule is "Access is denied" in a normal window; the firewall's COM object still lists the rules.
    [bool]((New-Object -ComObject HNetCfg.FwPolicy2).Rules | Where-Object { $_.Name -eq $RuleName })
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
# Windows lets only an administrator register a task that starts at boot with no stored password, and only for their
# own account: a standard user's elevation runs as another account. whoami lists the Administrators group even in a
# normal window, where it is held back for elevation.
if ($AtStartup -and -not ((whoami /groups) -match "S-1-5-32-544")) {
    throw "-AtStartup needs $env:USERNAME to be an administrator on this PC: Windows only lets an administrator " +
        "start their own program at boot without a stored password. Nothing was changed. Run this without -AtStartup " +
        "(the node then starts at logon)."
}
if (-not (Test-Path (Join-Path $Source "PlexDbNode.exe"))) { throw "$Source has no PlexDbNode.exe." }

$HadTask = (Test-Path (Join-Path $InstallDir $TaskMarker)) -or
    [bool](Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)

# A running copy holds its files open; stop it before replacing them. The task's copy runs with this user's full
# administrator rights when the user has them (Windows applies UAC only to signed-in sessions), so a normal window
# can't stop it directly; Task Scheduler stops it for the task's own user.
if ($HadTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Wait-TaskStopped
}
Get-OwnNode | Stop-Process -Force
Start-Sleep -Milliseconds 500

if ($Source -ne $InstallDir) {
    # A stopped node's files stay locked for a moment after it exits.
    for ($i = 0; (Test-Path $InstallDir) -and $i -lt 20; $i++) {
        Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
        if (Test-Path $InstallDir) { Start-Sleep -Milliseconds 500 }
    }
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

# Never New-Item -Force on a key that exists: it deletes the key, and every other program's logon entry with it.
if (-not (Test-Path $RunKey)) { New-Item -Path $RunKey | Out-Null }
# Even with -AtStartup: if the administrator step doesn't run, the node still starts at logon.
Set-ItemProperty -Path $RunKey -Name $RunValue -Value ('{0} {1}' -f (ConvertTo-Argument $Exe), $Quoted)

$elevated = @()
# The firewall rule first: if it fails, the task step doesn't run.
if ($AddFirewallRule) {
    $elevated += "Remove-NetFirewallRule -DisplayName $(ConvertTo-Literal $RuleName) -ErrorAction SilentlyContinue"
    # Dismissing Windows' own firewall question for the program leaves block rules for it, and a block beats any allow.
    $elevated += "Get-NetFirewallApplicationFilter | Where-Object { `$_.Program -eq $(ConvertTo-Literal $Exe) } | " +
        "Get-NetFirewallRule | Where-Object { `$_.Direction -eq 'Inbound' -and `$_.Action -eq 'Block' } | " +
        "Remove-NetFirewallRule"
    $elevated += "New-NetFirewallRule -DisplayName $(ConvertTo-Literal $RuleName) -Direction Inbound -Action Allow " +
        "-Program $(ConvertTo-Literal $Exe) -Protocol TCP -LocalPort $Port -Profile Private,Domain | Out-Null"
}
if ($AtStartup) {
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
} elseif ($HadTask) {
    $elevated += "Unregister-ScheduledTask -TaskName $(ConvertTo-Literal $TaskName) -Confirm:`$false " +
        "-ErrorAction SilentlyContinue"
}
$failure = $null
if ($elevated) {
    New-Item -ItemType Directory -Force -Path $NodeDir | Out-Null
    try {
        Invoke-Elevated ($elevated -join "; ") (Join-Path $NodeDir "install-elevated.log")
    } catch {
        # Declined or failed: the logon entry stays the node's starter, and it starts now as it would at logon.
        $failure = $_.Exception.Message
    }
}
$KeptTask = $false
if ($failure -and $HadTask) {
    # The boot task from before is still registered: it stays the one starter, as it was.
    try {
        Start-ScheduledTask -TaskName $TaskName
        $KeptTask = $true
    } catch { }
}
$BootTask = ($AtStartup -and -not $failure) -or $KeptTask

if ($BootTask) {
    # One starter only: the logon entry would start a second copy that can't take the port.
    Remove-ItemProperty -Path $RunKey -Name $RunValue
} else {
    # One quoted string: Windows PowerShell joins an argument array with bare spaces, splitting a path that has one.
    Start-Process -FilePath $Exe -ArgumentList $Quoted -WorkingDirectory $InstallDir
}

if ($BootTask -or ($failure -and $HadTask)) {
    # Replacing the folder removed it, and a task that stays registered still needs it to be found again.
    Set-Content -LiteralPath (Join-Path $InstallDir $TaskMarker) -Value $TaskName
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
    $why = if ($failure) { " The administrator step didn't run either: $failure" } else { "" }
    throw "The node didn't answer on $healthUrl; see $(Join-Path $NodeDir 'logs\node.log').$why"
}

$address = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -First 1 -ExpandProperty IPAddress
if (-not $address) { $address = $env:COMPUTERNAME }

if (-not $key -and (Test-Path $identityFile)) {
    $key = (Get-Content -Raw $identityFile | ConvertFrom-Json).key
}

""
if ($BootTask) {
    "The Plex database node is running and starts when Windows starts, before anyone signs in, as $env:USERNAME."
} else {
    "The Plex database node is running and starts at every logon of $env:USERNAME."
}
"  Address: http://${address}:$Port"
if ($key) { "  Key:     $key" } else { "  Key:     see $(Join-Path $NodeDir 'node.json')" }
"Paste both into the app: Settings -> Nodes -> Add node."
if ($failure) {
    Write-Warning "The administrator step didn't run: $failure"
    if ($AtStartup -and -not $KeptTask) {
        Write-Warning "The node starts at logon instead of at boot. Run this again to retry."
    }
    if ($KeptTask -and -not $AtStartup) {
        Write-Warning "It still starts at boot: its boot task is still there. Run this again to switch to logon."
    }
    if ($HadTask -and -not $KeptTask -and -not $AtStartup) {
        Write-Warning "Its boot task may still be there. Run this again to retry."
    }
    if ($AddFirewallRule -and -not (Test-FirewallRule)) { Write-Warning "No firewall rule was added." }
    exit 1
}
if ($AtStartup -and -not $AddFirewallRule) {
    "Windows can't ask to let it through the firewall when it starts before sign-in. If the app can't reach it, run"
    "this again with -AtStartup -AddFirewallRule."
} elseif (-not $AddFirewallRule) {
    "If the app can't reach it, run this again with -AddFirewallRule (or allow PlexDbNode.exe when Windows asks)."
}
