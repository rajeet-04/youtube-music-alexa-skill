"""Small authenticated admin: cookie management, interactive browser login, status.

Everything fails closed when the password hash or session key is absent. Admin
sessions are signed, HttpOnly, SameSite=Strict and revocable server-side; every
state-changing request needs a CSRF token. The legacy owner session cookies of
the old web player are never accepted here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, Response, current_app, jsonify, make_response, redirect, render_template, request
from itsdangerous import BadSignature, URLSafeSerializer
from werkzeug.security import check_password_hash

from .server_cookies import CookieFormatError, CookieValidationError, ServerCookies
from .credentials import CredentialError, MissingCredentialKey
from .resources import ResourceSampler

SESSION_COOKIE = "jukes_admin"
LOGIN_COOKIE = "jukes_login_csrf"
SESSION_TTL = 12 * 3600
LEASE_TTL = 15 * 60
MAX_UPLOAD = 1_000_000


@dataclass
class AdminConfig:
    password_hash: str = ""
    session_key: str = ""
    secure_cookies: bool = True
    login_per_minute: int = 5
    lease_ttl: float = LEASE_TTL
    session_ttl: float = SESSION_TTL

    @property
    def enabled(self) -> bool:
        return bool(self.password_hash and self.session_key)


class BrowserClient:
    """Control client for the private browser sidecar (never exposed to callers)."""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout

    def _call(self, path: str, method: str = "GET") -> tuple[int, bytes]:
        request_ = urllib.request.Request(self.base_url + path, method=method,
                                          headers={"X-Control-Token": self.token},
                                          data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(request_, timeout=self.timeout) as response:  # noqa: S310
                return response.status, response.read(2_000_000)
        except urllib.error.HTTPError as error:
            return error.code, error.read(2_000_000)
        except (urllib.error.URLError, OSError):
            return 0, b""

    def start(self) -> int:
        return self._call("/interactive/start", "POST")[0]

    def refresh(self) -> int:
        return self._call("/refresh", "POST")[0]

    def capture(self) -> int:
        return self._call("/interactive/capture", "POST")[0]

    def open_youtube(self) -> int:
        return self._call("/interactive/open-youtube", "POST")[0]

    def close(self) -> int:
        return self._call("/interactive/close", "POST")[0]

    def status(self) -> dict[str, Any]:
        code, body = self._call("/status")
        try:
            return json.loads(body) if code == 200 else {"state": "unavailable"}
        except ValueError:
            return {"state": "unavailable"}

    def export_cookies(self) -> tuple[int, str]:
        code, body = self._call("/cookies/export")
        return code, body.decode("utf-8", "replace") if code == 200 else ""


def make_admin(config: AdminConfig, services, cookies: ServerCookies | None,
               browser: BrowserClient | None, limiter, caller: Callable[[], str],
               clock: Callable[[], float] = time.time, refresher=None) -> Blueprint:
    bp = Blueprint("jukes_admin", __name__, template_folder="../templates")
    store = services.cache.store
    resources = ResourceSampler(services.cache.audio_dir)
    with store.transaction() as connection:
        connection.executescript(
            "CREATE TABLE IF NOT EXISTS jukes_admin_sessions (sid TEXT PRIMARY KEY, csrf TEXT NOT NULL, "
            "pw_fp TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL);"
            "CREATE TABLE IF NOT EXISTS jukes_browser_leases (lease_id TEXT PRIMARY KEY, sid TEXT NOT NULL, "
            "created_at REAL NOT NULL, expires_at REAL NOT NULL, closed_at REAL);")

    def fingerprint() -> str:
        return hashlib.sha256((config.password_hash + "|" + config.session_key).encode()).hexdigest()[:24]

    def serializer() -> URLSafeSerializer:
        return URLSafeSerializer(config.session_key, salt="jukes-admin-session")

    def deny(status: int = 401, code: str = "unauthorized") -> Response:
        response = jsonify({"error": {"code": code, "retryable": False}})
        response.status_code = status
        response.headers["Cache-Control"] = "no-store"
        return response

    def current_session() -> dict | None:
        if not config.enabled:
            return None
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        try:
            sid = serializer().loads(raw)
        except BadSignature:
            return None
        with store.connection() as connection:
            row = connection.execute("SELECT * FROM jukes_admin_sessions WHERE sid = ?", (sid,)).fetchone()
        if row is None or row["revoked_at"] is not None or float(row["expires_at"]) <= clock() \
                or not hmac.compare_digest(str(row["pw_fp"]), fingerprint()):
            return None
        return dict(row)

    def require_admin(csrf: bool = False):
        if not config.enabled:
            return None, deny(503, "admin_not_configured")
        session = current_session()
        if session is None:
            return None, deny()
        if csrf:
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf") or ""
            if not hmac.compare_digest(supplied, session["csrf"]):
                return None, deny(403, "csrf_failed")
        return session, None

    def no_store(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @bp.after_request
    def _headers(response: Response) -> Response:
        return no_store(response)

    # -- login / logout ---------------------------------------------------
    @bp.get("/admin/login")
    def login_form():
        if not config.enabled:
            return deny(503, "admin_not_configured")
        token = secrets.token_urlsafe(24)
        response = make_response(render_template("admin_login.html", csrf=token, error=None))
        response.set_cookie(LOGIN_COOKIE, token, max_age=600, httponly=True, samesite="Strict",
                            secure=config.secure_cookies, path="/admin")
        return response

    @bp.post("/admin/login")
    def login():
        if not config.enabled:
            return deny(503, "admin_not_configured")
        wait = limiter.check("admin_login", caller(), config.login_per_minute)
        if wait:
            response = deny(429, "rate_limited")
            response.headers["Retry-After"] = str(wait)
            return response
        supplied = request.form.get("csrf", "")
        expected = request.cookies.get(LOGIN_COOKIE, "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return deny(403, "csrf_failed")
        password = request.form.get("password", "")
        if not password or len(password) > 1024 or not check_password_hash(config.password_hash, password):
            return deny(401, "invalid_credentials")
        sid, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        now = clock()
        with store.transaction() as connection:
            connection.execute("INSERT INTO jukes_admin_sessions (sid, csrf, pw_fp, created_at, expires_at) "
                               "VALUES (?, ?, ?, ?, ?)", (sid, csrf, fingerprint(), now, now + config.session_ttl))
        response = redirect("/admin/", code=303)
        response.set_cookie(SESSION_COOKIE, serializer().dumps(sid), max_age=int(config.session_ttl), httponly=True,
                            samesite="Strict", secure=config.secure_cookies, path="/")
        response.delete_cookie(LOGIN_COOKIE, path="/admin")
        return response

    @bp.post("/admin/logout")
    def logout():
        session, error = require_admin(csrf=True)
        if error:
            return error
        with store.transaction() as connection:
            connection.execute("UPDATE jukes_admin_sessions SET revoked_at = ? WHERE sid = ?", (clock(), session["sid"]))
            connection.execute("UPDATE jukes_browser_leases SET closed_at = ? WHERE sid = ? AND closed_at IS NULL",
                               (clock(), session["sid"]))
        response = redirect("/admin/login", code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # -- status -----------------------------------------------------------
    def status_view() -> dict[str, Any]:
        cache = services.cache
        usage = cache.usage()
        cookie_status = cookies.status() if cookies is not None else None
        view = {
            "pools": {
                "requested": {"used_bytes": usage["requested"], "limit_bytes": cache.config.requested_limit_bytes},
                "warmup": {"used_bytes": usage["warmup"], "limit_bytes": cache.config.warmup_limit_bytes,
                           "ttl_seconds": cache.config.warmup_ttl_seconds},
            },
            "jobs": services.jobs.stats(),
            "cookies": {"configured": cookies is not None, "connected": bool(cookie_status and cookie_status.connected),
                        "generation": cookie_status.generation if cookie_status else 0,
                        "cookie_count": cookie_status.cookie_count if cookie_status else 0,
                        "updated_at": cookie_status.updated_at if cookie_status else None},
            "browser_configured": browser is not None,
        }
        metrics = cache.metrics.snapshot()
        metrics['current'] = {'jobs':view['jobs'], 'pools':cache.operational_snapshot(),
            'health': 'serving' if (not services.jobs._threads or all(t.is_alive() for t in services.jobs._threads)) else 'degraded'}
        metrics['resources'] = resources.snapshot()
        metrics['retry_coverage'] = 'Extractor fallbacks and job recovery; transport retries unavailable'
        view['metrics'] = metrics
        if refresher is not None:
            r = refresher.state()
            view["refresh"] = {"needs_attention": r.needs_attention, "reason": r.reason,
                               "last_success": r.last_success, "last_attempt": r.last_attempt,
                               "failures": r.failures}
        return view

    @bp.get("/admin/")
    def index():
        session, error = require_admin()
        if error:
            return redirect("/admin/login", code=303) if error.status_code == 401 else error
        return render_template("admin.html", csrf=session["csrf"], status=status_view())

    @bp.get("/admin/api/csrf")
    def api_csrf():
        """CSRF token for same-origin admin scripts (e.g. the noVNC capture panel)."""
        session, error = require_admin()
        if error:
            return error
        return jsonify({"csrf": session["csrf"]})

    @bp.get("/admin/api/status")
    def api_status():
        session, error = require_admin()
        if error:
            return error
        return jsonify(status_view())

    # -- cookies ----------------------------------------------------------
    def apply_cookies(text: str):
        if cookies is None:
            return deny(503, "cookies_unavailable")
        try:
            status = cookies.replace(text)
        except CookieFormatError as error:
            return _error(400, "invalid_cookies", str(error))
        except CookieValidationError:
            return _error(422, "session_rejected", "cookies were not accepted by YouTube")
        except MissingCredentialKey:
            return deny(503, "cookies_unavailable")
        except CredentialError:
            return _error(503, "cookie_store_error", "stored cookies are unreadable")
        if refresher is not None:
            refresher.cookies_changed()  # re-plans from the new cookies' expiry; clears "needs attention"
        return jsonify({"connected": True, "generation": status.generation, "cookie_count": status.cookie_count})

    def _error(status: int, code: str, message: str) -> Response:
        response = jsonify({"error": {"code": code, "message": message, "retryable": False}})
        response.status_code = status
        return response

    @bp.post("/admin/cookies")
    def upload_cookies():
        session, error = require_admin(csrf=True)
        if error:
            return error
        if request.content_length is not None and request.content_length > MAX_UPLOAD + 4096:
            return _error(413, "too_large", "cookie file is too large")
        upload = request.files.get("file")
        text = upload.read(MAX_UPLOAD + 1).decode("utf-8", "replace") if upload else request.form.get("text", "")
        return apply_cookies(text)

    @bp.post("/admin/cookies/refresh")
    def refresh_cookies_now():
        """Manual one-off refresh from the saved profile (runs in the background)."""
        session, error = require_admin(csrf=True)
        if error:
            return error
        if refresher is None or browser is None:
            return deny(503, "refresh_unavailable")
        threading.Thread(target=refresher.run_now, daemon=True).start()
        return Response(status=202)

    @bp.post("/admin/cookies/delete")
    def delete_cookies():
        session, error = require_admin(csrf=True)
        if error:
            return error
        if cookies is not None:
            cookies.clear()
        return Response(status=204)

    # -- interactive browser ----------------------------------------------
    def active_lease(sid: str):
        with store.connection() as connection:
            return connection.execute(
                "SELECT * FROM jukes_browser_leases WHERE sid = ? AND closed_at IS NULL AND expires_at > ? "
                "ORDER BY created_at DESC LIMIT 1", (sid, clock())).fetchone()

    @bp.get("/admin/youtube/browser/authorize")
    def browser_authorize():
        """Caddy forward_auth target: 204 only with an admin session and a live lease."""
        session, error = require_admin()
        if error:
            return error
        if active_lease(session["sid"]) is None:
            return deny(403, "no_browser_lease")
        return Response(status=204)

    @bp.post("/admin/youtube/browser/start")
    def browser_start():
        session, error = require_admin(csrf=True)
        if error:
            return error
        if browser is None:
            return deny(503, "browser_unavailable")
        code = browser.start()
        if code not in (202, 409):  # 409: already running
            return _error(503, "browser_unavailable", "browser service is unavailable")
        lease_id, now = secrets.token_urlsafe(16), clock()
        with store.transaction() as connection:
            connection.execute("UPDATE jukes_browser_leases SET closed_at = ? WHERE closed_at IS NULL", (now,))
            connection.execute("INSERT INTO jukes_browser_leases (lease_id, sid, created_at, expires_at) "
                               "VALUES (?, ?, ?, ?)", (lease_id, session["sid"], now, now + config.lease_ttl))
        return jsonify({"lease_expires_at": now + config.lease_ttl, "login_path": "/youtube-login/vnc.html"})

    def close_leases(sid: str | None = None) -> None:
        with store.transaction() as connection:
            connection.execute("UPDATE jukes_browser_leases SET closed_at = ? WHERE closed_at IS NULL", (clock(),))

    @bp.post("/admin/youtube/browser/open-youtube")
    def browser_open_youtube():
        session, error = require_admin(csrf=True)
        if error:
            return error
        if browser is None:
            return deny(503, "browser_unavailable")
        if active_lease(session["sid"]) is None:
            return deny(403, "no_browser_lease")
        if browser.open_youtube() != 200:
            return _error(409, "browser_not_running", "start the browser from the admin page first")
        return jsonify({"opened": True})

    @bp.post("/admin/youtube/browser/export")
    def browser_export():
        """Capture the signed-in profile and install it through the validated replace path."""
        session, error = require_admin(csrf=True)
        if error:
            return error
        if browser is None:
            return deny(503, "browser_unavailable")
        if active_lease(session["sid"]) is None:
            return deny(403, "no_browser_lease")
        browser.capture()
        # The sidecar reports capture_requested/validating_login while it checks the
        # sign-in; anything else means the attempt finished (captured, signed out, ...).
        for _ in range(int(current_app.config.get("JUKES_EXPORT_POLLS", 60))):
            if browser.status().get("state") not in ("capture_requested", "validating_login"):
                break
            time.sleep(float(current_app.config.get("JUKES_EXPORT_POLL_SECONDS", 1.0)))
        code, jar = browser.export_cookies()
        if code != 200:
            return _error(409 if code in (409, 410) else 502, "export_unavailable",
                          "No signed-in YouTube Music session found. Sign in, then capture again.")
        return apply_cookies(jar)

    @bp.post("/admin/youtube/browser/refresh")
    def browser_refresh():
        """Bounded reconnect of the saved profile, then export."""
        session, error = require_admin(csrf=True)
        if error:
            return error
        if browser is None:
            return deny(503, "browser_unavailable")
        if browser.refresh() not in (202, 409):
            return _error(503, "browser_unavailable", "browser service is unavailable")
        return jsonify({"state": browser.status().get("state")})

    @bp.post("/admin/youtube/browser/close")
    def browser_close():
        session, error = require_admin(csrf=True)
        if error:
            return error
        if browser is not None:
            browser.close()
        close_leases()
        return Response(status=204)

    return bp
