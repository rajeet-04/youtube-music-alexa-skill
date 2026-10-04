"""Private controller for a persistent, owner-operated YT Music browser.

Only redacted lifecycle state crosses this API. Signed-in headers and Netscape
cookie snapshots are returned once to callers with the control token; secrets
are not logged or written to the Chromium profile volume.
"""
import hmac
import logging
import os
import subprocess
import threading
import time
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request

app = Flask(__name__)
logger = logging.getLogger(__name__)
CONTROL_TOKEN = os.environ.get("YT_BROWSER_CONTROL_TOKEN", "")
CAPTURE_TIMEOUT = float(os.environ.get("YT_BROWSER_CAPTURE_TIMEOUT", "120"))
LOGIN_TIMEOUT = float(os.environ.get("YT_BROWSER_LOGIN_TIMEOUT", "240"))
SUCCESS_DISPLAY_SECONDS = float(os.environ.get("YT_BROWSER_SUCCESS_DISPLAY", "12"))
PROFILE_DIR = os.environ.get("CHROME_USER_DATA_DIR", "/profile")
CHROMIUM = os.environ.get("CHROMIUM_EXECUTABLE", "/usr/bin/chromium")
CDP_URL = os.environ.get("CHROMIUM_CDP_URL", "http://127.0.0.1:9222")
COOKIE_EXPORT_TTL = 300
COOKIE_EXPORT_MAX_COOKIES = 512
COOKIE_EXPORT_MAX_BYTES = 1_000_000

_lock = threading.RLock()
_state = {
    "state": "idle", "interactive": False, "started_at": None,
    "updated_at": time.time(), "message": None,
}
_candidate = None
_cookie_export_candidate = None
_cookie_export_timer = None
_cookie_export_expired = False
_worker = None
_manual_browser = None
_complete_requested = False
_capture_requested = threading.Event()


class CookieExportTooLarge(Exception):
    """A browser cookie snapshot exceeded the bounded export size."""


class CookieExportInvalid(Exception):
    """A browser cookie cannot be represented safely in Netscape format."""


def _clear_cookie_export_candidate(expected=None, *, expired=False):
    """Drop a pending jar and cancel its timer, optionally by identity."""
    global _cookie_export_candidate, _cookie_export_timer, _cookie_export_expired
    with _lock:
        if expected is not None and _cookie_export_candidate is not expected:
            return None
        candidate, _cookie_export_candidate = _cookie_export_candidate, None
        timer, _cookie_export_timer = _cookie_export_timer, None
        if expired and candidate is not None:
            _cookie_export_expired = True
        elif expected is None:
            _cookie_export_expired = False
    if timer is not None and timer is not threading.current_thread():
        timer.cancel()
    return candidate


def _expire_cookie_export_candidate(candidate):
    _clear_cookie_export_candidate(candidate, expired=True)


def _store_cookie_export_candidate(candidate):
    """Keep one export candidate and schedule identity-checked cleanup."""
    global _cookie_export_candidate, _cookie_export_timer, _cookie_export_expired
    timer = threading.Timer(
        COOKIE_EXPORT_TTL, _expire_cookie_export_candidate, args=(candidate,),
    )
    timer.daemon = True
    with _lock:
        previous_timer = _cookie_export_timer
        _cookie_export_candidate = candidate
        _cookie_export_timer = timer
        _cookie_export_expired = False
        timer.start()
    if previous_timer is not None and previous_timer is not threading.current_thread():
        previous_timer.cancel()


def _take_cookie_export_candidate():
    """Consume a pending jar and cancel automatic cleanup."""
    global _cookie_export_candidate, _cookie_export_timer, _cookie_export_expired
    with _lock:
        candidate, _cookie_export_candidate = _cookie_export_candidate, None
        timer, _cookie_export_timer = _cookie_export_timer, None
        if candidate is not None:
            _cookie_export_expired = False
    if timer is not None and timer is not threading.current_thread():
        timer.cancel()
    return candidate


def _format_netscape_cookie_jar(cookies):
    """Return a bounded Netscape jar containing only YouTube cookies."""
    lines = ["# Netscape HTTP Cookie File"]
    youtube_cookie_count = 0
    has_auth_cookie = False
    auth_cookie_names = {
        "SAPISID", "SAPISID1P", "SAPISID3P", "SID",
        "__Secure-1PSID", "__Secure-3PSID",
        "__Secure-1PAPISID", "__Secure-3PAPISID",
    }

    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = cookie.get("domain")
        if not isinstance(domain, str):
            continue
        normalized_domain = domain.lstrip(".").rstrip(".").lower()
        if not (normalized_domain == "youtube.com"
                or normalized_domain.endswith(".youtube.com")):
            continue
        domain = (f".{normalized_domain}" if domain.startswith(".")
                  else normalized_domain)

        youtube_cookie_count += 1
        if youtube_cookie_count > COOKIE_EXPORT_MAX_COOKIES:
            raise CookieExportTooLarge

        name = cookie.get("name")
        value = cookie.get("value")
        path = cookie.get("path", "/")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            raise CookieExportInvalid
        if not isinstance(path, str) or not path.startswith("/"):
            path = "/"
        fields = (domain, path, name, value)
        if any(any(char in field for char in "\t\r\n") for field in fields):
            raise CookieExportInvalid

        try:
            expires = float(cookie.get("expires", -1))
        except (TypeError, ValueError, OverflowError):
            raise CookieExportInvalid from None
        if not (expires == expires and abs(expires) != float("inf")):
            raise CookieExportInvalid
        expiry = str(int(expires)) if expires > 0 else "0"
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        netscape_domain = domain
        if cookie.get("httpOnly"):
            netscape_domain = f"#HttpOnly_{domain}"
        lines.append("\t".join((
            netscape_domain, include_subdomains, path, secure, expiry, name, value,
        )))
        has_auth_cookie = has_auth_cookie or (name in auth_cookie_names and bool(value))

    if not youtube_cookie_count or not has_auth_cookie:
        raise CookieExportInvalid
    jar = "\n".join(lines) + "\n"
    if len(jar.encode("utf-8")) > COOKIE_EXPORT_MAX_BYTES:
        raise CookieExportTooLarge
    return jar


def _clear_profile_singleton_artifacts():
    """Remove Chromium locks left behind after an unclean browser exit.

    This controller is the only process allowed to open PROFILE_DIR, and
    _start() prevents concurrent workers, so artifacts present immediately
    before a new launch are stale.
    """
    for name in ("SingletonCookie", "SingletonLock", "SingletonSocket"):
        path = os.path.join(PROFILE_DIR, name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _stop_manual_browser():
    global _manual_browser
    with _lock:
        process, _manual_browser = _manual_browser, None
    if process is None or process.poll() is not None:
        return
    # Ask the X11 window to close first. This delivers Chromium's normal
    # WM_DELETE_WINDOW path so its cookie DB and profile state are flushed.
    # SIGTERM remains a bounded fallback for startup failures/wedged windows.
    try:
        window_ids = subprocess.check_output(
            ["xdotool", "search", "--onlyvisible", "--class", "chromium"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).splitlines()
        for window_id in window_ids:
            subprocess.run(
                ["xdotool", "windowclose", window_id.decode("ascii")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=2, check=False,
            )
        process.wait(timeout=8)
        return
    except (FileNotFoundError, subprocess.SubprocessError, UnicodeError):
        pass
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _plain_chromium_command(enable_capture=False):
    """Build Chromium command, keeping interactive login free of CDP."""
    command = [
        CHROMIUM, f"--user-data-dir={PROFILE_DIR}", "--no-sandbox",
        "--disable-dev-shm-usage", "--start-maximized", "--no-first-run",
        "--hide-crash-restore-bubble",
        # Keep cookie encryption identical across every launch.
        "--password-store=basic", "--use-mock-keychain",
    ]
    if enable_capture:
        # This is enabled only after Google login has completed. Playwright
        # observes the resulting YT Music request over loopback; it never
        # controls the browser while credentials are entered.
        command.extend([
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222",
            "--remote-allow-origins=http://127.0.0.1:9222",
        ])
    command.append("https://music.youtube.com")
    return command


def _manual_login(enable_capture=False):
    """Launch Chromium, optionally exposing loopback CDP after login."""
    global _manual_browser
    _clear_profile_singleton_artifacts()
    process = subprocess.Popen(
        _plain_chromium_command(enable_capture=enable_capture),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with _lock:
        _manual_browser = process
    return True


def _open_youtube_music():
    """Navigate the visible plain Chromium window without a DevTools channel."""
    window_ids = subprocess.check_output(
        ["xdotool", "search", "--onlyvisible", "--class", "chromium"],
        stderr=subprocess.DEVNULL, timeout=2,
    ).splitlines()
    if not window_ids:
        raise RuntimeError("Chromium window unavailable")
    window_id = window_ids[-1].decode("ascii")
    commands = (
        ["xdotool", "windowactivate", "--sync", window_id],
        ["xdotool", "key", "--window", window_id, "ctrl+l"],
        ["xdotool", "type", "--window", window_id, "--delay", "1",
         "https://music.youtube.com"],
        ["xdotool", "key", "--window", window_id, "Return"],
    )
    for command in commands:
        subprocess.run(command, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=3, check=True)


def authenticated_browse_headers(headers):
    """Accept only YouTube's signed-in SAPISID authorization schemes."""
    if not isinstance(headers, dict) or not headers.get("cookie"):
        return False
    scheme = headers.get("authorization", "").partition(" ")[0].upper()
    return scheme in {"SAPISIDHASH", "SAPISID1PHASH", "SAPISID3PHASH"}


def account_menu_is_signed_in(payload):
    """Require YouTube Music's active account, not merely Google cookies."""
    try:
        renderer = payload["actions"][0]["openPopupAction"]["popup"][
            "multiPageMenuRenderer"]
        active = renderer["header"]["activeAccountHeaderRenderer"]
        runs = active["accountName"]["runs"]
        return bool(runs and isinstance(runs[0].get("text"), str)
                    and runs[0]["text"].strip())
    except (KeyError, IndexError, TypeError, AttributeError):
        return False


def account_menu_reports_signed_out(payload):
    """Recognize a real account-menu response which lacks an active account."""
    try:
        renderer = payload["actions"][0]["openPopupAction"]["popup"][
            "multiPageMenuRenderer"]
    except (KeyError, IndexError, TypeError):
        return False
    return not account_menu_is_signed_in(payload) and isinstance(renderer, dict)


def accepted_browse_url(url):
    """Accept any signed-in YT Music Innertube request.

    The web app may call player, next, or account_menu after login instead of
    browse. All use the same signed browser headers required by ytmusicapi.
    """
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return False
    return (parsed.scheme == "https" and parsed.hostname == "music.youtube.com"
            and parsed.path.startswith("/youtubei/v1/"))


def _has_valid_control_token():
    supplied = request.headers.get("X-Control-Token", "")
    return bool(CONTROL_TOKEN) and hmac.compare_digest(supplied, CONTROL_TOKEN)


@app.before_request
def require_control_token():
    if request.path == "/health":
        return None
    if not _has_valid_control_token():
        return jsonify({"error": "unauthorized"}), 401


def _public_state():
    with _lock:
        return dict(_state, candidate_ready=_candidate is not None)


def _set_state(name, **extra):
    with _lock:
        _state.update(state=name, updated_at=time.time(), **extra)


def _capture_from_running_browser(interactive):
    """Attach briefly to the existing Chromium process and validate YT Music."""
    global _candidate
    _clear_cookie_export_candidate()
    from playwright.sync_api import sync_playwright

    captured = threading.Event()
    signed_out = threading.Event()
    # Exiting sync_playwright disconnects its transport. Deliberately do not
    # call browser.close(): the owner keeps using this exact Chromium process.
    with sync_playwright() as pw:
        browser = None
        connect_deadline = time.time() + 10
        while browser is None and time.time() < connect_deadline:
            try:
                browser = pw.chromium.connect_over_cdp(CDP_URL)
            except Exception:
                time.sleep(0.2)
        if browser is None or not browser.contexts:
            raise RuntimeError("Chromium CDP endpoint unavailable")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response):
            global _candidate
            if captured.is_set():
                return
            try:
                parsed = urlparse(response.url)
                if (parsed.scheme != "https"
                        or parsed.hostname != "music.youtube.com"
                        or parsed.path != "/youtubei/v1/account/account_menu"):
                    return
                payload = response.json()
                if account_menu_reports_signed_out(payload):
                    signed_out.set()
                    return
                if not account_menu_is_signed_in(payload):
                    return
                headers = response.request.all_headers()
            except Exception:
                return
            if not authenticated_browse_headers(headers):
                return
            export_error_status = None
            try:
                cookie_jar = _format_netscape_cookie_jar(context.cookies())
            except CookieExportTooLarge:
                cookie_jar = None
                export_error_status = 413
            except CookieExportInvalid:
                cookie_jar = None
                export_error_status = 422
            except Exception:
                cookie_jar = None
                export_error_status = 503
            cookie_candidate = {
                "created_at": time.time(),
                "jar": cookie_jar,
                "error_status": export_error_status,
            }
            with _lock:
                _candidate = {"url": response.url, "headers": headers}
                _store_cookie_export_candidate(cookie_candidate)
            captured.set()
            _set_state("captured", interactive=interactive, message=None)

        context.on("response", on_response)
        page.goto("https://music.youtube.com", wait_until="domcontentloaded", timeout=45000)
        deadline = time.time() + CAPTURE_TIMEOUT
        while not captured.is_set() and not signed_out.is_set() and time.time() < deadline:
            if interactive and not _public_state()["interactive"]:
                break
            page.wait_for_timeout(250)
        context.remove_listener("response", on_response)
        if captured.is_set():
            return "captured"
        if signed_out.is_set():
            return "signed_out"
        if interactive and not _public_state()["interactive"]:
            return "cancelled"
        return "timeout"


def _capture(interactive):
    phase = "startup"
    _set_state("waiting_for_login" if interactive else "refreshing",
               interactive=interactive, started_at=time.time(), message=None)
    try:
        # A fixed nonzero loopback CDP port does not set Chromium's webdriver
        # flag. Crucially, no client attaches while Google credentials are
        # entered. Explicit Capture attaches to this same process only after
        # the owner has visibly confirmed the YouTube Music account.
        phase = "Chromium startup"
        _manual_login(enable_capture=True)

        while interactive and _public_state()["interactive"]:
            if not _capture_requested.wait(timeout=0.25):
                continue
            _capture_requested.clear()
            _set_state("validating_login", interactive=True, message=None)
            phase = "same-process CDP validation"
            result = _capture_from_running_browser(True)
            if result == "captured":
                break
            if result == "cancelled":
                _set_state("idle", interactive=False, message=None)
                return
            message = ("YouTube Music is still signed out. Sign in, then capture again"
                       if result == "signed_out" else
                       "Could not validate YouTube Music yet. Try capture again")
            _set_state("waiting_for_login", interactive=True, message=message)

        if not interactive:
            phase = "saved-profile CDP validation"
            result = _capture_from_running_browser(False)
            if result != "captured":
                _set_state("reconnect_required", interactive=False,
                           message="Saved browser profile is signed out")
                return

        if _public_state()["state"] != "captured":
            return
        # Flask must promote the one-time candidate before Chromium is closed.
        # A rejected promotion also leaves this process alive for inspection.
        while _public_state()["interactive"] and not _complete_requested:
            time.sleep(0.25)
        if _complete_requested:
            _set_state("complete", interactive=False, message="Session saved")
            time.sleep(max(0, SUCCESS_DISPLAY_SECONDS))
    except Exception as exc:
        logger.warning("Browser workflow failed during %s (%s)", phase, type(exc).__name__)
        _set_state("unavailable", interactive=False,
                   message=f"Browser workflow failed during {phase}")
    finally:
        # Only explicit Close, lease expiry, or manager completion reaches this
        # boundary. Capture attempts themselves never replace Chromium.
        _stop_manual_browser()


def _start(interactive):
    global _worker, _candidate, _complete_requested
    with _lock:
        if _worker and _worker.is_alive():
            return False
        _candidate = None
        _clear_cookie_export_candidate()
        _complete_requested = False
        _capture_requested.clear()
        _state["interactive"] = interactive
        _worker = threading.Thread(target=_capture, args=(interactive,), daemon=True)
        _worker.start()
        return True


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/status")
def status():
    return jsonify(_public_state())


@app.post("/interactive/start")
def interactive_start():
    started = _start(True)
    return jsonify(_public_state()), (202 if started else 409)


@app.post("/refresh")
def refresh():
    started = _start(False)
    return jsonify(_public_state()), (202 if started else 409)


@app.post("/interactive/close")
def close():
    with _lock:
        _state["interactive"] = False
        _state["updated_at"] = time.time()
    return jsonify(_public_state())


@app.post("/interactive/open-youtube")
def open_youtube():
    if not _public_state()["interactive"]:
        return jsonify({"error": "interactive_browser_not_running"}), 409
    try:
        _open_youtube_music()
    except Exception:
        return jsonify({"error": "browser_window_unavailable"}), 503
    return jsonify(_public_state())


@app.post("/interactive/capture")
def capture_interactive():
    if not _public_state()["interactive"]:
        return jsonify({"error": "interactive_browser_not_running"}), 409
    _capture_requested.set()
    _set_state("capture_requested", interactive=True,
               message="Preparing signed-in session")
    return jsonify(_public_state()), 202


@app.post("/interactive/complete")
def complete():
    global _complete_requested
    with _lock:
        _complete_requested = True
        _state["state"] = "complete"
        _state["updated_at"] = time.time()
        _state["message"] = "Session saved"
    return jsonify(_public_state())


@app.post("/candidate/take")
def candidate_take():
    global _candidate
    with _lock:
        candidate, _candidate = _candidate, None
    if candidate is None:
        return jsonify({"error": "candidate_not_ready"}), 404
    _set_state("candidate_taken", message=None)
    return jsonify(candidate)


@app.get("/cookies/export")
def cookies_export():
    global _cookie_export_expired
    if not _has_valid_control_token():
        response = jsonify({"error": "unauthorized"})
        response.headers["Cache-Control"] = "no-store"
        return response, 401

    candidate = _take_cookie_export_candidate()
    if candidate is None:
        with _lock:
            expired, _cookie_export_expired = _cookie_export_expired, False
        if expired:
            response = jsonify({"error": "cookie_export_expired"})
            response.headers["Cache-Control"] = "no-store"
            return response, 410
        response = jsonify({"error": "cookie_export_unavailable"})
        response.headers["Cache-Control"] = "no-store"
        return response, 409
    if time.time() - candidate["created_at"] >= COOKIE_EXPORT_TTL:
        response = jsonify({"error": "cookie_export_expired"})
        response.headers["Cache-Control"] = "no-store"
        return response, 410
    if candidate["error_status"] is not None:
        errors = {
            413: "cookie_export_too_large",
            422: "cookie_export_invalid",
            503: "cookie_export_unavailable",
        }
        response = jsonify({"error": errors[candidate["error_status"]]})
        response.headers["Cache-Control"] = "no-store"
        return response, candidate["error_status"]

    response = Response(candidate["jar"], mimetype="text/plain")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
