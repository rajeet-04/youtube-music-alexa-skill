"""Lyrics response cache and the LRCLIB contribution queue (same SQLite database).

Contributions include audio-aligned subtitles, YouTube Music lyrics and timed captions. A song
is published only when LRCLIB has no equivalent or better record for its signature: publishing
to an existing signature replaces that record's lyrics. Captions (often machine-made) only go
where LRCLIB has nothing or its timing fails the checks.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3

# Runs in a child process: a few seconds of hashing would otherwise hold the GIL.
_SOLVER = """
import hashlib, sys
prefix, target = sys.argv[1], bytes.fromhex(sys.argv[2])
n = 0
while hashlib.sha256(f'{prefix}{n}'.encode()).digest() > target:
    n += 1
print(n)
"""


def to_lyricsfile(meta, lines, plain):
    """LRCLIB's YAML lyricsfile (version 1.0): line end times, and word timings when a line has
    them (`words`: text with its trailing space, start_ms). LRCLIB keeps it as given and marks
    word-synced records. Strings are JSON-quoted, which YAML reads as double-quoted scalars."""
    q = lambda v: json.dumps(v, ensure_ascii=False)
    out = ["version: '1.0'", 'metadata:', f"  title: {q(meta['title'])}", f"  artist: {q(meta['artist'])}",
           f"  album: {q(meta.get('album') or '')}", f"  duration_ms: {int(meta['duration_s'] * 1000)}",
           '  instrumental: false', 'lines:']
    for line in lines:
        out += [f"- text: {q(line['text'])}", f"  start_ms: {int(line['start_ms'])}"]
        if isinstance(line.get('end_ms'), int):
            out.append(f"  end_ms: {line['end_ms']}")
        words = [w for w in line.get('words') or [] if w.get('text') and isinstance(w.get('start_ms'), int)]
        if words:
            out.append('  words:')
            for word in words:
                out += [f"  - text: {q(word['text'])}", f"    start_ms: {word['start_ms']}"]
    out.append(f'plain: {q(plain)}')
    return '\n'.join(out) + '\n'


class LyricsStore:
    MISS = object()

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        with store.transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS jukes_lyrics (
                    key TEXT PRIMARY KEY, result TEXT, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jukes_lyrics_contrib (
                    video_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'published', 'skipped', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0, detail TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
            """)

    # -- cache --------------------------------------------------------------
    def get(self, key):
        with self.store.connection() as connection:
            row = connection.execute('SELECT result, expires_at FROM jukes_lyrics WHERE key = ?', (key,)).fetchone()
        if row is None or row['expires_at'] < self.clock():
            return self.MISS
        return json.loads(row['result']) if row['result'] else None

    def put(self, key, result, ttl):
        now = self.clock()
        with self.store.transaction() as connection:
            connection.execute('INSERT OR REPLACE INTO jukes_lyrics VALUES (?, ?, ?)',
                               (key, json.dumps(result, ensure_ascii=False) if result else None, now + ttl))
            connection.execute('DELETE FROM jukes_lyrics WHERE expires_at < ?', (now,))

    # -- contribution queue --------------------------------------------------
    def offer(self, video_id, result, meta):
        if not (meta.get('title') and meta.get('artist') and meta.get('duration_s')):
            return
        now = self.clock()
        lines = result.get('lines') or []
        plain = result.get('plain_lyrics') or '\n'.join(line['text'] for line in lines)
        if not plain.strip():
            return
        payload = json.dumps({'meta': meta, 'lines': lines, 'plain_lyrics': plain, 'source': result['source'],
                              'attribution': result.get('attribution'),
                              'subtitle_video_id': result.get('subtitle_video_id')}, ensure_ascii=False)
        with self.store.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO jukes_lyrics_contrib VALUES (?, ?, 'pending', 0, NULL, ?, ?)",
                               (video_id, payload, now, now))
            if lines:
                # Upgrade an unprocessed plain candidate without replacing a timed one.
                row = connection.execute("SELECT payload FROM jukes_lyrics_contrib WHERE video_id = ? AND state = 'pending'", (video_id,)).fetchone()
                if row and not json.loads(row['payload']).get('lines'):
                    connection.execute("UPDATE jukes_lyrics_contrib SET payload = ?, updated_at = ? WHERE video_id = ? AND state = 'pending'", (payload, now, video_id))

    def next_pending(self):
        with self.store.connection() as connection:
            row = connection.execute("SELECT video_id, payload, attempts FROM jukes_lyrics_contrib "
                                     "WHERE state = 'pending' ORDER BY created_at LIMIT 1").fetchone()
        return (row['video_id'], json.loads(row['payload']), row['attempts']) if row else None

    def mark(self, video_id, state, detail=None, attempts=None):
        with self.store.transaction() as connection:
            connection.execute('UPDATE jukes_lyrics_contrib SET state = ?, detail = ?, attempts = COALESCE(?, attempts), '
                               'updated_at = ? WHERE video_id = ?', (state, detail, attempts, self.clock(), video_id))

    def counts(self):
        with self.store.connection() as connection:
            return dict(connection.execute('SELECT state, COUNT(*) FROM jukes_lyrics_contrib GROUP BY state').fetchall())


class Publisher:
    """Publishes queued contributions to LRCLIB, one per interval. Off unless enabled."""

    def __init__(self, lyrics_store, interval=600, post=None, fetch=None):
        from .lyrics import Lyrics
        self.queue, self.interval = lyrics_store, interval
        self.post = post or self._post
        self.fetch = fetch or Lyrics._lrclib_fetch

    @staticmethod
    def _post(path, body, headers=None):
        req = urllib.request.Request('https://lrclib.net/api/' + path, data=json.dumps(body).encode(), method='POST',
                                     headers={'User-Agent': 'JUKES/1.0 (lyrics contribution)',
                                              'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, response.read(100_000)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(10_000)

    def run_once(self):
        """Handle the oldest pending contribution; returns its new state or None."""
        from .lyrics import Lyrics, timing_problem, to_lrc
        item = self.queue.next_pending()
        if item is None:
            return None
        video_id, payload, attempts = item
        meta, lines = payload['meta'], payload.get('lines') or []
        signature = {'track_name': meta['title'], 'artist_name': meta['artist'],
                     'album_name': meta.get('album') or '', 'duration': str(meta['duration_s'])}
        try:
            existing = Lyrics._record(self.fetch('get', signature), meta['duration_s'] * 1000)
            if existing and existing['sync'] in ('line', 'word', 'instrumental'):
                self.queue.mark(video_id, 'skipped', 'lrclib_has_timed')
                return 'skipped'
            # Machine captions only fill a gap: no record, or one whose timing fails the checks.
            if existing and (not lines or (payload.get('source') == 'youtube_captions'
                                           and not existing.get('timing_rejected'))):
                self.queue.mark(video_id, 'skipped', 'lrclib_has_lyrics')
                return 'skipped'
            if lines and (timing_problem([l['start_ms'] for l in lines], meta['duration_s'] * 1000) or len(lines) < 4):
                self.queue.mark(video_id, 'skipped', 'timing_check')
                return 'skipped'
            status, raw = self.post('request-challenge', {})
            challenge = json.loads(raw)
            nonce = subprocess.run([sys.executable, '-c', _SOLVER, challenge['prefix'], challenge['target']],
                                   capture_output=True, text=True, timeout=300, check=True).stdout.strip()
            body = {'trackName': meta['title'], 'artistName': meta['artist'], 'albumName': meta.get('album') or '',
                    'duration': meta['duration_s'], 'plainLyrics': payload.get('plain_lyrics') or '\n'.join(l['text'] for l in lines)}
            if lines:
                body['syncedLyrics'] = to_lrc(lines)
                body['lyricsfile'] = to_lyricsfile(meta, lines, body['plainLyrics'])
            status, _ = self.post('publish', body, {'X-Publish-Token': f"{challenge['prefix']}:{nonce}"})
            if status == 201:
                self.queue.mark(video_id, 'published')
                log.info('Published lyrics contribution')
                return 'published'
            detail = f'http_{status}'
        except Exception as exc:
            detail = type(exc).__name__
        state = 'failed' if attempts + 1 >= MAX_ATTEMPTS else 'pending'
        self.queue.mark(video_id, state, detail, attempts + 1)
        log.warning('Lyrics contribution attempt failed: %s', detail)
        return state

    def start(self):
        def loop():
            while True:
                time.sleep(self.interval)
                try:
                    self.run_once()
                except Exception:
                    log.warning('Lyrics contribution loop error')
        threading.Thread(target=loop, name='lyrics-contrib', daemon=True).start()
