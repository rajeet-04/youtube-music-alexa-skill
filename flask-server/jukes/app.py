"""Application factory for the JUKES backend."""

from __future__ import annotations

import os

from flask import Flask

from .cache import Cache
from .config import CacheConfig
from .extractor import Extractor
from .jobs import Jobs
from .music import Music, UserContext
from .routes import Services, Settings, register_routes

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
        database_path=os.environ.get("JUKES_DB_FILE") or os.environ.get("DB_FILE") or defaults.database_path,
        audio_dir=os.environ.get("AUDIO_CACHE_DIR", str(defaults.audio_dir)),
        requested_limit_bytes=_int_env("JUKES_REQUESTED_BYTES", defaults.requested_limit_bytes),
        warmup_limit_bytes=_int_env("JUKES_WARMUP_BYTES", defaults.warmup_limit_bytes),
        warmup_ttl_seconds=_int_env("JUKES_WARMUP_TTL_SECONDS", int(defaults.warmup_ttl_seconds)),
        min_free_disk_bytes=_int_env("JUKES_MIN_FREE_BYTES", defaults.min_free_disk_bytes),
    )


def _anonymous_ytmusic(context: UserContext):
    from ytmusicapi import YTMusic  # imported lazily so tests need no network library

    return YTMusic(language="en", location=context.region)


def build_services(config: CacheConfig | None = None) -> Services:
    cache = Cache(config or cache_config_from_env())
    jobs = Jobs(
        cache, Extractor(),
        worker_count=_int_env("JUKES_WORKERS", 4),
        max_queue_size=_int_env("JUKES_QUEUE_SIZE", 100),
    )
    return Services(cache=cache, jobs=jobs, music=Music(_anonymous_ytmusic))


def create_app(config: Settings | None = None, services: Services | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    settings = config or Settings(
        legacy_wait_seconds=float(os.environ.get("JUKES_LEGACY_WAIT_SECONDS", 25)),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", ""),
    )
    services = services or build_services()
    app.extensions["jukes"] = services
    register_routes(app, services, settings)
    return app
