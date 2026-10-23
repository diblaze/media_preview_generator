# Loudness analysis measures the whole stream: one filter graph, and one guarded retry when the audio format really changes
Issue: diblaze/media_preview_generator#9
Status: approved

## Goal
Analysis ffmpeg passes `-reinit_filter 0` before `-i`, so a metadata-only change no longer splits loudnorm's report into whole-stream + tail (today `parse()` keeps the tail). If the audio format really changes mid-stream, that ffmpeg exits 234. A single-stream run then retries once with `-drop_changed 1`. It accepts the result only if ffmpeg dropped at most `MAX_DROPPED_FRAMES = 10` frames. Batches fall to the existing per-stream retry. Branch from `upstream/dev` 1dec50b2; line numbers are identical there (`git diff --stat upstream/dev HEAD` on `loudness/`, `nodes/`, their tests is empty).

## Evidence
Measured (live library, stock and `/opt/ffmpeg-loudness` ffmpeg agree):
- M1 88 of 61,488 analysed Plex audio streams store `ln:loudness -inf` (dca 24, eac3 41, ac3 23). 7/7 re-measured are wrong tail readings, not silent tracks. Random sample: 1 of 205 (~0.2%).
- M2 Metadata-only change (`Reconfiguring filter graph because downmix medatata changed`, AC3/EAC3): default prints two reports `[-24.05, -inf]`. `-reinit_filter 0`: one full report, rc 0. `-drop_changed 1`: one report but 505–1000 frames dropped. So reinit 0 is the default, not drop_changed.
- M3 Real change on the final frame (`audio parameters changed to 48000 Hz, 5.1(side), s32p`; DTS movies, eac3 Kyle XY): `-reinit_filter 0` gives rc 234 `Changing audio frame properties on the fly is not supported`. `-drop_changed 1` gives rc 0, one report, `Total changed input frames dropped : 1` (default log level). Both flags together behave like `-drop_changed 1` alone.
- M4 43 Ghost Whisperer S04/S05 (ac3) fail with `loudnorm reported unsupported loudness measurements`; `-reinit_filter 0` gives -25.56 LUFS.
- M5 Synthetic 48k stereo + 44.1k mono with `-drop_changed 1`: rc 0, half the audio silently dropped (`Total changed input frames dropped : 144`). `-progress` out_time is pts-based and does not show drops. Only the stderr count does, hence the N guard.

Code:
- E1 `loudness/analyze.py:102-104` `command()`: `*(["-drc_scale", "0"] if codec in NO_DRC_CODECS else []),` / `"-i",`. `:126` `command_many()`: `args.extend(("-i", path))`. No `-reinit_filter` (ffmpeg default 1).
- E2 `analyze.py:153` `parse()`: `start = stderr.rfind("{")`. It keeps the last (tail) report.
- E3 `analyze.py:372-376` `if proc.returncode != 0:` … `if any(hint in text for hint in _NO_DECODER_HINTS): raise LoudnessNoDecoder(message)` / `raise LoudnessError(message)`. Message keeps only `text.strip()[-200:]`, so a later string match on the message is unreliable. Classify here, as for no-decoder.
- E4 `analyze.py:414-424` `run()`: `text = _run_command(command(ffmpeg, path, index, codec), …)` / `return ln_fields(parse(text))`. This is the only single-stream entry.
- E5 `analyze.py:457-468` `run_many()` → `_run_command`. The exception goes to `loudness/job.py:536` `except analyze.LoudnessError as exc:` … `:547` `"Loudness batch failed for {}: {}; retrying its streams separately"`. Then `:564-565` `run_one = remote_ops.loudness_run if current_placement() else analyze.run` / `fields = run_one(`. A batch with a format change already reaches `run()` per stream.
- E6 Nodes: `nodes/server/ops.py:226` `results = analyze.run_many(`, `:244` `fields = analyze.run(`. The retry inside `run()` therefore also runs on the node. `nodes/remote_ops.py:197-208` `_loudness_errors()` rebuilds only `LoudnessError, LoudnessNoDecoder, LoudnessCancelled, LoudnessTimeout` by name (`:42-47` `make = known.get(exc.error_type)` … `return exc`). An unlisted subclass comes back as `NodeTaskError`. job.py:536 would then miss it and skip the per-stream retry. A new subclass must be added there.
- E7 Tests: `tests/loudness/test_analyze.py:57-60` exact argv `… "pipe:1", "-i", "/m/a.mkv", …`. `:64-67` `cmd[5:7] == ["-drc_scale", "0"]` and `cmd[-9:] == ["-i", …]` still hold if the flag sits between them. `tests/loudness/test_analyze_batch.py:36-47` exact argv `"-drc_scale:3", "0", "-i", …`. Mock pattern: `test_analyze.py:305-311` `proc = MagicMock(returncode=234)`, `proc.stderr = io.BytesIO(stderr)`, `monkeypatch.setattr(analyze.subprocess, "Popen", MagicMock(return_value=proc))`. `tests/nodes/test_remote_ops.py:259` `@pytest.mark.parametrize("cls", [analyze.LoudnessTimeout, analyze.LoudnessNoDecoder, analyze.LoudnessError])`.
- E8 `docs/plex-loudness-normalization.md:92` `ffmpeg -i FILE -map 0:TRACK -af loudnorm=I=-16:TP=-1:LRA=9:print_format=json -f null -`.
- E9 No force option: `loudness/plex_db.py:280-282` `def needs_analysis(stream)` / `return not has_analysis(stream.extra_data)`. `-inf` with `+inf` offset passes `valid_measurements` (`analyze.py:241-247`), so `has_analysis` is True and these streams are skipped forever. `rg -i "force|overwrite|regenerate"` in `loudness/` and `web/routes/api_loudness.py` finds no option (only `job.py:1143` "retry limit in force").

## Changes
All in `media_preview_generator/loudness/analyze.py` unless noted:
- Constants beside `_NO_DECODER_HINTS` (`:59`): `_FORMAT_CHANGED_HINT = "Changing audio frame properties on the fly is not supported"`, `_DROPPED = re.compile(r"Total changed input frames dropped : (\d+)")`, `MAX_DROPPED_FRAMES = 10`. Comment why: a stray changed frame at the end is harmless, but many dropped frames mean part of the track is unmeasured (M5).
- `class LoudnessFormatChanged(LoudnessError)`: "The audio format changed mid-stream, which one filter graph can't measure."
- `command(…, drop_changed: bool = False)`: insert `*(["-drop_changed", "1"] if drop_changed else ["-reinit_filter", "0"]),` between the `-drc_scale` splat and `"-i"`. Comment: a metadata-only change (AC3 downmix info) would otherwise rebuild the graph and split loudnorm's report (M2).
- `command_many()`: `args.extend(("-reinit_filter", "0", "-i", path))`. No drop_changed path for batches (E5).
- `_run_command()` `:374`: before the no-decoder check, `if _FORMAT_CHANGED_HINT in text: raise LoudnessFormatChanged(message)`.
- `run()`: wrap the `_run_command` call. `except LoudnessFormatChanged:` re-run `_run_command(command(…, drop_changed=True), …)` once, with the same kwargs. Then `found = _DROPPED.search(text)`. If `found is None` or `int(found[1]) > MAX_DROPPED_FRAMES`, raise `LoudnessError(f"the audio format of stream {index} changed mid-stream; ffmpeg dropped {n} frames, not stored")`. Otherwise `ln_fields(parse(text))`.
- `nodes/remote_ops.py:203-206`: add `analyze.LoudnessFormatChanged` to `_loudness_errors()` (E6).
- `docs/plex-loudness-normalization.md:92`: change to `ffmpeg -reinit_filter 0 -i FILE …`. Add after the block: "`-reinit_filter 0` keeps one filter graph for the whole track, so a mid-stream metadata change can't split loudnorm's report. If the track's audio format really changes, the track is measured again with frames of the new format dropped. The result is stored only when at most 10 frames were dropped." Then run `.venv/bin/python scripts/generate_llms_full.py`.

## Tasks
1. Fix + tests + docs (one commit)
   1. Tests first. They fail today:
      - `tests/loudness/test_analyze.py`: update `:57-60` argv (add `"-reinit_filter", "0",` before `"-i"`). Add `test_analysis_keeps_one_filter_graph_for_the_whole_stream`: parametrize over `command(…, 1, "eac3")` and `command_many(…, [(1, "aac", None), (3, "eac3", None)])`, assert `cmd[cmd.index("-i") - 2 : cmd.index("-i")] == ["-reinit_filter", "0"]`.
      - Same file, `test_a_format_change_is_measured_again_dropping_the_changed_frames`. `Popen` `side_effect` gives two procs (E7 pattern). The first has rc 234 and stderr with the hint. The second has rc 0 and stderr with a report + `Total changed input frames dropped : 1`. Assert the fields come back, and that the second argv has `["-drop_changed", "1"]` right before `-i` and no `-reinit_filter`.
      - `test_a_format_change_that_drops_many_frames_is_refused`: parametrize the second stderr over `… dropped : 11` and no count line. Assert `LoudnessError` matching `not stored`.
      - Extend `test_a_missing_decoder_is_told_apart_from_other_ffmpeg_failures` (`:305`): name the mock, `assert popen.call_count == 1`. Non-matching failures are not retried.
      - `tests/loudness/test_analyze_batch.py:36-47`: add `"-reinit_filter", "0",` before `"-i"`. Add `test_a_batch_format_change_raises_for_the_per_stream_retry`: `run_many` with the hint raises `LoudnessFormatChanged`, `Popen` called once.
      - `tests/nodes/test_remote_ops.py:259`: add `analyze.LoudnessFormatChanged` to the parametrize.
      - Run `.venv/bin/python -m pytest --no-cov -q tests/loudness/ tests/nodes/test_remote_ops.py`. It must fail.
   2. Apply Changes (analyze.py, remote_ops.py). The same pytest command passes.
   3. Docs edit + `.venv/bin/python scripts/generate_llms_full.py`.
2. Check
   1. `cd /home/denis/git/media_preview_generator && .venv/bin/python -m pytest --no-cov -q tests/loudness/ tests/nodes/test_remote_ops.py tests/nodes/test_ops.py && .venv/bin/ruff check media_preview_generator/loudness/analyze.py media_preview_generator/nodes/remote_ops.py tests/loudness/test_analyze.py tests/loudness/test_analyze_batch.py tests/nodes/test_remote_ops.py && .venv/bin/ruff format --check media_preview_generator/loudness/analyze.py media_preview_generator/nodes/remote_ops.py tests/loudness/test_analyze.py tests/loudness/test_analyze_batch.py tests/nodes/test_remote_ops.py && .venv/bin/python scripts/generate_llms_full.py --check`
   2. Architecture review is required: the change touches FFmpeg command construction (CLAUDE.md).

## Prod follow-up (fork only, not in the upstream PR)
- The 43 failed Ghost Whisperer episodes have no `ln:*`, so the nightly "Loudness catch-up" picks them up. Next morning, check that the logs show no `unsupported loudness measurements`, and that S05E01 has `ln:loudness -25.56` (M4).
- The 88 `-inf` streams (M1) are not re-analysed by anything: the app has no force/overwrite option (E9). Choose one: (a) add a fork-only "re-measure `-inf` streams" switch, a separate change that makes `needs_analysis` also select `ln:loudness == -inf`; or (b) a one-off cleanup that strips `ln:*` from those 88 `media_streams.extra_data` rows. Run it with Plex stopped and a DB backup, then let catch-up run. See Open Questions.

## Open Questions
- (a) Re-analysis of the 88 `-inf` streams: a new force option (code + its own plan) or a one-off DB cleanup? With either, a true silent track gets re-measured as `-inf` again, which is harmless.
- (b) Should a missing `Total changed input frames dropped` line on the retry be refused (planned: fail closed) or treated as 0? This matters only if a future ffmpeg stops printing it at the default log level.
- (c) `MAX_DROPPED_FRAMES = 10`: M3 saw 1. Is 10 the right ceiling, or should it be relative to the stream length?
- Upstream issue/PR number: `Issue: <TBD>` until filed.
