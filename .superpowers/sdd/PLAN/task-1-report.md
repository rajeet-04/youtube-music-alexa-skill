# Task 1 implementation report: persistent two-pool cache

Status: implemented on `feat/jukes-backend`. The Task 1 code is limited to
`flask-server/jukes/{__init__,config,models,store,cache}.py` and
`flask-server/tests/test_jukes_cache.py`.

## Interfaces introduced

- `AudioKey(video_id: str, policy: str)` is the shared cache identity. File names
  derive from a hash of both fields, so IDs and policy strings never become paths.
- `CacheConfig` defaults to the approved decimal pool sizes, 7,200 second warmup
  expiry, 2,000,000,000 byte free-disk reserve, 16,000,000 byte initial unknown
  reservation, and 16,000,000 byte reservation increments. It accepts explicit
  database and audio paths for tests and deployments, normalizes paths, and rejects
  invalid limits.
- `Store(database_path)` opens a new SQLite connection per operation. Use
  `connection()` for read operations and `transaction()` for `BEGIN IMMEDIATE`
  writes. `get_audio`, `all_audio`, `get_reservation`, `all_reservations`, and
  `all_leases` return detached dictionaries. `begin_publication` journals a
  same-filesystem publication and `finalize_publication` atomically creates the
  ready row and removes its journal.
- `Cache(config, clock=time.time, store=None, media_validator=None)` provides
  `lookup(key)`, `complete(key, path, requested)`, `promote(key)`,
  `lease(key)`, `prune(now)`, and `mark_used(key)`. `lookup` is read-only for LRU;
  accepted use is recorded by `mark_used`, `complete` for requested audio,
  promotion, or lease acquisition.
- `reserve(key, requested, expected_size=None)` returns a `CacheReservation` with
  `.path`, `.reserved_bytes`, and `.write(bytes)`. Writes extend pool and disk
  reservations before appending. `promote(key)` or a repeated
  `reserve(key, requested=True)` moves an in-flight warmup reservation into the
  requested budget without copying its partial bytes. On extractor failure,
  `release_reservation(key) -> bool` removes the partial and releases its budgets.
  On success, pass `reservation.path` to `complete`.
- `CacheCapacityError.code` is `track_too_large` for an oversized requested
  track, `warmup_too_large` for an oversized warmup, and `cache_capacity` when
  pool or free-disk capacity is pinned or reserved.
- `CacheEntry` carries `key`, `path`, `pool`, `size_bytes`, `expires_at`, and
  `last_used`. `PruneResult` carries removed entry and byte counts. A missing
  lease target yields `None` inside the lease context.

## Lifecycle and persistence

The store creates only `jukes_` tables, leaving legacy tables and records intact.
It persists audio metadata, reservations, publication journals, and reader leases.
Reservations include owner PID and Linux process start ticks: another Cache object
in the same live process preserves in-flight work and leases, while a dead owner is
reconciled once by cleaning its partial and leaving an `interrupted` reservation
that a later `reserve` call can replace.

One module-wide reentrant lock serializes file and metadata transitions within the
service process. Pool use counts ready files and pending reservations. Unknown-size
work starts with the configured bounded reservation and extends in fixed increments
before accepting bytes. Admission reclaims expired warmups first, then LRU entries
as needed, and active leases pin files. Warmup TTL is assigned at completion and
duplicates preserve it. Promotion only changes metadata, so the same final file is
reused. Publication records `publishing`, atomically renames on the audio filesystem,
then finalizes in SQLite; startup recovery completes a valid rename or removes a
failed publication and its staged source.

The default media validator accepts nonempty files. The extractor integration should
perform its media check before publication or inject its validator into `Cache`;
Task 1 intentionally has no ffprobe or YouTube dependency. `cache.py` is relatively
large because it owns reservations, admission accounting, leases, atomic publication,
and restart recovery together under one coordinator lock, as required by the approved
lifecycle design.

## RED/GREEN evidence

The initial focused run against the missing behavior produced 13 failures. Later
edge-case tests were also observed failing before their fixes: live-process
reservation preservation, failed-job release, reservation reuse without shrinking,
pre-rename recovery cleanup, invalid config rejection, and disk accounting during
in-flight promotion. The focused suite now passes:

```text
/tmp/jukes-backend-venv/bin/python -m pytest flask-server/tests/test_jukes_cache.py -q
20 passed in 1.12s
```

The tests use a fake clock, small configured budgets, real temporary files and real
SQLite databases. They cover no-TTL requested audio, warmup expiry and duplicate
behavior, independent pools, promotion and file reuse, LRU, leases and an eviction
race, restart reconciliation, orphan/missing files, atomic publication recovery,
oversize rejection, byte reservations, low-disk pinning, and preservation of legacy
database records. `git diff --check` passed. No credentials or live endpoints were
accessed.

## Existing-suite result and boundary

The one repository-wide `pytest -q` attempt stopped during collection at the known
legacy Lambda import error:

```text
lambda/tests/lambda_tests/test_enqueue_next_stream.py:
ModuleNotFoundError: No module named 'mediaUtils'
```

No repository-wide tests ran in that attempt. The parent agent’s baseline run of the
Flask suite before this task recorded 298 passed, 31 failed, and 3 warnings; failures
were in existing Echo/player, browser-sidecar, and static-asset tests. The cache
focused suite passes independently. Full integration should rerun the complete
service suite after later JUKES tasks land.

## Follow-up review fix

The first Task 1 commit could count a planned LRU removal even when unlink failed.
A temporary-file regression test injected `PermissionError` on the oldest file and
confirmed the bug: completion was admitted without raising, leaving requested usage
above the configured pool limit. The fix re-reads persisted pool usage and available
disk after attempted removals. Failed unlinks retain their rows and bytes; admission
raises `CacheCapacityError` if the remaining limits still do not fit. The low-disk
fixture now reflects reclaimed space only after the real temporary file is removed.

RED/GREEN for the regression:

```text
RED: test_failed_unlink_does_not_count_as_evicted_pool_capacity
     failed because Cache.complete did not raise
GREEN: focused Task 1 suite — 21 passed in 1.18s
```

`reserve` intentionally does not provide a duplicate-job coordinator: Task 2 must
single-flight each `AudioKey` and ensure exactly one extractor writes a returned
reservation path. The deployment also assumes one cache-owner service process; the
later app lifecycle task must enforce that exclusive-process assumption. The cache
lock itself coordinates threads in that process.
