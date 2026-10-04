# Inspector and automatic loudness: live proof, 2026-10-04

The combined source snapshot passed against the retained lab Plex server, through the real
Inspector HTTP route and Chromium. A second manual preview job skipped its existing BIF and
still queued a separate loudness job, which preserved the existing measurements.

## Runtime and isolation

- Candidate: `mlab-loudness-inspector-candidate`, local `storage`, `127.0.0.1:18085`.
- Base image: `stevezzau/media_preview_generator@sha256:c24ee0771af3ba29853149332b8d5511ee3613a27874b4c1a365ac38b15c6337`.
- Source: working snapshot after integrating dev `893394d`, mounted read-only over the official base.
- Source manifest SHA-256: `1a3c5b54b3cb1fa0d5f9a657ea94ade5a762a6915b4facb95803d9d4d67b3caa`.
- All manifest entries matched the working package after the proof. This is source-snapshot evidence,
  not evidence for a newly published image.
- Separate private app configuration and jobs database; one CPU worker, zero GPU workers.
- Read-only lab sample media; guarded writable lab Plex volume for the dedicated fixture's BIF.
- Existing official lab container on port 18084 remained running and healthy. Production was untouched.

The retained fixture was Plex item `1464`, part `2058`, audio stream `15062`, index `1`, the
30-second “Loudness Proof packaged (2026)” sample. Its native loudness was already measured in
the preceding official-image acceptance. This proof did not clear or replace those measurements.

## Inspector and browser results

The authenticated Inspector response and rendered page agreed with Plex's native metadata:

- Integrated loudness: **-21.75 LUFS**.
- True peak: **-16.32 dBTP**.
- Loudness range: **0 LU**.
- Threshold: **-31.75 LUFS**.
- Gain offset: **0.05 dB**.
- Analysis version: **0.02**; normalization available: **true**.

The response preserved the exact item, part and stream IDs. Both server and stream states were
`available`, displayed as “Available in Plex”. These are server-reported measurements, not a
claim that the API proves when the current file contents were analysed.

With the candidate server's global loudness switch still on, temporarily selecting no loudness
libraries made this file's `enabled` flag false. The same measurements remained visible and the
page stated that analysis by this app was off. The original library selection was restored.

A real loudness job queued while candidate processing was paused displayed:

> Queued for this file: Plex loudness job “Inspector live queued loudness proof”

Resuming processing completed the job. SocketIO-driven refresh removed the banner, retained
the measurements, and returned `job: null` through the Inspector API. Chromium reported no
page errors.

## Automatic job results

First scoped manual preview:

- Preview `8b0f605d-683c-47fa-9dfd-c1ecd53135eb`: completed, `generated: 1`.
- Automatic loudness `fdc96b28-180c-4a62-aaaf-5db77f7e5a72`: completed, `loudness_up_to_date: 1`.

Second scoped manual preview:

- Preview `edbf5bd8-95cf-47e9-841b-6a7618053d5f`: completed, `skipped_bif_exists: 1`.
- Automatic loudness `eecc600b-a332-4339-b0b6-777ac07746ef`: completed, `loudness_up_to_date: 1`.

Both follow-ups persisted a dependency on their own preview job. Native measurements were
identical afterward, the per-file setting was restored, and no candidate jobs remained active.

The first harness attempt exposed an inherited lab setting: empty GPU configuration fell back
to one legacy GPU worker in a container without GPUs. That preview failed before generation;
its loudness follow-up still completed. The candidate-only configuration was corrected to
explicitly use zero GPU workers before the successful proof above. No application-source
change was made for that harness correction.

## Retained artifacts

Private local directory: `/tmp/loudness-inspector-followup-20261004/`.

- `proof.json`: sanitized Inspector payloads, job results, banner text, and browser assertions.
- `source-sha256.json`: exact source manifest.
- `inspector-native.png`: native measurements before BIF generation.
- `inspector-unselected-native.png`: native measurements with this file's automatic analysis off.
- `inspector-queued-loudness.png`: actual queued loudness banner and measurements.
- `inspector-completed-refresh.png`: page after completion and automatic refresh.
- `start_candidate.py` and `prove.py`: retained reproduction harnesses.

The candidate container and all artifacts remain available. Private configuration and credentials
are intentionally confined to the local private directory and are not included here.
