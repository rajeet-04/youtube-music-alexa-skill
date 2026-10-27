"""Title/artist matching regressions: wrong-song picks seen in production."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from jukes.music import Music, NoMatch, RejectedMatch, TrackSelector  # noqa: E402


def item(video_id, title, artists, duration):
    return {"videoId": video_id, "title": title, "artists": [{"name": a} for a in artists],
            "duration": duration, "album": None, "thumbnails": []}


class YT:
    def __init__(self, songs=(), videos=()):
        self.by_filter = {"songs": list(songs), "videos": list(videos)}
        self.filters = []

    def search(self, query, filter=None, limit=None, ignore_spelling=False):
        self.filters.append(filter)
        return self.by_filter[filter]


def resolve(yt, title, artist, duration_ms=None):
    return Music(lambda _ctx: yt).resolve(TrackSelector(title=title, artist=artist, duration_ms=duration_ms))


def test_artist_spacing_is_not_identity():
    yt = YT(songs=[item("32gV5MWWzzs", "Sapne", ["Lashcurry", "Aditya Pushkarna"], "3:17")])
    assert resolve(yt, "Sapne", "Lash Curry", 197_000).video_id == "32gV5MWWzzs"


@pytest.mark.parametrize("duration_ms", [None, 352_000])
def test_plain_title_beats_decorated_version_ranked_first(duration_ms):
    yt = YT(songs=[item("7IID5YLPg7w", "Enchanted (Taylor's Version)", ["Taylor Swift"], "5:54"),
                   item("vv3um0BlygY", "Enchanted", ["Taylor Swift"], "5:53")])
    assert resolve(yt, "Enchanted", "Taylor Swift", duration_ms).video_id == "vv3um0BlygY"


def test_requested_decorated_version_still_wins():
    yt = YT(songs=[item("vv3um0BlygY", "Enchanted", ["Taylor Swift"], "5:53"),
                   item("7IID5YLPg7w", "Enchanted (Taylor's Version)", ["Taylor Swift"], "5:54")])
    assert resolve(yt, "Enchanted (Taylor's Version)", "Taylor Swift", 354_000).video_id == "7IID5YLPg7w"


def test_video_only_release_is_found_when_artist_and_length_agree():
    yt = YT(songs=[item("pMHydGL4URo", "Gore Gore Mukhde Pe", ["Udit Narayan"], "5:12")],
            videos=[item("UT7lqQYDh2k", "Goray Goray Gaal", ["Omar Mukhtar"], "2:38")])
    assert resolve(yt, "Goray Goray Gaal", "Omar Mukhtar", 158_000).video_id == "UT7lqQYDh2k"


def test_video_fallback_never_accepts_another_artist_or_length():
    other = item("aaaaaaaaaaa", "Goray Goray Gaal", ["Somebody Else"], "2:38")
    with pytest.raises(NoMatch):
        resolve(YT(videos=[other]), "Goray Goray Gaal", "Omar Mukhtar", 158_000)
    long = item("bbbbbbbbbbb", "Goray Goray Gaal", ["Omar Mukhtar"], "5:00")
    with pytest.raises(RejectedMatch):
        resolve(YT(videos=[long]), "Goray Goray Gaal", "Omar Mukhtar", 158_000)
    unlisted = item("ccccccccccc", "Goray Goray Gaal", ["Omar Mukhtar"], "")
    with pytest.raises(RejectedMatch):
        resolve(YT(videos=[unlisted]), "Goray Goray Gaal", "Omar Mukhtar", 158_000)


def test_videos_are_only_searched_when_songs_have_no_match():
    yt = YT(songs=[item("32gV5MWWzzs", "Sapne", ["Lashcurry"], "3:17")])
    resolve(yt, "Sapne", "Lashcurry")
    assert yt.filters == ["songs"]


def test_misses_are_remembered_briefly_and_upstream_errors_are_not():
    clock = [0.0]
    yt = YT()
    music = Music(lambda _ctx: yt, clock=lambda: clock[0])
    selector = TrackSelector(title="Nothing", artist="Nobody")
    for _ in range(2):
        with pytest.raises(NoMatch):
            music.resolve(selector)
    assert yt.filters == ["songs", "videos"]  # second attempt never reached YouTube
    clock[0] = 301
    with pytest.raises(NoMatch):
        music.resolve(selector)
    assert yt.filters == ["songs", "videos", "songs", "videos"]


def test_concurrent_resolves_share_one_search():
    import threading, time

    class SlowYT(YT):
        def search(self, *a, **k):
            time.sleep(0.2)
            return super().search(*a, **k)

    yt = SlowYT(songs=[item("32gV5MWWzzs", "Sapne", ["Lashcurry"], "3:17")])
    music = Music(lambda _ctx: yt)
    out = []
    threads = [threading.Thread(target=lambda: out.append(music.resolve(TrackSelector(title="Sapne", artist="Lashcurry"))))
               for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(out) == 4 and len(yt.filters) == 1


@pytest.mark.parametrize("spotify_title,listed", [
    ('Tum Hi Ho - From "Aashiqui 2"', "Tum Hi Ho"),
    ("Bohemian Rhapsody - Remastered 2011", "Bohemian Rhapsody"),
    ("Hotel California - 2013 Remaster", "Hotel California"),
    ("Heeriye (feat. Arijit Singh)", "Heeriye"),
    ("Calm Down (with Selena Gomez)", "Calm Down"),
    ("Lag Ja Gale - Studio Version", "Lag Ja Gale"),
])
def test_spotify_decorations_do_not_hide_the_song(spotify_title, listed):
    yt = YT(songs=[item("aaaaaaaaaaa", "Something Else Entirely", ["Other"], "3:00"),
                   item("bbbbbbbbbbb", listed, ["Main Artist", "Guest"], "3:30")])
    assert resolve(yt, spotify_title, "Main Artist").video_id == "bbbbbbbbbbb"


def test_a_requested_live_version_is_not_stripped():
    yt = YT(songs=[item("aaaaaaaaaaa", "Song", ["Main Artist"], "3:30"),
                   item("bbbbbbbbbbb", "Song (Live)", ["Main Artist"], "4:10")])
    assert resolve(yt, "Song (Live)", "Main Artist").video_id == "bbbbbbbbbbb"
    assert resolve(yt, "Song", "Main Artist").video_id == "aaaaaaaaaaa"
