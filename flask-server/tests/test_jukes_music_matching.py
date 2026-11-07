"""Title/artist matching regressions: wrong-song picks seen in production."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from jukes.music import ANONYMOUS, Music, NoMatch, RejectedMatch, TrackSelector, UserContext  # noqa: E402


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


def test_searches_never_use_the_signed_in_client():
    """Authenticated searches land in the account's YouTube Music history."""
    yt = YT(songs=[item("32gV5MWWzzs", "Sapne", ["Lashcurry"], "3:17")])
    contexts = []

    def factory(ctx):
        contexts.append(ctx)
        return yt

    user = UserContext(installation_id="inst-1", generation=1, personalization_status="connected")
    Music(factory).resolve(TrackSelector(title="Sapne", artist="Lashcurry"), user)
    searched = contexts[1:]  # contexts[0] is the resolve-level client, which is only handed to _search
    assert contexts and all(c is ANONYMOUS for c in searched)
    assert yt.filters == ["songs"]


def test_exact_title_with_music_video_length_gap_is_accepted_but_flagged():
    # Spotify lists 2:18; the artist's video is 2:38 (20 s intro). Exact title + artist: the right track.
    yt = YT(songs=[item("8CbgxW1atzY", "Dil na laga", ["Omar Mukhtar"], "2:23")],
            videos=[item("UT7lqQYDh2k", "Goray Goray Gaal", ["Omar Mukhtar"], "2:38")])
    track = resolve(yt, "Goray Goray Gaal", "Omar Mukhtar", 138_000)
    assert track.video_id == "UT7lqQYDh2k" and track.length_relaxed and track.as_dict()["length_relaxed"] is True


def test_in_tolerance_candidate_beats_a_relaxed_exact_one():
    yt = YT(songs=[item("longlonglon", "Sapne", ["Lashcurry"], "3:45"), item("exactexact1", "Sapne", ["Lashcurry"], "3:17")])
    track = resolve(yt, "Sapne", "Lashcurry", 197_000)
    assert track.video_id == "exactexact1" and not track.length_relaxed and "length_relaxed" not in track.as_dict()


def test_length_gap_beyond_a_quarter_is_still_rejected():
    yt = YT(songs=[item("longlonglon", "Sapne", ["Lashcurry"], "6:00")])
    with pytest.raises(RejectedMatch):
        resolve(yt, "Sapne", "Lashcurry", 197_000)


def test_songs_rejected_for_length_still_try_the_video_upload():
    yt = YT(songs=[item("longlonglon", "Main Dhoondne Ko Zamaane Mein", ["Gaurav Dagaonkar"], "2:00")],
            videos=[item("xQrMELqpf6E", "Main Dhoondne Ko Zamaane Mein", ["Gaurav Dagaonkar"], "4:24")])
    assert resolve(yt, "Main Dhoondne Ko Zamaane Mein", "Gaurav Dagaonkar", 262_000).video_id == "xQrMELqpf6E"
    assert yt.filters == ["songs", "videos"]


def test_composer_requested_but_singer_credited_is_accepted_and_flagged():
    # The app sent the composer ("Salim–Sulaiman"); YouTube Music credits the singer.
    yt = YT(songs=[item("gLBoyzFnAdE", "Haule Haule", ["Sukhwinder Singh"], "4:24"),
                   item("57viGjog3EE", "Zara Haule Haule", ["Not Applicable"], "4:37")])
    track = resolve(yt, "Haule Haule", "Salim–Sulaiman", 265_000)
    assert track.video_id == "gLBoyzFnAdE" and track.artist_relaxed and track.as_dict()["artist_relaxed"] is True


def test_uncredited_artist_is_not_guessed_when_titles_collide_or_length_is_off():
    songs = [item("aaaaaaaaaaa", "Challa", ["Rabbi"], "5:21"), item("bbbbbbbbbbb", "Challa", ["Shilpa Rao"], "5:22")]
    with pytest.raises(NoMatch):
        resolve(YT(songs=songs), "Challa", "A.R. Rahman", 320_000)  # two songs fit: ambiguous
    with pytest.raises(NoMatch):
        resolve(YT(songs=songs[:1]), "Challa", "A.R. Rahman", 250_000)  # right title, wrong length
    with pytest.raises(NoMatch):
        resolve(YT(songs=songs[:1]), "Challa", "A.R. Rahman")  # no length to verify with


def test_artist_named_in_the_video_title_counts_as_credit():
    video = item("vvvvvvvvvvv", "Challa | Full Song | Jab Tak Hai Jaan | Rabbi | A. R. Rahman | Gulzar", ["Yash Raj Films"], "5:20")
    assert resolve(YT(videos=[video]), "Challa", "A.R. Rahman", 320_000).video_id == "vvvvvvvvvvv"


def test_official_song_beats_a_fan_upload_that_names_the_artist():
    songs = [item("sssssssssss", "Challa", ["Rabbi"], "5:21")]
    video = item("vvvvvvvvvvv", "Challa Song | A.R Rahman | Jab Tak Hain Jaan", ["Fan Channel"], "5:31")
    track = resolve(YT(songs=songs, videos=[video]), "Challa", "A.R. Rahman", 320_000)
    assert track.video_id == "sssssssssss" and track.artist_relaxed


def test_rejected_video_does_not_stop_the_uncredited_artist_tier():
    songs = [item("gLBoyzFnAdE", "Haule Haule", ["Sukhwinder Singh"], "4:24")]
    videos = [item("xxxxxxxxxxx", "Haule Haule", ["Salim-Sulaiman"], "9:00")]  # credited but far too long
    assert resolve(YT(songs=songs, videos=videos), "Haule Haule", "Salim–Sulaiman", 265_000).video_id == "gLBoyzFnAdE"


def test_uncredited_artist_picks_the_recording_whose_length_is_clearly_closest():
    songs = [item("aaaaaaaaaaa", "Haule Haule", ["Sukhwinder Singh"], "4:24"), item("bbbbbbbbbbb", "Haule Haule", ["Ar Ishu"], "4:15")]
    assert resolve(YT(songs=songs), "Haule Haule", "Salim–Sulaiman", 265_000).video_id == "aaaaaaaaaaa"
    with pytest.raises(NoMatch):  # 4:24 vs 4:15 around 4:20: neither is clearly the one
        resolve(YT(songs=songs), "Haule Haule", "Salim–Sulaiman", 260_000)
