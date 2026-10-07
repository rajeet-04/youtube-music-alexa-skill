# Progressive backend testing and rollout

Scope: backend only. No Android builds or tests run as part of this workflow.

## Branch and containers

Backend branch: `feat/progressive-audio`, worktree `/tmp/jukes-progressive-backend`.
Use `docker-compose.benchmark.yml` with a separate project name. Control uses the
immutable production code image `jukes-admin-metrics:be9ca17`; candidate is built
from `Dockerfile.benchmark`. Both use the same VPN namespace, provider and HTTP
thread count but separate databases and caches. Never attach production volumes
or delete production cache entries to create cold samples.

The `tests` build target includes the whole repository because deployment checks
read Compose files and helper scripts outside flask-server. Its entrypoint runs
the full Python suite. The optional `fixtures` profile feeds identical throttled
real AAC/MP4 into both actual extraction/job/cache implementations. Fixture results
prove early publication and serving; they do not establish YouTube performance.

## Benchmark

Run `scripts/benchmark_progressive.py BASE --videos ID ... --parallel 4 --output RESULT.json`.
Use `--progressive` for the candidate. The same workload checks HEAD before each
preparation, excludes cache hits from cold percentiles, reads the first actual
audio byte (not a playlist byte), waits for full completion, and reports failures.
Returned URLs are rebased to BASE, so an origin run stays at the origin and a
public-proxy run stays on the proxy. Use fresh isolated project volumes for repeat
rounds, alternate candidate/control order, and avoid builds or other benchmarks
while measuring. Keep raw rows, not just successful averages. Four samples are a
pilot, not a statistically stable P95.

## JUKES beta client handoff

JukesApi.kt currently waits for `ready`. To use early playback it must opt in on
prepare and poll and accept `stream_url` while downloading. Media3 requires its
matching HLS module. Playback should disable file-range probing for the playlist;
offline/background downloads should poll normally and fetch the completed
`audio_url`. The playlist URL carries `video_id` as a query parameter for queue
metadata. The app checkout has not been modified by this backend task.

## Replacement gate

Replace production only if backend tests and real-source comparisons pass with
lower request-to-first-audio latency, no higher failure rate or stalls, and no
regression in completed-file delivery. Upstream rate limiting, unmatched success
cohorts or small pilot samples do not satisfy the gate. Keep progressive opt-in;
an old app still waits for full completion. Preserve the current image and volumes
for rollback. If measurements do not establish improvement, retain production and
leave the feature on its branch/test containers.
