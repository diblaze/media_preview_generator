# Prior art: how others find intros and credits (2026-09-27)

A survey of every public tool, product, paper and building block for intro and credits detection we could find, read
from primary sources (code, docs, papers, staff posts), ranked by what it could do for the problems still open after
#320 and #324 (spec §0 "Open after the 2026-09-27 credits fixes"; `../credits-remaining/README.md`,
`../credits-accuracy/README.md`, `../intro-end/README.md`, `../season-across-disks/README.md`). The three ideas that
ranked highest were measured cheaply on the failing files. Nothing here changes the app.

**The open problems**, numbered as they are used below:

1. **Names over bright or busy footage, or a collage**: the start lands on the crawl after them (17 Again +82 s,
   '71 +119 s, 21 Jump Street +102 s, 14 Peaks +42 s).
2. **Prose epilogue cards on black glued to the roll**: the start lands on the card (A Beautiful Imperfection, as
   Plex's own does; Accused ×4, 11.5–40.5 s early). **#SKYKING belongs here, not in 1** (M2): the audit's truth
   (5243 s) and our answer (5267 s) are both on epilogue sentences, which run to 5284 s; the first credit card,
   "Directed and Produced by Patricia E. Gillespie", is on screen by 5290 s. Our answer is about 20 s early, not 24 s
   late.
3. **A first card over the closing scene missed**: the start a few seconds late.
4. **Intros**: a theme past the 900 s fingerprint window (Alias S02E09), seasons with two openings, bumpers before the
   intro, cold opens.
5. **More reliable across GPUs and CPUs, or cheaper.**

## Short version

- **Nobody public does better on 1 or 2.** Plex combines text detection, black frames and frame entropy with a learned
  model, and starts A Beautiful Imperfection on the same epilogue we do. The one published learned model for intros and
  credits (CLIP + attention, 2025) names credits over footage as its own failure. Jellyfin's Intro Skipper, the most
  active open project, finds credits from chapters, black frames, flat "cards" and the season's shared ending audio,
  with no text detection at all; it publishes no accuracy.
- **Problem 2 needs the words, and a 7.9 MB model reads them** (M2). PaddleOCR's Latin recognition model
  (Apache-2.0, the same family as our detector) on the card an answer starts on, with one rule — a line ending in a
  period, or a line of 7+ words mostly in lower case — calls 10 of 11 known epilogue cards prose (Accused 6 of 7,
  A Beautiful Imperfection, #SKYKING) and 0 of 40 first credit cards. It also flags epilogue starts in the 205 set
  (Breach, Trainwreck: The Astroworld Tragedy, Gandhari) and a truth card that is itself prose (To Dye For). About
  1–2.5 s of CPU per card read.
- **Problem 1 is mostly in how we sample and count, not in the model** (M1). Over the missed cards, our own detector
  at 1 fps and 320×180 boxes text on 48–65 % of lit frames, against 1–12 % of the story just before them and a median
  1 % on 16 right answers' stories. So what loses them is keyframe sampling or rule J, not the model. A crude 1 fps
  walk back from our start fixes 17 Again (0 s) and 14 Peaks (+3 s), not '71 or 21 Jump Street, and pulls 1 of 16
  right answers 16 s earlier (To Dye For, whose truth card is itself prose).
- **Audio is a cheap guard, not a locator** (M3). Silero VAD (2.3 MB, MIT, CPU) hears no speech in the 30 s after the
  first card on 22 of 25 right answers, and 0–1 s of it between the real first card and our late start on 3 of the
  4 problem-1 films. Where there is speech in the minute before, it stops 1–60 s before the first card (median 27 s),
  so it can't place a start; as a "don't walk back over speech" guard it took the walk above to 0 of 16 right answers
  moved (and cost 17 Again 14 s).
- **Problem 5: nothing found is cheaper or more portable than what we have.** No media-server project publishes its
  cost per file or a cross-vendor check like ours. Both models suggested here are small and cheap enough on the CPU
  to run the same on every worker: reading a card 1–2.5 s; for speech, 240 s of a film's audio took 1.4 s to decode
  and 1.9 s of VAD on one thread.
- **Our intro matcher is the field's standard.** Intro Skipper uses the same Chromaprint algorithm with the same
  6-bit, 3.5 s-gap and ±2-shift values. Nothing we found handles two openings in a season explicitly. Only Plex
  looks far enough for Alias S02E09 (the first half of the episode); everyone else stops at 10–15 minutes.

## Everything we found

"Reusable" means usable in our Python + ffmpeg + ONNX Runtime stack on the CPU and on every GPU vendor, and under a
licence a public app can ship. GPL code is marked **ideas only**: the method can be reimplemented, the code can't be
copied without making ours GPL. Accuracy is quoted only where the source publishes it.

### Media servers and their plugins

| Name | Method | Accuracy (published) | Licence | Reusable? | Idea for us | URL |
|---|---|---|---|---|---|---|
| Plex intro detection | Compares the beginning of a season's episodes, "primarily ... the audio". Ignores intros under 20 s; "Intros ending more than halfway into an episode will not be detected". | None | Proprietary | No | Search the first half of the episode, not min(900 s, 35 %): problem 4 (Alias S02E09's intro ends at 905.9 s). | [support](https://support.plex.tv/articles/skip-content/) |
| Plex credits detection | "a machine learning algorithm to make sense of several inputs (text detection, the presence of black frames, and a few other secret ingredients)"; staff: "entropy of analysed frames, text detection and other magic sauce", detector "on version 4" (Dec 2022). Finds mid- and post-credit scenes as further markers. Results shared through a cloud store keyed by file hash. | None. The staff post's heading "93.18 percent of the time, it works every time" is a joke, not a measurement. Our audit: 8 of 80 wrong, 8 of them skipping story (`../credits-remaining/README.md`). | Proprietary | No | Frame entropy as one more input. Plex makes our problem-2 mistake on A Beautiful Imperfection, so there's no hidden trick to copy. | [blog](https://www.plex.tv/blog/let-the-next-episode-roll/), [staff post](https://forums.plex.tv/t/forum-preview-credits-detection-for-plex-media-server/822998), [support](https://support.plex.tv/articles/credits-detection/) |
| Emby intro detection | Per season, at least two episodes; "analyzes the first 10 minutes of the episodes". No credits detection documented. | None | Proprietary | No | None (a smaller window than ours). | [docs](https://emby.media/support/articles/Intro-Skip.html) |
| Jellyfin Intro Skipper (intro-skipper org, branch `12.0`, active on 2026-09-27) | **Intros:** ffmpeg `-ac 2 -f chromaprint -fp_format raw` with no `-algorithm`, so ffmpeg's default, algorithm 1 (TEST2), the same as ours; window min(25 % of the file, 10 min); ≤ 6 differing bits, 3.5 s maximum gap, inverted-index shift 2, 15–120 s; snaps to silence (−50 dB, 0.33 s) and chapters. **Credits:** credits chapters; a keyframe scan for black frames (85 % of the frame, threshold 28, normalised per episode); "card-like" keyframes (luma p90 − p10 ≤ 8, something ≥ 60 levels off it, saturation < 96) grouped into runs at least half cards; `LeadInProbe`, a 160 px-wide decode between two keyframes walked back while frames match, for a frame-exact start; the season's shared ending audio; candidates within 20 s merged. Credits at most 450 s (TV) or 900 s (movies), at least 15 s. | None published (README, issues searched) | GPL-3.0-only | Ideas only (C#) | `LeadInProbe` for problem 3. The card-like test aims at flat cards, not text over footage (M1: 0 of the missed cards pass). Issue #1008: a real match longer than the 120 s cap is dropped silently, which our own 120 s run cap shares (problem 4). | [repo](https://github.com/intro-skipper/intro-skipper), [config](https://github.com/intro-skipper/intro-skipper/blob/12.0/IntroSkipper/Configuration/PluginConfiguration.cs), [card test](https://github.com/intro-skipper/intro-skipper/blob/12.0/IntroSkipper/Analyzers/Credits/KeyframeVisualTraits.cs), [lead-in](https://github.com/intro-skipper/intro-skipper/blob/12.0/IntroSkipper/Analyzers/Credits/LeadInProbe.cs), [window](https://github.com/intro-skipper/intro-skipper/blob/12.0/IntroSkipper/Manager/SeasonResolver.cs), [#1008](https://github.com/intro-skipper/intro-skipper/issues/1008) |
| Intro Skipper, original (ConfusedPolarBear) | The same Chromaprint matcher before the fork. Archived. | None | GPL-3.0 | Ideas only | Nothing beyond the current fork. | [repo](https://github.com/ConfusedPolarBear/intro-skipper) |
| jellyfin-plugin-media-analyzer | Fork of the original. Intros "within the first 30% of an episode, or the first 15 minutes"; credits by black frames (under 4 min) and chapter names. Archived. | None | GPL-3.0 | Ideas only | Nothing new. | [repo](https://github.com/endrl/jellyfin-plugin-media-analyzer) |
| Jellyfin Media Segments, chapter-segments plugin | Storage for typed segments; the plugin turns chapters into segments. No detection. | n/a | GPL-3.0 (plugin) | n/a | None. | [docs](https://jellyfin.org/docs/general/server/metadata/media-segments/), [plugin](https://github.com/jellyfin/jellyfin-plugin-chapter-segments) |
| plex-credits-detect | Season-wide audio matching with SoundFingerprinting (5,512 Hz, 100–2,750 Hz band, stride 512) for credits and extra intros; optional video fingerprints; black frames for movies (75 % of the screen, pixels ≤ 2 %); silence. | None | MIT (C#) | Ideas only in practice (.NET) | Nothing new: we measured audio-matched credits at 54 % precision and rejected them (spec §5.3). | [repo](https://github.com/cjmanca/plex-credits-detect) |
| bw_plex | The show's theme song (from YouTube, tvtunes or Plex) fingerprinted and found in the first 10 minutes; black frames plus silence as the fallback; recaps from subtitles ("previously on"). | None | MIT | Ideas | **Match the show's own theme song** (Plex's show theme music; Jellyfin and Emby theme songs) anywhere in the episode: no window, no need for a sibling episode (problem 4). | [repo](https://github.com/Hellowlol/bw_plex) |
| Kodi Up Next (MoojMidge fork) | At playback, captured frames are image-hashed and compared with credit-layout templates and with other episodes' hashes at the same time; 5 s of matches in a row. | None | GPL-2.0 | Ideas only | None for us (runs during playback). | [detector](https://github.com/MoojMidge/service.upnext/blob/master/resources/lib/detector.py) |
| IntroHater (Stremio) | Looks up a community database, then AniSkip, then chapters. No detection. | n/a | Not stated | n/a | None. | [repo](https://github.com/introhaterapp/IntroHater) |
| TheIntroDB, IntroDB, SkipDB, AniSkip | Crowd-sourced times. | Ours: `../online/`, `../eval/aniskip-facts.md` | Various | Already used (AniSkip measured and not taken) | None new. | [TheIntroDB](https://theintrodb.org/) |

### Other open-source detectors

| Name | Method | Accuracy (published) | Licence | Reusable? | Idea for us | URL |
|---|---|---|---|---|---|---|
| recurring-content-detector (master's thesis) | Frames of a season's episodes as colour histograms or CNN features; content repeated across episodes, unsupervised: recaps, openings, closing credits, previews. | In the thesis (not read here) | MIT | Yes (Python, CPU) | **Video repeats** find an opening whose audio differs or sits outside the audio window (problem 4). | [repo](https://github.com/nielstenboom/recurring-content-detector) |
| butu-markers | Chromaprint pairs: a masked-hash vote picks candidate shifts, a neighbourhood around each is verified; a brute-force shift scan when the vote finds nothing ("a pilot whose title follows an 8-minute cold open"); ≤ 12 bits per hash, as the same music transcoded "differs by ~10-11 bits/hash" in its author's measurements; the longest match in a start cluster. | None | MIT or Apache-2.0 (Rust) | Ideas (Rust) | A wide shift fallback for cold opens and bumpers (problem 4). | [source](https://docs.rs/butu-markers/latest/src/butu_markers/detect.rs.html) |
| needle | Chromaprint across a season's episodes for openings and endings. | None | Custom ("Other" on GitHub) | Unclear | None new. | [repo](https://github.com/aksiksi/needle) |
| intro-fingerprint (mpv) | The user marks one intro; audio constellation hashes or a PDQ perceptual video hash find it in other episodes. | None | MIT | Ideas | A perceptual hash of the title card's last frame as a video check (problem 4). | [repo](https://github.com/jjangsangy/intro-fingerprint) |
| open-anime-timestamps | Dejavu fingerprints of each show's OP/ED songs (themes.moe) found in the episodes. | None | Not stated | n/a | Theme-song reference, as bw_plex (problem 4). | [repo](https://github.com/jonbarrow/open-anime-timestamps) |
| detect-show-episode-credits (hack day) | Frames at 1 fps scored by Tesseract keywords ("Produced By", "Director", "Dolby"), lines of text and a black background. | None | Not stated | n/a | Read the words (problem 2; M2). | [repo](https://github.com/yanglinz/detect-show-episode-credits) |
| closing-credits-recognizer | ResNet50 classifier, credits frame vs film frame (Keras). | None | MIT | Would need our own labels | A small learned classifier (problem 1). | [repo](https://github.com/parallel-places/closing-credits-recognizer) |
| end-credits-detection | ML model for the end-credits start; no method described. | None | Not stated | n/a | None. | [repo](https://github.com/thiago-franco/end-credits-detection) |
| Comskip | Commercial detection from cut points: black frame (1), logo (2), scene change (4), resolution change (8), closed captions (16), aspect ratio (32), silence (64), cut scene (128); blocks between cuts scored. | None | GPL-2.0 | Ideas only | Block scoring over several weak cues, not one detector (problems 1–3). | [source](https://github.com/erikkaashoek/Comskip/blob/master/comskip.c) |
| MythTV mythcommflag | The same family: blank frames, scene changes, logo. | None | GPL-2.0 | Ideas only | As Comskip. | [repo](https://github.com/MythTV/mythtv/tree/master/mythtv/programs/mythcommflag) |

### Streaming services and patents

| Name | Method | Accuracy (published) | Licence | Reusable? | Idea for us | URL |
|---|---|---|---|---|---|---|
| Netflix, 2015 | End sequence: "it appears at the end of the movie ... it almost always is comprised of text ... there is very little variation between contiguous frames". End of the title sequence: an image-histogram fingerprint of a reference frame found in the other episodes. | None | Blog | Ideas | Frame-to-frame stillness as a credits cue (problems 1, 3); a reference-frame match for intros (problem 4). | [TechBlog](https://netflixtechblog.com/extracting-contextual-information-from-video-assets-ee9da25b6008) |
| Netflix, 2023 (scene boundaries) | biGRU over pretrained shot embeddings; audio adds 10–15 %. Not about credits. | Internal benchmark only | Blog | No | None. | [TechBlog](https://netflixtechblog.com/detecting-scene-changes-in-audiovisual-content-77a61d3eaad6) |
| Amazon Prime Video, Hao et al., WACV 2021 | Intros and recaps only: per-second CNN visual + audio features, early fusion, BiLSTM + CRF; first 600 s of 46,946 titles (91.4 % TV). Their observations: title cards are "high-contrast (usually white) text" on black, a silent dark gap follows, "music usually plays without any speech when credits are showing". | Test set, both ends within 1 s: intro P 73.22 %, R 68.64 %, F1 70.85 %; recap F1 70.57 %; all metrics up more than 8 % at 3 s. | Paper; no code or weights | No | Music without speech as a credits cue (M3). | [paper](https://openaccess.thecvf.com/content/WACV2021/papers/Hao_Intro_and_Recap_Detection_for_Movies_and_TV_Series_WACV_2021_paper.pdf) |
| Amazon Rekognition segment detection | Cloud API: black frames, colour bars, opening and end credits, studio logos, shots. | None public | Commercial API | No (cloud) | None. | [docs](https://docs.aws.amazon.com/rekognition/latest/dg/segments.html) |
| Google patent US9723374B2 (lapsed) | Decode a set portion near the end, find scene transitions, OCR the frames, place the credits point from both. | n/a | Patent, expired (fee) | n/a | Confirms OCR + cuts is the obvious design. | [patent](https://patents.google.com/patent/US9723374B2/en) |
| Charter patent US12477174B2 | Several end-credits detectors (a CNN over black screen, text, channel-logo and subtitle position; black screen + text; AWS Rekognition; audio fingerprints) combined with weights chosen on a labelled set. | n/a | Patent, active | n/a | That is our `decide()`; weights tuned on our truth sets is the patent's own recipe. | [patent](https://patents.google.com/patent/US20250294208A1/en) |

### Papers

| Name | Method | Accuracy (published) | Licence | Reusable? | Idea for us | URL |
|---|---|---|---|---|---|---|
| Korolkov & Yanchenko, 2025 | 1 fps frames, CLIP image embeddings (512-d), 16-layer attention over 60 s windows labels each second intro/credits or film. 972 episodes, 27 hours; split by series. | Per second on the test set: F1 91.0 %, P 89.0 %, R 97.0 %, accuracy 94.3 %; 11.5 frames/s on CPU (ONNX). Stated weakness: "Overlaid credits ... the model sometimes misclassified these as part of the main content." | Paper CC BY 4.0; no code or weights | No (needs a CLIP encoder and training data) | Per-second scores over 1 fps frames, as M1 does with text boxes. Doesn't solve problem 1 by its own account. | [arXiv](https://arxiv.org/abs/2504.09738) |
| Berrani et al., ISM 2010 | Broadcast streams: sequences repeated on a stable schedule, clustered, checked against the EPG. | Closing credits found, precision 0.87 and 0.77 on two datasets | Paper | No | None (broadcast schedules). | [paper](https://inria.hal.science/inria-00545500/document) |
| Classic caption-text detection (Wolf & Jolion 2004) | Text "probability" image, geometry features, SVM. | See paper (not measured on credits) | Paper | Superseded by DB detectors like ours | None. | [report](https://liris.cnrs.fr/Documents/Liris-1920.pdf) |

### Building blocks

| Name | Method | Accuracy (published) | Licence | Reusable? | Idea for us | URL |
|---|---|---|---|---|---|---|
| PP-OCR recognition (PaddleOCR) | Reads a detected box. `ch_PP-OCRv4_rec` 10.9 MB (in the rapidocr 1.4.4 wheel our detector comes from; no spaces in English); `latin_PP-OCRv5_rec_mobile` 7.9 MB ONNX (spaces, punctuation, Latin-script languages). | Not on credits | Apache-2.0 | **Yes**: same ONNX Runtime and helper as the detector; only a few candidate cards per file | Problem 2 (M2). | [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR), [ONNX models](https://www.modelscope.cn/models/RapidAI/RapidOCR) |
| PP-OCRv5 mobile detector | Successor to our v4 detector, 4.7 MB of weights. | Not on credits | Apache-2.0 | Yes, after its own self-test on every vendor | Not measured here. | [HF](https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_det) |
| Silero VAD v5 | Speech probability per 32 ms, 2.3 MB ONNX. | See repo | MIT | **Yes** (CPU, 1 thread, audio only) | Where speech stops (M3). | [repo](https://github.com/snakers4/silero-vad) |
| inaSpeechSegmenter | Speech / music / noise segmentation (CNN). | See repo | MIT | Yes (CPU) | Music without speech (problem 1 guard). | [repo](https://github.com/ina-foss/inaSpeechSegmenter) |
| TransNetV2 | Shot-boundary CNN on 48×27 frames; TensorFlow and PyTorch inference (an ONNX export not checked). | See paper | MIT | Probably (CPU) | A cut right before a card (problem 3). Our 1 fps refine already walks to the fade. | [repo](https://github.com/soCzech/TransNetV2) |
| PySceneDetect | HSV-difference cut detection, adaptive threshold. | See site | BSD-3-Clause | Yes | As TransNetV2, cheaper. | [repo](https://github.com/Breakthrough/PySceneDetect) |
| ffmpeg filters | `blackdetect`, `blackframe`, `freezedetect`, `scdet`, `silencedetect`, `signalstats`; `-flags2 +export_mvs` for the decoder's motion vectors. | n/a | LGPL/GPL (as built) | Already in the image | A vertical-scroll check from motion vectors or `phaseCorrelate` for crawls (not needed for today's misses, M1). | [filters](https://ffmpeg.org/ffmpeg-filters.html) |
| MobileCLIP | Small CLIP image/text encoders. | See Apple's paper | Apple's own licence ("Other") | Licence needs a legal read | Only with a trained head (see Korolkov & Yanchenko). | [repo](https://github.com/apple/ml-mobileclip) |
| credit-scout | A cloud vision-language model (Gemini) reads sampled frames and labels logos, title cards and credit lists. | "Tested on 10 open-source films with 100% success rate" (self-reported) | MIT | No (cloud) | Reading the frame is what separates prose from credits (problem 2). | [repo](https://github.com/PatrickKalkman/credit-scout) |

## Ranked shortlist

By expected payoff on the open problems. Effort is rough: small = a day or two, medium = a lane like #324,
large = a new detector.

1. **Read the card the start lands on** — problem 2 (and #SKYKING, and the 205 set's epilogue starts). Evidence: M2,
   10 of 11 epilogue cards, 0 of 40 credit cards. From: the hack-day credits detector's keyword reading, credit-scout's
   vision-language reading, the Google patent's OCR; none of the on-device tools we found reads the words. Shape: when
   an answer starts on a dark card, grab that keyframe at full size, detect and read it; while it reads as prose, move
   the start to the next card. Model: `latin_PP-OCRv5_rec_mobile` (7.9 MB ONNX, Apache-2.0), run in the text helper
   like detection (the worker's GPU after a self-test, else the CPU; a few frames per file, so cheap either way), CTC
   decoding in a few lines of numpy. Portability: the period test works for any script that ends sentences with ".";
   the lower-case test only for cased scripts; a script the Latin model can't read gives no text, so the start stays
   where it is today. Effort: medium, and it must beat version 7 on the 80, the 205, Accused and I Survived like every
   rule J change.
2. **A 1 fps read of the stretch before the roll** — problems 1 and 3. Evidence: M1, the missed cards are boxed at
   320×180 on 48–65 % of lit frames at 1 fps; the walk fixed 2 of 4. From: Netflix 2015 and the CLIP paper (per-second
   scoring), Intro Skipper's frame-exact lead-in. Shape: after rule J's start, one 1 fps decode of up to ~120 s before
   it (as the refine windows do), frames with 2+ boxes joined over lit gaps of up to ~12 s, bounded by item 3. Needs
   the harness sets before any claim: 4 films and 16 controls is a feasibility check, not a measurement. '71 (one
   faint box a frame at 320×180) and 21 Jump Street (a long card-free montage) need more than this. Effort: medium.
3. **Speech as a guard** — makes 2 safe; a check on every credits start. Evidence: M3. Model: Silero VAD v5 (2.3 MB
   ONNX, MIT), CPU, audio of the tail at 16 kHz. Never a decider: shows put dialogue over credits (17 Again's vocals
   here; sitcom tags). Effort: small to medium.
4. **Search the first half of the episode for intros** (Plex's limit) — problem 4, Alias S02E09 (intro ends at
   905.9 s). Not measured here: it costs longer fingerprints for every episode (Chromaprint is CPU-only), and a longer
   window lets more mid-episode music match, so it needs the 118, the held-out 175 and the guards re-run. A cheaper
   shape: only widen when the season's other episodes place the intro near the edge. Effort: small to try, medium to
   prove.
5. **The show's theme song as a reference** (bw_plex, open-anime-timestamps) — problem 4: finds the theme anywhere in
   the file, with no window and no sibling needed; helps the first episode of a season and bumpers that outrank the
   theme. Plex has show theme music and Jellyfin and Emby have theme songs when the library has them; the theme isn't
   always the opening. Not measured.
   Effort: medium.
6. **Video repeats across episodes** (recurring-content-detector, Netflix 2015's reference frame, intro-fingerprint's
   PDQ hash) — problem 4: two openings in a season (quorum per visual cluster), openings whose audio differs.
   Effort: large.
7. **Log a match dropped for being longer than 120 s** (Intro Skipper #1008) — problem 4 diagnostics. Effort: small.
8. **Not recommended now:** CLIP-style classifiers (need our own labels and a model of unmeasured size; the paper's
   own failure is our problem 1), lower DB thresholds (M1: no gain), Intro Skipper's card test (M1: 0 of the missed
   cards pass), scroll or motion detection (today's misses aren't crawls), a cloud vision model (not local).

## Measurements

Scripts ran from the session scratchpad against `origin/dev` at `0e1ddb9` (the app's own
`markers/credits/textdet.py` for boxes), CPU only, `nice -n 19`, read-only on `/data*`; they weren't kept, so each
rule is written out here. Truth: the audit's frame checks (`../credits-accuracy/items.json`, `checks.json`) and the
harness sets' (`../credits-remaining/local/allsets_w6.json`); all local-only.

### M1. Problem 1: what our detector sees over the missed cards

**How.** 1 fps, `scale=640:360:flags=neighbor`, from 150 s before the first card to 10 s after our start; 320×180 is
every second pixel of that (close to, not identical with, the app's direct 320×180 scale). Per frame: boxes at
320×180 and 640×360 (the app's thresholds 0.3 / 0.5), boxes at 640×360 with lowered thresholds (0.2 / 0.35), Intro
Skipper's card-like test, and "static overlay" (a 640×360 box at the same place, IoU ≥ 0.5, one second later while the
rest of the picture changes by more than 6 levels). Lit = mean luma ≥ 30. Share of lit frames with the signal,
story (before the first card) / cards (first card to our start):

| Film | 320: ≥ 1 box | 320: ≥ 3 | 640: ≥ 1 | 640: ≥ 3 | 640 low: ≥ 1 | Card-like | Static overlay |
|---|---|---|---|---|---|---|---|
| '71 | 0.03 / 0.48 | 0.00 / 0.00 | 0.24 / 0.88 | 0.00 / 0.13 | 0.25 / 0.97 | 0 / 0 | 0.01 / 0.00 |
| 17 Again | 0.09 / 0.62 | 0.00 / 0.28 | 0.24 / 0.70 | 0.00 / 0.35 | 0.25 / 0.70 | 0.01 / 0 | 0.08 / 0.22 |
| 21 Jump Street | 0.12 / 0.55 | 0.00 / 0.23 | 0.40 / 0.69 | 0.01 / 0.27 | 0.43 / 0.72 | 0 / 0 | 0.09 / 0.31 |
| 14 Peaks | 0.01 / 0.65 | 0.00 / 0.22 | 0.32 / 0.89 | 0.03 / 0.49 | 0.38 / 0.89 | 0 / 0 | 0.05 / 0.03 |

- The cards are seen. At 320×180, 48–65 % of the lit card frames hold a box; the story before holds one on 1–12 %.
  At 640×360 the story holds boxes too (24–40 %: signs, the "THE END" card), so 640×360 needs 3 boxes to separate,
  which only 13–49 % of card frames reach.
- Lowered thresholds add 0–9 points on either side: no gain. Intro Skipper's card-like test passes none of the card
  frames (it wants a flat background). Static overlay is weak.
- **Walk back** from our start over 1 fps frames: keep going while frames hold the boxes asked for; a dark frame
  without text is neutral; stop after more than `gap` lit frames without them. 16 right answers (first card on black,
  1080p and below, from the harness sets) decoded the same way over the 120 s before their truth are the controls.

| Rule | '71 | 17 Again | 21 Jump Street | 14 Peaks | Controls moved > 5 s early |
|---|---|---|---|---|---|
| 320: ≥ 1 box, gap 5 s | +116 | +82 | +103 | +21 | 2 of 16 (The Sinner S03E03 −36, In Time −6) |
| **320: ≥ 2 boxes, gap 12 s** | +119 | **0** | +102 | **+3** | 1 of 16 (To Dye For −16) |
| 640: ≥ 1 box, gap 5 s | **+1** | +75 | +102 | −19 | 9 of 16 |
| 640: ≥ 2 boxes, gap 12 s | +116 | **0** | +102 | −5 | 5 of 16 |
| 320: ≥ 2 boxes, gap 12 s, speech guard (M3) | +119 | +14 | +102 | **+3** | **0 of 16** |

  To Dye For's truth is itself a prose card (M2), so its move may be from one epilogue card to another; not
  frame-checked. The per-second box strips show why the other two stay late: '71's names give one faint box a frame at
  320×180, and 21 Jump Street's cards sit between stretches of montage with no text for longer than any gap tried.

### M2. Problem 2: reading the card

**How.** The frame 1 s after the first boxed keyframe at or after the answer's start (A Beautiful Imperfection and
#SKYKING: hand-picked seconds), grabbed at 1920 px wide (`-ss` before
`-i`, bicubic), our detector at that size, then `latin_PP-OCRv5_rec_mobile.onnx` (RapidOCR's ONNX export) on each
box, lines kept at confidence ≥ 0.5. Prose = a line ending in "." or a line of 7+ words with at least half its words
starting lower-case. (The Chinese `ch_PP-OCRv4_rec` model in the rapidocr wheel reads English without spaces —
"LUCYJAMIESONWASBURIED…" — so word counts need the Latin one.)

| Cards | Read | Called prose |
|---|---|---|
| Known epilogue cards (Accused ×7, A Beautiful Imperfection ×2, #SKYKING ×2) | 11 | **10** (missed: Accused S03E09, only "Found guilty of Deliberate Homicide," read) |
| First credit cards on black of right answers (40 from the harness sets, A Beautiful Imperfection's first name card) | 41 | **1**: To Dye For, whose card reads "ON OCTOBER 7TH, 2023, GOVERNER NEWSOM SIGNED THE BILL AB 418, BANNING RED 3 IN CALIFORNIA." — prose, so its truth is on an epilogue |
| Early answers on a dark card from the 205 set and tv40 (unlabelled) | 25 of 31 (6 read no text) | 3: Breach, Trainwreck: The Astroworld Tragedy, Gandhari — all epilogue sentences. Missed: Lover Stalker Killer ("LIZ GOLYAR WAS SENTENCED TO LIFE IN PRISON / WITH NO CHANCE OF PAROLE", capitals, no period). Most of the other 21 read as credit cards ("Directed by", "Unit Production Manager", names), as the earlier frame checks found; the rest are a network promo, a memorial card and one unreadable |

- A dedication ("IN LOVING MEMORY OF / ERWIN OLAF / (1959 - 2023)") reads as not prose. Whether a dedication is
  credits is the owner's call.
- The box-level signals `../credits-remaining/README.md` tried caught 5–22 of 41 early starts and moved 3–28 of 192
  right ones (all sets); reading called none of 40 credit cards prose.
- **#SKYKING**, read second by second: epilogue sentences from 5232 to 5284 s ("On November 9, 2018, the FBI closed
  its investigation.", "The filmmakers reached out to Hannah…", "Horizon states that they did not…"), then
  "Directed and Produced by / PATRICIA E. GILLESPIE" at 5290 s.
- Cost on storage's loaded CPU (load ≈ 12–18), 2 threads: grab 0.25 s (1080p H.264), detection at 1920 px 1.65 s
  (0.27 s at 960 px), recognition of 5 lines 0.68 s.

### M3. Audio: where speech stops

**How.** Mono 16 kHz audio (the late films: M1's windows; right answers: 60 s before to 30 s after the truth),
Silero VAD v5 ONNX on 512-sample chunks, a second counted as speech
when at least 30 % of its chunks score above 0.5.

- 25 right answers (first card on black): no speech in the 30 s after the first card on 22 (1, 2 and 11 s on the
  others); speech in the minute before on 20, ending 1–60 s before the first card (median 27 s).
- The late films: speech between the real first card and our start 0 s ('71, 21 Jump Street), 1 s (14 Peaks), 9 s
  (17 Again, 5735–5748 s, not checked whether dialogue or a song).
- As M1's guard (the walk may not cross the last speech before our start) it removed every control's move and cost
  17 Again 14 s.

## What others found works badly (and we do)

- **A hard cap on intro length.** An Intro Skipper user found a clean ~126 s shared run (popcount 0–5, no gaps)
  silently dropped by its 120 s cap (#1008, open). Our matcher also leaves out runs over 120 s
  (`markers/audio/matcher.py`); a long theme, or a theme merged with a shared bumper, disappears the same way.
- **A fixed intro window.** Everyone else looks less far than we do (Intro Skipper min(25 %, 10 min), Emby 10 min,
  the WACV model 600 s) except Plex (half the episode). None of them reaches Alias S02E09 but Plex.
- **Credits from audio matching** (plex-credits-detect, Intro Skipper's season chromaprint of the ending): we measured
  54 % precision and rejected it (spec §5.3); nobody publishes a number for theirs.
- **Credits from black frames alone** (Intro Skipper, plex-credits-detect, media-analyzer): miss credits over footage
  by construction; Intro Skipper adds flat-card detection and Plex text detection on top. Our text-first design
  is what Plex and both patents describe.
- **Learned frame classifiers**: the CLIP paper's stated failure is credits overlaid on footage (our problem 1), and
  Amazon's intro model reaches 70.85 % F1 at 1 s. Neither is a shortcut for us.
- Nothing found says text detection or Chromaprint algorithm 1 works badly: Plex detects text, and Intro Skipper
  uses the same algorithm and scans keyframes as we do.

## Leads that didn't check out

Found during the search, checked against the source, and wrong:

- Plex's intro detection "uses SoundFingerprinting": the Hackaday article such claims point to is about a hobbyist's
  script, not Plex ([Hackaday](https://hackaday.com/2020/11/25/audio-fingerprinting-skips-a-shows-intro-reliably/)).
  Plex says only "primarily ... the audio".
- US12477174B2 is Charter Communications', not Amazon's; US9723374B2 is Google's and has lapsed.
- Intro Skipper doesn't use Chromaprint algorithm 2: it passes no `-algorithm`, and ffmpeg's default is
  `CHROMAPRINT_ALGORITHM_DEFAULT` = TEST2 = 1
  ([ffmpeg](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/chromaprint.c),
  [chromaprint.h](https://github.com/acoustid/chromaprint/blob/master/src/chromaprint.h)).
- No accuracy figure for Emby's intro detection or for Intro Skipper traces to a primary source.
- No Netflix, Disney, Hulu or Max write-up on how they place intro or credits markers today was found; the Netflix
  2015 post is the only primary one.
