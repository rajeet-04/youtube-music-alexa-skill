"""Versioned warmup/prepare/jobs/audio API and the legacy /audio/ endpoint."""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from jukes.app import create_app  # noqa: E402
from jukes.cache import Cache  # noqa: E402
from jukes.config import CacheConfig  # noqa: E402
from jukes.extractor import DownloadResult  # noqa: E402
from jukes.jobs import Jobs  # noqa: E402
from jukes.models import AudioKey  # noqa: E402
from jukes.music import Music  # noqa: E402
from jukes.routes import AUDIO_POLICY, Services, Settings  # noqa: E402

PAYLOAD = b"\x00\x00\x00\x18ftypM4A " + bytes(range(256)) * 4  # 1048 bytes, mp4 magic
VID = "abcdefghijk"
VID2 = "bbbbbbbbbbb"


class FakeExtractor:
    def __init__(self):
        self.gate = None
        self.calls = []

    def download(self, key, destination, snapshot):
        self.calls.append(key.video_id)
        if self.gate is not None:
            assert self.gate.wait(5)
        destination.write(PAYLOAD)
        return DownloadResult(destination.path, len(PAYLOAD), "mp4", "audio/mp4", 10.0, "aac", "default")


class FakeYT:
    def __init__(self):
        self.searches = []
        self.fail = False
        self.items = []

    def get_song(self, video_id):
        if self.fail:
            raise RuntimeError("boom")
        if video_id == "zzzzzzzzzzz":
            return {"playabilityStatus": {"status": "ERROR"}}
        return {"playabilityStatus": {"status": "OK"}, "videoDetails": {
            "videoId": video_id, "title": "Song", "author": "Band - Topic", "lengthSeconds": "210",
            "thumbnail": {"thumbnails": [{"url": "http://img/s"}, {"url": "http://img/l"}]}}}

    def search(self, query, filter=None, limit=None, ignore_spelling=False):
        self.searches.append(query)
        if self.fail:
            raise RuntimeError("boom")
        return self.items


def song(vid, title, artist, seconds, album="Album"):
    return {"videoId": vid, "title": title, "artists": [{"name": artist}], "album": {"name": album} if album else None,
            "duration_seconds": seconds, "thumbnails": [{"url": "http://img/x"}]}


@pytest.fixture
def env(tmp_path):
    config = CacheConfig(
        database_path=tmp_path / "s.sqlite3", audio_dir=tmp_path / "audio",
        requested_limit_bytes=100_000, warmup_limit_bytes=20_000, min_free_disk_bytes=0,
        unknown_size_reservation_bytes=2_048, reservation_increment_bytes=2_048,
    )
    cache = Cache(config)
    extractor = FakeExtractor()
    jobs = Jobs(cache, extractor, worker_count=2)
    yt = FakeYT()
    services = Services(cache=cache, jobs=jobs, music=Music(lambda ctx: yt))
    app = create_app(Settings(legacy_wait_seconds=0.2), services)
    class Env:  # noqa: D401
        pass
    e = Env()
    e.app, e.cache, e.jobs, e.yt, e.extractor = app, cache, jobs, yt, extractor
    e.client = app.test_client()
    raw_open = e.client.open

    def buffered_open(*args, **kwargs):  # buffered responses are closed like a real WSGI server does
        kwargs.setdefault("buffered", True)
        return raw_open(*args, **kwargs)

    e.client.open = buffered_open
    yield e
    if e.extractor.gate is not None:
        e.extractor.gate.set()
    jobs.shutdown()


def wait_ready(env, job_id):
    return env.jobs.wait(job_id, 3)


def post(env, path, body, **kw):
    return env.client.post(path, data=json.dumps(body), content_type="application/json", **kw)


def key(vid=VID):
    return AudioKey(vid, AUDIO_POLICY)


def prepared(env, vid=VID):
    r = post(env, "/v1/audio/prepare", {"video_id": vid})
    wait_ready(env, r.get_json()["job_id"])
    return r


# ---- selectors, metadata, warmup/prepare ----

def test_warmup_by_video_id_returns_metadata_shape_and_202_then_ready(env):
    env.extractor.gate = threading.Event()
    r = post(env, "/v1/warmup", {"video_id": VID})
    body = r.get_json()
    assert r.status_code == 202
    assert body["video_id"] == VID and body["title"] == "Song"
    assert body["artists"] == ["Band"] and body["artist"] == "Band"
    assert body["album"] is None and body["duration_ms"] == 210_000
    assert body["artwork_url"] == "http://img/l"
    assert body["status"] in ("queued", "downloading") and body["pool"] == "warmup"
    assert "audio_url" not in body and body["personalization_status"] == "anonymous"
    assert r.headers["Cache-Control"] == "no-store"
    env.extractor.gate.set()
    wait_ready(env, body["job_id"])
    again = post(env, "/v1/warmup", {"video_id": VID})
    assert again.status_code == 200
    assert again.get_json()["audio_url"].endswith(f"/v1/audio/{VID}")


def test_title_artist_selector_picks_matching_song_and_starts_download_immediately(env):
    env.yt.items = [song(VID, "Song Name", "Some Artist", 200)]
    r = post(env, "/v1/warmup", {"title": "Song Name", "artist": "Some Artist", "duration_ms": 201_000})
    body = r.get_json()
    assert r.status_code in (200, 202) and body["video_id"] == VID
    assert body["album"] == "Album" and body["duration_ms"] == 200_000
    wait_ready(env, body["job_id"])
    assert env.extractor.calls == [VID]


def test_wrong_artist_is_no_match_and_version_or_duration_mismatch_is_422(env):
    env.yt.items = [song(VID, "Song Name", "Different Person", 200)]
    assert post(env, "/v1/warmup", {"title": "Song Name", "artist": "Some Artist"}).status_code == 404
    env.yt.items = [song(VID, "Song Name (Live)", "Some Artist", 200)]
    r = post(env, "/v1/warmup", {"title": "Song Name", "artist": "Some Artist"})
    assert r.status_code == 422 and r.get_json()["error"]["code"] == "match_rejected"
    env.yt.items = [song(VID, "Song Name", "Some Artist", 400)]
    r = post(env, "/v1/warmup", {"title": "Song Name", "artist": "Some Artist", "duration_ms": 200_000})
    assert r.status_code == 422
    assert env.extractor.calls == []


def test_duration_tolerance_is_max_8s_or_7_percent(env):
    env.yt.items = [song(VID, "Song Name", "Some Artist", 207)]  # 7s off a 200s track
    assert post(env, "/v1/warmup", {"title": "Song Name", "artist": "Some Artist", "duration_ms": 200_000}
                ).status_code in (200, 202)
    env.yt.items = [song(VID2, "Long Name", "Some Artist", 1_070)]  # 7% of 1000s = 70s
    assert post(env, "/v1/warmup", {"title": "Long Name", "artist": "Some Artist", "duration_ms": 1_000_000}
                ).status_code in (200, 202)


def test_null_metadata_fields_are_null(env):
    item = song(VID, "Song Name", "Some Artist", 0, album=None)
    item["thumbnails"] = []
    env.yt.items = [item]
    body = post(env, "/v1/warmup", {"title": "Song Name", "artist": "Some Artist"}).get_json()
    assert body["album"] is None and body["duration_ms"] is None and body["artwork_url"] is None


@pytest.mark.parametrize("body", [
    [], {}, {"video_id": "short"}, {"video_id": VID, "title": "x"}, {"title": "x"}, {"artist": "x"},
    {"title": "x", "artist": "y", "duration_ms": 0}, {"title": "x", "artist": "y", "duration_ms": "9"},
    {"title": "x", "artist": "y", "duration_ms": True}, {"title": "x", "artist": "y", "extra": 1},
    {"video_id": 12345678901},
])
def test_invalid_input_is_400_with_error_contract(env, body):
    r = post(env, "/v1/warmup", body)
    assert r.status_code == 400
    error = r.get_json()["error"]
    assert set(error) == {"code", "message", "retryable"} and error["retryable"] is False


def test_oversized_body_413_and_non_json_400(env):
    assert env.client.post("/v1/warmup", data=b"x" * 1_100_000, content_type="application/json").status_code == 413
    assert env.client.post("/v1/warmup", data=b"not json", content_type="application/json").status_code == 400


def test_upstream_errors_are_502_retryable_and_unknown_video_404(env):
    env.yt.fail = True
    r = post(env, "/v1/warmup", {"video_id": VID})
    assert r.status_code == 502 and r.get_json()["error"]["retryable"] and "Retry-After" in r.headers
    env.yt.fail = False
    assert post(env, "/v1/warmup", {"video_id": "zzzzzzzzzzz"}).status_code == 404


def test_invalid_or_unsupported_bearer_token_is_401_not_anonymous(env):
    r = post(env, "/v1/warmup", {"video_id": VID}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.get_json()["error"]["code"] == "invalid_token"
    assert env.extractor.calls == []


def test_duplicate_warmups_share_one_job_and_prepare_promotes(env):
    env.extractor.gate = threading.Event()
    a = post(env, "/v1/warmup", {"video_id": VID}).get_json()
    b = post(env, "/v1/warmup", {"video_id": VID}).get_json()
    c = post(env, "/v1/audio/prepare", {"video_id": VID}).get_json()
    assert a["job_id"] == b["job_id"] == c["job_id"] and c["pool"] == "requested"
    env.extractor.gate.set()
    wait_ready(env, a["job_id"])
    assert env.extractor.calls == [VID]
    assert env.cache.lookup(key()).pool == "requested"


def test_prepare_of_ready_warmup_returns_200_and_promotes_without_new_download(env):
    r = post(env, "/v1/warmup", {"video_id": VID})
    wait_ready(env, r.get_json()["job_id"])
    assert env.cache.lookup(key()).pool == "warmup"
    p = post(env, "/v1/audio/prepare", {"video_id": VID})
    assert p.status_code == 200 and p.get_json()["pool"] == "requested"
    assert env.extractor.calls == [VID]


def test_job_status_is_public_and_excludes_user_metadata(env):
    job_id = prepared(env).get_json()["job_id"]
    r = env.client.get(f"/v1/jobs/{job_id}")
    body = r.get_json()
    assert r.status_code == 200 and body["status"] == "ready" and body["video_id"] == VID
    assert not ({"title", "artists", "artist", "album", "artwork_url", "personalization_status"} & set(body))
    assert env.client.get("/v1/jobs/nonexistent").status_code == 404


# ---- completed-audio delivery ----

def test_get_missing_starts_job_and_returns_503_pending_then_serves(env):
    env.extractor.gate = threading.Event()
    r = env.client.get(f"/v1/audio/{VID}")
    assert r.status_code == 503 and r.get_json()["error"]["code"] == "pending" and "Retry-After" in r.headers
    env.extractor.gate.set()
    job = env.jobs.get(env.jobs._by_key[key()])
    wait_ready(env, job.job_id)
    assert env.client.get(f"/v1/audio/{VID}").data == PAYLOAD


def test_head_never_starts_a_job_and_does_not_refresh_lru(env):
    assert env.client.head(f"/v1/audio/{VID}").status_code == 404
    assert env.extractor.calls == [] and key() not in env.jobs._by_key
    prepared(env)
    before = env.cache.store.get_audio(key())["last_used"]
    env.cache.clock = lambda: before + 100
    r = env.client.head(f"/v1/audio/{VID}")
    assert r.status_code == 200 and int(r.headers["Content-Length"]) == len(PAYLOAD)
    assert env.cache.store.get_audio(key())["last_used"] == before
    env.client.get(f"/v1/audio/{VID}")
    assert env.cache.store.get_audio(key())["last_used"] == before + 100


def test_full_get_headers_ranges_and_invalid_range(env):
    prepared(env)
    full = env.client.get(f"/v1/audio/{VID}")
    assert full.status_code == 200 and full.data == PAYLOAD
    assert full.headers["Content-Length"] == str(len(PAYLOAD)) and full.headers["Accept-Ranges"] == "bytes"
    assert full.headers["Content-Type"] == "audio/mp4"
    r = env.client.get(f"/v1/audio/{VID}", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.data == PAYLOAD[10:20]
    assert r.headers["Content-Range"] == f"bytes 10-19/{len(PAYLOAD)}"
    r = env.client.get(f"/v1/audio/{VID}", headers={"Range": "bytes=1000-"})
    assert r.status_code == 206 and r.data == PAYLOAD[1000:]
    r = env.client.get(f"/v1/audio/{VID}", headers={"Range": "bytes=-16"})
    assert r.status_code == 206 and r.data == PAYLOAD[-16:]
    r = env.client.get(f"/v1/audio/{VID}", headers={"Range": f"bytes={len(PAYLOAD) + 10}-"})
    assert r.status_code == 416 and r.headers["Content-Range"] == f"bytes */{len(PAYLOAD)}"
    assert env.cache.store.all_leases() == []


def test_etag_conditional_and_if_range(env):
    prepared(env)
    first = env.client.get(f"/v1/audio/{VID}")
    etag = first.headers["ETag"]
    assert env.client.get(f"/v1/audio/{VID}", headers={"If-None-Match": etag}).status_code == 304
    ok = env.client.get(f"/v1/audio/{VID}", headers={"Range": "bytes=0-9", "If-Range": etag})
    assert ok.status_code == 206
    stale = env.client.get(f"/v1/audio/{VID}", headers={"Range": "bytes=0-9", "If-Range": '"other"'})
    assert stale.status_code == 200 and stale.data == PAYLOAD
    assert env.cache.store.all_leases() == []


def test_lease_is_held_until_response_closes_and_survives_eviction_pressure(env):
    prepared(env)
    response = env.client.get(f"/v1/audio/{VID}", buffered=False)
    assert len(env.cache.store.all_leases()) == 1
    path = env.cache.lookup(key()).path
    env.cache.prune(env.cache.clock() + 10 ** 9)  # requested audio has no TTL
    assert path.exists()
    response.close()  # client disconnect without reading the body
    assert env.cache.store.all_leases() == []


def test_concurrent_segmented_downloads_and_streams(env):
    prepared(env)
    def fetch(i):
        start = (i * 50) % 900
        r = env.client.get(f"/v1/audio/{VID}", headers={"Range": f"bytes={start}-{start + 49}"})
        return r.status_code, r.data == PAYLOAD[start:start + 50]
    with ThreadPoolExecutor(8) as pool:
        assert all(r == (206, True) for r in pool.map(fetch, range(24)))
    assert env.cache.store.all_leases() == []


def test_audio_url_rejects_invalid_ids(env):
    assert env.client.get("/v1/audio/bad").status_code == 400
    assert env.client.get("/v1/audio/..%2f..%2fetc").status_code in (400, 404)


def test_failed_job_is_reported_with_error_code_and_not_cached(env):
    class Boom(FakeExtractor):
        def download(self, *a):
            from jukes.extractor import ExtractionError
            raise ExtractionError("video_unavailable")
    env.jobs.extractor = Boom()
    r = post(env, "/v1/audio/prepare", {"video_id": VID})
    job = env.jobs.wait(r.get_json()["job_id"], 3)
    assert job.status == "failed"
    view = env.client.get(f"/v1/jobs/{job.job_id}").get_json()
    assert view["error"] == {"code": "video_unavailable", "retryable": False}
    assert env.client.get(f"/v1/audio/{VID}").status_code in (404, 503, 502)


# ---- legacy /audio/ ----

def test_legacy_video_id_serves_complete_file_with_range(env):
    r = env.client.get(f"/audio/?video_id={VID}&key=obsolete-secret")
    assert r.status_code == 200 and r.data == PAYLOAD and r.headers["X-Cache"] == "MISS"
    assert "obsolete-secret" not in repr(r.headers)
    hit = env.client.get(f"/audio/?video_id={VID}", headers={"Range": "bytes=0-3"})
    assert hit.status_code == 206 and hit.headers["X-Cache"] == "HIT"


def test_legacy_url_and_invalid_inputs(env):
    r = env.client.get(f"/audio/?url=https://youtu.be/{VID}")
    assert r.status_code == 200
    assert env.client.get("/audio/?url=https://example.com/watch?v=" + VID).status_code == 400
    assert env.client.get("/audio/?url=https://youtube.com/nothing").status_code == 400
    assert env.client.get("/audio/").status_code == 400
    assert env.client.get("/audio/?q=x&duration=abc").status_code == 400


def test_legacy_q_uses_seconds_duration_and_sets_metadata_headers(env):
    env.yt.items = [song(VID2, "Studio", "Artist", 400), song(VID, "Studio", "Artist", 205)]
    r = env.client.get("/audio/?q=studio artist&duration=200")
    assert r.status_code == 200 and r.headers["X-Video-Id"] == VID
    assert r.headers["X-Duration-Ms"] == "205000" and r.headers["X-Title"] == "Studio"


def test_legacy_info_is_side_effect_free(env):
    r = env.client.get(f"/audio/?video_id={VID}&info=1")
    body = r.get_json()
    assert body["video_id"] == VID and body["cached"] is False
    assert body["audio_url"].endswith(f"/audio/?video_id={VID}") and "key" not in body["audio_url"]
    env.yt.items = [song(VID, "Studio", "Artist", 205)]
    q = env.client.get("/audio/?q=studio&duration=200&info=1").get_json()
    assert q["title"] == "Studio" and q["duration_ms"] == 205_000
    assert env.extractor.calls == [] and env.jobs._by_key == {}


def test_legacy_pending_returns_503_after_timeout_and_job_continues(env):
    env.extractor.gate = threading.Event()
    r = env.client.get(f"/audio/?video_id={VID}&wait=1")
    assert r.status_code == 503 and "Retry-After" in r.headers
    env.extractor.gate.set()
    wait_ready(env, env.jobs._by_key[key()])
    assert env.client.get(f"/audio/?video_id={VID}").data == PAYLOAD


def test_legacy_head_inspects_only_completed_files(env):
    assert env.client.head(f"/audio/?video_id={VID}").status_code == 404
    assert env.extractor.calls == []
    prepared(env)
    r = env.client.head(f"/audio/?video_id={VID}")
    assert r.status_code == 200 and r.headers["Content-Length"] == str(len(PAYLOAD))
