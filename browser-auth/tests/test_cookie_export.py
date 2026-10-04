import importlib.util
import sys
import types
from pathlib import Path

SERVICE_PATH = Path(__file__).parents[1] / "service.py"
CONTROL_TOKEN = "test-internal-control-token"


def signed_in_account_menu():
    return {
        "actions": [{
            "openPopupAction": {
                "popup": {
                    "multiPageMenuRenderer": {
                        "header": {
                            "activeAccountHeaderRenderer": {
                                "accountName": {"runs": [{"text": "Operator"}]}
                            }
                        }
                    }
                }
            }
        }]
    }


def signed_out_account_menu():
    return {"actions": [{"openPopupAction": {"popup": {"multiPageMenuRenderer": {}}}}]}


class FakeRequest:
    def all_headers(self):
        return {
            "cookie": "SAPISID=header-secret",
            "authorization": "SAPISIDHASH signed-header",
        }


class FakeResponse:
    url = "https://music.youtube.com/youtubei/v1/account/account_menu"
    request = FakeRequest()

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakePage:
    def __init__(self, context, payload):
        self.context = context
        self.payload = payload

    def goto(self, url, **kwargs):
        response = FakeResponse(self.payload)
        for callback in tuple(self.context.response_callbacks):
            callback(response)

    def wait_for_timeout(self, milliseconds):
        return None


class FakeContext:
    def __init__(self, payload, cookies):
        self.response_callbacks = []
        self.cookies_value = cookies
        self.pages = [FakePage(self, payload)]

    def on(self, event, callback):
        assert event == "response"
        self.response_callbacks.append(callback)

    def remove_listener(self, event, callback):
        assert event == "response"
        self.response_callbacks.remove(callback)

    def cookies(self):
        return self.cookies_value

    def new_page(self):
        page = FakePage(self, {})
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = types.SimpleNamespace(
            connect_over_cdp=lambda url: browser,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeTimer:
    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.cancelled = False
        self.started = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self, even_if_cancelled=False):
        if self.cancelled and not even_if_cancelled:
            return
        self.function(*self.args, **self.kwargs)


def load_service(monkeypatch, payload=None, cookies=None):
    spec = importlib.util.spec_from_file_location(
        "browser_auth_cookie_export_test_service", SERVICE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module.CONTROL_TOKEN = CONTROL_TOKEN

    context = FakeContext(payload or signed_in_account_menu(), cookies or [])
    browser = FakeBrowser(context)
    playwright_sync_api = types.ModuleType("playwright.sync_api")
    playwright_sync_api.sync_playwright = lambda: FakePlaywright(browser)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", playwright_sync_api)
    return module


def install_fake_timers(monkeypatch, service):
    timers = []

    def make_timer(interval, function, args=None, kwargs=None):
        timer = FakeTimer(interval, function, args=args, kwargs=kwargs)
        timers.append(timer)
        return timer

    monkeypatch.setattr(service.threading, "Timer", make_timer)
    return timers


def test_cookie_export_requires_the_control_token(monkeypatch):
    service = load_service(monkeypatch)
    client = service.app.test_client()

    assert client.get("/cookies/export").status_code == 401
    assert client.get(
        "/cookies/export", headers={"X-Control-Token": "wrong"},
    ).status_code == 401


def test_cookie_export_returns_a_netscape_jar_from_a_signed_in_profile(monkeypatch):
    cookies = [
        {
            "name": "SAPISID", "value": "cookie-secret", "domain": ".youtube.com",
            "path": "/", "secure": True, "httpOnly": True, "expires": -1,
        },
        {
            "name": "PREF", "value": "f1=50000000", "domain": "youtube.com",
            "path": "/music", "secure": False, "httpOnly": False, "expires": 1_800_000_000,
        },
        {
            "name": "SID", "value": "google-secret", "domain": ".google.com",
            "path": "/", "secure": True, "httpOnly": True, "expires": 1_800_000_000,
        },
    ]
    service = load_service(monkeypatch, cookies=cookies)
    assert service._capture_from_running_browser(interactive=False) == "captured"

    response = service.app.test_client().get(
        "/cookies/export", headers={"X-Control-Token": CONTROL_TOKEN},
    )

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_data(as_text=True) == (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tcookie-secret\n"
        "youtube.com\tFALSE\t/music\tFALSE\t1800000000\tPREF\tf1=50000000\n"
    )
    assert "google-secret" not in response.get_data(as_text=True)


def test_export_requires_a_successful_signed_in_account_menu(monkeypatch):
    service = load_service(monkeypatch, payload=signed_out_account_menu(), cookies=[
        {
            "name": "SAPISID", "value": "stale-secret", "domain": ".youtube.com",
            "path": "/", "secure": True, "httpOnly": True, "expires": -1,
        },
    ])
    assert service._capture_from_running_browser(interactive=False) == "signed_out"

    response = service.app.test_client().get(
        "/cookies/export", headers={"X-Control-Token": CONTROL_TOKEN},
    )

    assert response.status_code == 409
    assert "stale-secret" not in response.get_data(as_text=True)


def test_cookie_export_does_not_consume_the_music_header_candidate(monkeypatch):
    service = load_service(monkeypatch, cookies=[
        {
            "name": "SAPISID", "value": "cookie-secret", "domain": ".youtube.com",
            "path": "/", "secure": True, "httpOnly": True, "expires": -1,
        },
    ])
    service._capture_from_running_browser(interactive=False)
    client = service.app.test_client()
    headers = {"X-Control-Token": CONTROL_TOKEN}

    assert client.get("/cookies/export", headers=headers).status_code == 200
    candidate_response = client.post("/candidate/take", headers=headers)

    assert candidate_response.status_code == 200
    assert candidate_response.get_json()["headers"]["cookie"] == "SAPISID=header-secret"
    assert client.get("/cookies/export", headers=headers).status_code == 409


def test_expired_cookie_export_is_rejected(monkeypatch):
    service = load_service(monkeypatch, cookies=[
        {
            "name": "SAPISID", "value": "cookie-secret", "domain": ".youtube.com",
            "path": "/", "secure": True, "httpOnly": True, "expires": -1,
        },
    ])
    service._capture_from_running_browser(interactive=False)
    service._cookie_export_candidate["created_at"] -= service.COOKIE_EXPORT_TTL
    response = service.app.test_client().get(
        "/cookies/export", headers={"X-Control-Token": CONTROL_TOKEN},
    )

    assert response.status_code == 410
    assert "cookie-secret" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


def test_oversized_cookie_export_is_rejected_without_returning_cookie_data(monkeypatch):
    service = load_service(monkeypatch, cookies=[
        {
            "name": "SAPISID", "value": "x" * 256, "domain": ".youtube.com",
            "path": "/", "secure": True, "httpOnly": True, "expires": -1,
        },
    ])
    service.COOKIE_EXPORT_MAX_BYTES = 64
    service._capture_from_running_browser(interactive=False)
    response = service.app.test_client().get(
        "/cookies/export", headers={"X-Control-Token": CONTROL_TOKEN},
    )

    assert response.status_code == 413
    assert "x" * 32 not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


def test_cookie_count_limit_rejects_the_complete_snapshot(monkeypatch):
    service = load_service(monkeypatch, cookies=[
        {
            "name": "SAPISID", "value": "cookie-secret", "domain": ".youtube.com",
            "path": "/", "secure": True, "httpOnly": True, "expires": -1,
        },
        {
            "name": "PREF", "value": "f1=50000000", "domain": "youtube.com",
            "path": "/", "secure": False, "httpOnly": False, "expires": -1,
        },
    ])
    service.COOKIE_EXPORT_MAX_COOKIES = 1
    service._capture_from_running_browser(interactive=False)
    response = service.app.test_client().get(
        "/cookies/export", headers={"X-Control-Token": CONTROL_TOKEN},
    )

    assert response.status_code == 413
    assert "cookie-secret" not in response.get_data(as_text=True)


def test_expiry_timer_clears_the_unconsumed_cookie_jar(monkeypatch):
    service = load_service(monkeypatch, cookies=[
        {
            "name": "SAPISID", "value": "cookie-secret", "domain": ".youtube.com",
            "path": "/", "secure": True, "httpOnly": True, "expires": -1,
        },
    ])
    timers = install_fake_timers(monkeypatch, service)
    service._capture_from_running_browser(interactive=False)
    assert len(timers) == 1
    assert timers[0].interval == service.COOKIE_EXPORT_TTL
    assert timers[0].started

    timers[0].fire()

    assert service._cookie_export_candidate is None
    response = service.app.test_client().get(
        "/cookies/export", headers={"X-Control-Token": CONTROL_TOKEN},
    )
    assert response.status_code == 410
    assert "cookie-secret" not in response.get_data(as_text=True)


def test_stale_expiry_timer_cannot_clear_a_newer_cookie_snapshot(monkeypatch):
    service = load_service(monkeypatch, cookies=[
        {
            "name": "SAPISID", "value": "cookie-secret", "domain": ".youtube.com",
            "path": "/", "secure": True, "httpOnly": True, "expires": -1,
        },
    ])
    timers = install_fake_timers(monkeypatch, service)
    service._capture_from_running_browser(interactive=False)
    old_candidate = service._cookie_export_candidate
    old_timer = timers[0]

    service._capture_from_running_browser(interactive=False)
    new_candidate = service._cookie_export_candidate
    assert old_timer.cancelled
    assert new_candidate is not old_candidate

    old_timer.fire(even_if_cancelled=True)

    assert service._cookie_export_candidate is new_candidate
    response = service.app.test_client().get(
        "/cookies/export", headers={"X-Control-Token": CONTROL_TOKEN},
    )
    assert response.status_code == 200
