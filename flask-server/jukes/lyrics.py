"""Lyrics for a known recording: the best trustworthy timed source.

Order (the first trustworthy timed result wins):
1. LRCLIB exact lookup, then search (title variants such as "Song | Coke Studio …").
2. YouTube Music timed lyrics for the same video.
3. Captions on the same video (length within 3 s).
4. Plain text (LRCLIB, YouTube Music) is returned at once while a background job looks for
   hand-made subtitles on another upload of the same recording (an official video), proves
   it is the same recording by audio alignment, shifts the subtitles onto the song and
   caches that for the next request.

Rules learnt from real songs:
- Rangeen: every LRCLIB copy had whole-second stamps a minute off and wrong words. Timing
  that is whole-second only, runs past the song, or goes backwards is not trusted.
- A record claiming 167 s with stamps to 3:32 is inconsistent: not trusted.
- Official subtitles carry "lyric\\ntranslation"; only the lyric line is kept.
- Other videos of a song differ in length (intro, outro): only audio alignment maps them.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import http.cookiejar
import json
import time
import logging
import os
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .personalization import build_auth_headers
from .server_cookies import cookie_header, parse_netscape

log = logging.getLogger(__name__)
MAX_RESPONSE = 2_000_000
SEARCH_TOLERANCE_MS = 5_000   # LRCLIB search: record length vs the song
WORD_SYNC_TOLERANCE_MS = 2_000  # a word-synced copy wins only when its length is this close
LENGTH_TOLERANCE_MS = 3_000   # a YouTube video vs the song, without alignment
LANGUAGES = ('en', 'hi', 'ur')
CAPTION_RULES = 2             # bump when caption parsing changes: older cached captions are fetched again
HIT_TTL = 30 * 86400
MISS_TTL = 86400
UPGRADE_TTL = 15 * 60          # plain result cached while the background upgrade runs
OFFICIAL_CANDIDATES = 3        # other uploads checked per song
UPGRADES_PER_HOUR = 30
LRC_RE = re.compile(r'\[(\d+):(\d{1,2}(?:\.\d{1,3})?)\]\s*(.*)')
SOUND_LABEL = re.compile(r'\[[^\]]*\]')  # [Music], [संगीत], [Applause], also inside a line


def _norm(text):
    return ' '.join(re.sub(r'[^\w\s]', ' ', str(text or '')).casefold().split())


class _Silent:
    """yt-dlp still writes errors to stderr when quiet; drop them (they can carry URLs)."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


# -- timing checks ------------------------------------------------------

def lrc_lines(synced):
    """[(start_ms, text)] from LRC text."""
    out = []
    for raw in str(synced or '').splitlines():
        if m := LRC_RE.match(raw.strip()):
            out.append((round((int(m[1]) * 60 + float(m[2])) * 1000), m[3].strip()))
    return out


def timing_problem(stamps_ms, duration_ms=None):
    """Why timing is not trustworthy, or None."""
    stamps = list(stamps_ms)
    if len(stamps) < 2:
        return None
    if any(b < a - 500 for a, b in zip(stamps, stamps[1:])):
        return 'out_of_order'
    if duration_ms and stamps[-1] > duration_ms + 5_000:
        return 'past_end'
    if len(stamps) >= 6 and all(ms % 1000 == 0 for ms in stamps):
        return 'whole_seconds'  # hand-guessed: Rangeen's copies were up to 57 s off
    return None


def to_lrc(lines):
    """LRC from lines; a blank stamp marks an instrumental gap after a line."""
    def stamp(ms):
        m, s = divmod(max(ms, 0) / 1000, 60)
        return f'[{int(m):02d}:{s:05.2f}]'
    out = []
    for i, line in enumerate(lines):
        out.append(f"{stamp(line['start_ms'])} {line['text']}")
        end, nxt = line.get('end_ms'), (lines[i + 1]['start_ms'] if i + 1 < len(lines) else None)
        if end and (nxt is None or nxt - end > 4_000):
            out.append(f'{stamp(end)} ')
    return '\n'.join(out)


# -- captions -------------------------------------------------------------

def _first_alpha(text):
    return next((c for c in text if c.isalpha()), '')


def _bilingual(events):
    """Hand-made subtitles that pair each lyric line with a translation line."""
    pairs = []
    for event in events:
        rows = [r.strip() for r in ''.join(s.get('utf8', '') for s in event.get('segs') or []
                                           if isinstance(s, dict) and isinstance(s.get('utf8'), str))
                .replace('♪', '').split('\n') if r.strip()]
        if rows:
            pairs.append(rows)
    multi = [p for p in pairs if len(p) == 2]
    if not pairs or len(multi) < 0.6 * len(pairs):
        return False
    # A wrapped line continues in lower case; a translation starts a sentence or changes script.
    second = [p for p in multi if _first_alpha(p[1]).isupper()
              or _first_alpha(p[0]).isascii() != _first_alpha(p[1]).isascii()]
    return len(second) >= 0.8 * len(multi)


_EDGE = r'[\s.,;:~*_\-]'


def tidy_subtitle(text):
    """An uploader's decoration removed: "Dil Tere Agge.....", "ISHQ FITOOR,,,,", "*JI HUZOOR*".

    Lines written in capitals are shouting the same words as elsewhere in the song, so they are
    put in title case. Returns '' for a line without words, such as "....♡♡♡....".
    """
    text = text.replace('*', ' ')
    text = re.sub(r',{2,}', ',', re.sub(r'\.{2,}', '…', text))
    text = re.sub(f'^(?:{_EDGE}|…)+', '', text)
    text = re.sub(f'(?:{_EDGE}|…)+$', '', text)
    text = ' '.join(text.split())
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return ''
    if len(letters) >= 4 and all(c.isascii() for c in letters) and text == text.upper():
        text = ' '.join(word[:1] + word[1:].lower() for word in text.split(' '))
    return text


def caption_lines(payload, kind='automatic_captions'):
    """Lines with start/end times; segment offsets are kept, never claimed as word sync."""
    events = [e for e in payload.get('events', []) if isinstance(e, dict)]
    bilingual = kind == 'subtitles' and _bilingual(events)
    lines = []
    for event in events:
        start = event.get('tStartMs')
        segments = event.get('segs') or []
        if not isinstance(start, int) or start < 0 or not isinstance(segments, list):
            continue
        segments = [s for s in segments if isinstance(s, dict) and isinstance(s.get('utf8'), str)]
        if bilingual:
            rows = [r for r in ''.join(s['utf8'] for s in segments).replace('♪', '').split('\n') if r.strip()]
            text, segments = ' '.join(SOUND_LABEL.sub(' ', rows[0] if rows else '').split()), []
        else:
            segments = [{**s, 'utf8': SOUND_LABEL.sub('', s['utf8'].replace('♪', '').replace('\n', ' '))}
                        for s in segments]
            segments = [s for s in segments if s['utf8'].strip()]
            text = ' '.join(SOUND_LABEL.sub(' ', ''.join(s['utf8'] for s in segments)).split())
        # Drop sound labels such as [Music], [RINGING SOUND], (applause), [संगीत].
        if not text or re.fullmatch(r'[\[(][^\])]*[\])]', text):
            continue
        if kind == 'subtitles' and not any(isinstance(seg.get('tOffsetMs'), int) for seg in segments):
            # Hand-made subtitles carry the uploader's decoration; ASR captions don't.
            if not (text := tidy_subtitle(text)):
                continue
        line = {'text': text, 'start_ms': start}
        if isinstance(event.get('dDurationMs'), int) and event['dDurationMs'] > 0:
            line['end_ms'] = start + event['dDurationMs']
        if any(isinstance(s.get('tOffsetMs'), int) for s in segments):
            line['segments'] = [{'text': s['utf8'], 'start_ms': start + s.get('tOffsetMs', 0)}
                                for s in segments
                                if isinstance(s.get('tOffsetMs', 0), int) and s.get('tOffsetMs', 0) >= 0]
        lines.append(line)
    return lines


# -- audio for alignment ----------------------------------------------------

class _Sink:
    """Download destination for Extractor.download (it truncates between clients)."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.touch()

    def write(self, chunk):
        with open(self.path, 'ab') as handle:
            handle.write(chunk)


def audio_source(cache, extractor):
    """Context manager factory: a path to a video's audio, cached or a temporary download."""
    @contextlib.contextmanager
    def audio(video_id):
        from .models import AudioKey
        from .routes import AUDIO_POLICY
        key = AudioKey(video_id, AUDIO_POLICY)
        entry = cache.lookup(key) if cache else None
        if entry is not None:
            yield entry.path  # an eviction mid-read leaves the open inode readable
            return
        folder = tempfile.mkdtemp(prefix='jukes-lyrics-')
        try:
            result = extractor.download(key, _Sink(Path(folder) / 'audio'), None)
            yield result.path
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    return audio


class Lyrics:
    def __init__(self, cookies=None, store=None, audio=None, contributions=None, clock=None):
        import time
        self.cookies = cookies
        self.store = store                # LyricsStore: response cache
        self.audio = audio                # audio_source(...) for the alignment upgrade
        self.contributions = contributions
        self.clock = clock or time.time
        self._permits = threading.BoundedSemaphore(3)
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='lyrics-upgrade')
        self._pending = set()
        self._upgrades = []               # start times in the last hour
        self._lock = threading.Lock()

    def _rows(self):
        snapshot = self.cookies.snapshot() if self.cookies else None
        return parse_netscape(snapshot.cookie_jar_text) if snapshot and snapshot.cookie_jar_text else []

    # -- LRCLIB -----------------------------------------------------------
    @staticmethod
    def _lrclib_fetch(path, params):
        url = f'https://lrclib.net/api/{path}?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'JUKES/1.0 (lyrics lookup)', 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ValueError('LRCLIB response too large')
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def _lrclib(self, title, artist, album, duration):
        params = {'track_name': title, 'artist_name': artist}
        if album:
            params['album_name'] = album
        if duration:
            params['duration'] = str(duration / 1000)
        exact = self._record(self._lrclib_fetch('get', params), duration)
        if exact and exact['sync'] == 'word':
            return exact
        # A line-timed exact match still searches: another copy may have word timings.
        # /api/get misses title variants. Search ignores length; accept the closest record
        # within SEARCH_TOLERANCE_MS, or an exact title+artist match without a length.
        try:
            found = self._lrclib_fetch('search', {'track_name': title, 'artist_name': artist})
        except Exception:
            if exact:
                return exact  # keep the plain record the exact lookup found
            raise
        candidates = []
        for data in found if isinstance(found, list) else []:
            record = self._record(data, duration)
            if not record:
                continue
            length = record['track']['duration_ms']
            want, got = _norm(title), _norm(data.get('trackName'))
            # One title must start with the other: allows "Song (From …)", "Song - Remastered".
            if not want or not got or not (got.startswith(want) or want.startswith(got)):
                continue  # search is fuzzy: never take a different song by the same artist
            if duration:
                if length is None or abs(length - duration) > SEARCH_TOLERANCE_MS:
                    continue
                gap = abs(length - duration)
            elif want == got and _norm(data.get('artistName')) == _norm(artist):
                gap = 0
            else:
                continue
            # timed first; word-timed when its length is as close as a word highlight needs; then closest
            words = record['sync'] == 'word' and gap <= WORD_SYNC_TOLERANCE_MS
            candidates.append((record['sync'] == 'none', not words, gap, record))
        if not candidates:
            return exact
        best = min(candidates, key=lambda c: c[:3])
        if exact and (best[0] or (exact['sync'] != 'none' and best[1])):
            return exact  # the search found nothing better than the exact match
        return best[3]

    @staticmethod
    def _record(data, duration=None):
        if not isinstance(data, dict):
            return None
        length = round(data['duration'] * 1000) if isinstance(data.get('duration'), (int, float)) else None
        synced, plain = data.get('syncedLyrics'), data.get('plainLyrics')
        problem = None
        if synced and not data.get('instrumental'):
            stamps = [ms for ms, _ in lrc_lines(synced)]
            problem = timing_problem(stamps, length) or timing_problem(stamps, duration)
        if data.get('instrumental'):
            sync = 'instrumental'
        elif problem:
            sync = 'none'  # keep the words, never the untrustworthy timing
            plain = plain or '\n'.join(text for _, text in lrc_lines(synced) if text)
        elif data.get('hasWordSync') and data.get('lyricsfile'):
            sync = 'word'
        elif synced:
            sync = 'line'
        elif plain:
            sync = 'none'
        else:
            return None
        record = {'source': 'lrclib', 'sync': sync, 'verified': False,
                  'track': {'title': data.get('trackName'), 'artist': data.get('artistName'),
                            'album': data.get('albumName'), 'duration_ms': length},
                  'plain_lyrics': plain, 'synced_lyrics': None if problem else synced,
                  'lyricsfile': None if problem else data.get('lyricsfile'), 'lrclib_id': data.get('id')}
        if problem:
            record['timing_rejected'] = problem
        return record

    # -- YouTube ------------------------------------------------------------
    @contextlib.contextmanager
    def _ydl(self, rows, **extra):
        import yt_dlp
        opts = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'noplaylist': True,
                'socket_timeout': 8, 'extractor_retries': 0, 'retries': 0, 'logger': _Silent(), **extra}
        # Same PO-token provider as the audio extractor; timedtext can require one.
        if (pot := os.environ.get('YTDLP_BGUTIL_BASE_URL', 'http://bgutil-provider:4416')):
            opts['extractor_args'] = {'youtubepot-bgutilhttp': {'base_url': [pot]}}
        with yt_dlp.YoutubeDL(opts) as downloader:
            for domain, include, path, secure, expiry, name, value in rows:
                domain = domain.removeprefix('#HttpOnly_')
                downloader.cookiejar.set_cookie(http.cookiejar.Cookie(
                    0, name, value, None, False, domain, include == 'TRUE', domain.startswith('.'), path,
                    True, secure == 'TRUE', int(expiry) or None, False, None, None, {}, False))
            yield downloader

    @staticmethod
    def _tracks(downloader, info, kinds):
        """Caption lines of the first usable track: (lines, language, kind), or None."""
        attempts = failures = 0
        # yt-dlp marks the ASR track in the sung language "<lang>-orig". When it is known, only
        # tracks in that language count: a Hindi song gets Hindi (or Urdu, the same speech) lyrics,
        # never an English translation, even one an uploader typed in.
        sung = next((lang.lower().split('-')[0] for lang in info.get('automatic_captions') or {}
                     if lang.endswith('-orig')), None)
        order = {'en': ['en'], 'hi': ['hi', 'ur'], 'ur': ['ur', 'hi']}.get(sung, list(LANGUAGES))

        def own(fmts):
            # Machine translations (Hindi ASR shown as English) carry tlang= in the URL.
            return next((x for x in fmts if x.get('ext') == 'json3' and 'tlang' not in
                         urllib.parse.parse_qs(urllib.parse.urlsplit(x.get('url') or '').query)), None)

        for kind in kinds:
            tracks = info.get(kind) or {}
            found = {}
            for lang, fmts in tracks.items():
                base = lang.lower().split('-')[0]
                if base in order and base not in found and (fmt := own(fmts or [])):
                    found[base] = (lang.removesuffix('-orig'), fmt)
            for base in sorted(found, key=order.index):
                lang, fmt = found[base]
                attempts += 1
                for retry in range(2):  # timedtext sometimes fails once (seen on Ji Huzoor, 18m9ovEsP6s)
                    try:
                        with downloader.urlopen(fmt['url']) as response:
                            raw = response.read(MAX_RESPONSE + 1)
                        break
                    except Exception as exc:
                        log.warning('Caption track fetch failed: %s %s', type(exc).__name__,
                                    getattr(exc, 'status', None) or getattr(exc, 'code', '') or '')
                        if retry == 0:
                            time.sleep(1)
                else:
                    failures += 1
                    continue
                if len(raw) > MAX_RESPONSE:
                    continue
                try:
                    if lines := caption_lines(json.loads(raw), kind):
                        return lines, lang, kind
                except ValueError:
                    failures += 1
                    log.warning('Caption track was not JSON')
        if attempts and failures == attempts:
            # Every track errored: an upstream failure (502), not "no lyrics" (404).
            raise OSError('all caption fetches failed')
        return None

    def _youtube(self, video_id, duration, rows):
        with self._ydl(rows) as downloader:
            # process=False: captions and duration only; no format selection that can fail.
            info = downloader.extract_info('https://www.youtube.com/watch?v=' + video_id, download=False, process=False)
            actual = info.get('duration')
            if duration and (not isinstance(actual, (int, float)) or abs(actual * 1000 - duration) > LENGTH_TOLERANCE_MS):
                return None
            found = self._tracks(downloader, info, ('subtitles', 'automatic_captions'))
        if not found:
            return None
        lines, lang, kind = found
        result = {'source': 'youtube_captions',
                  'sync': 'segment' if any(line.get('segments') for line in lines) else 'line',
                  'verified': False, 'video_id': video_id, 'language': lang, 'caption_kind': kind,
                  'caption_rules': CAPTION_RULES,
                  'duration_ms': round(actual * 1000) if isinstance(actual, (int, float)) else None,
                  'lines': lines, 'plain_lyrics': '\n'.join(line['text'] for line in lines)}
        return self._checked(result, duration)

    @staticmethod
    def _checked(result, duration):
        """Timed lines that fail the timing rules are returned as text only."""
        if result.get('lines') and (problem := timing_problem([l['start_ms'] for l in result['lines']], duration)):
            return {**{k: v for k, v in result.items() if k != 'lines'}, 'sync': 'none', 'timing_rejected': problem}
        return result

    def _watch(self, video_id, rows):
        from ytmusicapi import YTMusic
        auth = build_auth_headers(cookie_header(rows), 0) if rows else None
        yt = YTMusic(auth=auth, language='en', location='IN')
        watch = yt.get_watch_playlist(videoId=video_id, limit=1)
        track = next((t for t in watch.get('tracks', []) if t.get('videoId') == video_id), {})
        seconds = 0
        try:
            for part in (track.get('length') or track.get('duration') or '').split(':'):
                seconds = seconds * 60 + int(part)
        except (ValueError, AttributeError):
            seconds = 0
        return yt, watch, track, seconds

    def track_metadata(self, video_id, rows=None):
        """YouTube Music's own title/artists/album/length: the LRCLIB signature apps will send."""
        _, _, track, seconds = self._watch(video_id, self._rows() if rows is None else rows)
        album = track.get('album')
        return {'title': track.get('title'), 'duration_s': seconds,
                'artist': ', '.join(a.get('name', '') for a in track.get('artists') or [] if a.get('name')),
                'album': album.get('name') if isinstance(album, dict) else album}

    def _music(self, video_id, duration, rows):
        yt, watch, _, seconds = self._watch(video_id, rows)
        if duration and (not seconds or abs(seconds * 1000 - duration) > LENGTH_TOLERANCE_MS):
            return None
        ref = watch.get('lyrics')
        # Timed lyrics belong to the song itself, so they line up with its audio.
        data = yt.get_lyrics(ref, timestamps=True) if ref else None
        body = data.get('lyrics') if data else None
        base = {'source': 'youtube_music', 'verified': False, 'video_id': video_id,
                'attribution': data.get('source') if data else None}
        if data and data.get('hasTimestamps') and isinstance(body, list):
            lines = []
            for line in body:
                text = ' '.join(str(getattr(line, 'text', '') or '').replace('♪', '').split())
                start, end = getattr(line, 'start_time', None), getattr(line, 'end_time', None)
                if text and isinstance(start, int) and start >= 0:
                    lines.append({'text': text, 'start_ms': start,
                                  **({'end_ms': end} if isinstance(end, int) and end >= start else {})})
            if not lines:
                return None
            return self._checked({**base, 'sync': 'line', 'lines': lines,
                                  'plain_lyrics': '\n'.join(line['text'] for line in lines)}, duration)
        if not isinstance(body, str) or not body.strip():
            return None
        return {**base, 'sync': 'none', 'plain_lyrics': body}

    # -- official subtitles on another upload (background) ------------------
    def _official(self, title, artist, video_id, duration, rows):
        from .lyrics_align import envelope, offset
        want_title, want_artist = _norm(title), _norm(artist.split(',')[0])
        with self._ydl(rows, extract_flat='in_playlist') as downloader:
            found = downloader.extract_info(f'ytsearch8:{title} {artist}', download=False) or {}
        candidates = []
        for entry in found.get('entries') or []:
            length = entry.get('duration')
            name = _norm(f"{entry.get('title', '')} {entry.get('channel', '')}")
            if (entry.get('id') and entry['id'] != video_id and isinstance(length, (int, float))
                    and duration / 1000 - 1 <= length <= duration / 1000 + 120
                    and want_title in name and want_artist in name):
                candidates.append(entry['id'])
        song_env = None
        for candidate in candidates[:OFFICIAL_CANDIDATES]:
            with self._ydl(rows) as downloader:
                info = downloader.extract_info('https://www.youtube.com/watch?v=' + candidate, download=False, process=False)
                found = self._tracks(downloader, info, ('subtitles',))  # hand-made only
            if not found or len(found[0]) < 4:
                continue
            if song_env is None:
                with self.audio(video_id) as path:
                    song_env = envelope(path)
            with self.audio(candidate) as path:
                shift = offset(song_env, envelope(path))
            if shift is None:
                continue  # not the same recording (live, remix, edit)
            lines, lang, _ = found
            moved = []
            for line in lines:
                start = line['start_ms'] - round(shift * 1000)
                if 0 <= start < duration:
                    item = {'text': line['text'], 'start_ms': start}
                    if 'end_ms' in line:
                        item['end_ms'] = min(line['end_ms'] - round(shift * 1000), duration)
                    moved.append(item)
            if len(moved) < 4 or timing_problem([l['start_ms'] for l in moved], duration):
                continue
            return {'source': 'youtube_subtitles', 'sync': 'line', 'verified': False,
                    'video_id': video_id, 'subtitle_video_id': candidate, 'offset_ms': round(shift * 1000),
                    'language': lang, 'caption_kind': 'subtitles', 'caption_rules': CAPTION_RULES, 'lines': moved,
                    'plain_lyrics': '\n'.join(line['text'] for line in moved)}
        return None

    def _upgrade(self, key, title, artist, video_id, duration, fallback):
        try:
            rows = self._rows()
            better = self._official(title, artist, video_id, duration, rows)
        except Exception:
            log.warning('Lyrics upgrade failed')
            rows, better = [], None
        final = better or fallback
        if self.store:
            self.store.put(key, final, HIT_TTL if final else MISS_TTL)
        if better and self.contributions:
            try:
                self.contributions.offer(video_id, better, self.track_metadata(video_id, rows))
            except Exception:
                log.warning('Lyrics contribution offer failed')
        with self._lock:
            self._pending.discard(key)

    def _schedule(self, key, *args):
        if not (self.audio and self.store):
            return False
        now = self.clock()
        with self._lock:
            if key in self._pending:
                return True
            self._upgrades = [t for t in self._upgrades if now - t < 3600]
            if len(self._upgrades) >= UPGRADES_PER_HOUR:
                return False
            self._upgrades.append(now)
            self._pending.add(key)
        self._worker.submit(self._upgrade, key, *args)
        return True

    # -- lookup -----------------------------------------------------------
    @staticmethod
    def cache_key(title, artist, album, duration, video_id):
        # A video ID is the recording; metadata-only lookups also key on the length.
        seconds = round(duration / 1000) if duration else ''
        return f'v:{video_id}' if video_id else f'm:{_norm(title)}|{_norm(artist)}|{_norm(album)}|{seconds}'

    def _resolve(self, title, artist, album, duration, video_id):
        failed = False
        plain = []  # untimed fallbacks, best first
        sources = [lambda: self._lrclib(title, artist, album, duration)]
        if video_id:
            try:
                rows = self._rows()
            except Exception:
                log.warning('YouTube credentials unavailable')
                rows = []
            sources += [lambda: self._music(video_id, duration, rows),
                        lambda: self._youtube(video_id, duration, rows)]
        for source in sources:
            try:
                result = source()
            except Exception:
                failed = True
                log.warning('Lyrics source failed')
                continue
            if result and self.contributions and (result.get('source') == 'youtube_music' or (
                    result.get('source') == 'youtube_captions' and result['sync'] != 'none')):
                try:
                    self.contributions.offer(video_id, result, self.track_metadata(video_id, rows))
                except Exception:
                    log.warning('Lyrics contribution offer failed')
            if result and result['sync'] != 'none':
                return result, failed
            if result:
                plain.append(result)
        return (plain[0] if plain else None), failed

    def lookup(self, title, artist, album='', duration=None, video_id=''):
        from .routes import ApiError
        key = self.cache_key(title, artist, album, duration, video_id)
        if self.store and (hit := self.store.get(key)) is not self.store.MISS and not (
                hit and hit.get('source') in ('youtube_captions', 'youtube_subtitles')
                and hit.get('caption_rules') != CAPTION_RULES):
            if hit is None:
                return None
            return hit
        if not self._permits.acquire(blocking=False):
            raise ApiError(503, 'lyrics_busy', 'lyrics service is busy', True)
        try:
            result, failed = self._resolve(title, artist, album, duration, video_id)
        finally:
            self._permits.release()
        if result is None and failed:
            raise ApiError(502, 'lyrics_upstream_error', 'lyrics source unavailable', True)
        if video_id and duration and (result is None or result['sync'] == 'none') and self.audio and self.store:
            placeholder = {**result, 'upgrading': True} if result else None
            # Written before the job starts: a fast job must not be overwritten by it.
            self.store.put(key, placeholder, UPGRADE_TTL)
            if self._schedule(key, title, artist, video_id, duration, result):
                if result is None:
                    raise ApiError(404, 'lyrics_not_found', 'lyrics not found yet', True, '60')
                return placeholder
        if self.store and not failed:
            self.store.put(key, result, HIT_TTL if result else MISS_TTL)
        return result
