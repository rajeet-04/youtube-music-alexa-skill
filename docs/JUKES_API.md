# JUKES backend API (app handoff)

Base URL: your `PUBLIC_BASE_URL`. All `/v1` JSON responses are `Cache-Control: no-store`.
Durations in `/v1` are **milliseconds**; the legacy `/audio/?duration=` is **seconds**.

## Errors

```json
{"error": {"code": "match_rejected", "message": "…", "retryable": false}}
```

| Status | Meaning | Codes |
|---|---|---|
| 400 | invalid input | `invalid_request` |
| 401 | bad/revoked token, admin session | `invalid_token`, `unauthorized` |
| 404 | no match / unknown job / not cached / unavailable video | `no_match`, `job_not_found`, `not_ready`, `video_unavailable` |
| 413 | body or track too large | `input_too_large`, `track_too_large`, `warmup_too_large` |
| 422 | match rejected (wrong version/duration), non-public audio, session rejected | `match_rejected`, `public_audio_required`, `session_rejected` |
| 429 | admission limit (new work/queue) | `rate_limited`, `queue_full` |
| 502 | upstream/extraction failure | `upstream_error`, `extraction_failed`, `extraction_timeout`, `invalid_media` |
| 503 | pending / capacity / unavailable | `pending`, `cache_capacity`, `rate_limited`, `personalization_unavailable` |

`retryable: true` responses carry `Retry-After` (seconds).

## Choosing a track

Exactly one selector (extra fields → 400):

```json
{"video_id": "dQw4w9WgXcQ"}
{"title": "Never Gonna Give You Up", "artist": "Rick Astley", "duration_ms": 213000}
```

`duration_ms` is optional and positive. With it, a candidate must be within
`max(8 s, 7 %)` or the request is `422 match_rejected`. Unrequested versions
(live, remix, karaoke…) and a wrong artist are rejected, never substituted
(`404 no_match` when nothing resembles the song).

## Warmup and prepare

`POST /v1/warmup` — speculative: resolve, then start downloading **immediately**
(no confirmation call). Use it when the user is *probably* about to play a track
(debounced search, likely match). One track per request.

`POST /v1/audio/prepare` — the user wants this track: same, but it is a
requested (long-lived) track. If a warmup of it exists — even still downloading —
it is promoted in place: no second download, no byte copy.

Both return **200** if the audio is ready, otherwise **202**:

```json
{
  "video_id": "…", "title": "…", "artists": ["…"], "artist": "A and B", "album": null,
  "duration_ms": 213000, "artwork_url": null,
  "job_id": "<opaque>", "status": "queued|downloading|ready|failed",
  "pool": "warmup|requested", "audio_url": "https://…/v1/audio/<id>",   // only when ready
  "personalization_status": "anonymous|connected|reconnect_required|…"
}
```

**Verify metadata in the app** (title/artist/duration) before showing it as the
chosen track; the backend picks the best match but the app owns the final call.

### Polling

`GET /v1/jobs/{job_id}` → `{job_id, video_id, status, pool, audio_url?, error?}`.

### Optional progressive playback

When the backend is started with `JUKES_PROGRESSIVE=1`, playback clients can use
`POST /v1/audio/prepare?progressive=1` and
`GET /v1/jobs/{job_id}?wait=10&progressive=1`. The JSON body is unchanged.
If early AAC segments are available, these responses add `streamable: true`,
`stream_url`, `stream_id`, `video_id` and `stream_ready_seconds`. The job remains
`downloading` until the complete source is downloaded and validated. A progressive
long poll wakes when segments are available; a normal long poll still waits for
completion. `stream_ready_seconds` measures segment generation from its start,
not total preparation or actual device playback latency.

The `.m3u8` URL serves an AAC EVENT playlist; relative `.ts` URLs identify immutable
audio-only MPEG-TS segments. Playlists use `no-store` and acquire a reader lease.
Segments support normal GET/HEAD and conditional file requests. Only completed,
atomically published segments can be fetched. Playlist/segment HEAD does not start
extraction. A failed generation returns 404; it is never restarted under the same
stream ID. Successful streams retain their full timeline and receive ENDLIST only
after full-source validation. Inactive completed streams expire after 30 minutes;
active response leases and a 30-minute grace between requests pin their files.

Use `stream_url` for immediate playback as soon as it is present, even while the
job is downloading. Continue to use `audio_url` once `status: ready` for completed
file downloads and offline persistence. Never pass a playlist to a segmented
file downloader. Clients without progressive support omit the query parameter;
they retain completed-file behavior and do not start an HLS encoder.

Tracks with unknown duration, duration over 30 minutes, unavailable streaming
capacity, or an unsuitable input container fall back to completed-file delivery.
The input URL is always `https://music.youtube.com/watch?v=<id>`. Format selectors
exclude video in every fallback, and media containing a video stream is rejected.
Public: it never contains title/artist or user data. Add `?wait=<seconds>` to
long-poll: the server answers as soon as the job is ready or failed, holding the
request at most `JUKES_MAX_JOB_WAIT_SECONDS` (default 10). Without `wait` (or on an
older server) back off 1 s, 2 s, 3 s, then every 3 s; give up after ~2 min. Jobs stay queryable 24 h; an unknown job does
**not** mean the audio was deleted — call prepare again (it is idempotent and free
when the file is cached). A `failed` job with `retryable: true` may be retried
with a fresh prepare.

## Playing audio

`GET /v1/audio/{video_id}` — complete file with `Content-Length`, `Accept-Ranges`,
`ETag`, `If-Range`, byte ranges (open-ended and suffix). Missing → the server starts
the download and answers `503 pending` (do not wait on the socket). `HEAD` only
inspects a finished file (`404 not_ready` otherwise) and never starts work or
refreshes cache recency. Content type reflects the real container (`audio/mp4`
normally, `audio/webm` possible) regardless of any URL suffix.

## Legacy `/audio/` (kept for the current app)

`GET|HEAD /audio/` with `video_id`, `url` (YouTube link), or `q` (+ `duration` in
**seconds**); `info=1` returns `{video_id, title, artist, duration_ms, cached, audio_url}`
without any download; `wait=1` is accepted (files are now always completed first).
A cache miss waits up to `JUKES_LEGACY_WAIT_SECONDS` (default 25 s) then returns
`503` with `Retry-After`; the download keeps running. The obsolete `key` parameter
and `X-Api-Key` header are ignored and never logged.

Verified in the JUKES beta source (`AlexaBackendApi.kt`, `ApiClient.kt`): the app
only enables the backend when URL **and** key are both non-blank (the key value is
ignored now, but must stay non-empty in the build config); the Ktor client uses
connect 30 s / socket 60 s / request 120 s, so the 25 s legacy wait fits inside the
socket timeout. The app treats a JSON 404 from `/audio/?info=1` as an error (next
source takes over), not as "endpoint missing". **Not verified:** Android playback of
the served container (ExoPlayer with `.m4a`/`audio/mp4` and DASH-style m4a), and
the app's per-call timeout overrides — test on a device.

## Optional personalisation

1. `POST /v1/installations` → `201 {"token": "…"}`. Store it in app-private
   storage; it is shown **once**. Limited to 5/min per caller. Losing it means a
   new installation and reconnecting.
2. Send `Authorization: Bearer <token>` on any call to use that installation.
   No header = anonymous. A supplied bad token is **401**, never silently anonymous.
3. `PUT /v1/me/youtube` `{"cookie": "<Cookie header>", "account_index": 0}` — on
   connect and on refresh. Only these two fields are accepted; the cookie must
   contain `__Secure-3PAPISID`. The backend checks it against a signed-in account
   (an HTTP-200 logged-out page is rejected, `422 session_rejected`), encrypts it,
   and keeps the previous session if validation fails.
4. `GET /v1/me/youtube` → `{connected, credential_generation?, connected_at?}` (redacted).
5. `DELETE /v1/me/youtube` disconnect; `DELETE /v1/me` revoke the token and delete everything.

Idle expiry: token-only installations 30 days, connected 180 days (credentials
deleted). Personalisation needs `JUKES_CREDENTIAL_ENCRYPTION_KEY`; without it,
issuance and connect return `503 personalization_unavailable` and everything
anonymous keeps working. Never log cookies or tokens in the app.

## Recommendations

`POST /v1/recommendations` `{"video_id": "…", "limit": 25}` (`limit` 1–100) →

```json
{"video_id": "…", "tracks": [ /* same track shape, YouTube Music order, seed first */ ],
 "personalization_status": "anonymous|connected|reconnect_required|personalization_unavailable"}
```

Anonymous by default (locale `en-IN`/`IN`). If a connected session has expired you
get anonymous results with `reconnect_required`: re-capture the session and `PUT`
again. The backend does **not** filter, dedupe or reseed queues: keep the app's
existing fetch → filter → dedup → reseed engine and call this endpoint as its
radio source.

## Caches

Requested pool: 10 GB, LRU by accepted prepare/play/download, no TTL. Warmup pool:
1 GB, expires 2 h after completion (duplicates don't extend). Polling, HEAD and
`info=1` never refresh recency. Tracks larger than a pool return `track_too_large` /
`warmup_too_large` (a too-large *warmup* can still be requested directly if it fits
the requested pool).

## Handoff checklist for the app

- [ ] Debounced search → pick likely match → `POST /v1/warmup` (optional but cuts start time).
- [ ] On play: `POST /v1/audio/prepare`, poll the job, then play `audio_url` with range support.
- [ ] Verify returned metadata against the selected track.
- [ ] Handle 202/429/503 with `Retry-After`; fall back to the existing provider chain on terminal failure.
- [ ] (Optional) installation token + session connect/refresh/disconnect; redact credentials from logs.
- [ ] Keep the build-config backend key non-blank until the app gate is relaxed.

## Administrative operational metrics

`GET /admin/api/status` remains session-authenticated and `Cache-Control: no-store`.
Its additive `metrics` object describes only the first-party JUKES pipeline;
no third-party provider metrics or credentials are returned. Lifetime counters
start at `metrics.started_at`, survive restart, and are not backfilled from old
jobs. Rolling windows (`15m`, `1h`, `24h`) use minute buckets (up to 60 seconds of
boundary approximation), 24-hour retention, and bounded latency samples (10,000
per category). P99 requires 100 samples; null means unavailable. Means use all
observations; percentile sample counts and sampling flags are explicit.

Preparation latency includes metadata resolution, queue wait and validation.
Cached, warmed, joined/in-flight, cold and recovered paths are separate. Recovery
includes restart downtime. Polls, HEAD, metadata-only lookups and repeated audio
ranges are excluded. Audio cache hit rates describe preparation requests, not
HTTP range reads or metadata-cache lookups. Admission rejection is separate from
accepted preparation failure. Retries cover attempted extractor fallbacks and
job recovery; internal transport retry counts are unavailable.

Warmup consumption is unique per speculative result. Completed-before-request
warmup hits and in-flight promotions are separate. Requested warmup-hit share is
completed warmup hits divided by requested preparations. Cohort usefulness is
consumed results divided by successful results completed inside the selected
window; recent cohorts are still maturing. Comparing path latency describes
observations, not causal savings for identical tracks.

Job status now includes `evicted` as a distinct terminal state alongside `ready`
and `failed`. Evicted responses omit `audio_url` and include a redacted retryable
reason (`cache_evicted` or `warmup_expired`). Clients should initiate a fresh
prepare for these results. Eviction does not increase failure counts. Old
`failed/cache_evicted` records migrate to `evicted` without changing other data.

Resource usage is locally sampled every five seconds or less frequently, on
admin demand. CPU/RAM include host/container scope and effective limits. Network
counts non-loopback interfaces in the shared network namespace (including other
services sharing it); RX/TX rates require two samples and do not measure network
capacity. No monitoring stack or external health probe is added.
