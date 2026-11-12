# Lyrics lookup

`GET /v1/lyrics?title=Rangeen&artist=Hasan%20Raheem&duration_ms=222000&video_id=J78lINC__HE`

`title` and `artist` are required. `album`, `duration_ms` and `video_id` are optional. Send the video ID that is actually playing and its length: most rules below need both.

## Order

The first trustworthy timed result is returned:

1. **LRCLIB.** An exact lookup is tried first, then a search. A search result is accepted when its title starts with the requested title (or the reverse), for example "Rangeen | Coke Studio Nu.WAV", and its length is within 5 s.
2. **YouTube Music timed lyrics** for the same video. These are timed to the song itself.
3. **Captions on the same video**, accepted only when its length is within 3 s.
4. **Plain text**, from LRCLIB and then YouTube Music.

When the result is plain text, or there is no result, the response is returned at once. A background job then looks for **hand-made subtitles on another upload of the same recording**, such as an official video. It proves that the upload contains the same recording by audio alignment and shifts the subtitles onto the song. The result is cached, so the next request gets it.

## Trust rules

| Rule | Found on |
|---|---|
| LRCLIB timing with only whole-second stamps (six or more lines) is rejected; the words are kept. | Rangeen: four copies, up to 57 s off. |
| Timing that runs more than 5 s past the song or record length is rejected. | A Rangeen copy claiming 167 s with stamps to 3:32. |
| Timing that goes backwards is rejected. | |
| Captions: `♪` is removed, wrapped lines are joined, and lines that are only a sound label (`[Music]`, `[RINGING SOUND]`) are dropped. | Shape of You music video |
| Bilingual subtitles (a lyric line plus a translation line) keep only the lyric line. | Coke Studio's official Rangeen video |
| Another upload is used only if audio alignment finds one constant offset: at least 6 of 8 song excerpts within 0.1 s, searched only where the whole song fits inside the video. | The repeated Rangeen chorus matched its other repeats until the search range was limited. |
| A video of a different length is never used without that alignment. | Blinding Lights: 263 s video vs 202 s song |

Rejected timing is reported as `timing_rejected` (`whole_seconds`, `past_end` or `out_of_order`), and that result is returned as text only.

## Response

Every response contains `source`, `sync` and `verified`. The possible values are:

- `source`: `lrclib`, `youtube_music`, `youtube_captions` or `youtube_subtitles`.
- `sync`: `word`, `segment`, `line`, `none` or `instrumental`.
- `verified`: always `false`. No human has reviewed the result.

Other fields depend on the source:

- **LRCLIB:** `plain_lyrics`, `synced_lyrics` (LRC), `lyricsfile`, `lrclib_id` and `track`.
- **YouTube results:** `video_id`, `plain_lyrics` and, when timed, `lines` (each with `text`, `start_ms` and an optional `end_ms`).
- **Aligned subtitles:** additionally `subtitle_video_id`, `offset_ms` and `language`.

`upgrading: true` means a better version may be available on a later request.

## Status codes

| Status | Meaning |
|---|---|
| 200 | A result |
| 404 `lyrics_not_found` | Nothing found. If the background search is running, the error is `retryable` with `Retry-After: 60`. |
| 502 `lyrics_upstream_error` | A source failed and nothing usable was found. Retryable. |
| 503 `lyrics_busy` | Three lookups are already running. Retryable. |
| 429 | More than 20 lookups a minute from one client |

Results are cached in the server database: hits for 30 days, misses for 1 day, and a placeholder for 15 minutes while an upgrade runs. At most 30 upgrades start per hour.

## Contributions to LRCLIB

Aligned official-subtitle results and YouTube Music lyrics (including Musixmatch or LyricFind results) are queued in `jukes_lyrics_contrib`, keyed by YouTube Music's own title, artists, album and length. Source attribution is retained in the queue. Both timed and plain lyrics are eligible. The publisher runs only with `JUKES_LYRICS_CONTRIBUTE=1`, at most one item every `JUKES_LYRICS_CONTRIBUTE_SECONDS` (default 600). Before publishing, it checks LRCLIB again:

- If LRCLIB already has trustworthy timing under that signature, the item is skipped.
- A plain-only candidate is also skipped if LRCLIB already has a record; otherwise it is published as plain lyrics without invented timing.
- If LRCLIB has no record, or only untrustworthy timing, the lyrics are published. Publishing to an existing signature replaces that record's lyrics.

The proof-of-work challenge runs in a child process. An item is marked `failed` after 3 failed attempts.
