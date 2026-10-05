# Loudness runs on CPU workers only: GPU workers never pick up its items
Status: approved (on hold: loudnorm speed work first)
Issue: diblaze/media_preview_generator#4 (upstream-bound later, no PR without approval)
Base: upstream/dev d70bcbe (all cited lines are identical on upstream/dev and the working tree unless noted)

## Goal
A job kind can declare `cpu_only`; the dispatcher never hands such an item to a GPU worker; loudness declares it, so with CPU Workers = N at most N loudnorm processes run and GPU slots stay free for previews and Intro & Credits; a loudness job started with no CPU workers fails at once with a message naming the setting.

## Evidence
- E1 `media_preview_generator/job_kinds.py:87-101` — `class KindHandlers: check_fn / process_fn / outcome_keys / check_label / check_worker_label / check_share: float = 1.0 / pickup_fn` — the per-kind contract; `check_share` is the precedent for a per-kind dispatcher knob read off `tracker.handlers`.
- E2 `media_preview_generator/jobs/dispatcher.py:995-1006` — `if tracker.handlers is None or tracker.handlers.check_share >= 1: return None` — how the dispatcher reads a kind's knob.
- E3 `dispatcher.py:867-879` — `worker = self.worker_pool._find_available_worker(claim=True)` … `picked = self._get_next_item()` … `if not picked: worker.is_busy = False; break` — the worker is claimed first (any type), then the item is picked with no knowledge of the worker type.
- E4 `dispatcher.py:1203-1213` — `_get_next_item`: `eligible = [t for t in self._trackers.values() if not t.done_event.is_set() and not t.is_paused() and not t.is_cancelled() and t.item_queue]` … `eligible.sort(key=lambda t: (t.priority, t.submission_order))` … `item = tracker.item_queue.popleft()` — priority then submission order; multiple jobs do share the pool concurrently (drain-first).
- E5 `jobs/worker.py:1370-1393` — `def _find_available_worker(self, cpu_only: bool = False, *, claim: bool = False)` … `if cpu_only and worker.worker_type != "CPU": continue` — a CPU-only claim already exists; no caller passes `cpu_only=True` today (`rg cpu_only media_preview_generator` finds only this definition).
- E6 `worker.py:1042-1043` — `self.add_workers("GPU", gpu_workers)` then `self.add_workers("CPU", cpu_workers)`; `worker.py:994-1009` `find_available`: "GPU workers are prioritized (they come first in the array)" — so with E3 the first free worker is a GPU worker whenever one is idle, and a plain `break` after a failed pick would leave idle CPU workers unused.
- E7 `worker.py:816-830` — `_process_custom_item._run`: `self.process_fn(item, gpu=gpu, gpu_device_path=gpu_device, …, gpu_worker=self.worker_type == "GPU", worker_name=self.display_name)` — the only way a kind learns which worker type runs it; `loudness/job.py:206-215` `process_item(... **_ignored)`: "Worker stage: analyse each stream Plex lacks loudness for and write it. A GPU worker runs it on the CPU too." — loudness ignores `gpu`, so today a GPU worker spends its slot on a CPU-bound ffmpeg.
- E8 `loudness/job.py:357-364` — `return KindHandlers(check_fn=…, process_fn=…, outcome_keys=OUTCOME_KEYS, check_label="Checking Plex loudness…", check_worker_label="Loudness", check_share=CHECK_SHARE)` — where loudness declares itself.
- E9 `loudness/job.py:544-546` — `detected_gpus = _ensure_gpu_cache()` / `dispatcher = get_or_create_dispatcher(config, _build_selected_gpus(settings, detected=detected_gpus))` / `jm.set_active_worker_pool(job_id, dispatcher.worker_pool)`; `dispatcher.py:1435-1438` — `WorkerPool(gpu_workers=… if selected else 0, cpu_workers=int(getattr(config, "cpu_threads", 0) or 0), …)` — the pool is sized from CPU Workers and exists before `submit_items`.
- E10 `loudness/job.py:461-462` — `if registry is None: raise RuntimeError("Couldn't load the media servers configuration")`; `:600-610` — `except Exception as exc: detail = redact_secrets(f"{type(exc).__name__}: {exc}") … jm.complete_job(job_id, error=detail); if chain_head: _finish_chain(…)` — the existing "refuse to start" path; a raise before `submit_items` fails the job (and its retry chain) with the message and releases the slot. `tests/loudness/test_runner.py:224-228` tests exactly this shape.
- E11 `worker.py:1063` — `def _snapshot_workers(self) -> list["Worker"]` (copy under the pool lock); `worker.py:1352` — `cpu_count = sum(1 for w in self.workers if w.worker_type == "CPU")` — counting CPU workers is an inline one-liner elsewhere too.
- E12 `markers/pipeline.py:4155-4177` — "Worker stage: the same steps plus local detectors on the worker's GPU/CPU … gpu: The worker's GPU type, None on a CPU worker" and `:3462` `on_gpu = gpu is not None` — Intro & Credits decodes on the GPU; it is not CPU-only. No other kind exists (`job_kinds.py:15-18` `JOB_KINDS = (previews, intro_credits, loudness)`), so loudness is the only declarer.
- E13 `tests/test_job_kinds_engine.py:400-427` — a test drives `dispatcher._assign_tasks()` directly on a hand-built `JobTracker` with `tracker.item_queue.extend(...)`; `tests/test_job_dispatcher.py:57-58` — `_make_gpu_list(n)` = `[("nvidia", f"/dev/nvidia{i}", {"name": …})]`, the `selected_gpus` shape for a pool with GPU workers. `tests/test_priority.py:269` — `dispatcher._get_next_item()` is called with no arguments (the new parameter needs a default).
- E14 `tests/loudness/test_runner.py:56-58, 68` — the `run` fixture's `dispatcher = MagicMock()` is returned by the patched `get_or_create_dispatcher`; iterating a MagicMock yields nothing, so a CPU-worker check in `run_loudness_job` needs the fixture to say the pool has a CPU worker, or every runner test fails.
- E15 Docs/UI text to update: `docs/plex-loudness-normalization.md:30-31` (upstream/dev) — "Decoding audio gains nothing from a GPU, so the analysis runs on the CPU even on a GPU worker."; `docs/reference.md:849` — "Same `Job` row, queue, priorities, pause and cancel as the other kinds"; `docs/reference.md:131` — "| `cpu_threads` | Yes | `1` | Number of CPU worker threads (0–32) |"; `web/templates/settings.html:209` — `<div class="form-text">Parallel preview jobs to run on the CPU.</div>`; `web/templates/setup.html:391` — `<div class="form-text">Parallel preview jobs on the CPU.</div>`. `docs/llms-full.txt:1420` is generated from the docs page (`scripts/generate_llms_full.py:21-23` "`--check` exit 1 if the committed file is stale"; `tests/test_llms_full.py` drift test).
- E16 Live observation (task): CPU Workers 8 + 4 GPU workers on one iGPU gave 12 parallel loudnorm processes; the working tree's `feat/fast-loudnorm-ffmpeg` branch is independent (it touches `loudness/analyze.py` only) and is not part of this change.

## Changes
- `media_preview_generator/job_kinds.py` — `KindHandlers` gains `cpu_only: bool = False` after `check_share`, with a two-line comment: the kind's `process_fn` only ever runs on the CPU, so the dispatcher gives its items to CPU workers only and GPU workers stay free for kinds that decode; such a kind needs CPU Workers > 0 (E1, E2).
- `media_preview_generator/jobs/dispatcher.py:1191-1213` — `_get_next_item(self, cpu_worker: bool = True)`; the eligibility filter adds `and (cpu_worker or t.handlers is None or not t.handlers.cpu_only)`; docstring: a GPU worker passes over CPU-only kinds and takes the next job's item (E4, E13 default keeps `test_priority.py:269`).
- `media_preview_generator/jobs/dispatcher.py:867-880` — `_assign_tasks` becomes worker-type aware with the existing claim filter (E5), no new pool API:
  ```python
  cpu_only = False
  while True:
      worker = self.worker_pool._find_available_worker(cpu_only=cpu_only, claim=True)
      if not worker:
          break
      picked = self._get_next_item(cpu_worker=worker.worker_type == "CPU")
      if not picked:
          worker.is_busy = False
          if cpu_only or worker.worker_type == "CPU":
              break
          # GPU workers have nothing they may take; a CPU-only kind's items may still wait for a CPU worker.
          cpu_only = True
          continue
  ```
  Why this shape: GPU workers sit first in the pool (E6), so after a GPU worker finds nothing it may take the loop must go on to CPU workers instead of `break`ing; a CPU worker may take anything, so a failed pick there means the queues are empty (E3, E4). Priority semantics are unchanged: CPU workers still drain higher-priority preview items before a lower-priority loudness job.
- `media_preview_generator/loudness/job.py:357-364` — `kind_handlers` passes `cpu_only=True` (E8); `:215` docstring drops "A GPU worker runs it on the CPU too." for "CPU workers only (`KindHandlers.cpu_only`)." (E7).
- `media_preview_generator/loudness/job.py:545` — right after `get_or_create_dispatcher(...)`: `if not any(w.worker_type == "CPU" for w in dispatcher.worker_pool._snapshot_workers()): raise RuntimeError("Plex loudness runs on CPU workers only and CPU Workers is 0; set it above 0 in Settings, Processing Options")` (E9, E11). Smallest correct no-hang option: it reuses the existing failure path (E10), so the job, its retry chain and automatic follow-ups fail immediately with the fix spelled out instead of sitting at "0/N" with every GPU worker idle. Falling back to GPU workers was rejected: it is the behaviour being removed. A check at `create_loudness_job` time was rejected: the live pool, not the saved setting, is the truth (workers can be added or removed live).
- `media_preview_generator/web/templates/settings.html:209` — form text: "Parallel files on the CPU. Plex loudness analysis runs here only." `setup.html:391` — same sentence (E15).
- `docs/plex-loudness-normalization.md:30-31` — replace the clause with: "Decoding audio gains nothing from a GPU, so only CPU workers run it; GPU workers stay on previews and Intro & Credits. It needs **CPU Workers** above 0 in Settings, Processing Options; a job started with none fails and says so." (E15; no em dashes).
- `docs/reference.md:849` — append "Its files go to CPU workers only (CPU Workers must be above 0)." `docs/reference.md:131` — append "Plex loudness runs on these only." (E15).
- `docs/llms-full.txt` — regenerate with `.venv/bin/python scripts/generate_llms_full.py` (E15).
- `tests/test_job_kinds_engine.py` — two tests next to `:400-427` (E13):
  1. `test_a_cpu_only_kind_never_runs_on_a_gpu_worker_and_the_gpu_worker_takes_the_next_job`: `WorkerPool(gpu_workers=1, cpu_workers=1, selected_gpus=_make_gpu_list(1))`; job A (`cpu_only=True`, priority 1, 3 items, `check_fn` returns None, `process_fn` records `kwargs["gpu_worker"]` and `worker_name`) and job B (default handlers, priority 2, 1 item); after both trackers wait: every A call saw `gpu_worker is False`, and B's call saw `gpu_worker is True`.
  2. `test_a_cpu_only_item_stays_queued_when_the_pool_has_no_cpu_worker`: `WorkerPool(gpu_workers=1, cpu_workers=0, …)`, hand-built tracker with `handlers.cpu_only=True` and one queued item; `dispatcher._assign_tasks()`; assert the item is still in `tracker.item_queue`, `process` not called and `pool.has_busy_workers()` is False (the GPU worker was released, not left claimed).
- `tests/loudness/test_runner.py` — fixture `:56-58`: `dispatcher.worker_pool._snapshot_workers.return_value = [SimpleNamespace(worker_type="CPU")]` (E14); new `test_a_job_with_no_cpu_workers_fails_naming_the_setting`: set that return value to `[SimpleNamespace(worker_type="GPU")]`, run, assert `"CPU Workers" in run["jm"].complete_job.call_args.kwargs["error"]`, `run["gate"].release.assert_called_once()` and `"submitted" not in run`.
- `tests/loudness/test_job.py:150-167` — unchanged (`cpu_workers=1, gpu_workers=0` already exercises the CPU path).

## Check
```
cd /home/denis/git/media_preview_generator && .venv/bin/python -m pytest --no-cov -n 0 tests/test_job_kinds_engine.py tests/loudness/test_runner.py tests/loudness/test_job.py tests/test_job_dispatcher.py tests/test_priority.py tests/test_dispatcher_checking_stage.py && .venv/bin/python scripts/generate_llms_full.py --check && .venv/bin/ruff check && .venv/bin/ruff format --check
```
Live assert after deploy: with CPU Workers = 2 and GPU workers > 0, a loudness job over many files shows at most 2 `loudnorm` ffmpeg processes (`docker exec <ctr> pgrep -fc loudnorm` <= 2) and the GPU worker cards never show a loudness title.

## Deferred (YAGNI)
- Failing or pausing a loudness job whose last CPU worker is removed live mid-run (items would wait in `item_queue` with the job still "running"; cancellable) — reopen when someone removes CPU workers to 0 during a loudness run and reports the stuck job; the fix is one more guard in `_assign_tasks` draining CPU-only queues through `_fail_unstarted_item`-style accounting.
- Running the check stage first and failing only files that need a worker when CPU Workers = 0 — reopen if up-to-date-only follow-ups failing on a GPU-only box becomes a nuisance.
- A per-kind config setting or a dedicated loudness worker count — reopen only if CPU Workers proves the wrong cap for loudness (it is the cap the user asked for, E16).
- Marking Intro & Credits text detection or chromaprint as CPU-only — not applicable; the kind decodes on the GPU (E12).

## Open questions
- None that change the Changes section. (Message wording and docs sentences are editorial.)
