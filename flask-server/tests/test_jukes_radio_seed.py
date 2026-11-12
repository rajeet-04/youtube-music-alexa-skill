"""Radio seeding: a music-video seed is swapped for its audio (album-track) upload."""
from jukes.music import Music, ANONYMOUS

VIDEO, AUDIO = "2F1oT-3g9rU", "ftThrZ3c0LQ"


class Client:
    def __init__(self, video_type="MUSIC_VIDEO_TYPE_OMV", songs=None):
        self.video_type = video_type
        self.songs = songs if songs is not None else [
            {"videoId": AUDIO, "title": "Khidki (feat. urbann)", "artists": [{"name": "nish."}, {"name": "TheKrypt"}],
             "duration": "2:14"}]
        self.radio_seeds, self.searches = [], 0

    def get_watch_playlist(self, videoId, limit=1, radio=False):
        item = {"videoId": videoId, "title": "Khidki (feat. urbann)", "videoType": self.video_type,
                "artists": [{"name": "nish."}, {"name": "TheKrypt"}], "length": "2:13"}
        if radio:
            self.radio_seeds.append(videoId)
        return {"tracks": [item]}

    def search(self, query, filter=None, limit=8, ignore_spelling=False):
        self.searches += 1
        return self.songs


def radio(client, video=VIDEO):
    music = Music(lambda ctx: client)
    music.radio(video, 5, ANONYMOUS)
    return music, client


def test_video_seed_becomes_audio_upload():
    _, c = radio(Client())
    assert c.radio_seeds == [AUDIO]


def test_audio_seed_is_left_alone():
    _, c = radio(Client("MUSIC_VIDEO_TYPE_ATV"), AUDIO)
    assert c.radio_seeds == [AUDIO] and c.searches == 0


def test_no_audio_twin_keeps_requested_id():
    _, c = radio(Client(songs=[]))
    assert c.radio_seeds == [VIDEO]


def test_other_version_is_not_used():
    live = [{"videoId": "AAAAAAAAAAA", "title": "Khidki (Live)", "artists": [{"name": "nish."}], "duration": "2:14"}]
    _, c = radio(Client(songs=live))
    assert c.radio_seeds == [VIDEO]


def test_lookup_is_cached():
    music, c = radio(Client())
    music.radio(VIDEO, 5, ANONYMOUS)
    assert c.searches == 1 and c.radio_seeds == [AUDIO, AUDIO]


def test_unknown_type_is_untouched():
    class Plain(Client):
        def get_watch_playlist(self, videoId, limit=1, radio=False):
            r = super().get_watch_playlist(videoId, limit, radio)
            r["tracks"][0].pop("videoType"); return r
    _, c = radio(Plain())
    assert c.radio_seeds == [VIDEO] and c.searches == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"): fn(); print("ok", name)
