"""Application factory for the JUKES backend."""

from __future__ import annotations

import os
import time

from flask import Flask

from .admin import AdminConfig, BrowserClient, make_admin
from .cache import Cache
from .config import CacheConfig
from .credentials import Credentials
from .extractor import Extractor
from .guard import acquire_exclusive
from .health import register_health
from .identity import Identity
from .jobs import Jobs
from .limits import RateLimiter, client_address, parse_networks
from .music import Music
from .personalization import ClientFactory, account_probe
from .refresher import CookieRefresher
from .routes import Services, Settings, register_routes
from .server_cookies import ServerCookies

MAX_BODY_BYTES = 1_000_000


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def cache_config_from_env() -> CacheConfig:
    defaults = CacheConfig()
    return CacheConfig(
        database_path=os.environ.get("JUKES_DB_PATH") or defaults.database_path,
        audio_dir=os.environ.get("JUKES_AUDIO_DIR") or os.environ.get("AUDIO_CACHE_DIR") or defaults.audio_dir,
        requested_limit_bytes=_int_env("JUKES_REQUESTED_CACHE_LIMIT_BYTES", defaults.requested_limit_bytes),
        warmup_limit_bytes=_int_env("JUKES_WARMUP_CACHE_LIMIT_BYTES", defaults.warmup_limit_bytes),
        warmup_ttl_seconds=_int_env("JUKES_WARMUP_TTL_SECONDS", int(defaults.warmup_ttl_seconds)),
        min_free_disk_bytes=_int_env("JUKES_MIN_FREE_DISK_BYTES", defaults.min_free_disk_bytes),
    )


def _browser_lease_active(store):
    """True while the admin holds a live interactive-browser lease (automation stays out of the way)."""
    def check() -> bool:
        try:
            with store.connection() as connection:
                row = connection.execute(
                    "SELECT 1 FROM jukes_browser_leases WHERE closed_at IS NULL AND expires_at > ? LIMIT 1",
                    (time.time(),)).fetchone()
            return row is not None
        except Exception:  # noqa: BLE001 - table not created yet
            return False
    return check


def _cookie_probe(cookie_header: str) -> bool:
    return account_probe({"cookie": cookie_header, "account_index": 0})


def build_services(config: CacheConfig | None = None) -> Services:
    config = config or cache_config_from_env()
    acquire_exclusive(config.database_path)
    cache = Cache(config)
    key = os.environ.get("JUKES_CREDENTIAL_ENCRYPTION_KEY", "").strip() or None
    # Personalisation and admin cookies fail closed without a persistent key: no
    # token is issued, nothing is stored, and a missing key never causes a new
    # one to be generated over existing ciphertext.
    identity = credentials = server_cookies = None
    if key:
        identity = Identity(cache.store)
        credentials = Credentials(cache.store, key, account_probe=account_probe)
        server_cookies = ServerCookies(cache.store, key, probe=_cookie_probe)
    extractor=Extractor()
    if os.environ.get('JUKES_PROGRESSIVE') == '1':
        from .streams import Streams
        extractor.streams=Streams(cache)
    jobs = Jobs(
        cache, extractor,
        worker_count=_int_env("JUKES_WORKERS", 4),
        max_warmup_workers=_int_env('JUKES_WARMUP_WORKERS',1),
        max_queue_size=_int_env("JUKES_QUEUE_SIZE", 100),
        credential_provider=server_cookies.snapshot if server_cookies else None,
    )
    services = Services(
        cache=cache, jobs=jobs, music=Music(
            ClientFactory(credentials),
            # A song's match (title/artist/length -> video) rarely changes; repeat prepares skip
            # the ~1.3 s YouTube Music search for a day.
            cache_ttl=float(os.environ.get("JUKES_RESOLVE_CACHE_SECONDS", 86400)),
            negative_ttl=float(os.environ.get("JUKES_RESOLVE_MISS_SECONDS", 300)),
        ),
        identity=identity, credentials=credentials,
    )
    services.server_cookies = server_cookies
    return services


def create_app(config: Settings | None = None, services: Services | None = None,
               admin: AdminConfig | None = None, browser: BrowserClient | None = None,
               refresher: CookieRefresher | None = None, autorefresh: bool | None = None) -> Flask:
    app = Flask(__name__, static_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static"), template_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"))
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES + 64 * 1024  # multipart framing headroom
    settings = config or Settings(
        legacy_wait_seconds=float(os.environ.get("JUKES_LEGACY_WAIT_SECONDS", 25)),
        max_job_wait_seconds=float(os.environ.get("JUKES_MAX_JOB_WAIT_SECONDS", 10)),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", ""),
        trusted_proxies=parse_networks(os.environ.get("JUKES_TRUSTED_PROXY_CIDRS", "").split(",")),
    )
    default_build = services is None
    services = services or build_services()
    app.extensions["jukes"] = services
    app.extensions["jukes_metrics"] = services.cache.metrics
    register_routes(app, services, settings)
    register_health(app, services)

    if admin is None:
        admin = AdminConfig(
            password_hash=os.environ.get("JUKES_ADMIN_PASSWORD_HASH", ""),
            session_key=os.environ.get("JUKES_ADMIN_SESSION_KEY", ""),
            secure_cookies=os.environ.get("JUKES_ADMIN_INSECURE_COOKIES") != "1",
        )
    if browser is None and os.environ.get("YT_BROWSER_CONTROL_TOKEN"):
        browser = BrowserClient(os.environ.get("YT_BROWSER_SERVICE_URL", "http://127.0.0.1:8765"),
                                os.environ["YT_BROWSER_CONTROL_TOKEN"])

    def caller() -> str:
        from flask import request
        return client_address(
            request.remote_addr,
            {"cf": request.headers.get("CF-Connecting-IP"), "xff": request.headers.get("X-Forwarded-For")},
            settings.trusted_proxies,
        )

    limiter = app.extensions.get("jukes_limiter") or RateLimiter()
    server_cookies = getattr(services, "server_cookies", None)
    if autorefresh is None:
        autorefresh = default_build and os.environ.get("JUKES_COOKIE_AUTOREFRESH", "1") != "0"
    if refresher is None and autorefresh and server_cookies is not None and browser is not None:
        refresher = CookieRefresher(
            services.cache.store, server_cookies, browser,
            lease_active=_browser_lease_active(services.cache.store),
            maintenance_days=float(os.environ.get("JUKES_COOKIE_MAINTENANCE_DAYS", "0") or 0))
        refresher.start()
    if refresher is not None:
        services.jobs.on_cookie_suspect = refresher.request
        app.extensions["jukes_refresher"] = refresher
    app.register_blueprint(make_admin(admin, services, server_cookies, browser, limiter, caller,
                                      refresher=refresher))
    return app
