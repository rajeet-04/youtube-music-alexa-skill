# Faster uncached audio

Status: execution approved; backend implementation and isolated verification in progress.
Scope correction: this server performs backend implementation and tests only; no Android testing/compilation. App integration is documented as a client handoff.

## Objective and baseline

Reduce request-to-playback latency for uncached public music while downloading audio only. Use explicit https://music.youtube.com/watch?v=<video_id> URLs for extraction and public-scope probes. Existing successful cold preparations have P95 37.88 seconds and maximum 58.16 seconds. Queue wait peaked at 33.50 seconds. Playback-start latency is currently unmeasured.

The current selector is 140/bestaudio[ext=m4a]/bestaudio/best: its last fallback allows combined video and audio. Downloads are piped into a private file and ffprobe validates the completed file. No explicit FFmpeg extraction/transcoding stage exists. Waitress is configured with 16 threads in the Dockerfile, so thread capacity must be measured rather than assumed to be four.

## Selected approach

Use strict audio-only extraction and progressive AAC HLS delivery, initially opt-in. Keep completed-file delivery for existing callers. A growing ordinary M4A is unsuitable as a drop-in replacement because initialization metadata and incomplete-file range semantics need explicit handling. The consumer is the JUKES Android app, not Alexa. Target its beta branch, inspected at ab398b1 (2.4.1-beta). Actual startup, resume, seeking and next-track behavior must be tested in JUKES before enabling progressive delivery by default.

### Verified client integration

JUKES beta's JukesApi.kt posts /v1/audio/prepare and long-polls /v1/jobs/<id>?wait=10 until ready; it currently expects audio_url to identify a completed file with byte ranges. PlaybackService.kt uses Media3 ExoPlayer and DefaultMediaSourceFactory with a cache data source. The app build lists no Media3 HLS dependency. Progressive HLS therefore requires coordinated app changes: add the matching Media3 HLS module, opt into progressive preparation, consume the stream URL when streamable, and keep offline downloads on the completed-file path. Review cache keys, metadata MIME hints, seeking, URL video-id parsing and download range probes for playlist compatibility. A backend-only change cannot make the current prepare-and-wait client begin playback early.

## Extraction and output

Select 140[vcodec=none]/bestaudio[ext=m4a][vcodec=none]/bestaudio[vcodec=none]; never fall back to a combined video format. Fail explicitly if audio-only media is unavailable. Reject video streams during validation as defense in depth. Use the Music watch URL in every extraction attempt and the anonymous public-scope probe. This controls the input URL; it does not guarantee a different underlying YouTube extractor or media CDN.

Run one extraction per audio key. Feed selected audio to FFmpeg; map only the first audio stream and disable video. Copy AAC when compatible with the chosen HLS container; transcode other audio codecs to AAC only when necessary. Do not download video and then strip it. Measure whether the selected upstream container is incrementally demuxable; if metadata is unavailable until EOF, do not announce progressive readiness. Retain completed-file fallback for that track.

Produce approximately two-second MPEG-TS segments using atomic segment and playlist publication. Start an EVENT playlist after two complete segments exist, retain all segments for seeking, and append ENDLIST only after successful completion. Segment duration and initial buffering are tuning values, not promised startup times.

## API and lifecycle

Add explicit progressive opt-in to preparation. Return a stream URL only once the startup buffer is published; distinguish streamable from completed without marking a partial result as a ready completed-file cache entry. Existing callers continue receiving the existing completed-file URL and job states. Add playlist and segment endpoints under /v1/streams/<opaque-stream-id>/ with HTTPS URLs and correct MIME types. Segment endpoints serve only atomically published immutable segments. Playlist responses prevent stale caching. HEAD never starts extraction.

Concurrent callers join the same job. A reader disconnect does not cancel shared preparation. Maintain leases across playback and a bounded grace period between segment requests so eviction cannot remove an active stream between requests. Reserve and account for all source, segment and finalized-file bytes. Enforce existing disk limits and deadlines; prune abandoned streams and recover or discard incomplete generations after restart.

Once complete, validate and publish the cached audio file. Reuse the downloaded source or remux the complete segments without another upstream download. Retain active stream segments until readers finish; reclaim them afterward. Keep separate format policy keys where output compatibility changes.

## Failures and scheduling

Before playlist publication, fallbacks may restart private output. After publication, never restart or mix another extraction attempt into that stream generation. Mark the stream failed and expose that outcome to clients; do not fabricate a successful ENDLIST. Later retries use a new stream ID. Continue respecting rate-limit cooldown and public-only credential checks.

Cap speculative warmups at one worker initially, preserve requested queue priority, and pause new warmup dispatch while requested work is queued. Do not preempt an already published stream. Measure contention before increasing extractor concurrency or HTTP thread counts.

## Observability and acceptance

Record correlated monotonic timings for metadata resolution, queue wait, extractor first byte, first published segment, streamable readiness, download completion, validation and response first byte. Keep preparation completion and progressive readiness distributions separate. Track post-start failures and playback stalls; exclude failed outcomes from successful latency percentiles but report their rate.

Required tests: audio-only format selection on every fallback; Music URLs for probes and downloads; rejection of video streams; incremental FFmpeg output before upstream EOF using real media fixtures; no unpublished segments exposed; joined callers; HEAD behavior; midstream failure; restart cleanup; capacity accounting; leases across segment requests; completed-file range behavior; cache hits; and requested priority under warmup load.

Benchmark identical controlled uncached workloads before and after, including four simultaneous requested tracks with warmups. Success requires a lower request-to-streamable P95 without increased failure or stall rates, and no video downloaded. Report full preparation P95 separately. Enable progressive delivery by default only after JUKES Android startup, pause/resume, seeking, offline downloads and next-track playback pass. If Android device validation is unavailable, report it as outstanding and keep the feature opt-in.

## References

- https://github.com/yt-dlp/yt-dlp#format-selection
- https://github.com/rajeet-04/JUKES/tree/beta

## Review and next step

Review this design before writing the implementation plan. The plan must cover both this backend and JUKES beta, including how the app opts into progressive preparation and consumes the stream URL, and preserve completed-file compatibility for existing app versions. The inspection clone at /tmp/jukes-app-inspect is a reference checkout, not the user's app development checkout.
