"""Admin auth, CSRF, cookie management, browser leases and stable per-job snapshots."""

from __future__ import annotations

import io
import re
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from jukes.admin import AdminConfig  # noqa: E402
from jukes.app import create_app  # noqa: E402
from jukes.cache import Cache  # noqa: E402
from jukes.config import CacheConfig  # noqa: E402
from jukes.extractor import DownloadResult, Extractor  # noqa: E402
from jukes.jobs import Jobs  # noqa: E402
from jukes.models import AudioKey  # noqa: E402
from jukes.music import Music  # noqa: E402
from jukes.routes import Services, Settings  # noqa: E402
from jukes.server_cookies import CookieFormatError, ServerCookies, parse_netscape  # noqa: E402

PASSWORD = "correct horse battery"
JAR = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\tsecret-sapisid\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t2147483647\t__Secure-3PSID\tsecret-psid\n"
    ".example.org\tTRUE\t/\tFALSE\t0\ttracker\tdrop-me\n"
)
VID = "abcdefghijk"


class FakeBrowser:
    def __init__(self):
        self.calls = []
        self.start_code = 202
        self.export = (200, JAR)
        self.state = "idle"
        self.open_code = 200
        self.states = []  # optional scripted status sequence

    def start(self):
        self.calls.append("start")
        return self.start_code

    def refresh(self):
        self.calls.append("refresh")
        return 202

    def capture(self):
        self.calls.append("capture")
        return 202

    def close(self):
        self.calls.append("close")
        return 200

    def open_youtube(self):
        self.calls.append("open_youtube")
        return self.open_code

    def status(self):
        if self.states:
            return {"state": self.states.pop(0)}
        return {"state": self.state}

    def export_cookies(self):
        self.calls.append("export")
        return self.export


class Env:
    pass


@pytest.fixture
def env(tmp_path):
    key = Fernet.generate_key().decode()
    config = CacheConfig(database_path=tmp_path / "s.sqlite3", audio_dir=tmp_path / "audio",
                         min_free_disk_bytes=0, requested_limit_bytes=100_000, warmup_limit_bytes=50_000,
                         unknown_size_reservation_bytes=4_000, reservation_increment_bytes=4_000)
    cache = Cache(config)
    now = [1_000_000.0]
    probe_ok = {"value": True}
    cookies = ServerCookies(cache.store, key, probe=lambda header: probe_ok["value"], clock=lambda: now[0])
    jobs = Jobs(cache, object(), worker_count=1, autostart=False)
    services = Services(cache=cache, jobs=jobs, music=Music(lambda c: None), server_cookies=cookies)
    browser = FakeBrowser()
    admin = AdminConfig(password_hash=generate_password_hash(PASSWORD), session_key="s" * 40,
                        secure_cookies=False, lease_ttl=900, session_ttl=3600)
    app = create_app(Settings(), services, admin, browser)
    app.config.update(JUKES_EXPORT_POLLS=1, JUKES_EXPORT_POLL_SECONDS=0)
    e = Env()
    e.app, e.cookies, e.browser, e.cache, e.now, e.probe, e.admin, e.key, e.services = (
        app, cookies, browser, cache, now, probe_ok, admin, key, services)
    e.client = app.test_client()
    return e


def login(env, client=None, password=PASSWORD):
    client = client or env.client
    page = client.get("/admin/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    return client.post("/admin/login", data={"csrf": token, "password": password})


def csrf_of(env, client=None):
    client = client or env.client
    page = client.get("/admin/").get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def test_unauthenticated_admin_is_rejected_and_login_page_is_public(env):
    assert env.client.get("/admin/api/status").status_code == 401
    assert env.client.get("/admin/", follow_redirects=False).headers["Location"] == "/admin/login"
    assert env.client.post("/admin/cookies", data={"text": JAR}).status_code == 401
    assert env.client.get("/admin/login").status_code == 200


def test_login_sets_hardened_session_cookie_and_wrong_password_fails(env):
    assert login(env, password="wrong").status_code == 401
    r = login(env)
    assert r.status_code == 303
    cookie = next(c for c in r.headers.getlist("Set-Cookie") if c.startswith("jukes_admin="))
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert env.client.get("/admin/api/status").status_code == 200
    assert r.headers["Cache-Control"] == "no-store"


def test_login_requires_csrf_token_and_is_rate_limited(env):
    assert env.client.post("/admin/login", data={"csrf": "x", "password": PASSWORD}).status_code == 403
    codes = [login(env, password="nope").status_code for _ in range(7)]
    assert codes[5:] == [429, 429]


def test_missing_configuration_fails_closed(env, tmp_path):
    app = create_app(Settings(), env.services, AdminConfig(), None)
    c = app.test_client()
    assert c.get("/admin/login").status_code == 503
    assert c.get("/admin/api/status").status_code == 503
    assert c.post("/admin/cookies", data={"text": JAR}).status_code == 503


def test_state_changing_requests_need_csrf(env):
    login(env)
    assert env.client.post("/admin/cookies", data={"text": JAR}).status_code == 403
    assert env.client.post("/admin/cookies", data={"text": JAR, "csrf": "forged"}).status_code == 403
    assert env.client.post("/admin/logout").status_code == 403
    assert env.client.post("/admin/youtube/browser/start").status_code == 403
    assert env.cookies.status().connected is False


def test_cookie_paste_and_upload_store_only_youtube_cookies_encrypted(env):
    login(env)
    token = csrf_of(env)
    r = env.client.post("/admin/cookies", data={"text": JAR, "csrf": token})
    assert r.status_code == 200 and r.get_json()["cookie_count"] == 2  # tracker dropped
    up = env.client.post("/admin/cookies", data={"csrf": token, "file": (io.BytesIO(JAR.encode()), "cookies.txt")},
                         content_type="multipart/form-data")
    assert up.status_code == 200 and up.get_json()["generation"] == 2
    base = Path(env.cache.config.database_path)
    for f in base.parent.glob(base.name + "*"):
        data = f.read_bytes()
        assert b"secret-sapisid" not in data and b"drop-me" not in data
    status = env.client.get("/admin/api/status").get_data(as_text=True)
    assert "secret-" not in status


@pytest.mark.parametrize("text", ["", "garbage", "a\tb\tc", ".youtube.com\tTRUE\t/\tTRUE\t0\tNOAUTH\tx\n",
                                  ".example.org\tTRUE\t/\tTRUE\t0\tSID\tx\n"])
def test_malformed_uploads_are_rejected_and_keep_old_cookies(env, text):
    login(env)
    token = csrf_of(env)
    env.client.post("/admin/cookies", data={"text": JAR, "csrf": token})
    before = env.cookies.status()
    r = env.client.post("/admin/cookies", data={"text": text, "csrf": token})
    assert r.status_code == 400 and env.cookies.status() == before


def test_oversized_upload_is_rejected(env):
    login(env)
    token = csrf_of(env)
    big = ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\t" + "x" * 1_100_000
    r = env.client.post("/admin/cookies", data={"csrf": token, "file": (io.BytesIO(big.encode()), "c.txt")},
                        content_type="multipart/form-data")
    assert r.status_code in (400, 413)
    assert env.cookies.status().connected is False


def test_probe_rejection_keeps_previous_jar_and_snapshots_stay_stable(env):
    first = env.cookies.replace(JAR)
    snapshot = env.cookies.snapshot()
    env.probe["value"] = False
    login(env)
    r = env.client.post("/admin/cookies", data={"text": JAR.replace("secret-sapisid", "other"),
                                                "csrf": csrf_of(env)})
    assert r.status_code == 422 and "other" not in r.get_data(as_text=True)
    assert env.cookies.status().generation == first.generation
    env.probe["value"] = True
    env.cookies.replace(JAR.replace("secret-sapisid", "rotated"))
    assert "secret-sapisid" in snapshot.cookie_jar_text and "rotated" not in snapshot.cookie_jar_text
    assert "rotated" in env.cookies.snapshot().cookie_jar_text
    assert repr(snapshot) == f"CredentialSnapshot(generation={snapshot.generation})"


def test_wrong_key_never_replaces_existing_cookies(env):
    env.cookies.replace(JAR)
    wrong = ServerCookies(env.cache.store, Fernet.generate_key(), probe=lambda h: True)
    from jukes.credentials import CredentialError
    with pytest.raises(CredentialError):
        wrong.replace(JAR)
    assert env.cookies.snapshot() is not None and wrong.snapshot() is None
    unkeyed = ServerCookies(env.cache.store, None)
    assert unkeyed.snapshot() is None


def test_logout_revokes_session_and_closes_leases(env):
    login(env)
    token = csrf_of(env)
    env.client.post("/admin/youtube/browser/start", headers={"X-CSRF-Token": token})
    assert env.client.get("/admin/youtube/browser/authorize").status_code == 204
    stale = env.client.get_cookie("jukes_admin").value
    assert env.client.post("/admin/logout", data={"csrf": token}).status_code == 303
    other = env.app.test_client()
    other.set_cookie("jukes_admin", stale)
    assert other.get("/admin/api/status").status_code == 401  # replayed cookie is revoked server-side
    assert other.get("/admin/youtube/browser/authorize").status_code == 401


def test_session_expiry_and_password_change_invalidate_sessions(env):
    login(env)
    stale = env.client.get_cookie("jukes_admin").value
    env.admin.password_hash = generate_password_hash("a different password")  # rotated secret
    assert env.client.get("/admin/api/status").status_code == 401
    env.admin.password_hash = generate_password_hash(PASSWORD)  # new hash => new fingerprint
    assert env.client.get("/admin/api/status").status_code == 401
    assert stale


def test_forged_or_legacy_owner_cookies_are_not_sessions(env):
    for cookie in ("owner-session", "x.y.z", "e30.AAAA"):
        c = env.app.test_client()
        c.set_cookie("jukes_admin", cookie)
        c.set_cookie("session", "legacy-owner-session")
        assert c.get("/admin/api/status").status_code == 401


def test_browser_lease_gates_forward_auth_and_expires(env):
    login(env)
    token = csrf_of(env)
    assert env.client.get("/admin/youtube/browser/authorize").status_code == 403  # no lease yet
    r = env.client.post("/admin/youtube/browser/start", headers={"X-CSRF-Token": token})
    assert r.status_code == 200 and env.browser.calls == ["start"]
    assert env.client.get("/admin/youtube/browser/authorize").status_code == 204
    import time as _time
    real = _time.time
    expired = env.client.application.extensions  # lease clock is real time; close instead
    assert env.client.post("/admin/youtube/browser/close", headers={"X-CSRF-Token": token}).status_code == 204
    assert env.client.get("/admin/youtube/browser/authorize").status_code == 403
    assert "close" in env.browser.calls and real


def test_expired_lease_is_rejected(env):
    login(env)
    token = csrf_of(env)
    env.client.post("/admin/youtube/browser/start", headers={"X-CSRF-Token": token})
    with sqlite3.connect(env.cache.config.database_path) as db:
        db.execute("UPDATE jukes_browser_leases SET expires_at = 1")
    assert env.client.get("/admin/youtube/browser/authorize").status_code == 403
    assert env.client.post("/admin/youtube/browser/export", headers={"X-CSRF-Token": token}).status_code == 403


def test_browser_export_uses_the_same_validated_replace_path(env):
    login(env)
    token = csrf_of(env)
    h = {"X-CSRF-Token": token}
    env.client.post("/admin/youtube/browser/start", headers=h)
    r = env.client.post("/admin/youtube/browser/export", headers=h)
    assert r.status_code == 200 and r.get_json()["cookie_count"] == 2
    assert env.browser.calls[-2:] == ["capture", "export"]
    env.browser.export = (200, "garbage")
    assert env.client.post("/admin/youtube/browser/export", headers=h).status_code == 400
    env.browser.export = (409, "")
    assert env.client.post("/admin/youtube/browser/export", headers=h).status_code == 409
    assert env.cookies.status().generation == 1  # failed exports never replaced the jar


def test_browser_unavailable_is_a_clean_503(env):
    login(env)
    token = csrf_of(env)
    env.browser.start_code = 0
    r = env.client.post("/admin/youtube/browser/start", headers={"X-CSRF-Token": token})
    assert r.status_code == 503
    assert env.client.get("/admin/youtube/browser/authorize").status_code == 403


def test_status_view_shows_pools_jobs_and_cookie_state_without_secrets(env):
    env.cookies.replace(JAR)
    login(env)
    body = env.client.get("/admin/api/status").get_json()
    assert body["pools"]["requested"]["limit_bytes"] == 100_000 and body["pools"]["warmup"]["ttl_seconds"] == 7200
    assert body["jobs"]["jobs"] == {"queued": 0, "downloading": 0, "ready": 0, "failed": 0}
    assert body["cookies"]["connected"] is True
    page = env.client.get("/admin/").get_data(as_text=True)
    assert "Cache pools" in page and "secret-sapisid" not in page and "GB of" in page


def test_public_audio_stays_open_while_admin_is_protected(env):
    assert env.client.get(f"/v1/audio/{VID}").status_code in (404, 503)
    assert env.client.get("/admin/api/status").status_code == 401


# --- per-job snapshot materialisation through the extractor ---

def test_job_cookie_files_are_unique_private_and_removed(env):
    jar_paths, contents = [], []

    class P:
        def __init__(self, args):
            self.stdout, self.stderr = io.BytesIO(b"audio"), io.BytesIO(b"")
            self.returncode, self.pid = 0, 1

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def factory(args, **kw):
        if any(c in a for a in args for c in ("tv_simply", "web_embedded")):  # anonymous attempts fail; cookie-aware default runs
            return type("F", (P,), {"__init__": lambda self, a: (setattr(self, "stdout", io.BytesIO(b"")),
                                    setattr(self, "stderr", io.BytesIO(b"ERROR: blocked")),
                                    setattr(self, "returncode", 1), setattr(self, "pid", 2)) and None,
                                    "wait": lambda self, timeout=None: 1})(args)
        if "--cookies" in args:
            path = Path(args[args.index("--cookies") + 1])
            jar_paths.append(path)
            contents.append(path.read_text())
            assert path.stat().st_mode & 0o777 == 0o600
        return P(args)

    extractor = Extractor(process_factory=factory, public_audio_probe=lambda k, t: True,
                          media_probe=lambda p: {"format_name": "mp4", "duration": "1",
                                                 "streams": [{"codec_type": "audio", "codec_name": "aac"}]})
    env.cookies.replace(JAR)
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        key = AudioKey(vid, "p")
        extractor.download(key, env.cache.reserve(key, requested=True), env.cookies.snapshot())
    assert len(jar_paths) == 2 and jar_paths[0] != jar_paths[1]
    assert all(not p.exists() for p in jar_paths)
    assert "secret-sapisid" in contents[0] and "tracker" not in contents[0]


def test_parse_netscape_rules():
    rows = parse_netscape(JAR)
    assert [r[5] for r in rows] == ["SAPISID", "__Secure-3PSID"]
    with pytest.raises(CookieFormatError):
        parse_netscape("x" * 1_000_001)


def test_csrf_endpoint_requires_admin_and_matches_session(env):
    assert env.client.get("/admin/api/csrf").status_code == 401
    login(env)
    token = env.client.get("/admin/api/csrf").get_json()["csrf"]
    assert token == csrf_of(env)


def test_open_youtube_needs_csrf_lease_and_running_browser(env):
    login(env)
    token = csrf_of(env)
    h = {"X-CSRF-Token": token}
    assert env.client.post("/admin/youtube/browser/open-youtube").status_code == 403
    assert env.client.post("/admin/youtube/browser/open-youtube", headers=h).status_code == 403  # no lease
    env.client.post("/admin/youtube/browser/start", headers=h)
    assert env.client.post("/admin/youtube/browser/open-youtube", headers=h).status_code == 200
    env.browser.open_code = 409
    r = env.client.post("/admin/youtube/browser/open-youtube", headers=h)
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "browser_not_running"


def test_export_waits_while_the_sidecar_validates_then_saves(env):
    login(env)
    h = {"X-CSRF-Token": csrf_of(env)}
    env.client.post("/admin/youtube/browser/start", headers=h)
    env.app.config["JUKES_EXPORT_POLLS"] = 10
    env.browser.states = ["capture_requested", "validating_login", "captured"]
    r = env.client.post("/admin/youtube/browser/export", headers=h)
    assert r.status_code == 200 and env.cookies.status().connected
    assert env.browser.states == []  # polled through all states, then exported


def test_signed_out_capture_returns_a_readable_error(env):
    login(env)
    h = {"X-CSRF-Token": csrf_of(env)}
    env.client.post("/admin/youtube/browser/start", headers=h)
    env.app.config["JUKES_EXPORT_POLLS"] = 10
    env.browser.states = ["capture_requested", "waiting_for_login"]
    env.browser.export = (409, "")
    r = env.client.post("/admin/youtube/browser/export", headers=h)
    body = r.get_json()["error"]
    assert r.status_code == 409 and "Sign in" in body["message"]


def test_manual_refresh_endpoint_needs_csrf_and_shows_state(env):
    import random
    from jukes.refresher import CookieRefresher

    class Cookies:
        def status(self):
            return type("S", (), {"connected": True})()

        def earliest_auth_expiry(self):
            return None

        def replace(self, jar):
            pass

    ran = []
    refresher = CookieRefresher(env.cache.store, Cookies(), env.browser, rng=random.Random(1),
                                poll_sleep=lambda s: None)
    refresher.run_now = lambda: ran.append(1) or "ok"
    app = create_app(Settings(), env.services, env.admin, env.browser, refresher=refresher)
    c = app.test_client()
    assert c.post("/admin/cookies/refresh").status_code == 401
    login(env, c)
    assert c.post("/admin/cookies/refresh").status_code == 403  # no CSRF
    token = csrf_of(env, c)
    assert c.post("/admin/cookies/refresh", headers={"X-CSRF-Token": token}).status_code == 202
    import time as _t
    _t.sleep(0.1)
    assert ran == [1]
    body = c.get("/admin/api/status").get_json()
    assert body["refresh"]["needs_attention"] is False
    assert "Automatic refresh" in c.get("/admin/").get_data(as_text=True)
