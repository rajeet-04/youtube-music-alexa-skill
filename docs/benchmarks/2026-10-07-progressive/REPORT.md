# Backend progressive audio results — 2026-10-07

Decision: retain the running production container. The candidate works, but live-source evidence does not establish faster serving or preserve completed-file latency. Implementation is isolated on `feat/progressive-audio`; progressive delivery defaults off and requires explicit request opt-in.

## Final matched comparisons

| Workload | Version | Cold successes | First audio P95 | Complete file P95 | Highest first audio |
|---|---|---:|---:|---:|---:|
| Live Music, 4 tracks | Production-image control | 4/4 | 10.62 s | 10.60 s | 10.62 s |
| Live Music, same tracks | Candidate | 4/4 | 10.85 s | 12.32 s | 10.85 s |
| Throttled AAC fixture, 20 tracks | Production-image control | 20/20 | 4.84 s | 4.78 s | 4.96 s |
| Throttled AAC fixture, same tracks | Candidate | 20/20 | 2.12 s | 5.16 s | 2.19 s |

The controlled fixture starts audio about 56% sooner. It demonstrates incremental publication, not YouTube performance. The live pilot starts about 2% slower and completes about 16% slower. Four observations are insufficient for a stable P95; nearest-rank P95 here equals the maximum. No production replacement is warranted.

All final candidate streams decoded completely as audio-only AAC, with continuous segment numbering, matching duration, zero post-start failures and zero measured buffer deficit. Buffer deficit is a local scripted consumer measure, not a device playback test.

Raw final rows: [live control](r3-control.json), [live candidate](r3-candidate.json), [fixture control](fixture-repeat-control.json), [fixture candidate](fixture-repeat-candidate.json).

## Method

Both versions used the immutable current production image as their base, the same VPN/network namespace and provider, 16 HTTP threads, four extraction workers, and separate test databases/cache volumes. No production cache was cleared or mounted. Each paired cold round used identical track IDs, concurrency four, and sequential version runs without other test/build load. Live round r2 ran candidate first; final r3 ran control first. Fixture repeat used 20 new IDs after equal prior workload on both caches.

The driver checks cache status before preparation, excludes hits from cold percentiles, measures the first actual audio byte rather than playlist bytes, waits for completed-file readiness and retains failures. Progressive consumers read every segment through ENDLIST, validate audio-only AAC with ffprobe and decode the complete stream with FFmpeg. Returned URLs are rebased to the measured origin. JSON artifacts are extracted from preserved driver stdout rows.

The final live cohort is `lYBUbBu4W08`, `9bZkp7q19f0`, `kJQP7kiw5Fk`, `JGwWNGJdvx8`. First-audio candidate times were 8.53, 8.85, 10.85 and 9.08 seconds respectively; completed-file times were 10.66, 11.26, 12.32 and 10.94 seconds.

Earlier pilots remain alongside the final rows for audit. They include upstream rate limiting and some concurrent build/test load; they do not justify rollout. Round r2 had 4/4 successes on both sides, first-audio P95 10.29 s control versus 9.99 s candidate, but completed-file P95 10.28 s versus 11.51 s. The first fixture run overlapped local tests and is superseded by the repeat above. A production operational probe included cache hits and an upstream failure and is not a cold comparator.

## Implementation and verification

- Explicit `music.youtube.com/watch` extraction URLs; strict audio-only selectors; validation rejects video streams.
- Opt-in FFmpeg HLS generation while downloading, with bounded transient reservations, atomic publication, reader leases, failed-generation isolation, and validated completion.
- Existing completed-file API remains available; no HLS encoder starts for clients that do not opt in.
- Speculative work is limited to one worker; source cooldown survives a failed published stream.
- 248 backend tests passed locally and in the Docker test image. Real FFmpeg tests cover early publication, full decoding, failures, capacity and reader lifecycle. Independent backend review findings were corrected and verified.
- No Android compilation or testing is part of the final workflow; app checkout is unchanged. Client integration requirements are documented in [the API](../../JUKES_API.md) and [testing/handoff guide](../../PROGRESSIVE_BACKEND_TESTING.md).

At the conclusion of this first experiment, production remained on image `sha256:22597981473751aafe5f4cc12fa46c225cb8e11c2420c5d9dbcd060ab936e911`. The r3 pair has been superseded by the follow-up optimization tests. See [the later results](../2026-10-07-optimization/REPORT.md) for current status. Before rollout, gather a larger matched live cohort with reliable upstream access and demonstrate improved first-audio latency without completed-file or reliability regressions.
