# Backend optimization results — 2026-10-07

## Result

The final fully matched 14-track cold test passed 14/14 on each version. With 465 valid fixture cache entries per version, first-audio P95 fell from **30.56 s to 8.26 s (73%)**, and completed-file P95 from **30.51 s to 9.31 s (69%)**. All 14 candidate HLS streams decoded fully with zero post-start failures and zero measured buffer deficit. These are isolated backend benchmark percentiles, not production rolling-window metrics.

| Final workload | Control first audio P95 | Candidate first audio P95 | Control completion P95 | Candidate completion P95 | Successes |
|---|---:|---:|---:|---:|---|
| 14 cold tracks, concurrency 4, 465 starting cache entries | 30.56 s | 8.26 s | 30.51 s | 9.31 s | 14/14 each |

Raw rows: [control](final-control.json), [candidate](final-candidate.json). The strongest change removes repeated cache entry/lease scans from byte writes and replaces per-entry SQLite lease queries with one locked snapshot. Source-start overlap, preloaded isolated yt-dlp children and asynchronous HLS feeding preserve public source policy, bounded process lifetime and completed-file compatibility.

## Design and probe

User authorized further optimization and architecture changes if needed, with backend-only tests and conditional production replacement. Continue on isolated `feat/progressive-audio` from `55fc008`.

Three approaches were considered: trim yt-dlp network requests; reuse a persistent extraction worker; overlap exact-ID metadata lookup with extraction and remove repeated cache admission scans. The third preserves subprocess isolation and existing responses while removing serial work. Persistent extraction introduces additional shutdown/credential isolation complexity for a modest process-start saving and is deferred. Request-skipping probes did not show a consistent meaningful improvement; skipping webpage/config requests failed, so it is not enabled.

Exact-ID preparations can enable `JUKES_PREPARE_OVERLAP=1`. Extraction is admitted with the same requested-work limit before metadata resolution, which then runs concurrently with the existing worker. Title/artist matching still resolves before admission. Responses keep resolved metadata; failures keep metadata errors. A metadata failure can leave bounded public extraction running, but is recorded as one failed preparation rather than a successful extraction outcome. Initial cache/path observations are retained so overlapping one's own job is not counted as a joined request. The feature defaults off until benchmark validation.

Cache writes within an existing persisted reservation check real disk space and other outstanding disk commitments but do not rescan all cache entries and reader leases. Growing the reservation or encountering disk pressure still uses full admission and eviction logic before writing. This retains byte accounting while reducing work under the global writer lock.

## Validation

258 backend tests pass locally (17.22 s) and in the final Docker test image with the optimization options enabled (45.65 s). Independent review of the overlap/cache changes and architecture fix found no remaining material issues. No Android work is included.

## Method and replacement gate

Fresh separate control/candidate volumes, same production-image base, VPN namespace, provider, HTTP threads and concurrency four. Compare identical known-playable track IDs in alternating version order across fresh rounds. Cache hits are excluded; continuous HLS consumer validation and complete-file times are reported. Repeated IDs are explicitly counted as repeated requests, not unique tracks; small pilot P95 is descriptive. Production is replaced only for a consistent latency improvement with no reliability, stream or completed-file regression.

## Architecture follow-up

The first matched round (overlap + cache fast path) had four successes per version: first-audio P95 9.63 s control / 9.54 s candidate, completion P95 9.61 s / 10.48 s. This did not satisfy replacement. Node versus Deno source probes also did not establish a repeatable runtime benefit; Node is not included in the retained implementation.

The next candidate adds opt-in `JUKES_EXTRACTOR_FORKSERVER=1`: a clean Python forkserver preloads yt-dlp and YouTube extractor imports at service startup. Every CLI extraction still gets a new process, isolated stdout/stderr and a dedicated process group. There is no shared per-job downloader, cookie state, or upstream connection. This removes repeated import work under the two-core CPU budget without changing extraction options or source policy. Normal subprocess execution remains the default.

Independent architecture review identified descriptor retention if startup failed before a child consumed a pre-created transfer object. Writer connections now serialize during process startup and close in the parent afterward. Repeated startup-failure tests verify unchanged descriptor count; an early child-kill test verifies both pipes reach EOF and no descriptors remain. Binary audio integrity, failed CLI exit codes and timeout group termination are covered with offline real processes.

## Asynchronous encoder feed and representative cache size

The synchronous four-worker candidate improved the broader 11-track first-audio P95 from 8.77 s to 7.58 s, but completed-file P95 was 8.75 s control / 8.90 s candidate. Two workers performed worse and were rejected (four-track first-audio P95 9.99 s control / 10.10 s candidate).

The final candidate decouples download from FFmpeg: committed cache-file ranges are signaled to a reader thread, without an in-memory audio queue. Its descriptor survives atomic cache publication; a persistent source-reader lease prevents eviction until that descriptor closes. A 120-second encoder lifetime bound and 15-second post-source drain bound kill stalled writers. Validation failure kills/unblocks the encoder before source reservation cleanup. ENDLIST still requires validated job completion and actual encoder EOF. Offline tests cover gated encoders, rename/full decode, validation failure, deadlines, source leases and shutdown. Independent review findings were fixed and verified.

The final full suite passed 263 backend tests locally (20.61 s) and in Docker with optimization options enabled (48.97 s). The final seeded comparison uses 465 valid one-second AAC fixture entries in both isolated caches, matching production entry count but **not** production byte occupancy (7.38 MB fixtures versus 1.55 GB production). This tests per-chunk cache/lease scan cost. Fixtures are never used as the live request tracks. All 11 request tracks were cold and identical between versions. The dash-prefixed twelfth recent ID was omitted from both sides before HTTP measurement due to driver list parsing; no measured outcomes were filtered.

The seeded first comparison passed all 11 tracks on both versions: first-audio P95 29.02 s control / 12.55 s candidate, completed-file P95 28.98 s / 21.91 s. All candidate HLS streams decoded with zero post-start failures or measured buffer deficit. One source delivered its body slowly and remains in the result rows.

Further profiling identified a remaining admission cost: `_eligible_audio` fetched the full lease table once **per audio entry**. The final implementation snapshots lease keys once under the same coordinator lock used by insertion/release and eviction. The regression test preserves pinned-file exclusion and verifies constant query count as entries grow. Final verification is 264 passing backend tests locally (21.35 s) and in Docker (48.83 s); independent review found no remaining material issue.

## Final cohort and limitations

The reversed 11-track seeded round had 11/11 candidate successes but **10/11 control successes**, with one `no_match` metadata response for `j5WqmNr3jOY`. That row is retained in seeded-repeat-control.json and was not replaced with a successful retry. Its latency comparison alone was not used as the final replacement gate. The final 14-track cohort was selected before execution: the remaining 10 recent IDs plus the four known-playable pilot IDs. The unstable ID was omitted from both versions before this new run. Both fresh caches started with identical 465-entry datasets and every selected track was cold. The final comparison ran control first, candidate second, without other benchmarks, builds or tests.

The sample is a controlled deployment pilot rather than a stable population P95 estimate (14 samples makes nearest-rank P95 the maximum). The earlier seeded cohort replicated a large cache-size cost on a different round; offline tests confirm the query-scaling mechanism. Cache cardinality is matched; production byte occupancy, internet variation and device playback are not simulated. Upstream availability and long source-body stalls remain outside this optimization. Full earlier failure rows and rejected runtime/worker experiments remain in the artifacts.

## Existing-client file API

After the final pair had identical 479-entry caches, the same new cold track (`-0YEJbeTXEk`) was prepared with concurrency one and no progressive opt-in. Control completed in 10.68 s (first audio 10.70 s); candidate completed in 3.57 s (first audio 3.59 s). Neither started HLS. This is one matched compatibility observation, not a population P95 estimate. The served candidate file passed full FFmpeg decoding and ffprobe audio-only validation; HEAD returned the exact content length, and first/last byte ranges returned 206 with matching bytes. Raw rows: [file control](final-file-control.json), [file candidate](final-file-candidate.json).
