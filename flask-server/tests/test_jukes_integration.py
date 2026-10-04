"""End-to-end flows through the real app, cache, coordinator and routes (fake extractor/YouTube)."""

from __future__ import annotations

import sys
import threading
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

BODY = b"\x00\x00\x00\x18ftypM4A " + bytes(range(256)) * 8  # ~2 KB


class Extractor:
    def __init__(self):
        self.calls, self.gate = [], None

    def download(self, key, destination, snapshot):
        self.calls.append(key.video_id)
        if self.gate:
            assert self.gate.wait(5)
        destination.write(BODY)
        return DownloadResult(destination.path, len(BODY), "mp4", "audio/mp4", 1.0, "aac", "default")


class YT:
    def get_watch_playlist(self, videoId, limit=1, radio=False):
        return {"tracks": [{"videoId": videoId, "title": f"T-{videoId}", "artists": [{"name": "Artist"}],
                            "length": "1:40"}]}

    def search(self, *a, **k):
        return []


def buffered(client):
    raw = client.open

    def open_(*args, **kwargs):  # close responses like a real WSGI server so leases are released
        kwargs.setdefault("buffered", True)
        return raw(*args, **kwargs)

    client.open = open_
    return client


def build(tmp_path, extractor, *, requested=100_000, warmup=5_000):
    config = CacheConfig(database_path=tmp_path / "s.sqlite3", audio_dir=tmp_path / "audio", min_free_disk_bytes=0,
                         requested_limit_bytes=requested, warmup_limit_bytes=warmup,
                         unknown_size_reservation_bytes=2_048, reservation_increment_bytes=2_048)
    cache = Cache(config)
    jobs = Jobs(cache, extractor, worker_count=2)
    services = Services(cache=cache, jobs=jobs, music=Music(lambda c: YT()))
    app = create_app(Settings(legacy_wait_seconds=0.2), services)
    return app, cache, jobs


@pytest.fixture
def stack(tmp_path):
    extractor = Extractor()
    app, cache, jobs = build(tmp_path, extractor)
    yield buffered(app.test_client()), cache, jobs, extractor, tmp_path
    if extractor.gate:
        extractor.gate.set()
    jobs.shutdown()


V1, V2, V3 = "a" * 11, "b" * 11, "c" * 11


def test_warmup_then_prepare_then_range_playback_with_one_download(stack):
    client, cache, jobs, extractor, _ = stack
    extractor.gate = threading.Event()
    warm = client.post("/v1/warmup", json={"video_id": V1})
    assert warm.status_code == 202 and warm.get_json()["pool"] == "warmup"
    prep = client.post("/v1/audio/prepare", json={"video_id": V1})  # user taps play mid-download
    assert prep.status_code == 202 and prep.get_json()["job_id"] == warm.get_json()["job_id"]
    assert prep.get_json()["pool"] == "requested"
    extractor.gate.set()
    jobs.wait(prep.get_json()["job_id"], 3)
    poll = client.get(f"/v1/jobs/{prep.get_json()['job_id']}").get_json()
    assert poll["status"] == "ready" and poll["pool"] == "requested"
    audio_url = poll["audio_url"].split("://", 1)[1].split("/", 1)[1]
    got = client.get("/" + audio_url, headers={"Range": "bytes=100-199"})
    assert got.status_code == 206 and got.data == BODY[100:200]
    assert extractor.calls == [V1]  # warmup + promotion + playback = one extraction
    assert cache.usage() == {"requested": len(BODY), "warmup": 0}  # bytes counted once, in the new pool


def test_requested_audio_outlives_the_warmup_ttl_and_warmup_expires(stack):
    client, cache, jobs, extractor, _ = stack
    for vid, route in ((V1, "/v1/audio/prepare"), (V2, "/v1/warmup")):
        jobs.wait(client.post(route, json={"video_id": vid}).get_json()["job_id"], 3)
    cache.prune(cache.clock() + 7_201)
    assert cache.lookup(AudioKey(V1, AUDIO_POLICY)) is not None
    assert cache.lookup(AudioKey(V2, AUDIO_POLICY)) is None


def test_pools_are_independent_in_the_running_service(tmp_path):
    extractor = Extractor()
    app, cache, jobs = build(tmp_path, extractor, requested=10_000, warmup=4_200)
    try:
        client = buffered(app.test_client())
        for vid in (V1, V2, V3):  # three ~2 KB warmups cannot all fit in 4.2 KB with reservation headroom
            r = client.post("/v1/warmup", json={"video_id": vid})
            jobs.wait(r.get_json()["job_id"], 3)
        warm = [v for v in (V1, V2, V3) if (e := cache.lookup(AudioKey(v, AUDIO_POLICY))) and e.pool == "warmup"]
        assert len(warm) == 1  # LRU evicted older warmups only
        r = client.post("/v1/audio/prepare", json={"video_id": "d" * 11})
        jobs.wait(r.get_json()["job_id"], 3)
        assert cache.usage()["requested"] == len(BODY) and cache.usage()["warmup"] <= 4_200
        assert cache.lookup(AudioKey(warm[0], AUDIO_POLICY)) is not None  # requested work did not evict it
    finally:
        jobs.shutdown()


def test_restart_serves_existing_audio_and_recovers_interrupted_job_once(tmp_path):
    extractor = Extractor()
    app, cache, jobs = build(tmp_path, extractor)
    client = buffered(app.test_client())
    jobs.wait(client.post("/v1/audio/prepare", json={"video_id": V1}).get_json()["job_id"], 3)
    # Simulate a crash: a downloading job row left behind by a dead owner.
    with cache.store.transaction() as c:
        c.execute("INSERT INTO jukes_jobs (job_id, video_id, policy, requested, status, created_at, updated_at, "
                  "recovery_count, owner_pid, owner_start) VALUES ('dead-job', ?, ?, 1, 'downloading', 1, 1, 0, 0, '')",
                  (V2, AUDIO_POLICY))
    jobs.shutdown()

    from jukes import guard  # a new process would take the lock; same-process rebuild reuses the DB directly
    extractor2 = Extractor()
    app2, cache2, jobs2 = build(tmp_path, extractor2)
    try:
        c2 = buffered(app2.test_client())
        assert c2.get(f"/v1/audio/{V1}").data == BODY  # survived restart, no re-download
        assert extractor2.calls.count(V1) == 0
        recovered = jobs2.wait("dead-job", 3)
        assert recovered.status == "ready" and recovered.recovery_count == 1
        assert c2.get(f"/v1/audio/{V2}").data == BODY
        assert guard
    finally:
        jobs2.shutdown()


def test_legacy_and_versioned_paths_share_the_same_cached_file(stack):
    client, cache, jobs, extractor, _ = stack
    assert client.get(f"/audio/?video_id={V1}").data == BODY  # legacy path downloads
    assert client.get(f"/v1/audio/{V1}").data == BODY          # versioned path reuses it
    assert client.post("/v1/audio/prepare", json={"video_id": V1}).status_code == 200
    assert extractor.calls == [V1]
    assert cache.store.all_leases() == []


def test_failed_download_then_retry_creates_a_fresh_job(stack):
    client, cache, jobs, extractor, _ = stack
    from jukes.extractor import ExtractionError
    original = extractor.download
    extractor.download = lambda *a: (_ for _ in ()).throw(ExtractionError("extraction_failed"))
    first = client.post("/v1/audio/prepare", json={"video_id": V1}).get_json()
    assert jobs.wait(first["job_id"], 3).status == "failed"
    assert client.get(f"/v1/jobs/{first['job_id']}").get_json()["error"]["code"] == "extraction_failed"
    extractor.download = original
    second = client.post("/v1/audio/prepare", json={"video_id": V1}).get_json()
    assert second["job_id"] != first["job_id"]
    assert jobs.wait(second["job_id"], 3).status == "ready"
