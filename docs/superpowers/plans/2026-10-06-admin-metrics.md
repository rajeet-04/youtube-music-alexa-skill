# First-party Admin Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for native execution or superpowers:subagent-driven-development if explicitly selected by the user. Do not dispatch agents without that selection. Steps use checkbox syntax for tracking.

**Goal:** Make the authenticated compact admin dashboard answer operational questions about JUKES preparation, cache, warmup, jobs and local resources.

**Architecture:** Instrument semantic transitions in existing services, not internal probes. Persist aggregate counters, bounded observations and lifecycle deduplication in SQLite; expose only aggregate snapshots through the existing admin status interface. Resource sampling uses local Linux interfaces and cached deltas.

**Tech Stack:** Python 3.12, Flask, SQLite, existing pytest environment; vanilla browser JavaScript and CSS. Use uv and Bun where dependencies are needed; add no production dependency or monitoring stack.

**Spec:** `docs/superpowers/specs/2026-10-06-admin-metrics-design.md`.

## Global constraints

- First-party JUKES internal pipeline only; no provider/source comparisons.
- Retain minute aggregates and latency samples for 24 hours; at most 10,000 latency samples per category; lifetime counters survive restart.
- P99 requires at least 100 retained successful samples. Missing values are unavailable, not zero.
- Requested pool defaults to 10,000,000,000 bytes; warmup to 1,000,000,000 bytes with 7,200-second TTL. Show actual configuration.
- Eviction/expiration is distinct from preparation/download failure.
- No metrics from polls, HEAD, metadata-only requests, admin reads or internal probes.
- Metrics remain authenticated and no-store. Never store/export credentials, headers, titles, raw exceptions, paths, IPs or video IDs in metrics.
- Resource samples are cached for at least five seconds; browser refresh interval is 10 seconds, visible-only, no overlapping requests.
- No deployment or unrelated credential/provider changes.

## Review focus

1. A stale job must not invalidate a newly published result for the same key (Task 2).
2. Duplicate requests and range reads must not multiply warmup consumption or latency observations (Tasks 3–4).
3. Restart between publication and instrumentation must not duplicate completions (Tasks 1–3).
4. Recent warmup cohorts must not report effectiveness using unrelated consumption events (Task 4).
5. Counter resets, inaccessible cgroups and session expiry must produce honest unavailable/stale UI values (Tasks 5–6).

## File responsibilities

- Create `flask-server/jukes/metrics.py`: persistent aggregate collector and bounded preparation/cohort bookkeeping.
- Create `flask-server/jukes/resources.py`: cached local resource sampler.
- Modify `store.py`: additive metrics schema and safe job-state migration.
- Modify `jobs.py`, `cache.py`, `extractor.py`, `routes.py`, `app.py`: semantic hooks, lifecycle wiring and evicted compatibility.
- Modify `admin.py`, `templates/admin.html`; create `static/admin-metrics.js`: authenticated snapshot and compact presentation.
- Create focused metrics/resource tests; extend existing jobs/cache/API/admin tests.
- Update `docs/JUKES_API.md`, `README.md`: definitions, coverage and API compatibility.

### Task 1: Persistent bounded metrics collector

**Files:** create `jukes/metrics.py`, `tests/test_jukes_metrics.py`; modify `jukes/store.py` (all paths under `flask-server/`).

**Interfaces:** `Metrics(store, clock=time.time, sample_limit=10000)`;
`record(name: str, *, category: str = "all", duration_seconds: float | None = None, once: str | None = None, connection=None) -> bool`;
`snapshot(now: float | None = None) -> dict`; `prune(now: float | None = None) -> None`.
Allowlist names/categories; reject unsupported dimensions. Snapshot contains lifetime counters, collection start and `windows` keyed `15m`, `1h`, `24h`, each with counters, minute trends and latency categories.

- [ ] Write `test_metrics_windows_and_percentiles`: record durations 1..100; assert average 50.5, P50=50, P95=95, P99=99, sample_count=100; assert P99 absent at 99 samples.
- [ ] Write `test_once_counter_restart_and_pruning`: repeat one completion marker, reopen collector, assert count=1; advance 86401 seconds, prune, assert lifetime=1 and rolling count=0.
- [ ] Write `test_bounded_samples_and_allowlist`: inject limit=3, record five samples, assert retained=3 and sampled flag; unsupported name/category and secret-bearing fields cannot enter snapshot.
- [ ] Run `.venv/bin/python -m pytest tests/test_jukes_metrics.py -q` in `flask-server`; verify missing behavior fails.
- [ ] Implement prefixed tables for counters, minute buckets, samples and deduplication; use caller transactions when supplied. Prune old dedup records only when their owning lifecycle can no longer emit events.
- [ ] Run focused tests to passing and `git diff --check`; commit `feat(metrics): persist bounded operational counters and latency samples`.

### Task 2: Evicted lifecycle and accurate job gauges

**Files:** modify `jukes/{store,jobs,cache,routes}.py`; extend `tests/test_jukes_{jobs,cache,api}.py`.

**Interfaces:** `Jobs.stats() -> dict` adds evicted, active_workers, queue_utilization, worker_utilization while preserving existing fields. Job terminal statuses include evicted. `Cache` removal emits bounded reason and result identity using a callback invoked outside the cache lock; Jobs reconciles by persisted result generation, never just the key.

- [ ] Write `test_evicted_job_is_not_failed`: complete a job, remove its result, inspect job/stats, assert evicted=1, failed=0 and no audio URL.
- [ ] Write `test_old_result_eviction_preserves_new_job` and `test_failed_unlink_does_not_evict_job`; assert a newer generation remains ready and protected/failed removals emit nothing.
- [ ] Write migration test using old status CHECK and cache_evicted row; assert conversion preserves unrelated tables and indexes and migration is repeatable.
- [ ] Write long-poll/prune tests: evicted wakes waiters, counts as terminal, fresh submit starts work, and retained rows age out after 86400 seconds.
- [ ] Run focused tests for failure; implement transactional table migration, bounded eviction reasons, generation-safe cache notification and live gauges. Avoid cache-lock/coordinator-lock inversion; reconcile persisted rows at stats/get boundaries.
- [ ] Verify retained job gauges after restart and queue/worker saturation; run jobs/cache/API tests, commit `fix(jobs): distinguish evicted results from failed preparations`.

### Task 3: Job performance, retries and request latency

**Files:** modify `jukes/{app,jobs,extractor,routes,metrics}.py`; extend metrics/jobs/API/extractor tests.

**Interfaces:** shared collector wired before worker recovery/start. `begin_preparation(started_at: float, *, requested: bool) -> str`; `attach_preparation(observation_id: str, job_id: str, category: str) -> None`; `finish_preparation(observation_id: str, *, success: bool, duration_seconds: float, retryable: bool = False) -> None`. Cap pending observations at 10,000 and expose dropped-latency-observation count.

- [ ] Write cached/cold/joined request tests with fake clocks: metadata resolution is included, polls and HEAD add no samples, each accepted prepare records one terminal outcome, async attachment completes with its job.
- [ ] Write failure/admission tests: invalid request excluded; queue rejection separate; temporary and terminal failures follow shared bounded classification; eviction never increments failed.
- [ ] Write duplicate job-completion/recovery tests; assert completed extraction counts once, cached ready wrappers count zero extraction completions, recovered elapsed time includes downtime and is labelled recovered.
- [ ] Write retry tests asserting actual fallback and recovery attempts only, with unavailable transport-retry coverage rather than invented counts. Export no extractor-client labels.
- [ ] Run focused tests for failure; instrument routes before resolution and jobs at transitions; implement bounded persisted pending observations, durable elapsed durations, queue/extraction timings and completion deduplication.
- [ ] Run metrics/jobs/API/extractor suites; commit `feat(metrics): measure first-party preparation latency and outcomes`.

### Task 4: Cache effectiveness and warmup cohorts

**Files:** modify `jukes/{metrics,cache,jobs,routes}.py`; extend metrics/cache/jobs/API tests.

**Interfaces:** `record_cache_resolution(observation_id: str, *, pool: str | None, joined: bool) -> None`;
`warmup_completed(result_id: str, completed_at: float, *, consumed_inflight: bool) -> None`;
`consume_warmup(result_id: str, *, inflight: bool) -> bool`.
Coherent result identity survives promotion and is independent of ready wrapper job IDs. Cohorts retained through TTL+24 hours; lifetime unique consumption remains bounded by lifetime successful speculative completions.

- [ ] Write `test_audio_cache_observation_excludes_internal_probes`: one prepare hit/miss, unlimited lookups/polls/HEAD unchanged; main and warmup hits split, joined preparation is miss plus join.
- [ ] Write unique warmup request/job/success tests: duplicates and already-cached warmups separate; promoted job preserves speculative origin; first consumption counts once despite repeat/range reads.
- [ ] Write cohort tests: older completion consumed in current window does not inflate current cohort usefulness; in-flight promotions separate; recent cohorts labelled maturing; zero denominator unavailable.
- [ ] Write eviction/expiry tests: configured TTL=7200, actual successful removal records one bounded reason, failed deletion records zero. Verify reservations shown separately and SQL average age by pool.
- [ ] Run focused tests for failure; implement cache snapshots and transactional cohort/result bookkeeping; never instrument `lookup` indiscriminately.
- [ ] Run metrics/cache/jobs/API suites; commit `feat(metrics): track cache effectiveness and speculative warmup consumption`.

### Task 5: Lightweight local resource sampling

**Files:** create `jukes/resources.py`, `tests/test_jukes_resources.py`; modify `jukes/app.py`.

**Interfaces:** `ResourceSampler(audio_dir: Path, *, clock=time.monotonic, read_text=None, disk_usage=shutil.disk_usage)`; `snapshot() -> dict`. CPU percentage relative to effective quota, capacity/process memory, cache-filesystem disk and non-loopback namespace RX/TX bytes and rates each include scope/availability.

- [ ] Write injected-fixture tests: first CPU/network rate unavailable, second deltas correct, reads within five seconds reuse snapshot, clock/counter reset gives unavailable rather than negative rates.
- [ ] Write cgroup v1/v2/unlimited quota tests; assert effective memory/CPU limits and explicit host/container labels; inaccessible files degrade independently.
- [ ] Run resource tests for failure; implement local reads with no subprocess/upstream call or blocking sleep, bounded refresh and thread-safe snapshot.
- [ ] Verify resource suite, commit `feat(admin): sample local capacity without monitoring dependencies`.

### Task 6: Authenticated compact dashboard and handoff

**Files:** modify `jukes/admin.py`, `templates/admin.html`, `docs/JUKES_API.md`, `README.md`; create `flask-server/static/admin-metrics.js`, `flask-server/tests/test_admin_metrics_ui.js`; extend admin/integration tests.

**Interfaces:** existing status keys retained; `metrics` includes `source: "jukes"`, coverage, lifetime, windows, current cache/job gauges and resources. HTML/JSON share a snapshot. Authenticated static script contains no credentials; dynamic values use textContent.

- [ ] Write admin tests: unauthorized status denied, no-store preserved, authenticated schema contains requested metrics only, secrets/header sentinel values absent, existing forms/browser controls remain.
- [ ] Write UI behavior tests with injected DOM/fetch/timers: 10-second visible-only refresh, no overlapping requests, selected window preserved, 401 stops refresh, transient errors mark stale, zero denominators and insufficient samples render unavailable.
- [ ] Run targeted tests for failure; render compact sections for job/source performance, cache, warmup, resources and rolling trends with units/sample counts. Preserve existing controls and use responsive layouts without adding chart dependencies.
- [ ] Document metric definitions, coverage start, bounded samples, recovery latency, rate/cohort denominators and additive evicted status; remove obsolete contract statements that list only four states.
- [ ] Run `.venv/bin/python -m pytest tests ../browser-auth/tests -q` from `flask-server`, `bun flask-server/tests/test_admin_metrics_ui.js` from repository root, and `git diff --check`.
- [ ] Inspect a local authenticated page and resource snapshot where browser tooling permits, using test-only secrets; never expose real credentials. Record actual limitations.
- [ ] Review all spec requirements against final code/tests; commit `feat(admin): show first-party operational metrics and rolling trends`. No push or deployment unless separately authorized for this feature.

## Self-review and execution handoff

Coverage: collection/retention Task 1; terminal semantics/gauges Task 2;
latency/retry/failure Task 3; cache/warmup Task 4; resources Task 5;
security/UI/docs/integration Task 6. Review-focus cases have explicit tests.
Use native execution unless the user explicitly selects agents. Native means
implementing sequentially in this session with an end-to-end review; any fresh
reviewer agent still requires explicit delegation authorization. Present this
plan for review and execution-method selection before product changes.
