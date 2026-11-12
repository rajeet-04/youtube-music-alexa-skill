# Progressive Audio Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans; execute inline with a final independent review.

**Goal:** Reduce JUKES beta cold playback startup with strict audio-only Music extraction and opt-in progressive delivery, and replace production only after fair benchmarks establish improvement.

**Architecture:** One shared extraction publishes short AAC HLS segments while retaining completed-file compatibility. Stream lifetime and capacity are bounded independently from completed-file cache lifetime. The client handoff specifies opt-in playback and completed-file offline downloads; this task changes and tests only the backend.

**Tech Stack:** Python/Flask, yt-dlp, FFmpeg, Docker.

**Spec:** ../specs/2026-10-07-progressive-audio-design.md

## Global constraints

- Explicit Music URLs, audio-only selectors, reject video streams.
- Private staging, immutable published segments, no cache-ready state before successful validation.
- Separate test volumes/ports; no production cache deletion to manufacture cold results.
- Feature opt-in; failed or unavailable device validation prevents default rollout.
- User approved execution and conditional container replacement on 2026-10-07.

## Review focus

- Midstream extractor failure must not restart published output.
- Partial MP4 metadata may defer demuxing; detect rather than promise early playback.
- Concurrent segment readers and disk exhaustion must not corrupt files or exceed limits.
- Old app versions and offline downloads must still get completed files.
- Warm production results must not be compared against cold candidate results.

### Task 1: Strict source policy

Files: jukes/extractor.py, tests/test_jukes_extractor.py.
Interface: Extractor._command and _probe_public_audio use Music URLs; _validate rejects video.
- [x] Add failing selector, URL and video rejection tests; run them.
- [x] Implement strict selection and validation; run extractor suite and commit.

### Task 2: Progressive coordinator and routes

Files: jukes/streams.py, jukes/extractor.py, jukes/jobs.py, jukes/routes.py, jukes/app.py; tests/test_jukes_streams.py and API tests.
Interface: Streams.begin(key), write(bytes), finish(success), view(key), lease(stream_id, filename); Jobs waits can wake on stream readiness; prepare opts in via query parameter progressive=1.
- [x] Add tests for early publication, failure generations, leases, accounting, cleanup, GET/HEAD, joined requests and compatibility; observe failures.
- [x] Implement bounded incremental audio demux/segment pipeline, atomically published HLS, completion fallback and first-byte timing.
- [x] Cap speculative workers at one; run full backend and real FFmpeg tests; commit.

### Task 3: JUKES beta API handoff (scope corrected by user)

Files: docs/PROGRESSIVE_BACKEND_TESTING.md, docs/JUKES_API.md.
Interface: playback opts in, accepts stream_url while downloading, disables file-range probes for playlists; completed download flows opt out.
- [x] Document progressive fields, long-poll behavior and completed-file compatibility for JUKES beta.
- [x] No Android testing or compilation on this server. Leave app checkout unchanged. User explicitly restricted verification to backend tests.

### Task 4: Isolated deployment and comparison

Files: docker-compose.benchmark.yml, Dockerfile.benchmark, scripts/benchmark_progressive.py, docs benchmark results.
Interface: identical baseline image and candidate image get fresh isolated caches, same VPN egress and provider, alternating track batches.
- [x] Test benchmark percentile, failure reporting and cold-hit exclusion.
- [x] Build candidate and test images; run backend tests in test container.
- [x] Run same benchmark against production for operational baseline and isolated old-image control for fair cold comparison; repeat identical workloads in alternating order.
- [x] Run independent whole-branch review, fix material issues and rerun checks.
- [x] Replace production only with evidence of lower latency and no reliability regression; retain old image and documented rollback. If gates fail, retain production and report evidence.

## Execution outcome

248 backend tests pass locally and in the test container. Independent backend review completed and material findings were fixed. Real-source pilot comparisons do not satisfy the replacement gate; production remains unchanged. See ../../benchmarks/2026-10-07-progressive/REPORT.md for measurements and limitations. No Android compilation or tests belong to the final workflow.
