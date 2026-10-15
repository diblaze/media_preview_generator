# Plex database node for Windows

A small program that runs beside Plex Media Server on Windows, so that Media Preview Generator, running on another
machine, can write intro and credits markers (and chapter thumbnails) into Plex's database.

## Who needs it

You need it when **Plex runs natively on Windows** (the normal Plex installer) and keeps its data on a **local disk of
that PC** (NTFS or ReFS: the default `%LOCALAPPDATA%\Plex Media Server`), while the app runs somewhere else (a NAS, a
Linux server, Docker).

Plex has no API for markers, so the app writes them into Plex's SQLite database itself. SQLite's locks only work
between programs on the same machine; over a network share they can corrupt the database. The node is the part of the
app that opens the database, running on Plex's machine; the app sends it each item's markers and it does the write,
with exactly the same checks the app runs when it sits beside Plex.

You don't need it when:

* the app runs on the same machine as Plex (it opens the database itself);
* **Plex runs in Docker Desktop** (WSL 2) or any Linux container: use the Linux Plex database node
  (`nodes/plex-db/`) instead, inside the same Docker setup as Plex;
* Plex's data folder is on a network drive: SQLite can't be shared safely there, and the node refuses it.

## Install

1. Download `PlexDbNode-windows.zip` from the newest **Plex database node** release on the
   [Releases page](https://github.com/stevezau/media_preview_generator/releases) and unzip it anywhere. It holds
   `install.ps1`, `uninstall.ps1`, this README and the `PlexDbNode` program folder. No release with it yet? Build it
   yourself ([Build](#build), below) and run `nodes\windows\install.ps1` from the checkout in step 2: it finds the
   build in `dist\PlexDbNode`.
2. Sign in to Windows **as the user Plex runs as**, open PowerShell in the unzipped folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   It copies the program to `%LOCALAPPDATA%\Programs\MediaPreviewGenerator\PlexDbNode`, makes it start at every logon
   of this user, starts it, and prints the **address** and **key** to paste into the app
   (Settings → Nodes → Add node).

Options:

* `-AddFirewallRule` asks for administrator rights once to let other machines on private and domain networks reach
  the node (port 9494). Without it, allow `PlexDbNode.exe` when Windows asks, or add the rule yourself.
* `-Port 9500` listens on another port.
* `-PlexConfigDir "D:\PlexData\Plex Media Server"` when Plex's data folder isn't found by itself. By default the node
  uses Plex's own setting (the `LocalAppDataPath` registry value, when you moved Plex's data with it), else
  `%LOCALAPPDATA%\Plex Media Server`.

The node keeps its id, key, log and loudness undo record in `%LOCALAPPDATA%\MediaPreviewGenerator\node`
(`node.json`, `logs\node.log`, `loudness-writes.jsonl`). To use a key of your own, set it before installing
(`$env:NODE_KEY = "..."`); the installer saves it in `node.json`, so it still works after a reboot.

To remove it: `powershell -ExecutionPolicy Bypass -File .\uninstall.ps1` (`-RemoveData` also deletes the key).

## Undoing loudness writes

Stop Plex, then in PowerShell:

```powershell
& "$env:LOCALAPPDATA\Programs\MediaPreviewGenerator\PlexDbNode\PlexDbNode-debug.exe" --undo-loudness --dry-run
```

`--dry-run` only counts what would be put back; run it again without `--dry-run` to restore. It finds Plex's database
from its own record, also when you installed with `-PlexConfigDir`. What it restores and what it leaves alone is in
[Undoing it](../../docs/plex-loudness-normalization.md#undoing-it).

## Why it starts at logon instead of as a Windows service

The node has to run **as the same Windows user as Plex**: Plex's database files belong to that user, and the node
must be able to open them for writing without being given rights over anyone else's files. The standard Plex install
also runs at that user's logon, not as a service. A service would run as `SYSTEM` (far more access than it needs, and
a different user from Plex) or need that user's password stored in the service settings. A per-user logon entry needs
no administrator rights, starts and stops with Plex's own session, and can't reach anything that user can't.

## Build

On Windows with Python 3.12 and a git checkout of the repository:

```powershell
powershell -ExecutionPolicy Bypass -File nodes\windows\build.ps1
```

This makes `dist\PlexDbNode\` (about 25 MB) with two programs: `PlexDbNode.exe`, which has no window and is what the
logon entry runs, and `PlexDbNode-debug.exe`, the same program with a console for watching it start. Only the Plex
database role's few libraries are bundled (`requirements-windows.txt`); nothing of the preview worker (FFmpeg, GPU
libraries) is.

`smoke_test.py` starts a build against a throwaway Plex folder and checks it answers and sees the database held open;
`.github/workflows/windows-node.yml` runs it on every change. A `marker-agent-v*` tag builds it the same way and attaches
`PlexDbNode-windows.zip` to that tag's release (`.github/workflows/marker-agent.yml`).
