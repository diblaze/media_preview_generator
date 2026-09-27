# Card read and intro window (2026-09-27)

Three fixes the owner approved from the prior-art research: read the card the credits start lands on (credit text v8),
fingerprint half the episode up to 20 minutes (season audio v11), and let an intro run up to 300 s. Spec: §5.3 "Half
the episode, and intros up to 300 s", §5.4 "Version 8, prose cards", §14 2026-09-27 "Card read and intro window".

**Held (this branch, `fix/intro-window-cap`):** the window and cap changes are not in #327. On the library chapter set
they add new wrong verdicts (Evil S04 ×4 now partial where they were missed; Glass Heart S01E03 loses its title card),
and the bar is no new wrong answer on any set. Two general rules were tried at the new window on that set
(`season/tried_rules.patch`, cached pairs). Both were worse than v10's 113 / 54 / 57, at 109 / 57 / 58:
- **A one-partner stretch never ends the walk**: it is passed over for the quorum cluster below it.
  - Glass Heart S01E03 comes right.
  - The Simpsons S17E02/E05 go from missed to wrong: 0–11.3 s, the theme's start before each episode's couch gag.
  - RuPaul's Drag Race S12E05 goes from missed to wrong.
- **No answer for part of a longer shared stretch**: this fires when a stretch that 2 or more other episodes share runs
  on from the pick for 8 s or more past it, within the 3.5 s gap bridge.
  - Evil S04E02, E12 and E13 return to no answer, but S04E14 stays partial.
  - Sex and the City S04 ×4 lose right intros.
  - Recipes for Love and Murder S01E01 goes from wrong to missed.

Local-only (gitignored): `local/` holds the base tree (`local/base`, `git archive 95c222c`), the models
(`python3 scripts/fetch_textdet_model.py --out local/models`) and the season step's pair caches; each script writes
its answers, replays and logs beside itself (they hold library paths). The scripts ran from one scratch folder, the
season and reading ones at its top and the credits ones in its `ct/`, which is the layout their relative paths expect.

## Method

- **Base** is `fix/season-across-disks` at `95c222c` (`git archive` into `cardread/base`), **work** this branch.
- **Credits**: `credits/run.sh <base|work> <tag> <sets>` runs one tree's `find_credits` through the harness caches
  (`credits/ctrun.py`; GPU decode on storage's P5000, the worker's CPU rerun). The work run reuses the base's decodes
  (`--decode-digest`, `credits/digest.py`): v8 adds new decodes beside `decode_rows` and leaves every existing row as
  it was. `credits/drop_reads.py` drops cached card reads after a reader change. Sets: the 14 named files, the 80,
  the audit's answer-key files (`items`), Accused, I Survived, the 205. `credits/ct_eval.py` tallies each set (start
  within 5 s of the truth, or inside a frame check's range) and lists every changed file; `credits/show_moved.py`
  prints each moved file's card reads. The answer key: `credits/replay.py` (the audit's `markers.db` copy, with the
  tree's credit text and `prose_start_s` for `chapter_origin`), then `../credits-accuracy/metrics.py` (seed 20260927
  split, held-out reported apart).
- **Season**: `season/sruns.py` (via `season/srun.sh`) runs one tree's season step on the lab 118, the held-out 175,
  the library chapter set, Accused and the split-season sample, with `FAILFAST` against the base. `CAP` patches the
  matcher's intro cap for the 120 / 300 / none comparison. `season/audit_audio.sh` runs the answer key's 51 intro items
  through both trees for the replay's `--audio`. `season/cand_debug.py` prints one episode's ranked clusters.
- **Reading**: `reading/m2_again.py` read the research's M2 cards (11 epilogue, 41 first credit cards, 31 others)
  through the app's own decode at 1280×720 and 1920×1080 (the same calls; 0.72 against 1.59 s of CPU a card, median),
  `reading/prose_eval.py` compares prose rules on M2 and on every card a run read, `credits/confidences.py` prints
  per-line confidence, `credits/probe_offsets.py` what the frame at the start itself reads.
- **Costs**: `season/cost_fp.py` (fingerprint CPU at both windows, one pair's matching), `credits/cost_cards.py`
  (`find_credits` on a CPU worker without and with the reader, in-process models, no caches).

## Rules, and the file that set each

| Rule | Why |
|---|---|
| Read only a start whose first text is dark | Epilogue text sits on black; a lit start is never read |
| First card from its own 30 s at 1 fps | Mixed keyframe and 1 fps rows merged cards (Breach 12 s late, #SKYKING 10 s) |
| Read at the middle of the fullest seconds | A keyframe time on a fade read nothing (Accused S03E09) |
| Cards split where neither second's boxes lie inside the other's | Lines appear one by one (A Beautiful Imperfection) and fade out (Gandhari) |
| Sentence: 4+ words to a line-end full stop, not an initialism | "P.G.A." on Jay Kelly's first card; "U.S.", "Jr." |
| ... counted across lines, closing quotes stripped | "ABUSE THAT HAPPENED." ends a line of 3 words; "VALUE.'" |
| ... and 3 words to a line on average | Fightland S01E08's cast list ends in "JR.", Pamela's archive credits, My Sad Dead S01E04's "Waldo Salgado L.", Paris, Texas' dot leaders |
| Or a line of 7+ words, most lower-case | Epilogue lines without a full stop (#SKYKING, 13 Minutes) |
| Lines under 0.9 confidence left out | 777 Charlie's Kannada crawl read as lower-case "words" at 0.5–0.8; Latin lines 0.95+ |
| The rest decoded at 320×180 and 640×360 | Small credit cards only the larger frame boxes (Accused S02E04, S07E05) |
| A one-second card after the first ends the walk | The owner's "never past where the roll clearly continues"; a crawl moves every second |
| The first card is read however short | #SKYKING's disclaimer fades in over one second |
| 90 s window; all prose there keeps the start | Twice Gandhari's 46 s of epilogue |
| Window half the file, at most 1,200 s | Plex's own; Alias S02E10's title sequence ends 1,084 s in |
| Intro runs up to 300 s | The decision's own intro limit; Intro Skipper #1008 (a 126 s opening dropped at 120 s) |

## Results

Credit text alone (start within 5 s of the truth or inside a frame check's range), base → work:

| Set | Right | Early | Late |
|---|---|---|---|
| 14 named files | 1 → 12 | 13 → 2 | 0 → 0 |
| 80 (movies40 + tv40) | 54 → 54 | 11 → 11 | 13 → 13 |
| 205 (the named ones apart) | 71 → 73 | 38 → 36 | 41 → 41 |
| Accused (the 7 named apart) | 38 → 38 | 0 → 0 | 8 → 8 |
| I Survived | 13 → 13 | 1 → 1 | 0 → 0 |

The answer key, replayed decisions: credits wrong 8 → 7 of 88 (tuning 4 → 4 of 61, held out 4 → 3 of 27), skipping
story 1 → 0; the Plex comparison 7 → 6 of 81 (5 with #SKYKING's frame check), skipping story 1 → 0 (Plex's own 8 of 80).
Intros, each tree's season step for the 51 intro items (`season/audit_audio.sh`, the replay's `--audio`): 6 of 51
wrong before and after (tuning 5 of 35, held out 1 of 16), none skipping story; one answer moved (Homicide Hunter
S06E12's start 1.5 s earlier, right both ways).

Season step on its own, useful / wrong / missed, v10 → v11 (300 s cap):

| Set | v10 | v11 |
|---|---|---|
| Lab 118 (114 on disk) | 86 / 8 / 20 | 86 / 8 / 20 |
| Held-out 175 | 142 / 4 / 29 | 144 / 4 / 27 |
| Accused | 3 / 0 / 53 | 3 / 0 / 53 |
| Library chapter set (224) | 113 / 54 / 57 | 112 / 58 / 54 |
| Split sample (42) | unchanged | 3 The Chosen S04 answers within 0.4 s |

The 120 s, 300 s and uncapped runs gave the same verdicts on every set.

## Frame checks (this lane)

- **Credits starts** (the owner's rule: epilogue text is not credits; a dedication, a help-line notice or a closing
  title before the first card may sit either side): #SKYKING epilogue to 5,287 s, "Directed and Produced by" 5,289
  (the answer key's 5,243 sits on the epilogue); Ocean with David Attenborough epilogue 4,744–4,771, "Directors" 4,774
  (the 205's 4,743.7 sits on it); To Dye For "THE END" 4,885, epilogue cards 4,887–4,923 (the third letter-spaced and
  dissolving), "written and directed by" 4,926; Gandhari epilogue to 6,518, a last shot 6,520–6,530, the title 6,531,
  first card 6,544 (base 6,485 skipped the shot); Paris, Texas dedication 8,665–8,669, cast crawl 8,671; 13 Minutes
  dedication 6,258–6,266, crawl 6,270; Habeas Corpus S01E06 help-line notice 3,410–3,416, first credit 3,417.5.
- **Season**: Evil S04E02 title sequence 1,050–1,132 (answer 1,091–1,134), S04E12 862–938 (answer 862.5–889.9; its
  chapter 901–941 starts halfway): inside the sequence, no story skipped. Glass Heart S01E03's cluster walk (work):
  571.8–587.7 s with 1 partner (S01E07 at 1,148.9 s) ranks above the 13 s title card's 378.5–391.7 with 5.

## Costs

- Fingerprint CPU per episode, v10 → v11: 4.7 → 6.2 s (45 minutes, Alias S02E09/E10), 2.4 → 2.9 s (24 minutes,
  Mushoku Tensei); a pair's matching about 10 ms at either window.
- Reading the card, CPU worker, without → with: a dark start whose card isn't prose +7.8 / +9.8 / +18.0 s CPU on three
  1080p files (+1.5 to 3.5 s wall), +54.5 s on Avengers 4K HEVC (+5.8 s wall), almost all of it the 30 s 1 fps
  decode (reading a card is 1–3 s of CPU); a prose start +42.3 s (Accused S03E09) and +71.2 s (Breach, 5 cards read).
  421 of 736 files' starts were on black. On a GPU worker the decodes and both models run on the GPU.

## Both models on each GPU

The helper's WebGPU sessions read the CPU's words and find its boxes, with no CPU fallback: storage's P5000
(`tests/markers/credits/test_textdet_helper_integration.py`, `reading/gpu_read_test.py`) and, in a throwaway container
of plex's image with this branch's package mounted (`reading/intel_read.sh`, nothing from `/data`), plex's Intel iGPU
(`/dev/dri/renderD128`) and TITAN RTX: backend `webgpu`, 4 cards read as on the CPU, 20 frames' boxes the same. AMD is
untested (no hardware), as for detection.

## Not fixed

- Evil S04's re-cut title music (4 partial intros where there were none), Glass Heart S01E03 (the walk ends at a
  best-ranked cluster without the quorum, a deliberate rule), To Dye For's letter-spaced card and Trainwreck's
  statement without a full stop (both still early, less than before). The Wargame's "Inspired by …" card reads as prose
  (7+ words, most lower-case) and moves its start 1–2 s.
