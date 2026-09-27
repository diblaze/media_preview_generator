# A season on every disk of the library (season audio v10)

**Done** (spec §5.3 "A season on every disk, and three picking rules", §14 2026-09-27). Held fix 3 came back with the
two matcher changes it waited for (a second opening's quorum, the file-start bumper), and the Alias S02E09 miss was
traced to the fingerprint window. The held patch is gone; the scripts that measured it (`t3_*.py`, below) stay.

## Why it matters

sflix's TV library is spread over `/data_16tb`, `/data_16tb2`, `/data_16tb3` and `/data_28tb`. 8,058 of its 10,555
seasons sit in more than one folder (88,874 files; `v10/split_survey.py`), and before v10 each part was matched alone,
so an episode's group depended on where the pool put each file. No split season in the survey has differently named
show folders, but the grouping doesn't rely on that: the same show is told by the ids in its folder names.

## The rules (one line each)

- **Group across the library's disks**: an episode's season is wherever the library keeps it, not where one disk put it.
- **Same show by one key**: the folder's tvdb, else tmdb, else imdb id names the show whatever the folder is called;
  the name only without ids. One key per folder, so "the same show" can't depend on which folder asks.
- **Closure over libraries**: the libraries holding any folder found are looked in too, so an episode on a disk only
  one server holds finds the same season as one on a disk every server holds.
- **An unreadable disk holds the season**: a folder that errors (a stale handle) makes season audio wait, rather than
  re-decide the season without that disk and again when it is back.
- **At most 40 new pairs inline**: a second opening's quorum asks for other episodes' pairs; past one episode's worth,
  a worker matches them once for every sibling.
- **Folder names, not server calls**: every run, the Season view and Season Publish must see one group, server up or not.
- **A second opening's quorum**: a season whose opening changes once (a second cour) has two openings, each of about
  half the season, so each needs half of its own episodes.
- **Only in sequence, and not shared**: two cuts taken in turn through a season, or a stretch shared with an episode that
  has the season's opening (a recap), are not a second opening.
- **At least 15 s on both sides**: an opening is intro-length; shorter stretches keep the season's quorum.
- **A bumper at 0 s gives way to a floating stretch**: a spot put at the start of every file repeats as widely as the
  title sequence, which moves with the cold open.
- **A stretch the window cuts is passed over**: its end is the fingerprint's, not the audio's.
- **Pairs keep their cache** (`PAIR_RUNS_VERSION` 9): v10 changes what is picked from the runs, not the runs.

## Measured

Before = `dev` `cab4ccf` (own folder), after = this branch (every disk, v10 rules). Season step only, the app's code,
every member fingerprinted into the eval cache (`~/.cache/markers_eval`, CPU ffmpeg), end pictures decoded on
storage's P5000 and cached per share. useful / wrong / missed:

| Set | Before | After |
|---|---|---|
| Accused | 3 / 0 / 53 | 3 / 0 / 53 |
| lab 118 (114 still on disk) | 87 / 10 / 17 | 86 / 8 / 20 |
| held-out 175 (Plex 69 / 4 / 102) | 124 / 4 / 47 | 142 / 4 / 29 |
| library chapter set (224) | 106 / 58 / 60 | 113 / 54 / 57 |

Split-season sample (`v10/sample.py`, `random.Random(20260927)`: 14 split seasons, 3 episodes each; every answer
frame-checked on contact sheets, `v10/sheet.py`): correct 20 → 24, wrong 0 → 0, none skipping story. The four gains
(South Park S17E05, The Chosen S04E01, Unorthodox E01 and E04) come from the grouping; the rules change nothing there.

SPY x FAMILY S01 (25 episodes, two openings): answered 16 → 18 (E02–E12 at the first opening; E13, E15–E17, E20–E22 at
the second); E24 and E25, answered alone in their small folders, fail the end-picture check against the other
release's pictures. Without the second-opening quorum the whole season answers 0 of 25.

Star Trek: Strange New Worlds S04 (10 episodes): correct 6 → 8, wrong 4 → 2. The "Star Trek 60" bumper (0–28 s, 9 of
9) gave way to the title sequence (7 of 9) on E03, E05, E06; E01 keeps it (its first audio track is German, its title
sequence matches nothing) and E03's title sequence is found from 34 s in (its first part differs).

Alias S02: the group grows 11 → 22; E02, E04, E07, E11, E18 missed → useful; E09 wrong → none (below).

## Changed verdicts on the four sets

- lab 118: Outlander S08E02, E08 wrong → missed; The Sinner S03E07 missed → useful; **The WONDERfools S01E02, E04
  useful → missed** (the theme's end splits the 8 episodes into two clusters 8 s apart, 3 of 7 each).
- held-out 175: Alias S02 ×5 missed → useful, E09 wrong → missed; SPY x FAMILY E13, E16, E17, E22 missed → useful; Bluey
  S01E02, E18, Food Wars! S01 ×3, My Hero Academia S07 ×4 missed → useful; **Dateline NBC 2025-07-11 missed → wrong**
  (96.6–105.3 s: ends at the show's title; the chapter adds the episode's title card, frames checked).
- library chapter set: Sex and the City S04 ×4 wrong → useful; South Park S12E04, E06 and Glass Heart S01E03 missed →
  useful; RuPaul's Drag Race S12E11 wrong → missed; **Family Guy S14E03 missed → wrong**, but the frames show the theme
  song running 0–31 s and the chapter's 0–15.2 s cutting it: ours is right; **Family Guy S14E07, E17 wrong → missed**,
  though right by the same frames (their end-picture check against E03 fails with the cluster's end 0.12 s later, on a
  spinning end shot); **Turning Point 9/11 S01E02, E04 missed → wrong** (the middle of the title sequence: its runs break
  at 3.5 s gaps; nothing skipped past it).

## Alias S02E09

Its title sequence runs 881.0–905.9 s (frames: "ALIAS" at 901 s, "Created by J. J. Abrams" at 905.9 s). The episode is
2,605.9 s long, so its fingerprint window is capped at 900 s and its last point sits at 897.6 s; every run with another
episode ends there, and the answer (881.6–897.5 s) was the window's end, not the theme's. v10 passes over a stretch
ending within the 3.5 s gap bridge of the last point. Nothing else in the four sets reaches the window's end. Getting
the right end needs audio past the window: a second, longer fingerprint of that one file, with its own cache and worker
step, for one file in 573 — not done.

## One-time cost on sflix

On sflix's `markers.db` copy of today (2,124 season audio answers, `v10/prod_cost.py`): 1,414 groups, 54 of them across
disks; 308 members never fingerprinted (CPU ffmpeg, 2–14 s each); 3,419 pairs never matched (6.3 ms each on real
fingerprints, `v10/cost.py`: under a CPU-minute); every answer re-picked from cached pairs. The whole library's split
seasons hold 374,539 cross-folder pairs, about 0.7 CPU-hours once all of them are checked. A group lookup lists each
library folder once: about 16 ms for sflix's four.

## Scripts (`v10/`, run from the session scratchpad with `run.sh base|new`; paths still point there)

Logs, JSON rows, pickles and contact sheets are local-only (they name library files).

| Script | What |
|---|---|
| `lib.py`, `find.py`, `app_group.py` | Shared: the eval cache, pair and share caches, a code tree's season step on a group; the app's grouping for sflix's library |
| `split_survey.py` | Every split season of the four TV disks |
| `run_sets.py`, `diff_sets.py` | The four truth sets, own folder vs every disk, and the per-file changes |
| `variants.py`, `run_variants.py` | The rules as variants on dev's code, before porting them |
| `sample.py`, `sample_run.py`, `sample_sheets.py`, `sheet.py` | The split-season sample, its answers and contact sheets |
| `explore.py`, `explore_q.py`, `hits.py`, `endpic.py`, `season_before_after.py`, `season_sheets.py`, `set_sheets.py` | Per-season looks (clusters, the second-opening quorum, hits, end pictures) |
| `check_groups.py`, `time_group.py`, `cost.py`, `prod_cost.py` | The app's grouping equals the scratch one on 612 files; its speed; the one-time cost |

The held lane's scripts (`t3_*.py`) measured the patch as it was held on 2026-09-25.
