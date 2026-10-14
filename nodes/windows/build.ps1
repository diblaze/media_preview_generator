<#
.SYNOPSIS
    Build the Windows Plex database node (dist\PlexDbNode) with PyInstaller.

.DESCRIPTION
    Makes a private virtual environment under build\windows-node, installs requirements-windows.txt and the app's own
    package without its dependencies (the node needs only the Plex database role's few libraries), then runs
    plex-db-node.spec. Run it from anywhere; paths are relative to the repository root. Needs Python 3.12 on PATH (or
    -Python), and a git checkout with tags so the app's version can be worked out.

.PARAMETER Python
    The Python to build with (default: python).

.PARAMETER Clean
    Start from a fresh virtual environment and PyInstaller work folder.
#>
[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$Clean
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Venv = Join-Path $Root "build\windows-node\venv"
$Work = Join-Path $Root "build\windows-node\pyinstaller"
$Dist = Join-Path $Root "dist"

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

if ($Clean) {
    foreach ($path in @($Venv, $Work, (Join-Path $Dist "PlexDbNode"))) {
        if (Test-Path $path) { Remove-Item -Recurse -Force $path }
    }
}
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Invoke-Checked $Python @("-m", "venv", $Venv)
}
$VenvPython = Join-Path $Venv "Scripts\python.exe"

Invoke-Checked $VenvPython @("-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", (Join-Path $PSScriptRoot "requirements-windows.txt"))
# Not editable: PyInstaller bundles an installed package, with the version file setuptools-scm writes into it.
Invoke-Checked $VenvPython @("-m", "pip", "install", "--disable-pip-version-check", "-q", "--no-deps", $Root)
Invoke-Checked $VenvPython @(
    "-m", "PyInstaller", "--noconfirm", "--clean",
    "--distpath", $Dist, "--workpath", $Work,
    (Join-Path $PSScriptRoot "plex-db-node.spec")
)

$Out = Join-Path $Dist "PlexDbNode"
$SizeMb = (Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
"Built {0} ({1:N1} MB): PlexDbNode.exe (no console) and PlexDbNode-debug.exe (console)" -f $Out, $SizeMb
