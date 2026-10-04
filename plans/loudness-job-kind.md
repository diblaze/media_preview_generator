# Loudness job kind: Plex loudnorm analysis in the shared worker pool
Status: done (code, 9334e49 on feat/loudness-job-kind); lab + live proof pending
Issue: diblaze/media_preview_generator#1
## Goal
A `loudness` job kind runs Plex's exact `loudnorm` analysis per audio stream on the app's workers and writes `ln:*` into `media_streams.extra_data`, so Plex shows `canNormalizeLoudness="1"` for items its own one-at-a-time butler hasn't reached; off by default, per-server + per-library.

## Evidence
Live facts F1–F9 from the task are taken as given; F10–F14 are my own read-only checks of the live DB (2026-09-29).
- F10 `media_streams` DDL (live, absent from `tests/fixtures/markers/plex_schema_1_43.sql`): `CREATE TABLE "media_streams" ("id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, "stream_type_id" integer, "media_item_id" integer, "url" varchar(255), "codec" varchar(255), "language" varchar(255), "created_at" dt_integer(8), "updated_at" dt_integer(8), "index" integer, "media_part_id" integer, "channels" integer, "bitrate" integer, "url_index" integer, "default" boolean DEFAULT 0, "forced" boolean DEFAULT 0, "extra_data" varchar(255))`. No triggers on `media_streams`; indexes on `media_part_id`, `media_item_id`. `media_parts` has `index_media_parts_on_file`.
- F11 `media_streams.index` is the absolute 0-based ffmpeg stream index: part 395603 has video index 0, audio indexes 1–4 with `ma:streamIdentifier` = index+1. Answers F5's `-map 0:<n>` question: n = `media_streams.index`.
- F12 Plex marks the item too: `metadata_items.extra_data` gains `"ln:loudnessAnalysisVersion":"0.02"` (170 items have it; 0 items have stream `ln:*` without it; 2 have it without stream `ln:*`), and bumps `metadata_items.changed_at` (687128: 119810280 vs sibling 687130: 119778300 == its `resources_changed_at`). `metadata_items` has FTS4 triggers (`fts4_metadata_titles_before_update_icu` …) → cannot be updated from python sqlite3 (F7) and `_check_schema` would refuse it (E12). Plex also sets `media_streams.updated_at` at analysis (1790692331 vs `created_at` 1790680630); `metadata_items.updated_at/refreshed_at` untouched.
- F13 Unanalyzed audio streams in libraries: 67,185 (aac 48k, eac3 26k, ac3 15k, flac 3k, mp3 2k, opus 1k, dca 747, truehd 91 …); 29,488 with NULL `extra_data` all belong to media_items with NULL `library_section_id` (orphans; excluded by joining on live parts). All audio `extra_data` rows are JSON form (0 URL-form). Section 7 "YouTube": 452 unanalyzed.
- F14 Prefs: `LoudnessAnalysisBehavior="asap"`, `LoudnessAnalysisThreads="0"`. Generator container ffmpeg `/usr/local/bin/ffmpeg` has `loudnorm`.
- E1 `job_kinds.py:15-17` — `JOB_KIND_INTRO_CREDITS = "intro_credits"` / `JOB_KINDS: tuple[str, ...] = (JOB_KIND_PREVIEWS, JOB_KIND_INTRO_CREDITS)` — add the kind here.
- E2 `job_kinds.py:87-101` — `class KindHandlers: check_fn, process_fn, outcome_keys, check_label, check_worker_label, check_share, pickup_fn` — the per-kind contract.
- E3 `markers/pipeline.py:4170-4187` — `def kind_handlers(ctx) -> KindHandlers: return KindHandlers(check_fn=lambda item, *, cancel_check=None: …, process_fn=lambda item, **kwargs: …, outcome_keys=OUTCOME_KEYS, check_share=0.25)` — the shape to mirror.
- E4 `markers/job_runner.py:1640-1735` — `run_intro_credits_job`: `register_job_thread`, loguru job-log handler, `get_job_gate().acquire`, `jm.start_job`; `:1987-2020` — `get_or_create_dispatcher(config, selected_gpus)`, `jm.set_active_worker_pool`, `dispatcher.submit_items(… kind=JOB_KIND_INTRO_CREDITS, handlers=kind_handlers(ctx), …)`, `_wait_releasing_slot_while_paused`, `tracker.get_result()`; `:2078-2119` teardown (gate release, `set_file_result_callback(None)`, clear flags, `unregister_job_thread`). `:1444-1486` `_complete`, `:1528-1562` `_wait_releasing_slot_while_paused`, `:1565-1580` `_freeze_check`, `:2122-2161` `start_intro_credits_job_async` — reused by the loudness runner.
- E5 `markers/job_runner.py:1072-1176` — `build_items(job_config, *, registry, …)`: `file_paths` or `libraries`, filters with `load_server(cfg.markers, …).enabled` (`:1132`) and `marker_libraries(cfg)` (`:1137`), label `"Intro & Credits"` (`:1163`) — parametrize.
- E6 `jobs/dispatcher.py:1120` — `outcome = tracker.handlers.check_fn(item, cancel_check=tracker.is_cancelled)`; None → worker queue (`:1129-1131`).
- E7 `jobs/worker.py:746-760` — `self.process_fn(item, gpu=gpu, gpu_device_path=gpu_device, progress_callback=…, phase_callback=…, cancel_check=…, pause_check=…, ffmpeg_threads=…, fallback_callback=…, gpu_worker=…, worker_name=…)` — a GPU worker hands `gpu="…"`; the loudness `process_fn` ignores it and runs plain CPU ffmpeg (no `-hwaccel`), so no `CodecNotSupportedError` rerun path is needed.
- E8 `markers/publishers/plex_db.py:90-108` — `_WRITTEN_TABLES = ("taggings", "media_parts")` / `_REQUIRED_COLUMNS = {…}`; `:1044-1065` `_check_schema` refuses missing columns and any trigger on `_WRITTEN_TABLES`.
- E9 `plex_db.py:1014-1042` — `LocalPlexDb._connect` / `_database(read_only, deadline)` (process lock + connection); `:1456-1491` `_begin_write` (`BEGIN IMMEDIATE`, busy slices, cancel).
- E10 `plex_db.py:361-397` — `_plex_quote`, `_url_form`, `encode_extra_data(d, *, url_form=False)` (sorted keys, trailing `url`); `:400-443` `decode_extra_data`; `:647-655` `_same_extra_data`.
- E11 `plex_db.py:1820-1825` — `_local_candidates(plex_path)` uses `servers.ownership.apply_path_mappings`; `servers/ownership.py:161-190` `apply_inverse_path_mappings(local_path, mappings)` gives the Plex-side paths of a local file.
- E12 `plex_db.py:1659-1669` — `PlexMarkerPublisher.db_path_for(config)` = `plex_db_path((config.output or {}).get("plex_config_folder"))`.
- E13 `markers/audio/fingerprint.py:270-305` — ffmpeg `Popen(..., start_new_session=True)`, `Freeze.of(pause_check).hold(proc, …)`, deadline via `freeze.clock()`, `_stop` → `probe.kill_and_collect` — the run-ffmpeg-with-pause/cancel/timeout pattern to copy for loudnorm.
- E14 `markers/settings.py:63-82` `default_server_markers`, `:155-176` `ServerMarkersSettings(enabled, library_ids, db_write_confirmed_at, …)`, `:361-407` `validate_server` (`library_ids` list|null, Plex needs `db_write_confirmed_at`), `:478-514` `load_server`; `markers/ownership.py:68-84` `marker_libraries(cfg)` via `library_allowed`.
- E15 `servers/base.py:805-806` `markers: dict[str, Any] = field(default_factory=dict)` on `ServerConfig`; `servers/registry.py:79-97` reads `data.get("markers")` into it; `web/routes/api_servers.py:430-448` validates/carries the posted `markers` block on save.
- E16 `upgrade.py:28` `_CURRENT_SCHEMA_VERSION = 19`; `:484-487` `_run(19, _migrate_to_v19)` then `sm.set("_schema_version", …)`; `:1488-1521` `_migrate_to_v15` seeds a disabled per-server block (pattern for v20).
- E17 `markers/triggers.py:140-224` `create_intro_credits_job` builds `config={"kind": …, "source", "libraries", "file_paths", "follows_job_id", "force", "webhook_item_id_hints"}`, `jm.create_job(kind=…)`, starts async; `:65-75` `markers_enabled_anywhere`; `:454-500` `submit_pending_follow_up` reads `webhook_paths` + hints and calls `submit_follow_ups` under `_pending_follow_up_lock` before removing `INTRO_CREDITS_FOLLOW_UP`.
- E18 `web/routes/job_runner.py:393-396` `_start_job_async`: `if queued.kind == JOB_KIND_INTRO_CREDITS: start_intro_credits_job_async(...)`; `:1850-1866` `_queue_intro_credits_follow_up` calls `submit_pending_follow_up` on every preview start; `web/webhooks.py:489,954,1017,1208,1224` set `INTRO_CREDITS_FOLLOW_UP: True` on webhook preview jobs.
- E19 Kind-specific pause semantics: `web/routes/job_runner.py:365`, `web/routes/api_jobs.py:1147,1162,1228,1879`, `web/jobs.py:1348`, `web/app.py:441` (`fail_unrevived_interrupted_jobs(JOB_KIND_INTRO_CREDITS)`), `web/static/js/app.js:1559-1574` (`JOB_KIND_INTRO_CREDITS`, `JOB_KIND_LABELS`).
- E20 `web/routes/api_markers.py:41-107` `POST /markers/jobs` (libraries|file_paths, priority, force, library_name) — API shape to mirror; `web/templates/index.html:329` job-kind radio; `web/static/js/app.js:3184-3300` `_jobKindIsMarkers`, `_showJobKindControls`, `_startMarkersJob` → `_submitNewJob('/api/markers/jobs', …)`; `app.js:1540-1557` outcome pill map.
- E21 `tests/test_job_kinds_engine.py:1-80` engine test with `KindHandlers` + `dispatcher.submit_items(..., kind=…, handlers=…)`; `tests/test_jobs_kind_persistence.py:7-10` `parse_job_kind`; `tests/markers/test_plex_db_publisher.py:143-175` `_make_db` loads `tests/fixtures/markers/plex_schema_1_43.sql`.
- E22 Docs: `docs/reference.md:387-396` "### Job kind `intro_credits`" table; `:264-300` per-feature settings section; `:537,553` endpoint rows; `docs/_config.yml:65-66` nav; `docs/llms.txt:55` page link; `scripts/generate_llms_full.py` (`--check`) + `tests/test_llms_full.py` drift test; `.github/workflows/docs-check.yml:65` runs `pytest --no-cov -n 0` on docs tests; `Makefile` `test`/`lint`.
- E23 Deploy: `/sharedfolders/media/docker/services/plex-generate-previews/compose.yaml:4` `image: stevezzau/media_preview_generator:dev@sha256:7976a5c5…`; `:18` `…/Plex Media Server:/plex:rw`; `ytdl/compose.yaml:5-17` `build: {context: ., dockerfile: Dockerfile}` + `image: local/ytdl-sub:…` (local-build pattern). `Dockerfile:5` `FROM linuxserver/ffmpeg:8.1.2-cli-ls73`.

## Changes
Kind + engine
- `media_preview_generator/job_kinds.py` — add `JOB_KIND_LOUDNESS = "loudness"` to `JOB_KINDS`; add `SELF_PAUSED_KINDS = frozenset({JOB_KIND_INTRO_CREDITS, JOB_KIND_LOUDNESS})` (E1, E19).
- `web/routes/job_runner.py:365,393`, `web/routes/api_jobs.py:1147,1162,1228,1879`, `web/jobs.py:1348`, `web/app.py:441` — replace `== JOB_KIND_INTRO_CREDITS` with `in SELF_PAUSED_KINDS` where the check means "kind with its own pause/runner"; `_start_job_async` gets `elif queued.kind == JOB_KIND_LOUDNESS: start_loudness_job_async(job_id, config_overrides)`; `app.py` also calls `fail_unrevived_interrupted_jobs(JOB_KIND_LOUDNESS)` (E18, E19).

New package `media_preview_generator/loudness/` (no marker imports except the Plex DB helpers)
- `loudness/analyze.py` — `LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1:LRA=9:print_format=json"`; `command(ffmpeg, path, index) = [ffmpeg, "-hide_banner", "-nostats", "-i", path, "-map", f"0:{index}", "-af", LOUDNORM_FILTER, "-f", "null", "-"]` (F1, F11; no `-hwaccel`, E7); `run(...)` copies E13 (Popen new session, `Freeze.of(pause_check).hold`, cancel, deadline = clamp(3 × media duration, 600 s, 4 h), `kill_and_collect`); `parse(stderr) -> dict` takes the last `{…}` block; `ln_fields(parsed) -> dict[str, str]`: `input_i→ln:loudness`, `input_tp→ln:peak`, `input_lra→ln:lra`, `input_thresh→ln:threshold`, `target_offset→ln:gainOffset`, each `f"{float(v):.2f}"`, plus `"ln:loudnessAnalysisVersion": "0.02"` (F2, F3).
- `loudness/plex_db.py` — `AudioStream(id, index, codec, extra_data, duration_ms)`; `read_streams(db: LocalPlexDb, plex_paths, *, deadline)`: `SELECT ms.id, ms."index", ms.codec, ms.extra_data, mi.duration FROM media_parts mp JOIN media_items mi ON mi.id=mp.media_item_id JOIN media_streams ms ON ms.media_part_id=mp.id WHERE mp.file IN (…) AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL AND ms.stream_type_id=2 ORDER BY ms."index"` under `db._database(read_only=True)` + `db._check_schema` (E9, E8, F10 index on `file`); `needs_analysis(stream) = "ln:loudness" not in decode_extra_data(stream.extra_data)[0]`; `write_stream(db, stream_id, fields, *, deadline, log_path)`: `_database(read_only=False)` → `db._begin_write` → re-read the row → skip if `ln:loudness` already present (idempotent, F4) → `merged = {**decode(existing)[0] minus "url", **fields}` → `UPDATE media_streams SET extra_data=?, updated_at=? WHERE id=?` with `encode_extra_data(merged)` and `int(time.time())` (E10, F12) → `COMMIT` → read back and compare with `_same_extra_data` → append `{"stream_id", "before", "after", "at"}` to `log_path` (JSONL, the undo record). Never touches `metadata_items` (F12: FTS triggers).
- `markers/publishers/plex_db.py:90-108` — `_WRITTEN_TABLES += ("media_streams",)`; `_REQUIRED_COLUMNS["media_streams"] = {"id", "media_part_id", "stream_type_id", "index", "codec", "extra_data", "updated_at"}`; `_REQUIRED_COLUMNS["media_items"] |= {"duration"}` (E8, F10). Markers' own capability check gains only a no-op guard (no triggers on `media_streams`, F10).
- `loudness/settings.py` — `default_server_loudness() = {"enabled": False, "library_ids": None}`; `ServerLoudnessSettings(enabled, library_ids)`; `validate_server_loudness(raw, server_type, markers_block)`: Plex only, `enabled` requires `markers.plex.db_write_confirmed_at` (reuse the existing DB-write confirmation, E14) else `"Confirm the Plex database write (Intro & Credits tab) before turning on loudness analysis"`; `load_server_loudness(raw)`; `loudness_libraries(cfg)` = `cfg.libraries` filtered by `library_ids` (None = all) and `lib.kind in {"movie", "show"}` (music/audiobook excluded, see Open questions); `loudness_enabled_anywhere()` (mirror E17 `:65-75`).
- `servers/base.py:806` — `loudness: dict[str, Any] = field(default_factory=dict)`; `servers/registry.py:79-97` — read `data.get("loudness")` like `markers`; `web/routes/api_servers.py:430-448` — validate a posted `loudness` block with `validate_server_loudness(..., markers_block)`, else carry the stored one (E15).
- `upgrade.py` — `_CURRENT_SCHEMA_VERSION = 20`; `_run(20, _migrate_to_v20)`; `_migrate_to_v20` adds `loudness: default_server_loudness()` to each Plex `media_servers` entry lacking it, returns notes only when it changed something (E16, mirrors v15).
- `loudness/job.py` — `OUTCOME_KEYS = ("loudness_written", "loudness_up_to_date", "loudness_no_owners", "skipped_file_not_found", "failed")`; `LoudnessContext(registry, config, ffmpeg, dbs: {server_id: LocalPlexDb}, write_log_path, freeze_check)`; `check_item(item, *, ctx, cancel_check)`: `os.path.isfile` else `skipped_file_not_found`; owners = `markers.ownership.owning_servers(path, registry)` filtered to Plex + `load_server_loudness(cfg.loudness).enabled` + a match in `loudness_libraries(cfg)` → none: `loudness_no_owners`; per owner `read_streams(db, apply_inverse_path_mappings(path, cfg.path_mappings))` (E11) → all streams analysed: `loudness_up_to_date`; else return None (worker); `process_item(item, *, ctx, cancel_check, pause_check, phase_callback, **_ignored)`: re-read streams, for each `needs_analysis` stream `phase_callback(f"loudnorm {n}/{total}")`, `analyze.run` → `ln_fields` → `write_stream`; outcome `loudness_written` with `publisher_rows` per server (`status: "loudness_written"|"loudness_up_to_date"|"failed"`, `message: f"{written} stream(s)"`) so the Files panel and per-server aggregate work (E2 `ItemOutcome.publisher_rows`); `kind_handlers(ctx) -> KindHandlers(check_label="Checking Plex loudness…", check_worker_label="Loudness", check_share=0.5)` (E3); `run_loudness_job(job_id)`: the E4 skeleton minus retries/chains/seasons/reconcile — gate → `jm.start_job` → `build_items(cfg, registry=…, enabled=lambda cfg: load_server_loudness(cfg.loudness).enabled, libraries=loudness_libraries, label="Loudness")` → `submit_items(kind=JOB_KIND_LOUDNESS, handlers=kind_handlers(ctx), priority=live_priority())` → `_wait_releasing_slot_while_paused` → `_complete(jm, job_id, outcome, warnings)` → teardown; `start_loudness_job_async` = E4 `:2122-2161` without the `_JOINED_KEYS` merge; `create_loudness_job(library_name, priority, source, libraries, file_paths, follows_job_id)` = E17 `:140-224` reduced to those keys.
- `markers/job_runner.py:1072-1176` — `build_items(..., enabled=None, libraries=marker_libraries, label="Intro & Credits")`: the two filters and the label become parameters; existing callers unchanged (E5).
- `markers/triggers.py:454-500` — inside `submit_pending_follow_up`, before the key is removed: `if loudness_enabled_anywhere(): create_loudness_job(library_name=f"Loudness: {n} file(s)", priority=PRIORITY_LOW, source=request["source"], file_paths=paths, follows_job_id=preview_job_id)` (E17, E18: every webhook preview job already carries `INTRO_CREDITS_FOLLOW_UP` and `webhook_paths`, so this gives the loudness follow-up the same once-per-job guarantee with no new flag).

API + minimal UI
- `web/routes/api_loudness.py` (register in `web/routes/__init__.py`) — `POST /api/loudness/jobs`, body and validation copied from E20 `:41-107`, names `"Loudness: …"`, calls `create_loudness_job`.
- `web/templates/index.html:329` — third radio `jobKindLoudness` value `loudness` ("Plex loudness"); `app.js:3184-3300` — `_jobKindIsLoudness()`, hide `jobMarkersModeGroup`/`jobProcessingModeGroup`/`jobSortByGroup`/`jobMarkersForceGroup` for it, `_startLoudnessJob()` posting to `/api/loudness/jobs`; `app.js:1559-1574` — `JOB_KIND_LOUDNESS`, label `'Plex loudness'`, `_jobHasOwnPause` covers both kinds; `app.js:1540-1557` — pills `loudness_written`, `loudness_up_to_date`, `loudness_no_owners` (E20). Per-server enable + library pick in the server dialog, same as Previews and Intro & Credits (user, 2026-09-29):

- `web/templates/servers.html` — new nav tab `editTabLoudnessLi` after Intro & Credits (`:71-73` pattern) with pane `#edit-tab-loudness`: switch `#loudnessEnabled` "Analyse loudness for this server" + pointer "Choose which libraries on the Libraries tab once this is on" (mirror `:702-726`); Libraries table header `th.loudness-lib-col d-none` "Loudness" next to `markers-lib-col` (`:459`); `<script src=js/loudness_server_tab.js>` after `markers_server_tab.js` (`:892`).
- `web/static/js/servers.js` — row cell `<td class="text-center loudness-lib-col loudness-lib-cell d-none"></td>` beside `:1392`; `loadLoudnessTab(server)` beside `:1355`; `payload.loudness = window.readLoudnessFromForm(server)` beside `:1660`; `renderLoudnessLibraryColumn()` beside `:3414`; tab id map entry beside `:1311`.
- `web/static/js/loudness_server_tab.js` (new) — the library-column subset of `markers_server_tab.js` (`syncLibraryColumn`/`renderLibraryColumn`/`readLibraryIds` `:397-488`, stored/touched choice semantics identical): column shown only while `#loudnessEnabled` is on; default selection = every movie/show library (music/audiobook default off until stage 6); switch disabled with the hint "Confirm the Plex database write on the Intro & Credits tab first" while `markers.plex.db_write_confirmed_at` is unset (same rule as `validate_server_loudness`); exports `loadLoudnessTab`, `readLoudnessFromForm`, `renderLoudnessLibraryColumn`. No shared-helper refactor of `markers_server_tab.js` in this PR (keeps the upstream diff additive).
- Start-job modal (`index.html:329`, `app.js:3184-3300`, above) uses the same server + library picker as Intro & Credits, limited to libraries with Loudness on.
- `tests/e2e/test_loudness_server_tab.py` — mirror `tests/e2e/test_intro_credits_server_tab.py`: column hidden while off, shown when on, defaults, saved `library_ids` round-trip via `PUT /api/servers/{id}`, disabled until Plex DB write confirmed.

Tests
- `tests/fixtures/markers/plex_schema_1_43.sql` — append the F10 `media_streams` DDL + its three indexes (E21 `_make_db` loads it).
- `tests/loudness/test_analyze.py` — `command()` golden args (F1); `parse()` on a captured loudnorm stderr; `ln_fields` on the F3 sample → `{"ln:loudness": "-23.23", "ln:peak": "-8.57", "ln:lra": "7.30", "ln:threshold": "-33.93", "ln:gainOffset": "0.21", "ln:loudnessAnalysisVersion": "0.02"}`.
- `tests/loudness/test_plex_db.py` — fixture DB with one part and two audio streams (one with the F2 `ma:*`-only JSON, one NULL); `write_stream` output byte-identical to the F2 example including `url`; second write is a no-op; the write log line has `before`/`after`; a trigger on `media_streams` makes `_check_schema` refuse.
- `tests/loudness/test_job.py` — E21-style engine test: `check_fn` returning `loudness_up_to_date` never reaches a worker; `check_fn` None → `process_fn` called with `gpu="nvidia"` still runs (mock `analyze.run`) and counts `loudness_written`.
- `tests/loudness/test_settings.py` — defaults, `library_ids` null/list, Plex confirmation rule, `loudness_libraries` drops music kinds.
- `tests/test_upgrade.py` — v20 seeds the block once, idempotent; `tests/test_jobs_kind_persistence.py` — `parse_job_kind("loudness")`.

Docs (E22)
- `docs/plex-loudness-normalization.md` (new page: what it does, F1 command, off by default, confirmation, "Plex keeps analysing too", rollback log), `docs/_config.yml` nav entry, `docs/llms.txt` link line, `docs/reference.md` — `### Job kind \`loudness\`` table, `media_servers[].loudness` settings rows, `POST /api/loudness/jobs` rows; `python scripts/generate_llms_full.py` to refresh `docs/llms-full.txt`; one line in `README.md` features.

Rollback tooling
- `scripts/undo_loudness_writes.py` — reads `/config/loudness-writes.jsonl`, restores each `before` (`UPDATE media_streams SET extra_data=? WHERE id=? AND extra_data=?`), Plex stopped; `scripts/README.md` entry.

Deploy on thevault (other repo, separate follow-up commit)
- `/sharedfolders/media/docker/services/plex-generate-previews/compose.yaml` — replace the digest-pinned `image:` (E23 `:4`) with `build: {context: /home/denis/git/media_preview_generator, dockerfile: Dockerfile}` + `image: local/media_preview_generator:loudness` (ytdl pattern, E23); `docker compose build && docker compose up -d`; note in the repo's Renovate that this image is now local. Revert = restore the upstream digest line.

## Execute-time changes (2026-09-29)
- Plan lives at `plans/loudness-job-kind.md`, git-excluded (user: local only; upstream's `docs/` is the published site and a plan there broke the docs-site/llms tests).
- Duplicate Plex parts of one file (same path, several media_items — seen live on Puffin Rock: 3 parts, Plex analysed 1) are analysed once per stream index and written to each part's stream.
- Extra outcome `loudness_not_in_library` (Plex has no stream rows for the file yet), distinct from `loudness_no_owners`.
- `ln:*` values stored as loudnorm prints them (already 2 decimals), not re-formatted — answers Open question 3.
- Loudnorm reporting `-inf`/NaN (silent track) → the stream fails with a reason; nothing is written.
- `api_markers.parse_job_request()` extracted (behaviour-preserving) and reused by `POST /api/loudness/jobs`.
- `SELF_PAUSED_KINDS` also used by `app.py` startup settle, `job_modal.js` server pills (`_hasOwnRunner`).
- Upstream has no `scripts/README.md`; the undo script documents itself (docstring) and in `docs/plex-loudness-normalization.md`; test `tests/loudness/test_undo.py`.
- Start-job modal shows every library (as Intro & Credits does); the server skips libraries without Loudness and names them in the job warning.
- Tests that enumerate tabs/columns/schema version/startup settle calls/server dict keys/README features updated (`test_intro_credits_server_tab.py`, `test_upgrade.py`, `test_app.py`, `test_servers_registry.py`, `DOCKERHUB_README.md`).

## Review fixes (solo:reviewer, 2026-09-29: 0 CRIT, 13 WARN, 3 CUT, 4 NIT — all addressed)
- Loudness has its own schema/trigger check (`loudness.plex_db.check_schema`); the markers guard (`_WRITTEN_TABLES`/`_REQUIRED_COLUMNS`) is back to upstream, so a loudness table change can never stop Intro & Credits.
- No v20 migration: a missing `loudness` block already reads as off; claiming schema v20 in a fork would collide with upstream's next migration. The save route stores a block for Plex servers only.
- Undo record written right after COMMIT (before read-back); URL-form rows kept in URL form; no-op `url` pop removed.
- `LoudnessContext.db`: a writable answer is kept for the job, any other is re-checked after 60 s.
- Files Plex hadn't added yet (`loudness_not_in_library`) get a retry job (15 min × attempt, up to 3), reusing `_wait_for_retry_time`; UI tip corrected.
- Webhook follow-up keeps the preview's pin only when it names a Plex server with loudness on.
- Loudness refused when the server uses the Plex marker agent (it needs the DB file locally).
- Cancel mid-file → `failed` "cancelled before stream N", not up to date.
- Startup settle of loudness jobs in its own try, after Intro & Credits' pass-on.
- Undo script opens the DB `mode=rw` (a typo can't create an empty DB).
- Files panel outcome filter knows the loudness outcomes.
- CUTs: dashboard worker-card callback hoisted to `markers.job_runner.worker_cards(jm)` (used by both runners); markers' `_complete` reused; duplicate loguru import removed.
- Tests added: runner lifecycle + retry (`tests/loudness/test_runner.py`), follow-up isolation and pin, busy DB, URL-form row, DB re-check, cancel mid-file; API tests moved to `tests/loudness/test_api.py`.

## Check
```
cd /home/denis/git/media_preview_generator && ruff check . && ruff format --check media_preview_generator tests scripts \
  && pytest -q tests/loudness tests/test_job_kinds_engine.py tests/test_jobs_kind_persistence.py tests/markers/test_plex_db_publisher.py tests/markers/test_triggers.py \
  && pytest -q -m e2e -n 0 --no-cov tests/e2e/test_loudness_server_tab.py tests/e2e/test_intro_credits_server_tab.py \
  && python scripts/generate_llms_full.py --check && pytest -q -k "docs or llms" --no-cov -n 0
```
Live proof on thevault, in order (each step gates the next):
1. Backup: `docker exec plex '/usr/lib/plexmediaserver/Plex SQLite' '<db>' ".backup '/config/…/manual-pre-loudness-$(date +%F).db'"` then `PRAGMA quick_check` on the copy via the same binary (F7, F8).
2. `PUT /api/servers/{plex}` with `loudness: {"enabled": true, "library_ids": ["7"]}`; `POST /api/loudness/jobs {"file_paths": ["/shared/kidsshows/Puffin Rock …S01E35…"]}` → job `loudness_written: 1`; `SELECT extra_data FROM media_streams WHERE id=…` equals what Plex's transcoder wrote for E34 (F3: same JSON → identical `ln:*` strings); `GET /library/metadata/{ratingKey}` shows `canNormalizeLoudness="1"` on that stream (F9); Normalize Loudness offered in the player; Plex's butler log for that item shows no new `Plex Transcoder … loudnorm` run (F1) on its next pass.
3. Codec parity before wider enable: one file each of eac3 5.1, truehd/Atmos, dca, multi-audio — compare our `ln:*` with Plex's after Plex's own `asap` job reaches it (or a temporary `PUT /library/metadata/{id}/analyze` if it triggers loudness) (F5).
4. `POST /api/loudness/jobs {"libraries": [{"server_id": …, "library_id": "7"}]}` (452 streams, F13) → `loudness_written` count == streams lacking `ln:*` before; `quick_check` again.
5. Enable remaining video libraries; run "all libraries"; then and only then set `LoudnessAnalysisBehavior` to `never` if the proof holds (Open question 1).
Rollback: `python scripts/undo_loudness_writes.py --log /config/loudness-writes.jsonl` with Plex stopped, then compose back to the upstream digest.

## Deferred (YAGNI)
- Writing `metadata_items.extra_data` `ln:loudnessAnalysisVersion` + `changed_at` — reopen if step 2 shows Plex needs it for `canNormalizeLoudness` (F12; then it must go through Plex SQLite outside the app, e.g. a `docker exec plex` sidecar, because of the FTS triggers).
- Schedule `job_type: "loudness"` — reopen if the user wants a periodic sweep after the backlog is cleared (webhook follow-up + Plex `asap` cover new files).
- Plex marker agent (`RemotePlexDb`) support — reopen if a remote-Plex user asks; loudness uses `LocalPlexDb` only.
- Retry/verify chains, `force` re-analysis, Emby/Jellyfin loudness — no need shown.
- Per-kind worker cap — decision 3 says shared pool; reopen only if previews starve.

## Decisions (user, 2026-09-29)
- Q1: after step 5's proof, set Plex `LoudnessAnalysisBehavior` to `never`.
- Q4: include music/audiobook libraries too — but as a **gated stage 6**, not in the first cut: Plex's music loudness differs from video (binary strings: `LoudnessScannerOperationReplayGain`, `…MixRamp`, `…Waveform`, `DatabaseMigrationsComputeNewfangledAlbumLoudness`, `includeLoudnessRamps`; runs via `Plex Media Scanner --analyze-loudness --section N`). Before enabling music: compare the `media_streams.extra_data` (and album/metadata rows) of one Plex-analysed music track vs what our writer would produce; extend the writer only for keys we can reproduce exactly (ramps/album gain may need Plex's scanner — then music stays on Plex's scanner with `--loudness-parallelism`). `loudness_libraries` keeps the `movie`/`show` filter until stage 6 passes.
- Q5: multi-part items analysed per part — accepted.

## Open questions
1. Decision 2 interpretation: keep `LoudnessAnalysisBehavior=asap` until step 5's proof, then `never` — confirm; alternative is leaving `asap` forever (Plex then only tops up what we miss; costs nothing if step 2 shows it skips analysed streams).
2. F12: is `metadata_items` `ln:loudnessAnalysisVersion` required for `canNormalizeLoudness`/Normalize Loudness, or does Plex serve from `media_streams` alone? Decides whether the Deferred sidecar becomes part of the Changes.
3. Precision: Plex stores two decimals (F2); is that a print of loudnorm's already 2-decimal JSON (F3 suggests yes) or rounding — settle by comparing one stream where loudnorm prints more digits.
4. Music/audiobook libraries (`section_type` 8): excluded — Plex parallelises those via the scanner; confirm.
5. Multi-version items (two parts): analysed per part independently (streams hang off `media_part_id`); OK?

## pr-ready review fixes (2026-09-29)
- job: `retried=False` (retry covers not-in-Plex only); warning when retries run out
- job: undecodable extra_data → per-server FAILED row; lock per server; `check_item` drops unused `cancel_check`
- app.js: Retry now hidden for loudness retry jobs; app.py `_fail_unrevived_own_runner_jobs`
- settings: invalid stored block warned once per server
- undo → `media_preview_generator/loudness/undo.py` (ships in image), torn lines skipped, mode=rw tested
- follow-up plumbing (`follows_job_id`) moved from slice 2 to slice 3
- Skipped NITs: markers wording in loudness rows, read-back-after-commit message, follow-up UI grouping/retry chip, second follow-up call on batch growth
- Known flaky on dev too (not fixed): test_textdet_helper TestGpuFailures, test_season weekly_arrivals, test_job_dispatcher first_worker_update

## Live test on production Plex (2026-09-29/30)
- Write accepted: Plex API shows ln values + canNormalizeLoudness="1"
- Parity vs Plex: AAC/Opus/MP3/AC3/DTS/TrueHD identical. EAC3: Plex decodes via Dolby EAE (`-eae_prefix`, only inside PMS) with no DRC → we add `-drc_scale 0` for eac3 only (AC3 must keep DRC). EAC3 then identical or true peak ≤0.45 dB off
- Plex decides re-analysis by metadata_items.extra_data ln:loudnessAnalysisVersion, not stream values (flagged item skipped loudnorm; unflagged control re-analysed). → job marks item once all its audio streams (all versions) are done; only extra_data written, FTS triggers are UPDATE OF title/title_sort/original_title; schema check refuses any UPDATE trigger that could fire on extra_data; undo restores marks
- Plex runs loudness one item at a time (~1x realtime on 5.1 under load); Analyze requests while busy are dropped
- loudnorm itself ~4.5x realtime/core (192 kHz resample + full normalizer); ebur128 16x faster but not parity → possible follow-up
- Library.kind is plexapi METADATA_TYPE (movie/episode/track) → DEFAULT_KINDS includes "episode"
- YouTube library (485 streams / 480 items) fully written + marked, 0 failures
