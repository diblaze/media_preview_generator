# Plex database node for Windows

A small program that runs beside Plex Media Server on Windows. It lets Media Preview Generator, running on another
machine, write into Plex's database: Intro & Credits markers, chapter thumbnails, loudness and Library Health counts.

## Do you need it?

Yes, when both of these are true:

- Plex runs on Windows with the normal Plex installer, and keeps its data on a local disk of that PC (NTFS or ReFS,
  by default `%LOCALAPPDATA%\Plex Media Server`).
- The app runs somewhere else: a NAS, a Linux server, or Docker, including Docker Desktop on the same PC.

Plex has no API for markers, so the app writes them into Plex's SQLite database. SQLite's locks only work between
programs on the same machine, and over a network share they can corrupt the database. The node opens the database on
Plex's machine and makes the write, with the same checks the app runs when it sits beside Plex.

No, when:

- The app runs natively on the same PC as Plex. It opens the database itself.
- Plex runs in Docker Desktop (WSL 2) or another Linux container. Use the [Linux node](../plex-db/) in the same
  Docker setup.
- Plex's data folder is on a network drive. SQLite can't be shared safely there, and the node refuses it.

## What you need

- Windows with Plex installed and used by one Windows user. Install the node as that user.
- The app, reachable from this PC over your network.
- `PlexDbNode-windows.zip`, from the **Plex database node** release on the
  [Releases page](https://github.com/stevezau/media_preview_generator/releases). Take the newest one.

## Install

1. Unzip `PlexDbNode-windows.zip` anywhere. It holds `install.ps1`, `uninstall.ps1`, this README and the
   `PlexDbNode` folder.
2. Sign in to Windows as the user Plex runs as. Open PowerShell in the unzipped folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   It copies the program to `%LOCALAPPDATA%\Programs\MediaPreviewGenerator\PlexDbNode`, makes it start at every logon
   of this user, starts it, and prints an **address** and a **key**.
3. If the app runs on another machine, Windows may ask whether to allow `PlexDbNode.exe` through the firewall. Allow it
   on private networks. Or run the installer with `-AddFirewallRule` (below). With Docker Desktop on the same PC no
   rule is needed.

## Add it in the app

1. Open **Settings → Nodes → Add node**.
2. Enter the address and the key the installer printed. The address is `http://<this PC>:9494`. For the app in Docker
   Desktop on the same PC, use `http://host.docker.internal:9494`.
3. Open **Servers**, choose your Plex, then **Edit → Connection → Plex database** and select the node.

## Install options

Add these to the install command.

| Option | What it does |
|---|---|
| `-AtStartup` | Starts the node when Windows starts, before anyone signs in, instead of at logon. Use it when Plex runs as a Windows service or starts at boot. The user must be an administrator, and the installer asks for administrator rights once. If you decline, the node keeps starting at logon. Windows can't show its firewall question to a program started before sign-in, so add `-AddFirewallRule` when the app is on another machine. |
| `-AddFirewallRule` | Asks for administrator rights once, then lets other machines on private and domain networks reach the node (port 9494). It also removes the block Windows adds when its firewall question is dismissed. |
| `-Port 9500` | Listens on another port. |
| `-PlexConfigDir "D:\PlexData\Plex Media Server"` | Names Plex's data folder. You rarely need it: the node finds the folder from Plex's own `LocalAppDataPath` registry value, else `%LOCALAPPDATA%\Plex Media Server`. Name only that folder. Plex on Windows keeps its server id in the registry, so for any other folder the node can't tell which Plex it belongs to and refuses loudness and chapter thumbnails. |

The node keeps its id, key, log and loudness undo record in `%LOCALAPPDATA%\MediaPreviewGenerator\node`
(`node.json`, `logs\node.log`, `loudness-writes.jsonl`). To use a key of your own, set it before installing
(`$env:NODE_KEY = "..."`). The installer saves it in `node.json`, so it survives a reboot.

## Update

1. Download the newest release and unzip it.
2. Run `install.ps1` again as the same user, with the options you installed with (`-Port`, `-PlexConfigDir`,
   `-AtStartup`).

It replaces the program and keeps the key and the undo record. The node doesn't have to match the app's version, only
be new enough. The app shows **Update needed** when it isn't.

## Uninstall

Run this in PowerShell as the same user:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\MediaPreviewGenerator\PlexDbNode\uninstall.ps1"
```

This removes the program and its startup entry, plus the boot task and firewall rule if you added them. It keeps the node's data folder.
`-RemoveData` also deletes the key and log, but keeps the loudness undo record, the only way to put Plex's audio
tracks back. Add `-RemoveUndoRecord` to delete the loudness undo record too. To undo loudness writes after removing
the node, install it again first.

Then delete the node under **Settings → Nodes** in the app.

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
task started that way can open files on this PC, which is all the node needs, but not network shares. It runs with
the user's full administrator rights, as a Plex started at boot does, because Windows only holds rights back for a
user who signs in at the screen. The task is named after the user, so each user has their own, and uninstalling
removes it. A Windows service would run as `SYSTEM` (far more access than it needs, and a different user from Plex) or
need that user's password stored.

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
