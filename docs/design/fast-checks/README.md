# Fast checks for files that are already done

Status: draft for review (2026-10-14).

## The problem

A job looks at every file in the library. Most files are already done, but each one still pays for a full set of
slow reads before the job can say "nothing to do":

- **Intro & Credits:** 80–200 lookups in `markers.db`, 5–6 folder listings on the media share, up to 2 calls to Plex.
  About 0.4 s per file. A 116k-episode library takes hours just to confirm it's done.
- **Previews + chapter thumbnails:** 4–5 file checks on the share, sometimes a read of the file's start and end,
  2–3 calls to Plex, and every chapter image decoded again.
- **Loudness:** about 7 file checks and a read on the share, a Plex database query and a Plex call per file.

Seen live on job `ddf1aa6a`: after a restart it spent over an hour re-checking ~77k files it had already finished.

## The rule this design keeps

**Every file still gets the full check, with the same rules.** Nothing is skipped because "we remembered it was done".
Only *how the check gets its facts* changes: read shared things once instead of once per file.

So the only way to get a different answer is to read a fact wrongly, which the comparison test below catches.

We add no new saved state and no new settings.

## What changes (same for every job type)

1. **Ask Plex once per season, not once per file.**
   - When the first episode of a season is checked, read all its episodes from Plex's database in one query
     (markers, chapters, loudness values, part hash and size, item ids).
   - Plex's HTTP API takes several ids in one call (`/library/metadata/1,2,3`, checked on sflix: 3 items in 6 ms).
     Use that wherever a check must ask the API instead of the database.
   - The read is held only while that season is being checked (seconds), so it's as fresh as today's per-file read.
     Anything this job writes to Plex for the season drops the cached read.
2. **Look at the media share as little as possible.**
   - One `stat` per file at the start of its check, passed down, instead of 4–7 separate ones.
   - One more `stat` at the end, only where today's code checks "did the file change while we worked".
   - The start/end hash read (128 KB) happens at most once per file per job, and is shared by all job types.
3. **List each season folder once.** The folder listing and the other episodes' details are reused by every
   episode of that season, instead of being rebuilt for each one.
4. **Read our own database once per file.** One or two queries fetch everything `markers.db` knows about a file,
   instead of 80–200 small ones.
5. **Do per-job things once per job.** Settings, rule versions, Plex `/identity`, the loudness "can't decode" list
   and server capabilities are read once.

## Per job type

### Intro & Credits

- The season step (`season_intro_chapter_limit`) uses the cached season folder and episode details (change 3).
- `_decide` is currently called up to 3 times per file, each re-reading the evidence. It will read once and reuse it.
- Plex's live markers, part durations and "is this marker Plex's own" read-backs come from the per-season Plex read
  (change 1). Today's "always re-read Plex, never trust stored evidence" rule still holds. The read is just batched.
- The publish read-back ("is it showing on Plex") uses the same per-season read.

### Previews + chapter thumbnails

- The Plex bundle hash comes from the per-season Plex read instead of one `/tree` call per file. Webhook jobs
  (brand-new files) still hash the file, as today.
- Plex `/identity` is asked once per job instead of once per file.
- Chapter rows (count, start times, thumbnail URL) come from the per-season Plex read.
- Chapter images: check that each is a non-empty JPEG and hash its bytes, without fully decoding it.
  (To confirm during the work: why it decodes today. If decoding catches broken images, keep a cheaper header check.)

### Loudness

- Plex streams, part hash and size, and the "analysed" flag come from the per-season Plex read.
- The per-file Plex API check (`verify_streams`) becomes one batched call per season.
- The "can't decode" list in `loudness.db` is read once per job.
- After a restart, a revived job checks its files through the normal parallel checks. Today it re-checks them one at
  a time in a single thread.

### Restarts (all job types)

With checks this cheap, a restarted job can simply re-check every file. Re-checking ~100k done files should take
minutes. Intro & Credits currently skips "files finished before the restart" using the Files-panel rows, which only
keep 5,000 per outcome. That list is incomplete, so we remove it. The loudness one-at-a-time re-check goes too.

Trade-off: a restarted job's summary counts files it already published before the restart as "up to date", not
"published".

## How we make sure nothing is missed

1. **Same answer, file by file.** A comparison harness runs the current code and the new code over the same files and
   records what each *would* do (publish, keep, generate, analyse, wait, skip) without sending anything.
   - Inputs: copies of sflix's `markers.db`, `jobs.db`, `loudness.db` and Plex database, plus the real media read-only
     on the storage lab servers.
   - Sample: a few thousand files spread across shows, including the hard cases: replaced files, missing files, new
     episodes in an old season, multi-version items, "Keep Plex's" items, files Plex hasn't added yet, and
     undecodable audio.
   - **Pass = zero differences.** Any difference blocks the change until it's explained and fixed.
2. **Reads are counted in tests.** Tests count `markers.db` queries, share `stat`/`listdir` calls, Plex database
   queries and Plex HTTP calls per file, so a later change can't quietly bring the slow reads back.
3. **The existing test suites still pass.** That covers the Intro & Credits evaluation (`tools/markers_eval`) and
   the e2e tests.
4. **Live check on sflix.** After deploy, one full TV Shows Intro & Credits run and one loudness run. The counts
   (published / up to date / nothing found / failed) must match the last full run, apart from real library changes.

## Found while building (2026-10-14)

- `MarkerStore` shares one sqlite connection between all threads behind one lock (`markers/store.py:559`). The 8
  check threads therefore take turns on every query, which makes the 80–200 queries per file the real ceiling.
  Cutting the query count (season data once, one read per fact per file) matters more than batching Plex calls.
- Plex batching is deferred. We'll measure after the cheaper changes land and only batch if Plex calls still show up.

## Order of work

1. The comparison harness and read counters. These come first so every later step can be proven.
2. Intro & Credits (biggest cost).
3. Loudness.
4. Previews + chapter thumbnails.
5. Restart simplification.

Each step must pass the comparison with zero differences before the next one starts. The architecture review runs on
steps 2–5, because they touch concurrent checks and Plex reads.

## Out of scope

- Any new setting, or any change to what gets generated or published.
- Emby and Jellyfin keep their current reads, apart from the shared changes (one `stat`, one season listing, one
  `markers.db` read).
