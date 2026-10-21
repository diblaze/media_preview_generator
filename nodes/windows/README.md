# Plex database node for Windows

A small program that runs beside Plex Media Server on Windows, so that Media Preview Generator, running on another
machine, can write into Plex's database: intro and credits markers, chapter thumbnails, loudness measurements and
Library Health's database counts.

## Who needs it

You need it when **Plex runs natively on Windows** (the normal Plex installer) and keeps its data on a **local disk of
that PC** (NTFS or ReFS: the default `%LOCALAPPDATA%\Plex Media Server`), while the app runs somewhere else (a NAS, a
Linux server, Docker, including Docker Desktop on the same PC).

Plex has no API for markers, so the app writes them into Plex's SQLite database itself. SQLite's locks only work
between programs on the same machine; over a network share they can corrupt the database. The node is the part of the
app that opens the database, running on Plex's machine; the app sends it each item's markers and it does the write,
with exactly the same checks the app runs when it sits beside Plex.

You don't need it when:

* the app runs natively on the same machine as Plex (it opens the database itself). The app in **Docker Desktop**
  on Plex's PC does need the node: Docker Desktop's mount of a Windows folder can't hold SQLite's locks, so the
  app can't open Plex's database through it. Add the node as `http://host.docker.internal:9494`;
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

   It copies the program and `uninstall.ps1` to `%LOCALAPPDATA%\Programs\MediaPreviewGenerator\PlexDbNode`, makes
   it start at every logon of this user, starts it, and prints the **address** and **key** to paste into the app
   (Settings → Nodes → Add node).

Options:

* `-AtStartup` starts the node when Windows starts, before anyone signs in, instead of at logon. Use it when Plex
  runs as a Windows service (for example with PmsService) or starts at boot: without it, Plex runs after a reboot but
  the node waits for someone to sign in. It asks for administrator rights once. Windows can't show its firewall
  question to a program started before sign-in, so add `-AddFirewallRule` when the app runs on another machine.
* `-AddFirewallRule` asks for administrator rights once to let other machines on private and domain networks reach
  the node (port 9494). Without it, allow `PlexDbNode.exe` when Windows asks, or add the rule yourself.
* `-Port 9500` listens on another port.
* `-PlexConfigDir "D:\PlexData\Plex Media Server"` names Plex's data folder. You rarely need it: the node finds the
  folder from Plex's own setting (the `LocalAppDataPath` registry value, when you moved Plex's data with it), else
  `%LOCALAPPDATA%\Plex Media Server`. Name only that folder: Plex on Windows keeps its server id in the registry, not
  in the folder, so for any other folder the node can't tell which Plex it belongs to and refuses loudness and
  chapter thumbnails.

The node keeps its id, key, log and loudness undo record in `%LOCALAPPDATA%\MediaPreviewGenerator\node`
(`node.json`, `logs\node.log`, `loudness-writes.jsonl`). To use a key of your own, set it before installing
(`$env:NODE_KEY = "..."`); the installer saves it in `node.json`, so it still works after a reboot.

To update it, download the newest release, unzip it and run `install.ps1` again as the same user, with the options
you installed with (`-Port`, `-PlexConfigDir`, `-AtStartup`). It replaces the program and keeps the key and the undo record.

To remove it:
`powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\MediaPreviewGenerator\PlexDbNode\uninstall.ps1"`.
`-RemoveData` also deletes the key and log but keeps the loudness undo record, the only way to put Plex's audio
tracks back; add `-RemoveUndoRecord` to delete it too. To undo loudness writes after removing the node, install it
again first.

## Undoing loudness writes

Stop Plex, then in PowerShell:

```powershell
& "$env:LOCALAPPDATA\Programs\MediaPreviewGenerator\PlexDbNode\PlexDbNode-debug.exe" --undo-loudness --dry-run
```

`--dry-run` only counts what would be put back; run it again without `--dry-run` to restore. It finds Plex's database
from its own record, also when you installed with `-PlexConfigDir`. What it restores and what it leaves alone is in
[Undoing it](../../docs/plex-loudness-normalization.md#undoing-it).

## Why it starts at logon, or with a task, instead of as a Windows service

The node has to run **as the same Windows user as Plex**: Plex's database files belong to that user, and the node
must be able to open them for writing without being given rights over anyone else's files. The standard Plex install
runs at that user's logon, so by default the node does too: a per-user logon entry needs no administrator rights,
starts with Plex's own session, and can't reach anything that user can't.

A Plex that runs as a service or starts at boot is up before anyone signs in. `-AtStartup` matches it with a
scheduled task that starts at boot as the same user, logged on without a stored password (Windows calls it S4U). A
task started that way can open files on this PC, which is all the node needs, but not network shares. A Windows
service would run as `SYSTEM` (far more access than it needs, and a different user from Plex) or need that user's
password stored.

## Build

On Windows with Python 3.12 and a git clone of the repository with its tags (the build reads the app's version from
them):

```powershell
powershell -ExecutionPolicy Bypass -File nodes\windows\build.ps1
```

This makes `dist\PlexDbNode\` (about 25 MB) with two programs: `PlexDbNode.exe`, which has no window and is what the
logon entry runs, and `PlexDbNode-debug.exe`, the same program with a console for watching it start. Only the Plex
database role's few libraries are bundled (`requirements-windows.txt`); nothing of the preview worker (FFmpeg, GPU
libraries) is.

`smoke_test.py` starts a build against a throwaway Plex folder and checks it answers and sees the database held open;
`.github/workflows/windows-node.yml` runs it on every change, then installs the build for the runner's user both
ways (logon entry and `-AtStartup`), calls it and uninstalls it. A `marker-agent-v*` tag builds it the same way and attaches
`PlexDbNode-windows.zip` to that tag's release (`.github/workflows/marker-agent.yml`).
