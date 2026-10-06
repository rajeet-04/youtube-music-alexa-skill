# First-party JUKES admin operational metrics

## Purpose and scope

Extend the existing compact administrative dashboard to show backend health,
preparation latency, audio-cache effectiveness, speculative warmup usefulness,
evictions, and resource bottlenecks. Instrument only JUKES's internal request,
cache and job pipeline. No third-party source identifiers, provider metrics,
extractor-client comparisons or provider charts enter the dashboard.

Use existing Flask, SQLite and local operating-system interfaces. Add no
monitoring stack or upstream probes. Keep cookie-management and browser-login
controls in their existing authenticated interface. Deployment is separate.

## Collection and persistence

A focused metrics service owns allowlisted numeric events, lifetime counters,
minute aggregates, and bounded preparation-latency samples. Persist these in
new `jukes_metrics_*` tables in the existing database. Keep minute aggregates
and latency samples for 24 hours; lifetime counters survive pruning and restart.
Retain at most 10,000 latency samples per category across the rolling day;
identify sampled percentiles explicitly and expose retained sample counts.
Persist a collection-start timestamp; never invent historical counters from
old job rows. Display the coverage start when a window predates collection.

Record counters and lifecycle markers transactionally with owning transitions
where possible. Unique completion, consumption and eviction markers prevent
duplicate events after retry/recovery. Polling, admin refresh, HEAD, metadata-only
requests, range requests and internal cache probes do not create preparations
or inflate hits. GET audio contributes one consumption only on the first
accepted transition for that prepared result; subsequent segments do not.

Use wall time for window membership and monotonic time for live durations;
persist elapsed durations at completion. Restarted work retains original wall
time and is marked recovered; latency includes recovery downtime and is labelled
accordingly. Keep event storage free of titles, video IDs, tokens, IP addresses,
headers, cookie material, paths and arbitrary exception text. Internal lifecycle
deduplication may reference opaque job/result IDs but never returns them in
aggregate dashboard output.

## Job and preparation definitions

Job totals count unique actual queued extraction jobs ending ready or failed.
Creating a ready wrapper around existing cached audio is a cache resolution,
not a completed extraction. Completion stays counted after subsequent eviction.

Preparation counts refer to accepted first-party prepare requests, including
legacy audio requests that initiate preparation. Track cache-hit results,
joins to active jobs and cold jobs separately. Start timing after admission and
valid selector/context parsing, before metadata resolution; finish when the
resolved complete audio becomes ready or the operation fails. Asynchronous
requests remain linked to their shared job until terminal completion. Bound
pending observer records to avoid unbounded duplicates; excess observations
still contribute admission counters but must report latency coverage loss.
Do not count job-status polls or ready-file range reads as preparations.

Show average and nearest-rank P50/P95 of successful preparation durations,
split into cached, joined/in-flight and cold results, with combined totals.
P99 appears only with at least 100 retained successful samples in the selected
window; other empty values display unavailable rather than zero. Also show
queue wait and extraction-to-validation time when available to explain P95.

Retry count includes explicitly attempted extractor fallback/retry operations
and recovered-job retries, broken out by kind. Do not infer invisible yt-dlp
transport retries from configured retry limits; instrument actual retries if
available, otherwise identify that coverage as unavailable. No provider labels.
Temporary versus terminal failures follow the API's explicit retryability
classification. Queue/capacity rejection is an admission failure, separately
reported from accepted preparation failures. Expiration and eviction are never
download/preparation failures.

Current job gauges: queued, downloading, ready, failed and evicted. Count from
persisted retained jobs with live coordinator state reconciled. Ready must mean
an available file, not a stale ready row. Queue utilization is queued entries /
configured queue capacity; worker utilization is running jobs / configured
workers. Show active and configured workers and queue capacity explicitly.

## Eviction lifecycle and compatibility

Add `evicted` to persisted job states using a safe SQLite migration preserving
existing records, indexes and other tables. Convert historical `failed` rows
with `cache_evicted` to evicted without creating new metric failures.
Distinguish bounded reason codes: capacity eviction, warmup expiration, missing
file/reconciliation loss. Count an eviction only after successful removal of
the tracked ready entry; protected files and failed unlink attempts do not count.

Reconcile ready results when the cache removes their file, including dashboard
snapshots. Old jobs must not invalidate newer results for the same audio key.
Evicted jobs are terminal, retained/pruned like other terminal jobs, and cannot
be reattached as usable results. Fresh preparation starts new work normally.
The versioned job response reports `status: evicted`, no audio URL, and a
redacted retryable reason. Document the additive state for app clients and
update long-poll, error mapping and terminal-state checks. Keep legacy audio
HTTP behavior compatible: missing results can initiate a fresh preparation.

## Cache metrics

Show ready-file storage and reserved/in-flight bytes separately for each pool:
requested LRU (default 10,000,000,000 bytes, no TTL) and warmup (default
1,000,000,000 bytes, 7,200-second TTL). Utilization uses total admitted storage
including reservations so operational pressure is visible. Display configured
limits rather than hardcoding defaults. Average ready-entry age is calculated
with a bounded SQL aggregate by pool, based on completion time.

One hit/miss observation per accepted preparation after metadata resolution:
hit means complete reusable file at admission, miss means no complete file.
Joining an in-flight job is a miss plus a separate dedup/join count. Internal
lookups are uninstrumented. Overall audio hit rate = hits / (hits + misses).
Main and warmup hits remain separate; metadata-resolution cache is separate
and never mixed into audio-cache effectiveness.

Eviction counts distinguish requested capacity evictions, warmup capacity
evictions, warmup expirations and missing-file losses. Average age and gauges
are current snapshots; counters/rates use the selected time window.

## Speculative warmup effectiveness

Count valid accepted warmup requests separately from unique speculative jobs,
duplicate requests and already-cached responses. Successful warmups count unique
speculative jobs completing validated audio, even if promoted while running.
Preserve speculative origin when `requested` changes.

Count each speculative result's first subsequent requested use once. Split
completed-before-request warmup hits from in-flight promotions. A completed hit
must avoid new extraction; a promotion can reduce remaining wait but is not
claimed to eliminate it. Repeated prepares/range reads do not count consumption
again. Show the preparation latency distributions of warmed hits, in-flight
promotions and cold requests without claiming causal savings from unequal tracks.

Label both rates explicitly:

- Requested warmup-hit share = completed warmup hits / accepted requested
  preparations. Window membership is the consumption/request timestamp.
- Warmup usefulness = unique consumed speculative results / unique successful
  speculative results in a completion cohort. For a selected window, denominator
  is results completed in that window and numerator is those results consumed
  by snapshot time. In-flight-promoted completions count as consumed but are
  separately visible. Recent cohorts are labelled still maturing until TTL passes.

Maintain bounded cohort records through TTL plus 24 hours so rolling cohort
ratios do not divide different populations. Lifetime usefulness uses cumulative
unique completed and consumed results. Failed speculative jobs are shown
separately, not included in the usefulness denominator. Warmup-hit rate in cache
and efficiency sections uses the same requested-hit definition and label.
Show warmup requests, successes, consumed tracks, in-flight promotions,
evictions, expirations, usage and configured TTL together.

## Local resources and health

Sample locally on dashboard demand with a minimum five-second interval, cache
the snapshot and calculate CPU and RX/TX rates from counter deltas. Never sleep
inside a request to measure CPU. First samples show rates as unavailable.
Read Linux procfs and cgroup v1/v2 where accessible; otherwise degrade fields
individually to unavailable. Respect CPU quota and memory limits, and label host
versus container scope. Show process memory alongside capacity memory when cheap.
Disk is the filesystem containing the audio cache: used, total and free bytes.
Network excludes loopback, identifies namespace scope, and shows cumulative
bytes plus bytes/second; avoid claiming these measure upstream bandwidth limits.

The dashboard displays process/dependency readiness using existing health
signals, not external requests. Missing CPU data is not unhealthy. Queue/worker
utilization and cache/disk pressure provide capacity indicators with explicit
values; do not introduce unsupported health judgments or decorative scores.

## Admin interface

Extend `/admin/api/status` with an aggregate `metrics` object and preserve its
existing keys. All metrics remain under existing admin-session validation and
no-store/security response headers. No public metrics endpoint. The initial
HTML uses the same snapshot as JSON. Refresh the compact operations section
every 10 seconds only while visible, with no overlapping requests. On session
expiry stop refreshing and direct the operator to sign in; keep the last sample
marked stale on transient errors.

Use responsive compact tables/cards, pool/queue/worker utilization bars, and a
small minute-aggregate trend for latency, failure rate, audio hit rate and warmup
consumption. A 15-minute/1-hour/24-hour selector updates rates and trends;
lifetime totals remain explicitly labelled alongside current gauges. Show units,
sample counts, missing data and collection coverage. Preserve existing compact
style, forms and browser controls. Use DOM text nodes for dynamic values and
allowlisted JSON; never render diagnostic HTML or secrets.

## Validation and acceptance

Deterministic tests with injected clocks and fake extraction cover:

- Cold, cached and joined preparations, latency boundaries, empty/low-sample
  percentiles, sample bounds, counter pruning and restart persistence.
- Warmup completion, duplicate calls, first consumption, in-flight promotion,
  TTL expiry, capacity eviction and cohort denominators.
- Evicted versus failed classification, failed unlink/leased protection, old
  versus new results, migration, long polling and public response compatibility.
- Queue/worker gauges under saturation and recovery, actual counted retries,
  and temporary versus terminal failures.
- Resource counter deltas, cgroup limits, first samples and missing procfs.
- Unauthenticated access denied, no-store headers, redacted allowlisted JSON,
  preserved admin controls and responsive visible-only refresh behavior.

Run retained backend/browser tests plus focused metrics tests and whitespace
checks. Verify dashboard rendering and browser behavior where tooling permits;
report unavailable live/browser verification honestly. Document definitions,
collection start, retention, sampling and the evicted state in API/admin docs.
No live deployment, third-party instrumentation or credential changes are part
of this work.
