# Live Plex loudness evidence

These proofs ran on 2026-10-04 against the isolated, claimed `mlab-plex` server,
Plex Media Server `1.43.4.10903-e5521bd8c`, using dedicated **Loudness Proof** items.
The application image's FFmpeg was **8.1.2**; an additional comparison used system
FFmpeg **8.0.1**. Production media and settings were not changed.
The experiments explicitly enabled the lab configuration; the feature remains
off by default in application settings.

The native baseline covers 15 items and 16 audio streams: 11 items/12 streams in
[`native-parity.json`](native-parity.json), plus four programme items in
[`programme-parity.json`](programme-parity.json). Sources include generated tones,
silence, a 0.1-second track, multiple audio tracks, and programme excerpts of about
30 seconds and two minutes. Codec coverage includes AAC, AC3, EAC3, DTS, TrueHD,
Opus, MP3 and FLAC, with mono, stereo and 5.1 examples and 44.1/48 kHz rates.

[`acceptance.json`](acceptance.json) records 16 distinct app-written items and
18 streams, including additional multiple-version and mixed native/app cases.
Its `tested_source_sha256` values identify the exact Python source snapshot used
for that acceptance run; they are not media hashes or a claim that every later
edit was exercised live. The automated regression results cover subsequent
safety and retry changes separately.

The same file's `final_snapshot_recheck.source_sha256` identifies a later
publication snapshot. That snapshot was exercised in a fresh live write, API read-back
and up-to-date check, followed by exact undo of its two confirmed records and
restoration of the fixture's prior metadata. The original matrix's
hashes are retained separately rather than relabelled as a newer run. Subsequent
strict boolean opt-in and server-routing fixes are covered by automated tests;
they were not part of that live snapshot. The publication module's later hash
change only clarifies exception documentation; its runtime code matches the
live publication snapshot.

## Measurement and playback results

The packaged FFmpeg results agree closely with native Plex on this matrix, but
are not universally identical. The generated AAC example differs by 0.01 dB in
gain offset. For programme EAC3 stereo, integrated loudness and true peak each
differ by 0.01 dB; the 5.1 programme's true peak differs by 0.06 dB. The retained
JSON contains every compared field, including native silent/short sentinels.

[`playback.json`](playback.json) measures complete normalized audio output served
by Plex before and after replacing the native measurements with app results:

- Programme stereo: native/app integrated loudness **-16.10/-16.11 LUFS**, with
  true peak **-1.31 dBTP** in both.
- Programme 5.1: both **-23.36 LUFS** and **-6.95 dBTP**, with the other measured
  output fields also equal.
- Generated AAC: integrated loudness differed by **0.05 LU**. The 30-second
  programme EAC3 comparison had identical measured output fields.

These are observed results for the retained test matrix. They do not establish
bit-identical decoding, universal codec parity, or support for later Plex builds.
The playback artifact also retains eight sanitized Plex Transcoder filter
observations. Plex applies measured loudnorm before downmixing 5.1 to stereo, so
the rendered stereo output is not expected to equal the filter's -16 LUFS target.

## Publication, cancellation and restoration

The application wrote the fixture streams and verified their measurements and
normalization capability through Plex's API. Rechecking reported them up to
date. A stream with native measurements remained byte-identical when another
stream of its item needed work. An item with two versions was marked complete
only after the second version finished. Native non-forced analysis subsequently
launched zero loudnorm commands during the recorded four-second observation.

A cancellation requested 0.2 seconds into a two-minute EAC3 analysis returned in
0.52 seconds. With the lab stopped, scoped undo replayed eight confirmed records
and verified their exact previous values. The four programme fixtures' original
native stream and item metadata were then restored.

The lab was left running and claimed. Its original loudness-generation setting,
**Never**, was restored. Only dedicated scratch fixtures retain app metadata;
the programme fixtures retain their native baseline. Media clips, authentication
tokens and private source paths are excluded from these tracked artifacts.

## Repeat the read-only comparison

Run the retained harness against dedicated, already natively analysed fixtures:

```bash
PYTHONPATH=. python tests/integration/verify_plex_loudness.py \
  --run --report /tmp/loudness-parity.json
```

Supply JSON on stdin containing `url`, `token`, `local_root`, `remote_root` and
`item_ids`. Keep this input in memory rather than a file or shell command history.
The harness limits fixture names, duration and concurrency, reads Plex's API,
and runs the actual analyzer. It does not write Plex metadata or launch native
analysis. Use the application image to reproduce its packaged FFmpeg behavior.
This retained harness was run in the lab's application container against the four
restored native programme fixtures and produced `programme-parity.json`.
The write, playback and rollback proofs above are separate acceptance experiments.

## Full web application and retry lifecycle

[`fullweb-workflow.json`](fullweb-workflow.json) records a later adversarial proof
through real HTTP job creation, the shared dispatcher, CPU workers, Plex and
persisted job/file results. Chromium used the real login and dashboard. The
isolated `mlab-loudness-fullweb` container uses the packaged FFmpeg above with a
frozen source package; its exact source hashes are retained for each revision.
This proof does not claim a published image or production deployment was tested.

The baseline reproduced a misleading green Completed job while its file still
needed Plex indexing, with a separate pending retry. The corrected flow keeps
one visible pending parent. That parent survived an app restart; after a bounded
Plex scan and Retry Now, the same row completed with `loudness_written` in its
outcomes, publisher summary and Files panel. Completed results survived another
restart. Plex exposed the new measurements with normalization enabled.

Two additional defects found during the live proof were corrected and retested:
the recovered parent's stale publisher summary, and a cancelled row still showing
a retry countdown. The final UI revision clears the cancelled job's retry times,
keeps them cleared after restart, hides stale countdowns on older terminal rows,
and offers no unsupported Pause action on the visible retry parent. Cancelling
a waiting parent also cancelled its hidden attempt across restart. Cancelling
a real running FFmpeg worker stopped the process without publishing measurements.

A full job also analysed a 120-second EAC3 5.1 programme fixture byte-identical to
the earlier native reference. All 16 normalized playback segments were consumed.
Plex's rendered integrated loudness, true peak, range and threshold matched the
native reference exactly on that sample; this remains a bounded comparison.

The original labs and their settings were preserved. The additional full-web app
and dedicated fixtures are retained, with no pending or running test jobs.
