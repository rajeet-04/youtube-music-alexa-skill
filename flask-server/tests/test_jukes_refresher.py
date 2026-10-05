"""Rare, irregular, need-driven cookie refresh."""

from __future__ import annotations

import io
import random
import sys
import time
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from jukes.cache import Cache  # noqa: E402
from jukes.config import CacheConfig  # noqa: E402
from jukes.extractor import Extractor, ExtractionError  # noqa: E402
from jukes.jobs import Jobs  # noqa: E402
from jukes.models import AudioKey  # noqa: E402
from jukes.refresher import DAY, HOUR, MINUTE, CookieRefresher  # noqa: E402

NOW = 2_000_000_000.0


class Status:
    def __init__(self, connected=True):
        self.connected = connected


class FakeCookies:
    def __init__(self, expiry=None):
        self.connected = True
        self.expiry = expiry
        self.replaced = []

    def status(self):
        return Status(self.connected)

    def earliest_auth_expiry(self):
        return self.expiry

    def replace(self, jar):
        self.replaced.append(jar)


class FakeBrowser:
    def __init__(self):
        self.calls = []
        self.refresh_code = 202
        self.states = ["refreshing", "refreshing", "captured"]
        self.export = (200, "# jar")

    def refresh(self):
        self.calls.append("refresh")
        return self.refresh_code

    def status(self):
        self.calls.append("status")
        return {"state": self.states.pop(0) if self.states else "captured"}

    def export_cookies(self):
        self.calls.append("export")
        return self.export


class Env:
    pass


@pytest.fixture
def env(tmp_path):
    cache = Cache(CacheConfig(database_path=tmp_path / "s.sqlite3", audio_dir=tmp_path / "a", min_free_disk_bytes=0))
    e = Env()
    e.clock = [NOW]
    e.cookies, e.browser = FakeCookies(), FakeBrowser()
    e.sleeps = []
    e.lease = [False]
    e.make = lambda seed=1, **kw: CookieRefresher(
        cache.store, e.cookies, e.browser, clock=lambda: e.clock[0], rng=random.Random(seed),
        lease_active=lambda: e.lease[0], poll_sleep=e.sleeps.append, **kw)
    e.cache = cache
    return e


def test_nothing_happens_without_a_need(env):
    r = env.make()
    assert r.tick() == "idle" and env.browser.calls == [] and r.state().next_due is None
    env.clock[0] += 400 * DAY
    assert r.tick() == "idle" and env.browser.calls == []  # no schedule, no maintenance by default


def test_one_cookie_failure_is_not_enough_two_schedule_a_random_delay(env):
    r = env.make()
    assert r.request() is False and r.state().next_due is None
    env.clock[0] += HOUR
    assert r.request() is True
    due = r.state().next_due
    assert env.clock[0] + 20 * MINUTE <= due <= env.clock[0] + 3 * HOUR
    assert r.tick() == "idle"  # not due yet: still no browser contact
    assert env.browser.calls == []


def test_old_failure_events_expire(env):
    r = env.make()
    r.request()
    env.clock[0] += 13 * HOUR  # outside the 12 h window
    assert r.request() is False


def test_due_attempt_refreshes_and_saves_through_validated_replace(env):
    r = env.make()
    r.request(); env.clock[0] += HOUR; r.request()
    env.clock[0] = r.state().next_due + 1
    assert r.tick() == "ok"
    assert env.cookies.replaced == ["# jar"] and env.browser.calls[0] == "refresh" and env.browser.calls[-1] == "export"
    st = r.state()
    assert st.failures == 0 and st.last_success and st.next_due is None and not st.needs_attention


def test_delays_are_random_not_a_fixed_cadence(env):
    dues = set()
    for seed in range(12):
        r = env.make(seed)
        r.request(); env.clock[0] += 1; r.request()
        dues.add(round(r.state().next_due - env.clock[0]))
        env.cache.store.transaction  # noqa: B018
        with env.cache.store.transaction() as c:
            c.execute("UPDATE jukes_cookie_refresh SET next_due=NULL, events='[]', attempts='[]', last_attempt=NULL")
    assert len(dues) >= 10  # essentially all different


def test_expiry_planning_lands_days_before_expiry_at_a_random_time(env):
    env.cookies.expiry = NOW + 30 * DAY
    seen = set()
    for seed in range(8):
        r = env.make(seed)
        r.cookies_changed()
        due = r.state().next_due
        assert NOW + 18 * DAY <= due <= NOW + 25 * DAY
        seen.add(round(due))
    assert len(seen) > 1
    env.cookies.expiry = NOW + 2 * HOUR  # already about to expire: soon, but not instant or negative
    r = env.make(3)
    r.cookies_changed()
    assert r.state().next_due >= NOW + HOUR


def test_session_cookies_without_expiry_create_no_schedule(env):
    env.cookies.expiry = None
    r = env.make()
    r.cookies_changed()
    assert r.state().next_due is None


def test_signed_out_profile_pauses_automation_and_never_retries(env):
    r = env.make()
    r.request(); env.clock[0] += 1; r.request()
    env.clock[0] = r.state().next_due + 1
    env.browser.states = ["refreshing", "reconnect_required"]
    assert r.tick() == "signed_out"
    st = r.state()
    assert st.needs_attention and st.reason == "signed_out" and st.next_due is None
    n = len(env.browser.calls)
    env.clock[0] += 30 * DAY
    assert r.tick() == "idle" and r.request() is False and len(env.browser.calls) == n
    r.cookies_changed()  # operator signed in again manually
    assert not r.state().needs_attention


def test_transient_failures_back_off_days_then_stop_after_three(env):
    r = env.make()
    env.browser.refresh_code = 503
    with env.cache.store.transaction() as c:
        c.execute("UPDATE jukes_cookie_refresh SET next_due=?", (NOW,))
    results = []
    for i in range(3):
        env.clock[0] = max(env.clock[0], r.state().next_due or env.clock[0]) + 3 * DAY + 1
        with env.cache.store.transaction() as c:
            c.execute("UPDATE jukes_cookie_refresh SET next_due=?", (env.clock[0] - 1,))
        results.append(r.tick())
        if i < 2:
            gap = r.state().next_due - env.clock[0]
            assert DAY <= gap <= 3 * DAY
    assert results == ["failed", "failed", "paused"]
    assert r.state().needs_attention and r.state().reason == "repeated_failures"


def test_at_most_two_attempts_per_day(env):
    r = env.make()
    for _ in range(2):
        with env.cache.store.transaction() as c:
            c.execute("UPDATE jukes_cookie_refresh SET next_due=?", (env.clock[0],))
        assert r.tick() == "ok"
        env.clock[0] += 2 * HOUR
    with env.cache.store.transaction() as c:
        c.execute("UPDATE jukes_cookie_refresh SET next_due=?", (env.clock[0],))
    calls = len(env.browser.calls)
    assert r.tick() == "capped" and len(env.browser.calls) == calls
    assert r.state().next_due >= env.clock[0] + 4 * HOUR


def test_stays_out_of_the_way_while_admin_uses_the_browser(env):
    r = env.make()
    with env.cache.store.transaction() as c:
        c.execute("UPDATE jukes_cookie_refresh SET next_due=?", (NOW,))
    env.lease[0] = True
    assert r.tick() == "admin_busy" and env.browser.calls == []
    assert r.state().next_due >= NOW + 30 * MINUTE
    assert r.run_now() == "admin_busy"


def test_polling_of_the_sidecar_is_irregular(env):
    r = env.make()
    env.browser.states = ["refreshing"] * 5 + ["captured"]
    r.run_now()
    assert len(set(env.sleeps)) == len(env.sleeps) >= 5 and all(1.5 <= s <= 4.0 for s in env.sleeps)


def test_failed_export_or_unvalidated_cookies_keep_the_old_jar(env):
    r = env.make()
    env.browser.export = (409, "")
    assert r.run_now() == "failed" and env.cookies.replaced == []

    def boom(jar):
        raise RuntimeError("rejected")
    env.cookies.replace = boom
    env.browser.export = (200, "# jar")
    assert r.run_now() == "failed"


def test_optional_maintenance_is_off_by_default_and_jittered_when_enabled(env):
    r = env.make(maintenance_days=0)
    r.cookies_changed()
    assert r.state().next_due is None
    dues = set()
    for seed in range(6):
        r2 = env.make(seed, maintenance_days=10)
        r2.cookies_changed()
        d = (r2.state().next_due - NOW) / DAY
        assert 7 <= d <= 16
        dues.add(round(d, 3))
    assert len(dues) > 1


def test_disconnected_cookies_never_trigger_browser_contact(env):
    r = env.make()
    env.cookies.connected = False
    assert r.request() is False
    with env.cache.store.transaction() as c:
        c.execute("UPDATE jukes_cookie_refresh SET next_due=?", (NOW,))
    assert r.tick() == "idle" and env.browser.calls == []


# --- extractor / jobs signal ---

class P:
    def __init__(self, stderr, rc=1):
        self.stdout, self.stderr, self.returncode, self.pid = io.BytesIO(b""), io.BytesIO(stderr), rc, 7

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def test_cookie_specific_failures_are_flagged_only_when_cookies_were_used(env):
    from jukes.extractor import CredentialSnapshot
    msg = b"ERROR: [youtube] x: The page needs to be reloaded."
    ex = Extractor(process_factory=lambda a, **k: P(msg), public_audio_probe=lambda k, t: True, media_probe=lambda p: {})
    key = AudioKey("cookie-flag", "p")
    with pytest.raises(ExtractionError) as with_cookies:
        ex.download(key, env.cache.reserve(key, requested=True), CredentialSnapshot(cookie_header="SID=a", generation=1))
    assert with_cookies.value.cookie_suspect is True
    ex2 = Extractor(process_factory=lambda a, **k: P(msg), public_audio_probe=lambda k, t: True, media_probe=lambda p: {})
    key2 = AudioKey("anon-flag", "p")
    with pytest.raises(ExtractionError) as anonymous:
        ex2.download(key2, env.cache.reserve(key2, requested=True), None)
    assert anonymous.value.cookie_suspect is False  # nothing to refresh: no cookies were sent


def test_jobs_report_cookie_suspect_failures_to_the_hook(env):
    calls = []

    class Failing:
        def download(self, key, destination, snapshot):
            raise ExtractionError("extraction_failed", cookie_suspect=True)

    jobs = Jobs(env.cache, Failing(), worker_count=1, on_cookie_suspect=lambda: calls.append(1))
    try:
        job = jobs.submit(AudioKey("hook-check", "p"), requested=True)
        assert jobs.wait(job.job_id, 3).status == "failed"
        assert calls == [1]
    finally:
        jobs.shutdown()
