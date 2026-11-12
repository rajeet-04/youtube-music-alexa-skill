import json

import pytest

from jukes.lyrics import CAPTION_RULES, Lyrics, caption_lines
from jukes.routes import ApiError


def test_caption_offsets_preserve_segments_and_skip_labels():
    result = caption_lines({'events': [
        {'tStartMs': 1000, 'segs': [{'utf8': '[music]'}]},
        {'tStartMs': 2000, 'segs': [{'utf8': '[RINGING SOUND]'}]},
        {'tStartMs': 3000, 'segs': [{'utf8': '[♪♪♪]'}]},
        {'tStartMs': 4000, 'segs': [{'utf8': '♪ The club isn\'t\nthe best ♪'}]},
        {'tStartMs': 20480, 'dDurationMs': 3400, 'segs': [
            {'utf8': 'हम'}, {'utf8': ' नहीं', 'tOffsetMs': 400}]},
    ]})
    assert result[0] == {'text': "The club isn't the best", 'start_ms': 4000}
    assert result[1:] == [{'text': 'हम नहीं', 'start_ms': 20480, 'end_ms': 23880, 'segments': [
        {'text': 'हम', 'start_ms': 20480}, {'text': ' नहीं', 'start_ms': 20880}]}]


def test_lookup_prioritizes_lrclib_and_uses_video_fallback(monkeypatch):
    service = Lyrics()
    monkeypatch.setattr(service, '_lrclib', lambda *args: {'source': 'lrclib', 'sync': 'line'})
    monkeypatch.setattr(service, '_youtube', lambda *args: pytest.fail('should not call YouTube'))
    assert service.lookup('title', 'artist', video_id='abcdefghijk')['source'] == 'lrclib'
    monkeypatch.setattr(service, '_lrclib', lambda *args: None)
    monkeypatch.setattr(service, '_music', lambda *args: None)
    monkeypatch.setattr(service, '_youtube', lambda *args: {'source': 'youtube_captions', 'sync': 'line'})
    assert service.lookup('title', 'artist', video_id='abcdefghijk')['source'] == 'youtube_captions'
    assert service.lookup('title', 'artist') is None


def test_upstream_failures_are_not_reported_as_not_found(monkeypatch):
    service = Lyrics()
    def fail(*args):
        raise OSError('private upstream details')
    monkeypatch.setattr(service, '_lrclib', fail)
    with pytest.raises(ApiError) as err:
        service.lookup('title', 'artist')
    assert err.value.status == 502
    assert 'private' not in str(err.value)


def test_lrclib_parsing(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            return json.dumps({'id': 1, 'trackName': 'Song', 'artistName': 'Artist',
                               'duration': 210, 'plainLyrics': 'text', 'syncedLyrics': '[00:01]text'}).encode()
    monkeypatch.setattr('urllib.request.urlopen', lambda *args, **kwargs: Response())
    result = Lyrics().lookup('Song', 'Artist', duration=210000)
    assert result['source'] == 'lrclib'
    assert result['sync'] == 'line'
    assert result['track']['duration_ms'] == 210000



def test_all_caption_fetches_failing_is_upstream_error(monkeypatch):
    import yt_dlp

    class FakeDL:
        def __init__(self, opts): self.cookiejar = type('J', (), {'set_cookie': lambda *a: None})()
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download, process):
            return {'duration': 200, 'automatic_captions': {'en': [{'ext': 'json3', 'url': 'u'}]}}
        def urlopen(self, url): raise OSError('timedtext 429')

    monkeypatch.setattr(yt_dlp, 'YoutubeDL', FakeDL)
    service = Lyrics()
    monkeypatch.setattr(service, '_lrclib', lambda *args: None)
    monkeypatch.setattr(service, '_music', lambda *args: None)
    with pytest.raises(ApiError) as err:
        service.lookup('title', 'artist', duration=200000, video_id='abcdefghijk')
    assert err.value.status == 502


def _lrclib_replies(monkeypatch, get, search):
    def fake(path, params):
        return get if path == 'get' else search
    monkeypatch.setattr(Lyrics, '_lrclib_fetch', staticmethod(fake))


def test_lrclib_search_takes_closest_length_and_same_title(monkeypatch):
    rec = lambda i, name, sec: {'id': i, 'trackName': name, 'artistName': 'Artist', 'duration': sec,
                                'plainLyrics': 'x', 'syncedLyrics': '[00:01.00] x'}
    _lrclib_replies(monkeypatch, None, [
        rec(1, 'Other Song', 200), rec(2, 'Song', 196), rec(3, 'Song (From "Film")', 201), rec(4, 'Song', 230)])
    assert Lyrics()._lrclib('Song', 'Artist', '', 200000)['lrclib_id'] == 3   # 1 s off; id 1 is another song
    _lrclib_replies(monkeypatch, None, [rec(4, 'Song', 230)])
    assert Lyrics()._lrclib('Song', 'Artist', '', 200000) is None              # 30 s off
    _lrclib_replies(monkeypatch, None, [rec(5, 'song', 230), rec(6, 'Song Remix', 210)])
    assert Lyrics()._lrclib('Song', 'Artist', '', None)['lrclib_id'] == 5       # no length: exact names only


def test_lrclib_search_failure_keeps_exact_plain_record(monkeypatch):
    def fake(path, params):
        if path == 'search':
            raise OSError('down')
        return {'id': 9, 'trackName': 'Song', 'artistName': 'Artist', 'duration': 200, 'plainLyrics': 'x'}
    monkeypatch.setattr(Lyrics, '_lrclib_fetch', staticmethod(fake))
    assert Lyrics()._lrclib('Song', 'Artist', '', 200000)['sync'] == 'none'


def test_youtube_music_timed_lyrics(monkeypatch):
    from types import SimpleNamespace as Line
    import ytmusicapi

    class FakeYT:
        def __init__(self, **kwargs): pass
        def get_watch_playlist(self, videoId, limit):
            return {'tracks': [{'videoId': videoId, 'length': '4:22'}], 'lyrics': 'MPLY'}
        def get_lyrics(self, ref, timestamps):
            assert timestamps
            return {'hasTimestamps': True, 'source': 'Source: LyricFind', 'lyrics': [
                Line(text='♪', start_time=0, end_time=9440),
                Line(text='हम तेरे बिन', start_time=9440, end_time=14780)]}

    monkeypatch.setattr(ytmusicapi, 'YTMusic', FakeYT)
    result = Lyrics()._music('fsiPzT50ZiM', 262000, [])
    assert result['sync'] == 'line'
    assert result['lines'] == [{'text': 'हम तेरे बिन', 'start_ms': 9440, 'end_ms': 14780}]
    assert Lyrics()._music('fsiPzT50ZiM', 300000, []) is None   # wrong length


def test_timed_youtube_music_beats_plain_lrclib(monkeypatch):
    service = Lyrics()
    monkeypatch.setattr(service, '_lrclib', lambda *args: {'source': 'lrclib', 'sync': 'none'})
    monkeypatch.setattr(service, '_music', lambda *args: {'source': 'youtube_music', 'sync': 'line'})
    monkeypatch.setattr(service, '_youtube', lambda *args: pytest.fail('timed result already found'))
    assert service.lookup('t', 'a', video_id='abcdefghijk')['source'] == 'youtube_music'
    monkeypatch.setattr(service, '_music', lambda *args: {'source': 'youtube_music', 'sync': 'none'})
    monkeypatch.setattr(service, '_youtube', lambda *args: None)
    assert service.lookup('t', 'a', video_id='abcdefghijk')['source'] == 'lrclib'   # plain: LRCLIB first


# -- rules from real songs ---------------------------------------------------

from jukes.lyrics import lrc_lines, timing_problem, to_lrc


def test_timing_rules():
    assert timing_problem([0, 6000, 26000, 32000, 42000, 52000]) == 'whole_seconds'   # Rangeen copies
    assert timing_problem([14950, 18910, 22660, 26420, 30270, 33940]) is None          # Khoj
    assert timing_problem([1000, 2000, 212000], 167000) == 'past_end'                 # 167 s record
    assert timing_problem([5000, 1000]) == 'out_of_order'
    assert lrc_lines('[01:02.50] a\n[00:03] b') == [(62500, 'a'), (3000, 'b')]


def test_lrclib_whole_second_timing_keeps_words_only():
    synced = '\n'.join(f'[00:{s:02d}.00] line {s}' for s in (0, 6, 26, 32, 42, 52))
    record = Lyrics._record({'id': 7, 'trackName': 'Rangeen', 'artistName': 'A', 'duration': 222,
                             'syncedLyrics': synced, 'plainLyrics': None})
    assert record['sync'] == 'none' and record['timing_rejected'] == 'whole_seconds'
    assert record['synced_lyrics'] is None and record['plain_lyrics'].startswith('line 0')


def test_bilingual_official_subtitles_keep_the_lyric_line():
    events = [{'tStartMs': i * 5000, 'dDurationMs': 4000, 'segs': [{'utf8': f'{lyric}\n{translation}'}]}
              for i, (lyric, translation) in enumerate([
                  ('Ghubar-e-bekasi kaisi ye uthi', 'What is this sweet spell'),
                  ('Aap ko na hai khayal', "You haven't the faintest idea"),
                  ('Mera lahoo kar gaya', 'In deep red')])]
    lines = caption_lines({'events': events}, 'subtitles')
    assert [l['text'] for l in lines] == ['Ghubar-e-bekasi kaisi ye uthi', 'Aap ko na hai khayal', 'Mera lahoo kar gaya']
    assert lines[0]['end_ms'] == 4000
    urdu = [{'tStartMs': 0, 'segs': [{'utf8': 'میرا لہو کر گیا\nIn deep red'}]},
            {'tStartMs': 5000, 'segs': [{'utf8': 'رنگی قصوں کو تیرے\nIt was my devotion'}]}]
    assert [l['text'] for l in caption_lines({'events': urdu}, 'subtitles')] == ['میرا لہو کر گیا', 'رنگی قصوں کو تیرے']


def test_wrapped_captions_are_joined_not_split():
    events = [{'tStartMs': 0, 'segs': [{'utf8': "♪ The club isn't the best place\nto find a lover ♪"}]},
              {'tStartMs': 4000, 'segs': [{'utf8': '♪ Me and my friends at\nthe table doing shots ♪'}]}]
    assert [l['text'] for l in caption_lines({'events': events}, 'subtitles')] == [
        "The club isn't the best place to find a lover", 'Me and my friends at the table doing shots']


def test_to_lrc_marks_instrumental_gaps():
    lrc = to_lrc([{'text': 'a', 'start_ms': 1000, 'end_ms': 3000}, {'text': 'b', 'start_ms': 20000, 'end_ms': 22000}])
    assert lrc == '[00:01.00] a\n[00:03.00] \n[00:20.00] b\n[00:22.00] '


def test_untrustworthy_youtube_timing_is_text_only():
    result = Lyrics._checked({'source': 'youtube_music', 'sync': 'line', 'lines': [
        {'text': 'x', 'start_ms': 9000}, {'text': 'y', 'start_ms': 1000}]}, None)
    assert result['sync'] == 'none' and 'lines' not in result


class _MemoryStore:
    MISS = object()
    def __init__(self): self.rows, self.offers = {}, []
    def get(self, key): return self.rows.get(key, self.MISS)
    def put(self, key, result, ttl): self.rows[key] = result
    def offer(self, video_id, result, meta): self.offers.append((video_id, result['source'], meta))


def test_plain_result_is_upgraded_in_background_cached_and_offered(monkeypatch):
    store = _MemoryStore()
    service = Lyrics(store=store, audio=object(), contributions=store)
    monkeypatch.setattr(service, '_lrclib', lambda *a: {'source': 'lrclib', 'sync': 'none', 'timing_rejected': 'whole_seconds'})
    monkeypatch.setattr(service, '_music', lambda *a: None)
    monkeypatch.setattr(service, '_youtube', lambda *a: None)
    better = {'source': 'youtube_subtitles', 'sync': 'line', 'caption_rules': CAPTION_RULES,
              'lines': [{'text': 'x', 'start_ms': 0}]}
    monkeypatch.setattr(service, '_official', lambda *a: better)
    monkeypatch.setattr(service, 'track_metadata', lambda *a: {'title': 'T', 'artist': 'A', 'duration_s': 222})
    first = service.lookup('Rangeen', 'Hasan Raheem', duration=222000, video_id='J78lINC__HE')
    assert first['source'] == 'lrclib' and first['upgrading']
    service._worker.shutdown(wait=True)
    assert service.lookup('Rangeen', 'Hasan Raheem', duration=222000, video_id='J78lINC__HE') == better
    assert store.offers == [('J78lINC__HE', 'youtube_subtitles', {'title': 'T', 'artist': 'A', 'duration_s': 222})]


def test_nothing_found_yet_is_a_retryable_404_while_upgrading(monkeypatch):
    service = Lyrics(store=_MemoryStore(), audio=object())
    for name in ('_lrclib', '_music', '_youtube', '_official'):
        monkeypatch.setattr(service, name, lambda *a: None)
    with pytest.raises(ApiError) as err:
        service.lookup('Jaana', 'Maaidah', duration=149000, video_id='2LKJiANbmWA')
    assert err.value.status == 404 and err.value.retryable and err.value.retry_after == '60'
    service._worker.shutdown(wait=True)
    assert service.lookup('Jaana', 'Maaidah', duration=149000, video_id='2LKJiANbmWA') is None  # cached miss


def test_alignment_finds_constant_offset_and_rejects_other_recordings():
    import numpy as np
    from jukes.lyrics_align import offset
    rng = np.random.default_rng(1)
    song = rng.standard_normal(222 * 100)
    video = np.concatenate([rng.standard_normal(551), song, rng.standard_normal(700)])  # 5.51 s intro
    assert offset(song, video) == 5.51
    assert offset(rng.standard_normal(222 * 100), video) is None


def test_lyrics_store_cache_and_queue(tmp_path):
    from jukes.lyrics_store import LyricsStore
    from jukes.store import Store
    now = [1000.0]
    store = LyricsStore(Store(tmp_path / 'db.sqlite3'), clock=lambda: now[0])
    assert store.get('k') is store.MISS
    store.put('k', {'sync': 'line'}, 10); store.put('miss', None, 10)
    assert store.get('k') == {'sync': 'line'} and store.get('miss') is None
    now[0] += 11
    assert store.get('k') is store.MISS
    store.offer('vid', {'source': 'youtube_music', 'plain_lyrics': 'lyrics'}, {'title': 'T', 'artist': 'A', 'duration_s': 9})
    store.offer('bad', {'source': 'youtube_subtitles', 'lines': []}, {'title': '', 'artist': 'A', 'duration_s': 9})
    assert store.next_pending()[0] == 'vid' and store.counts() == {'pending': 1}


def test_music_lookup_offers_plain_and_timed_lyrics(monkeypatch):
    store = _MemoryStore()
    service = Lyrics(contributions=store)
    monkeypatch.setattr(service, '_lrclib', lambda *a: None)
    monkeypatch.setattr(service, '_youtube', lambda *a: None)
    monkeypatch.setattr(service, 'track_metadata', lambda *a: {'title': 'T', 'artist': 'A', 'duration_s': 222})
    for sync in ('none', 'line'):
        monkeypatch.setattr(service, '_music', lambda *a: {'source': 'youtube_music', 'sync': sync, 'plain_lyrics': 'text'})
        service._resolve('T', 'A', '', 222000, 'abcdefghijk')
    assert [offer[1] for offer in store.offers] == ['youtube_music', 'youtube_music']


def test_plain_music_queue_is_upgraded_and_does_not_replace_existing_lyrics(tmp_path):
    from jukes.lyrics_store import LyricsStore, Publisher
    from jukes.store import Store
    store = LyricsStore(Store(tmp_path / 'db.sqlite3'))
    meta = {'title': 'T', 'artist': 'A', 'duration_s': 222}
    plain = {'source': 'youtube_music', 'plain_lyrics': 'first\nsecond', 'attribution': 'Source: LyricFind'}
    store.offer('a', plain, meta)
    assert store.next_pending()[1]['attribution'] == 'Source: LyricFind'
    lines = [{'text': str(i), 'start_ms': 1234 + i * 3410} for i in range(4)]
    store.offer('a', {'source': 'youtube_music', 'lines': lines}, meta)
    assert store.next_pending()[1]['lines'] == lines
    store.offer('b', plain, meta)
    store.mark('a', 'skipped')
    existing = {'id': 4, 'duration': 222, 'plainLyrics': 'already exists'}
    assert Publisher(store, post=lambda *args: pytest.fail('must not publish'), fetch=lambda *args: existing).run_once() == 'skipped'


def test_plain_music_publisher_sends_plain_when_missing(tmp_path):
    from jukes.lyrics_store import LyricsStore, Publisher
    from jukes.store import Store
    store = LyricsStore(Store(tmp_path / 'db.sqlite3'))
    store.offer('a', {'source': 'youtube_music', 'plain_lyrics': 'one\ntwo'}, {'title': 'T', 'artist': 'A', 'duration_s': 222})
    posts = []
    def post(path, body, headers=None):
        posts.append((path, body))
        return (200, json.dumps({'prefix': 'p', 'target': 'ff' * 32}).encode()) if path == 'request-challenge' else (201, b'')
    assert Publisher(store, post=post, fetch=lambda *a: None).run_once() == 'published'
    assert posts[-1][1]['plainLyrics'] == 'one\ntwo'
    assert 'syncedLyrics' not in posts[-1][1]


def test_publisher_skips_existing_timed_record_and_publishes_otherwise(tmp_path):
    from jukes.lyrics_store import LyricsStore, Publisher
    from jukes.store import Store
    store = LyricsStore(Store(tmp_path / 'db.sqlite3'))
    lines = [{'text': f'line {i}', 'start_ms': 1000 + i * 3210, 'end_ms': 3000 + i * 3210} for i in range(6)]
    meta = {'title': 'Rangeen | Coke Studio Nu.WAV', 'artist': 'Hasan Raheem, Izzchughtai, Umair',
            'album': 'Rangeen | Coke Studio Nu.WAV', 'duration_s': 222}
    posts = []
    def post(path, body, headers=None):
        posts.append((path, body, headers))
        return (200, json.dumps({'prefix': 'p', 'target': 'ff' * 32}).encode()) if path == 'request-challenge' else (201, b'')

    good = {'id': 1, 'duration': 222, 'syncedLyrics': '[00:14.95] a\n[00:18.91] b'}
    store.offer('a', {'source': 'youtube_subtitles', 'lines': lines}, meta)
    assert Publisher(store, post=post, fetch=lambda *a: good).run_once() == 'skipped'

    coarse = {'id': 2, 'duration': 222, 'syncedLyrics': '\n'.join(f'[00:{s:02d}.00] x' for s in range(0, 60, 10))}
    store.offer('b', {'source': 'youtube_subtitles', 'lines': lines}, meta)
    assert Publisher(store, post=post, fetch=lambda *a: coarse).run_once() == 'published'
    path, body, headers = posts[-1]
    assert path == 'publish' and headers == {'X-Publish-Token': 'p:0'}
    assert body['trackName'] == meta['title'] and body['duration'] == 222
    assert body['syncedLyrics'].startswith('[00:01.00] line 0') and body['plainLyrics'].startswith('line 0')
    assert body['lyricsfile'].startswith("version: '1.0'") and '- text: "line 0"' in body['lyricsfile']
    assert store.counts() == {'published': 1, 'skipped': 1}


def test_alignment_is_not_fooled_by_a_repeated_chorus():
    import numpy as np
    from jukes.lyrics_align import offset
    rng = np.random.default_rng(2)
    verse, chorus = (rng.standard_normal(30 * 100) for _ in range(2))
    song = np.concatenate([verse, chorus, rng.standard_normal(40 * 100), chorus, rng.standard_normal(40 * 100), chorus])
    video = np.concatenate([rng.standard_normal(551), song, rng.standard_normal(800)])
    assert offset(song, video) == 5.51


def test_lrclib_search_falls_back_to_plain_when_all_timing_is_rejected(monkeypatch):
    coarse = '\n'.join(f'[00:{s:02d}.00] wrong' for s in range(0, 60, 10))
    _lrclib_replies(monkeypatch, None, [
        {'id': 1, 'trackName': 'Rangeen', 'artistName': 'A', 'duration': 221, 'syncedLyrics': coarse},
        {'id': 2, 'trackName': 'Rangeen | Coke Studio Nu.WAV', 'artistName': 'A', 'duration': 222, 'plainLyrics': 'words'}])
    result = Lyrics()._lrclib('Rangeen', 'A', '', 222000)
    assert result['sync'] == 'none' and result['lrclib_id'] == 2   # closest; no bad timing returned


def test_subtitle_decoration_is_removed():
    # Ji Huzoor (Sparsh), uploader subtitles on 18m9ovEsP6s
    texts = ['Dil Tere Agge.....', 'CHADHEYA CHADHEYA, ISHQ FITOOR,,,,', '*JI HUZOOR JI HUZOOR*',
             'Tere Naal Laiyaan,Laiyaan....', ' ', '....♡♡♡♡♡♡♡♡♡....', 'Kya hua?', 'Tu Hi Toh Mera ASMAAN.......']
    payload = {'events': [{'tStartMs': i * 1000, 'dDurationMs': 900, 'segs': [{'utf8': t}]} for i, t in enumerate(texts)]}
    assert [l['text'] for l in caption_lines(payload, 'subtitles')] == [
        'Dil Tere Agge', 'Chadheya Chadheya, Ishq Fitoor', 'Ji Huzoor Ji Huzoor',
        'Tere Naal Laiyaan,Laiyaan', 'Kya hua?', 'Tu Hi Toh Mera ASMAAN']
    # ASR captions are left as they are
    assert caption_lines(payload)[0]['text'] == 'Dil Tere Agge.....'


def test_lyricsfile_carries_line_ends_and_word_timings():
    yaml = pytest.importorskip('yaml')
    from jukes.lyrics_store import to_lyricsfile
    meta = {'title': 'Ji "Huzoor"', 'artist': 'Sparsh', 'album': '', 'duration_s': 167}
    lines = [{'text': "I've been", 'start_ms': 1000, 'end_ms': 2000,
              'words': [{'text': "I've ", 'start_ms': 1000}, {'text': 'been', 'start_ms': 1500}]},
             {'text': 'ਸੋਚਾਂ ਤੇਰੇ', 'start_ms': 3000}]
    doc = yaml.safe_load(to_lyricsfile(meta, lines, "I've been\nਸੋਚਾਂ ਤੇਰੇ"))
    assert doc['metadata'] == {'title': 'Ji "Huzoor"', 'artist': 'Sparsh', 'album': '', 'duration_ms': 167000, 'instrumental': False}
    assert doc['lines'][0] == {'text': "I've been", 'start_ms': 1000, 'end_ms': 2000,
                               'words': [{'text': "I've ", 'start_ms': 1000}, {'text': 'been', 'start_ms': 1500}]}
    assert doc['lines'][1] == {'text': 'ਸੋਚਾਂ ਤੇਰੇ', 'start_ms': 3000}


def test_word_synced_copy_beats_a_line_synced_exact_match():
    line = {'id': 1, 'trackName': 'Blinding Lights', 'artistName': 'The Weeknd', 'duration': 200,
            'syncedLyrics': '[00:13.12] Yeah\n[00:27.17] call'}
    word = {**line, 'id': 2, 'duration': 201, 'hasWordSync': True, 'lyricsfile': "version: '1.0'\nlines:\n- text: Yeah\n  words: []\n"}
    far = {**word, 'id': 3, 'duration': 204}
    fetch = lambda kind, params: line if kind == 'get' else [line, far, word]
    assert Lyrics._lrclib.__get__(type('L', (), {'_lrclib_fetch': staticmethod(fetch), '_record': staticmethod(Lyrics._record)})())(
        'Blinding Lights', 'The Weeknd', '', 200_000)['lrclib_id'] == 2
    # no word-synced copy close enough: the exact match stays
    fetch = lambda kind, params: line if kind == 'get' else [line, far]
    assert Lyrics._lrclib.__get__(type('L', (), {'_lrclib_fetch': staticmethod(fetch), '_record': staticmethod(Lyrics._record)})())(
        'Blinding Lights', 'The Weeknd', '', 200_000)['lrclib_id'] == 1


def test_sound_labels_inside_a_line_are_removed():
    lines = caption_lines({'events': [
        {'tStartMs': 1000, 'segs': [{'utf8': '[संगीत]'}, {'utf8': ' तेरे बिना', 'tOffsetMs': 300}]},
        {'tStartMs': 3000, 'segs': [{'utf8': 'जी हुज़ूर [संगीत]'}]},
        {'tStartMs': 5000, 'segs': [{'utf8': '[Music] [Applause]'}]},
    ]})
    assert [l['text'] for l in lines] == ['तेरे बिना', 'जी हुज़ूर']
    assert lines[0]['segments'] == [{'text': ' तेरे बिना', 'start_ms': 1300}]


def test_hindi_song_keeps_its_own_words_not_the_english_translation(monkeypatch):
    import yt_dlp
    fetched = []
    hindi = {'events': [{'tStartMs': 1000, 'segs': [{'utf8': 'तेरे बिना'}]},
                        {'tStartMs': 2500, 'segs': [{'utf8': 'जी हुज़ूर'}]}]}

    class FakeDL:
        def __init__(self, opts): self.cookiejar = type('J', (), {'set_cookie': lambda *a: None})()
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download, process):
            asr = 'https://www.youtube.com/api/timedtext?v=x&lang=hi&kind=asr&fmt=json3'
            return {'duration': 200, 'subtitles': {'en': [{'ext': 'json3', 'url': 'english-by-uploader'}]},
                    'automatic_captions': {
                'en': [{'ext': 'json3', 'url': asr + '&tlang=en'}],
                'hi-orig': [{'ext': 'json3', 'url': asr}],
                'hi': [{'ext': 'json3', 'url': asr}],
                'ur': [{'ext': 'json3', 'url': asr + '&tlang=ur'}]}}
        def urlopen(self, url):
            fetched.append(url)
            return __import__('io').BytesIO(json.dumps(hindi).encode())

    monkeypatch.setattr(yt_dlp, 'YoutubeDL', FakeDL)
    result = Lyrics()._youtube('abcdefghijk', 200000, [])
    assert [l['text'] for l in result['lines']] == ['तेरे बिना', 'जी हुज़ूर']
    assert result['language'] == 'hi'
    assert fetched and all('tlang' not in url and 'english' not in url for url in fetched)


def test_captions_cached_under_older_rules_are_fetched_again(tmp_path, monkeypatch):
    from jukes.lyrics_store import LyricsStore
    from jukes.store import Store
    store = LyricsStore(Store(tmp_path / 'db.sqlite3'))
    service = Lyrics(store=store)
    key = service.cache_key('t', 'a', '', None, 'abcdefghijk')
    store.put(key, {'source': 'youtube_captions', 'sync': 'line', 'plain_lyrics': '[संगीत] old'}, 3600)
    fresh = {'source': 'youtube_captions', 'sync': 'line', 'caption_rules': CAPTION_RULES}
    monkeypatch.setattr(service, '_resolve', lambda *args: (fresh, False))
    assert service.lookup('t', 'a', video_id='abcdefghijk') == fresh
    monkeypatch.setattr(service, '_resolve', lambda *args: pytest.fail('cached result is current'))
    assert service.lookup('t', 'a', video_id='abcdefghijk') == fresh
