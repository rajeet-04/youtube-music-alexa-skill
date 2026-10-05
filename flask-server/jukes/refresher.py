"""Rare, irregular, need-driven refresh of the operator's saved YouTube session.

Design goals (deliberately *not* a scheduler):
- no fixed interval and no cron-like cadence: every delay is drawn at random from a wide range;
- the YouTube-facing browser visit happens only when a need exists: persistent auth cookies are
  close to expiry, or downloads using the cookies have repeatedly failed in a cookie-specific way;
- no retry loops: a failed attempt waits a random 1-3 days, three failures stop automation, and a
  signed-out profile stops it at once (a human must sign in again, never a script);
- hard caps: at most 2 attempts per rolling 24 h, never while the admin is using the browser;
- the background thread wakes at random times only to read local state; waking never touches YouTube.

No password is ever stored or typed: it only reuses the signed-in Chromium profile on disk.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

DAY = 86_400.0
HOUR = 3_600.0
MINUTE = 60.0

MAX_ATTEMPTS_PER_DAY = 2
MAX_FAILURES = 3
EVENTS_NEEDED = 2          # cookie-specific download failures required (within the window) ...
EVENT_WINDOW = 12 * HOUR   # ... before a refresh is even considered
WORKING_STATES = ("refreshing", "capture_requested", "validating_login")


@dataclass(frozen=True)
class RefreshState:
    next_due: float | None
    last_attempt: float | None
    last_success: float | None
    failures: int
    needs_attention: bool
    reason: str | None


class CookieRefresher:
    def __init__(
        self,
        store,
        cookies,
        browser,
        *,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        lease_active: Callable[[], bool] | None = None,
        maintenance_days: float = 0.0,
        poll_sleep: Callable[[float], None] = time.sleep,
        max_wait: float = 150.0,
    ) -> None:
        self.store = store
        self.cookies = cookies
        self.browser = browser
        self._clock = clock
        self._rng = rng or random.SystemRandom()
        self._lease_active = lease_active or (lambda: False)
        self._maintenance_days = maintenance_days
        self._poll_sleep = poll_sleep
        self._max_wait = max_wait
        self._lock = threading.Lock()
        self._running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        with store.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS jukes_cookie_refresh (id INTEGER PRIMARY KEY CHECK (id = 1), "
                "next_due REAL, last_attempt REAL, last_success REAL, failures INTEGER NOT NULL DEFAULT 0, "
                "needs_attention INTEGER NOT NULL DEFAULT 0, reason TEXT, attempts TEXT NOT NULL DEFAULT '[]', "
                "events TEXT NOT NULL DEFAULT '[]')")
            connection.execute("INSERT OR IGNORE INTO jukes_cookie_refresh (id) VALUES (1)")

    # -- state -----------------------------------------------------------
    def _row(self):
        with self.store.connection() as connection:
            return connection.execute("SELECT * FROM jukes_cookie_refresh WHERE id = 1").fetchone()

    def state(self) -> RefreshState:
        row = self._row()
        return RefreshState(row["next_due"], row["last_attempt"], row["last_success"], int(row["failures"]),
                            bool(row["needs_attention"]), row["reason"])

    def _update(self, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self.store.transaction() as connection:
            connection.execute(f"UPDATE jukes_cookie_refresh SET {columns} WHERE id = 1", tuple(fields.values()))

    def _json_list(self, name: str, *, keep_after: float) -> list[float]:
        values = [float(v) for v in json.loads(self._row()[name] or "[]")]
        return [v for v in values if v >= keep_after]

    # -- scheduling ------------------------------------------------------
    def _random_between(self, low: float, high: float) -> float:
        return self._rng.uniform(low, high)

    def _plan_after_cookies(self, now: float) -> float | None:
        """Next due time implied by the cookies themselves (expiry, optional maintenance)."""
        candidates = []
        expiry = self.cookies.earliest_auth_expiry()
        if expiry is not None and expiry > now:
            candidates.append(max(now + HOUR, expiry - self._random_between(5 * DAY, 12 * DAY)))
        if self._maintenance_days > 0:
            candidates.append(now + self._maintenance_days * DAY * self._random_between(0.7, 1.6))
        return min(candidates) if candidates else None

    def cookies_changed(self) -> None:
        """Call after any successful cookie save (upload, export, refresh)."""
        now = self._clock()
        self._update(next_due=self._plan_after_cookies(now), needs_attention=0, failures=0, reason=None)

    def request(self, reason: str = "cookie_failure") -> bool:
        """Note that cookies seem stale. Schedules a refresh only after repeated evidence."""
        now = self._clock()
        state = self.state()
        if state.needs_attention or not self.cookies.status().connected:
            return False
        events = self._json_list("events", keep_after=now - EVENT_WINDOW)
        events.append(now)
        self._update(events=json.dumps(events))
        if len(events) < EVENTS_NEEDED:
            return False
        if state.last_attempt is not None and now - state.last_attempt < self._random_between(6 * HOUR, 14 * HOUR):
            return False  # recently tried; do not pile on
        due = now + self._random_between(20 * MINUTE, 3 * HOUR)
        if state.next_due is None or due < state.next_due:
            self._update(next_due=due)
        return True

    # -- running ---------------------------------------------------------
    def tick(self) -> str:
        """Run one attempt if (and only if) one is due. Safe to call at any time."""
        now = self._clock()
        state = self.state()
        if state.needs_attention or state.next_due is None or now < state.next_due:
            return "idle"
        if not self.cookies.status().connected:
            return "idle"
        attempts = self._json_list("attempts", keep_after=now - DAY)
        if len(attempts) >= MAX_ATTEMPTS_PER_DAY:
            self._update(next_due=now + self._random_between(4 * HOUR, 10 * HOUR))
            return "capped"
        if self._lease_active():
            self._update(next_due=now + self._random_between(30 * MINUTE, 2 * HOUR))
            return "admin_busy"
        return self._attempt(now, attempts)

    def run_now(self) -> str:
        """Manual refresh from the admin page. Still never overlaps another attempt."""
        if self._lease_active():
            return "admin_busy"
        now = self._clock()
        return self._attempt(now, self._json_list("attempts", keep_after=now - DAY))

    def _attempt(self, now: float, attempts: list[float]) -> str:
        with self._lock:
            if self._running:
                return "running"
            self._running = True
        try:
            attempts.append(now)
            self._update(last_attempt=now, attempts=json.dumps(attempts))
            outcome = self._refresh_once()
        finally:
            with self._lock:
                self._running = False
        done = self._clock()
        if outcome == "ok":
            self._update(last_success=done, failures=0, needs_attention=0, reason=None, events="[]",
                         next_due=self._plan_after_cookies(done))
            return "ok"
        if outcome == "signed_out":
            self._update(needs_attention=1, reason="signed_out", next_due=None)
            log.warning("saved YouTube profile is signed out; sign in again from /admin/ (automation paused)")
            return "signed_out"
        failures = self.state().failures + 1
        if failures >= MAX_FAILURES:
            self._update(failures=failures, needs_attention=1, reason="repeated_failures", next_due=None)
            return "paused"
        self._update(failures=failures, next_due=done + self._random_between(1 * DAY, 3 * DAY))
        return "failed"

    def _refresh_once(self) -> str:
        """One headless visit through the saved profile; returns ok | signed_out | error."""
        try:
            if self.browser.refresh() not in (202,):
                return "error"  # sidecar busy or down: treat as a (rare, backed-off) failure
            waited = 0.0
            state = None
            while waited < self._max_wait:
                step = self._random_between(1.5, 4.0)  # irregular polling of our own sidecar
                self._poll_sleep(step)
                waited += step
                state = self.browser.status().get("state")
                if state not in WORKING_STATES:
                    break
            if state == "reconnect_required":
                return "signed_out"
            if state not in ("captured", "complete"):
                return "error"
            code, jar = self.browser.export_cookies()
            if code != 200:
                return "error"
            self.cookies.replace(jar)
            return "ok"
        except Exception:  # noqa: BLE001 - never leak upstream/session details
            log.warning("saved-profile refresh failed")
            return "error"

    # -- background thread ----------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="jukes-cookie-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # Wake at random, wide-ranging times just to read local state; this never contacts YouTube.
        while not self._stop.wait(self._random_between(12 * MINUTE, 95 * MINUTE)):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("cookie refresh check failed")
