# Lyrics backend: verification handoff

## Status and scope

The changes are uncommitted local files in `backend/youtube-music-alexa-skill`, on branch `azure` at base commit `e7e58ca`. They were last revised on 2026-11-12 and have not been pushed, built into the VM image, or deployed. The image needs rebuilding, because `numpy` was added to `requirements.txt`. The Android and desktop clients have not been changed.

[LYRICS_API.md](LYRICS_API.md) is the current description of the order, the trust rules, the response, the cache and contributions. This document keeps the evidence.

## Files to review

- `flask-server/jukes/lyrics_align.py`: audio alignment (spectral flux, a range-limited and energy-normalised correlation). New.
- `flask-server/jukes/lyrics_store.py`: response cache, contribution queue and LRCLIB publisher. New.
- `flask-server/jukes/lyrics.py`: LRCLIB lookup, YouTube caption and YouTube Music fallback, cookie handling, caption conversion, and concurrency limit. This is a new, untracked file.
- `flask-server/jukes/routes.py`: adds `GET /v1/lyrics`, validation, and per-client rate limiting; adds `lyrics` to `Services`.
- `flask-server/jukes/app.py`: wires the service to the existing encrypted server-cookie store.
- `flask-server/tests/test_jukes_lyrics.py`: unit tests for caption conversion, lookup priority, upstream errors and an LRCLIB response. This is a new, untracked file.
- `flask-server/tests/test_jukes_api.py`: request validation and response test using a fake service.
- `docs/LYRICS_API.md`: client-facing endpoint and response notes. This and the present document are new, untracked files.

Use `git status --short` as well as `git diff`: ordinary `git diff` does not show untracked files.

## Current behavior

`GET /v1/lyrics` requires `title` and `artist`. Optional query parameters are `album`, `duration_ms`, and the actual playback `video_id`. The endpoint validates text length, video ID format, and positive duration; it applies a 20-request-per-minute per-client limit. The service has a three-request in-process concurrency limit. Responses follow the existing `/v1/` no-store policy.

Lookup order (the first timed result wins):

1. LRCLIB `/api/get` by metadata. If that misses or is plain-only, LRCLIB `/api/search`: accept the closest record within 5 s whose normalized title starts with the requested title or vice versa. Without a requested length, both title and artist must match exactly.
2. With a video ID: YouTube Music `get_lyrics(timestamps=True)` for that exact video, after the same 3 s length check. Timed lines come back as `lines` with `start_ms`/`end_ms`.
3. With a video ID: yt-dlp captions for that exact video (English, Hindi or Urdu, at most four tracks, `subtitles` before `automatic_captions`), rejected if the video's length differs by more than 3 s.
4. Plain-text fallbacks: LRCLIB, then YouTube Music.
5. Return 404 for no result, 502 when an upstream failed and no usable result exists, or 503 when the concurrency limit is reached. If every caption track fetch for the video fails, that counts as an upstream failure (502 unless YouTube Music or LRCLIB plain text then succeeds), not a miss.

yt-dlp runs with `process=False` (captions and duration only, no format selection), a silent logger so its errors never reach stderr, and the same bgutil PO-token provider as the audio extractor (`YTDLP_BGUTIL_BASE_URL`, empty to disable).

The stored YouTube cookie is decrypted through the existing server-cookie interface and used in memory; if unavailable, the implementation attempts anonymous requests. Never print cookies, auth headers, signed timedtext URLs, or upstream exception details in logs or test output.

Caption events are returned as `lines`, each with `text` and `start_ms`. Where present, segment timestamps are `event.tStartMs + segment.tOffsetMs`, defaulting missing offsets to zero. Event duration is not treated as a word duration. Offsets are exposed as `segments`, not asserted to be word boundaries; `sync` is `segment` if at least one line has such segments, otherwise `line`. Caption text has `♪` removed and whitespace (including embedded newlines) collapsed; lines that are only a bracketed label, such as `[Music]`, `[RINGING SOUND]`, `(applause)` or `[संगीत]`, are dropped. Segment text keeps its leading spaces so segments still join back into the line. Review rolling captions, overlaps, malformed events, line grouping and partial offsets before relying on this conversion.

## Evidence gathered before implementation

Read-only tests ran on the VM using the existing stored login. These tested upstream calls directly, not the new endpoint:

- Of six YouTube Music song results (Blinding Lights, Shape of You, Bohemian Rhapsody, Tum Hi Ho, Kesariya, Ajnabi), five returned unsynced lyrics text; Ajnabi had no lyrics reference.
- Those six song-video IDs exposed no captions through that yt-dlp run. Separate music-video searches found timed captions for several tracks.
- Shape of You's official video produced an automatic JSON3 track with 79 text events and 514 segment offsets; no transcription or timing accuracy review was performed.
- The video search for Tum Hi Ho returned a different song. The Kesariya video result was a shorter version. Blinding Lights had a 263-second video versus a 202-second song. These results show why the implementation does not search for a substitute video.
- The Ji Huzoor example video yielded 66 JSON3 events with no segment offsets in the tested track. The extractor called it subtitles, while the supplied browser capture had a `caps=asr` query; that discrepancy remains unexplained.
- Ajnabi's selected music video listed automatic caption tracks but the selected fetch failed with HTTPError.

These are small observations, not a reliability or accuracy estimate. Captions, sources, matching, authentication and upstream endpoints may change.

## Validation completed

From `flask-server`:

```sh
UV_CACHE_DIR=/tmp/jukes-uv-cache uv run --no-project --with-requirements requirements.txt --with pytest pytest -q tests/test_jukes_lyrics.py tests/test_jukes_api.py
```

Result: **61 passed**. The full suite gives 321 passed, 4 failed. The same 4 fail on base `e7e58ca` on macOS: two in fork_download, one in cache leases, and one bgutil deployment-config test. `git diff --check` is clean.

LRCLIB was checked live. `/api/get` returns `id`, `trackName`, `artistName`, `albumName`, `duration` (float seconds), `instrumental`, `hasWordSync`, `plainLyrics`, `syncedLyrics` and `lyricsfile`, matching the code. A decimal `duration` is accepted, and a miss returns 404.

Live service calls from a Mac (anonymous, no PO-token provider):

| Call | Result |
|---|---|
| lookup Shape of You, 234 s | LRCLIB line sync, id 1250, 0.6 s |
| captions, Shape of You MV `JGwWNGJdvx8`, no duration | 82 clean English lines, `subtitles`, 2.8 s |
| same MV with 234 s requested (video is 263 s) | rejected (None) |
| YouTube Music, Tum Hi Ho `fsiPzT50ZiM`, 262 s | plain text, 923 chars |
| same with 300 s requested | rejected (None) |
| LRCLIB miss, no video | None (404) |
| LRCLIB miss + Tum Hi Ho video | YouTube Music plain text, 4.1 s |

On the Azure VM (2026-11-12, with approval), the same service code ran in a throwaway process inside the live `ytmusic` container. It read the stored cookie (21 rows) and used bgutil at `127.0.0.1:4416`. Nothing was deployed or restarted, and the temporary files were removed:

| Call | Result |
|---|---|
| lookup Shape of You (LRCLIB) | line sync, 0.3 s |
| captions, MV, with cookie / anonymous | 82 lines, `subtitles`, 1.6 s / 1.4 s |
| YouTube Music Tum Hi Ho, with cookie / anonymous | 923 chars, 1.9 s / 1.0 s |
| LRCLIB miss + MV | captions, 1.7 s |
| LRCLIB miss + song video | YouTube Music text, 2.6 s |

The cookie made no difference for these public videos; it matters only where YouTube demands sign-in. The HTTP route itself is still exercised only through the Flask test client until the image is rebuilt.

Second VM run after adding YouTube Music timestamps and LRCLIB search. Twelve songs, each video ID and length taken from a YouTube Music song search:

| Song | LRCLIB | YouTube Music | Returned |
|---|---|---|---|
| Tum Hi Ho, Kesariya, Bohemian Rhapsody, Shape of You, Apna Bana Le, Kun Faya Kun, Chaleya, Heeriye | line | timed | LRCLIB line, 0.2–0.5 s |
| Blinding Lights, Pasoori | word | timed | LRCLIB word, 0.2 s |
| Ajnabi (Taba Chake), Ji Huzoor | miss | timed | YouTube Music timed, 1.5 s / 2.5 s |

YouTube Music had timed lyrics for all twelve. LRCLIB's exact lookup still matched with the length 4 s off, so its own tolerance is wider than the 2 s that was assumed. The search fallback therefore mainly helps title variants (for example "(From …)") rather than length. The caption path is now a last resort.

## Verification priorities

1. Review input validation, privacy and abuse resistance for a public endpoint that can trigger authenticated upstream requests. Check concurrency, rate limiting, worker blocking, timeouts, response-size limits, and treatment of caption URLs and redirects.
2. Verify actual LRCLIB field names, match behavior, duration tolerance, response errors, and the handling of `lyricsfile` and `hasWordSync`. Ensure returned metadata really identifies the intended recording.
3. Exercise the endpoint in an isolated test or rebuilt staging container with and without cookies, known LRCLIB hits/misses, captions, and YouTube Music fallback. Verify duration and exact-video matching against the actual recording, including absent or malformed watch metadata.
4. Verify timed JSON3 samples against audio: spaces, word/segment offsets, missing offsets, repeated/overlapping/rolling events, caption-only labels, language and transliteration. The source can be automatic transcription and no accuracy review has been performed.
5. Test fallback and error handling, especially a timedtext HTTP failure, invalid payload, oversized response, unavailable cookie, and LRCLIB outage with a successful or failed YouTube fallback. Check that clients can distinguish no lyrics from an upstream failure.
6. Decide the client contract for sync types, provenance, attribution, caching, locales and quality. Current clients have no implementation for this endpoint.
7. Design any LRCLIB contribution flow separately: source rights and attribution, review, correct recording and duration, Lyricsfile validation, proof-of-work challenge and publish request. Do not infer word synchronization merely from the presence of segment offsets.

LRCLIB's architecture describes `/api/get`, the challenge/publish flow, and `hasWordSync`: https://github.com/tranxuanthang/lrclib/blob/main/ARCHITECTURE.md. The local capture notes are in `../youtube-lyrics-calls.md` relative to the repository root's parent.


## Third VM run (2026-11-12): trust rules, upgrade and queue

This ran in a throwaway process inside the `ytmusic` container, with numpy in a temporary folder and the cache and queue in a temporary database. The production database was read only, for the cookie. Nothing was deployed, and the files were removed afterwards.

| Call | Result |
|---|---|
| Official subtitles for Rangeen (direct) | `8Ix-ekSoEdY`, offset 5.55 s, 40 lines, first at 21.33 s, 4.3 s |
| Rangeen with a 221 s length (old whole-second copy) | `timing_rejected=whole_seconds`, words returned, `upgrading` |
| The same, after the background job | aligned official subtitles from cache, 0.0 s |
| Khoj; Tum Hi Ho | LRCLIB line, 0.3 s |
| Jaana | retryable 404 at first; no official subtitles exist, so the miss is cached |
| Contribution queue | 1 pending item (Rangeen) under YouTube Music's signature |

Alignment on the server's m4a files first matched only 3 of 8 excerpts, because the repeated chorus matched its other repeats. Two changes fixed it: limiting the search to offsets where the whole song fits inside the video, and switching to spectral flux. After that, m4a gives 5.55 s and opus 5.51 s (encoder delay). A different song gives no match.

Khoj's LRCLIB timing was checked against Whisper anchors and is within 0.5 s. Its text is identical to Musixmatch.

On 2026-11-12 the corrected Rangeen record was published to LRCLIB by hand, from the official subtitles, at the owner's request. LRCLIB's search and ID endpoints still served the old text for a while afterwards, which looks like caching on LRCLIB's side.
