"""Installation tokens, encrypted sessions, per-user context and radio (Task 4 integration)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from jukes.app import create_app  # noqa: E402
from jukes.cache import Cache  # noqa: E402
from jukes.config import CacheConfig  # noqa: E402
from jukes.credentials import Credentials  # noqa: E402
from jukes.extractor import DownloadResult  # noqa: E402
from jukes.identity import Identity  # noqa: E402
from jukes.jobs import Jobs  # noqa: E402
from jukes.limits import RateLimiter, client_address, parse_networks  # noqa: E402
from jukes.music import ANONYMOUS, Music  # noqa: E402
from jukes.personalization import ClientFactory, account_probe, build_auth_headers  # noqa: E402
from jukes.routes import Services, Settings  # noqa: E402

VID = "abcdefghijk"
COOKIE = "SID=abc; __Secure-3PAPISID=papisid-secret; HSID=zzz"


class FakeClient:
    """Stands in for YTMusic; remembers whose session it is."""

    def __init__(self, auth, context, fail=None):
        self.auth, self.context, self.fail = auth, context, fail
        self.calls = []

    def get_account_info(self):
        return {"accountName": "Tester"} if self.auth and "papisid" in self.auth["cookie"] else {}

    def get_watch_playlist(self, videoId, radio, limit):
        self.calls.append((videoId, limit))
        if self.auth and self.fail:
            raise self.fail
        who = "user" if self.auth else "anon"
        return {"tracks": [
            {"videoId": VID, "title": f"{who}-seed", "length": "3:20", "artists": [{"name": "A"}],
             "album": {"name": "Alb"}, "thumbnail": [{"url": "http://img"}]},
            {"videoId": "bad id", "title": "skipped"},
            {"videoId": "bbbbbbbbbbb", "title": f"{who}-2", "length": "4:00", "artists": [], "album": None},
        ]}


class Env:
    pass


@pytest.fixture
def env(tmp_path):
    key = Fernet.generate_key().decode()
    config = CacheConfig(database_path=tmp_path / "s.sqlite3", audio_dir=tmp_path / "audio",
                         min_free_disk_bytes=0, requested_limit_bytes=10_000, warmup_limit_bytes=5_000,
                         unknown_size_reservation_bytes=1_000, reservation_increment_bytes=1_000)
    cache = Cache(config)
    built = []

    def builder(auth, context):
        client = FakeClient(auth, context)
        built.append(client)
        return client

    identity = Identity(cache.store)
    creds = Credentials(cache.store, key, account_probe=lambda b: account_probe(b, client_builder=builder))
    factory = ClientFactory(creds, client_builder=builder)

    class NullExtractor:
        def download(self, *a):
            raise RuntimeError("not used")

    jobs = Jobs(cache, NullExtractor(), worker_count=1)
    services = Services(cache=cache, jobs=jobs, music=Music(factory), identity=identity, credentials=creds)
    e = Env()
    e.app = create_app(Settings(), services)
    e.client = e.app.test_client()
    e.services, e.built, e.key, e.cache, e.factory = services, built, key, cache, factory
    yield e
    jobs.shutdown()


def issue(env):
    r = env.client.post("/v1/installations")
    assert r.status_code == 201 and r.headers["Cache-Control"] == "no-store"
    return r.get_json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def connect(env, token, cookie=COOKIE, **extra):
    return env.client.put("/v1/me/youtube", json={"cookie": cookie, **extra}, headers=auth(token))


def radio(env, token=None, **body):
    body = {"video_id": VID, "limit": 5, **body}
    return env.client.post("/v1/recommendations", json=body, headers=auth(token) if token else {})


def test_token_is_issued_once_and_only_its_digest_is_stored(env):
    token = issue(env)
    raw = sqlite3.connect(env.cache.config.database_path).execute(
        "SELECT * FROM jukes_installations").fetchall()
    assert len(raw) == 1 and token not in repr(raw)
    assert env.client.get("/v1/me/youtube", headers=auth(token)).get_json() == {"connected": False}


def test_invalid_missing_or_revoked_token_is_401(env):
    assert env.client.get("/v1/me/youtube").status_code == 401
    assert env.client.get("/v1/me/youtube", headers=auth("bogus")).status_code == 401
    assert radio(env, "bogus").status_code == 401  # never silently anonymous
    token = issue(env)
    assert env.client.delete("/v1/me", headers=auth(token)).status_code == 204
    assert env.client.get("/v1/me/youtube", headers=auth(token)).status_code == 401


def test_connect_encrypts_session_and_status_is_redacted(env):
    token = issue(env)
    r = connect(env, token, account_index=1)
    assert r.status_code == 200 and r.get_json()["connected"] is True
    status = env.client.get("/v1/me/youtube", headers=auth(token))
    assert set(status.get_json()) == {"connected", "credential_generation", "connected_at"}
    assert "papisid" not in status.get_data(as_text=True)
    base = Path(env.cache.config.database_path)
    for file in base.parent.glob(base.name + "*"):  # main db plus any WAL/SHM
        assert b"papisid-secret" not in file.read_bytes()


def test_rejected_validation_keeps_previous_session_and_leaks_nothing(env):
    token = issue(env)
    assert connect(env, token).status_code == 200
    before = env.client.get("/v1/me/youtube", headers=auth(token)).get_json()
    bad = connect(env, token, cookie="SID=x; __Secure-3PAPISID=nope")  # logged-out 200 -> no accountName
    assert bad.status_code == 422 and "nope" not in bad.get_data(as_text=True)
    assert connect(env, token, cookie="SID=only").status_code == 422  # no PAPISID at all
    assert env.client.get("/v1/me/youtube", headers=auth(token)).get_json() == before


@pytest.mark.parametrize("body", [{}, {"cookie": ""}, {"cookie": COOKIE, "origin": "http://evil"},
                                  {"cookie": COOKIE, "account_index": -1}, {"cookie": COOKIE, "account_index": True},
                                  {"cookie": "a\nb"}])
def test_bundle_allowlist_and_validation(env, body):
    token = issue(env)
    r = env.client.put("/v1/me/youtube", json=body, headers=auth(token))
    assert r.status_code == 400


def test_auth_headers_are_server_built_from_an_allowlist():
    headers = build_auth_headers(COOKIE, 2)
    assert headers["origin"] == "https://music.youtube.com" and headers["x-goog-authuser"] == "2"
    assert set(headers) == {"cookie", "x-goog-authuser", "authorization", "user-agent", "accept-language",
                            "content-type", "origin", "x-origin"}


def test_radio_personalised_vs_anonymous_and_unconnected_token(env):
    anon = radio(env).get_json()
    assert anon["personalization_status"] == "anonymous" and anon["tracks"][0]["title"] == "anon-seed"
    token = issue(env)
    assert radio(env, token).get_json()["tracks"][0]["title"] == "anon-seed"  # token but no session
    connect(env, token)
    mine = radio(env, token).get_json()
    assert mine["personalization_status"] == "connected" and mine["tracks"][0]["title"] == "user-seed"
    assert [t["video_id"] for t in mine["tracks"]] == [VID, "bbbbbbbbbbb"]  # order kept, invalid dropped
    t = mine["tracks"][0]
    assert t["duration_ms"] == 200_000 and t["artists"] == ["A"] and t["album"] == "Alb"
    assert mine["tracks"][1]["album"] is None and mine["tracks"][1]["artists"] == []


def test_users_never_share_clients_and_disconnect_invalidates(env):
    a, b = issue(env), issue(env)
    connect(env, a)
    connect(env, b, cookie="SID=q; __Secure-3PAPISID=papisid-other")
    ra, rb = radio(env, a), radio(env, b)
    assert ra.get_json()["personalization_status"] == rb.get_json()["personalization_status"] == "connected"
    cookies = {c.auth["cookie"] for c in env.built if c.auth and c.calls}
    assert cookies == {COOKIE, "SID=q; __Secure-3PAPISID=papisid-other"}
    assert env.client.delete("/v1/me/youtube", headers=auth(a)).status_code == 204
    after = radio(env, a).get_json()
    assert after["personalization_status"] == "anonymous" and after["tracks"][0]["title"] == "anon-seed"
    assert radio(env, b).get_json()["tracks"][0]["title"] == "user-seed"
    # reconnecting advances the generation, so the old client is never reused
    generation_before = env.client.get("/v1/me/youtube", headers=auth(b)).get_json()["credential_generation"]
    connect(env, b, cookie="SID=q; __Secure-3PAPISID=papisid-new")
    assert env.client.get("/v1/me/youtube", headers=auth(b)).get_json()["credential_generation"] > generation_before
    radio(env, b)
    assert any(c.auth and "papisid-new" in c.auth["cookie"] and c.calls for c in env.built)


def test_expired_session_falls_back_to_anonymous_with_reconnect_status(env):
    token = issue(env)
    connect(env, token)
    for client in env.built:
        client.fail = RuntimeError("HTTP 401 Unauthorized")
    env.factory._clients.clear()

    real_build = env.factory._build
    env.factory._build = lambda auth, ctx: (lambda c: (setattr(c, "fail", RuntimeError("HTTP 401 unauthorized")), c)[1])(
        real_build(auth, ctx))
    body = radio(env, token).get_json()
    assert body["personalization_status"] == "reconnect_required"
    assert body["tracks"][0]["title"] == "anon-seed"
    # credentials are retained: an outage is not treated as disconnection
    assert env.client.get("/v1/me/youtube", headers=auth(token)).get_json()["connected"] is True


def test_transient_upstream_failure_does_not_mark_reconnect(env):
    token = issue(env)
    connect(env, token)
    env.factory._clients.clear()
    real_build = env.factory._build
    env.factory._build = lambda auth, ctx: (lambda c: (setattr(c, "fail", RuntimeError("connection reset")), c)[1])(
        real_build(auth, ctx))
    body = radio(env, token).get_json()
    assert body["personalization_status"] == "personalization_unavailable"
    assert env.client.get("/v1/me/youtube", headers=auth(token)).get_json()["connected"] is True


def test_wrong_encryption_key_never_overwrites_or_discloses(env, tmp_path):
    token = issue(env)
    connect(env, token)
    wrong = Credentials(env.cache.store, Fernet.generate_key(), account_probe=lambda b: True)
    env.services.credentials = wrong
    r = connect(env, token, cookie="SID=y; __Secure-3PAPISID=papisid-2")
    assert r.status_code == 503 and "papisid" not in r.get_data(as_text=True)
    env.services.credentials = Credentials(env.cache.store, env.key, account_probe=lambda b: True)
    assert env.client.get("/v1/me/youtube", headers=auth(token)).get_json()["connected"] is True


def test_missing_key_fails_closed(tmp_path):
    config = CacheConfig(database_path=tmp_path / "s.sqlite3", audio_dir=tmp_path / "audio", min_free_disk_bytes=0)
    cache = Cache(config)
    jobs = Jobs(cache, object(), worker_count=1, autostart=False)
    app = create_app(Settings(), Services(cache=cache, jobs=jobs, music=Music(lambda c: FakeClient(None, c))))
    client = app.test_client()
    assert client.post("/v1/installations").status_code == 503
    assert client.put("/v1/me/youtube", json={"cookie": COOKIE}).status_code in (401, 503)
    assert client.post("/v1/recommendations", json={"video_id": VID}).status_code == 200  # anonymous still works


@pytest.mark.parametrize("body", [{}, {"video_id": "x"}, {"video_id": VID, "limit": 0}, {"video_id": VID, "limit": 101},
                                  {"video_id": VID, "limit": "5"}, {"video_id": VID, "limit": True},
                                  {"video_id": VID, "extra": 1}])
def test_recommendation_input_bounds(env, body):
    assert env.client.post("/v1/recommendations", json=body).status_code == 400


def test_radio_limit_is_applied_and_responses_are_no_store(env):
    r = radio(env, limit=1)
    assert len(r.get_json()["tracks"]) == 1 and r.headers["Cache-Control"] == "no-store"


def test_issuance_is_rate_limited_per_caller(env):
    statuses = [env.client.post("/v1/installations").status_code for _ in range(7)]
    assert statuses[:5] == [201] * 5 and statuses[5:] == [429, 429]
    limited = env.client.post("/v1/installations")
    assert limited.get_json()["error"]["retryable"] and "Retry-After" in limited.headers


def test_forged_forwarding_headers_cannot_bypass_limits_unless_peer_is_trusted(env):
    forged = lambda i: {"CF-Connecting-IP": f"9.9.9.{i}", "X-Forwarded-For": f"8.8.8.{i}"}
    codes = [env.client.post("/v1/installations", headers=forged(i)).status_code for i in range(7)]
    assert codes[5:] == [429, 429]


def test_client_address_trusts_headers_only_from_configured_proxies():
    trusted = parse_networks(["10.0.0.0/8", "172.18.0.0/16"])
    hdr = {"cf": "203.0.113.5", "xff": "1.1.1.1, 203.0.113.5, 10.1.1.1"}
    assert client_address("10.2.3.4", hdr, trusted) == "203.0.113.5"
    assert client_address("198.51.100.9", hdr, trusted) == "198.51.100.9"
    assert client_address("10.2.3.4", {"cf": None, "xff": "6.6.6.6, 203.0.113.5, 10.1.1.1"}, trusted) == "203.0.113.5"
    assert client_address("10.2.3.4", {"cf": "junk", "xff": None}, trusted) == "10.2.3.4"
    assert client_address("10.2.3.4", {"cf": None, "xff": "junk"}, trusted) == "10.2.3.4"


def test_rate_limiter_window():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert [limiter.check("b", "k", 2) for _ in range(3)][:2] == [0, 0]
    assert limiter.check("b", "k", 2) > 0
    now[0] = 61
    assert limiter.check("b", "k", 2) == 0
    assert limiter.check("b", "other", 2) == 0


def test_public_and_personalised_requests_share_one_audio_job(env):
    from jukes.models import AudioKey
    from jukes.routes import AUDIO_POLICY

    class Quick:
        def download(self, key, destination, snapshot):
            destination.write(b"\x00\x00\x00\x18ftypM4A " + b"x" * 50)
            return DownloadResult(destination.path, 58, "mp4", "audio/mp4", 1.0, "aac", "default")

    env.services.jobs.extractor = Quick()
    env.services.music = Music(lambda ctx: type("C", (), {"get_song": lambda s, v: {"videoDetails": {
        "videoId": v, "title": "T", "author": "A", "lengthSeconds": "5"}}})())
    token = issue(env)
    connect(env, token)
    app = create_app(Settings(), env.services)
    c = app.test_client()
    a = c.post("/v1/audio/prepare", json={"video_id": VID})
    b = c.post("/v1/audio/prepare", json={"video_id": VID}, headers=auth(token))
    assert a.get_json()["job_id"] == b.get_json()["job_id"]
    env.services.jobs.wait(a.get_json()["job_id"], 3)
    assert b.get_json()["personalization_status"] == "connected"
    assert a.get_json()["personalization_status"] == "anonymous"
    job = c.get(f"/v1/jobs/{a.get_json()['job_id']}").get_json()
    assert "personalization_status" not in job and "papisid" not in json.dumps(job)
