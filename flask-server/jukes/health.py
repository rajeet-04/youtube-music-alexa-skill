"""Cheap liveness and cached readiness endpoints.

Liveness only proves the process answers. Readiness separates serving health
(database, cache directory, workers) from extraction dependencies (FFmpeg, Deno,
bgutil provider); a degraded extraction stack still serves cached audio, so it
reports ``degraded`` with HTTP 200 rather than failing the container. Probes are
cached so health checks never hammer upstream services.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from flask import Flask, jsonify

CACHE_SECONDS = 30.0


def tcp_reachable(url: str, timeout: float = 1.5) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False
    try:
        with socket.create_connection((host, parsed.port or (443 if parsed.scheme == "https" else 80)), timeout):
            return True
    except OSError:
        return False


def register_health(app: Flask, services, *, clock: Callable[[], float] = time.monotonic,
                    reachable: Callable[[str], bool] = tcp_reachable,
                    which: Callable[[str], str | None] = shutil.which) -> None:
    lock = threading.Lock()
    cached: dict[str, Any] = {"at": -1e9, "value": None}

    def probe() -> dict[str, Any]:
        serving = {"database": True, "audio_dir": True, "workers": True}
        try:
            with services.cache.store.connection() as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception:  # noqa: BLE001
            serving["database"] = False
        serving["audio_dir"] = os.access(services.cache.audio_dir, os.W_OK)
        threads = getattr(services.jobs, "_threads", [])
        serving["workers"] = (not threads) or any(t.is_alive() for t in threads)
        bgutil = os.environ.get("YTDLP_BGUTIL_BASE_URL", "http://bgutil-provider:4416")
        extraction = {
            "ffmpeg": which("ffmpeg") is not None,
            "yt_dlp": which("yt-dlp") is not None,
            "js_runtime": any(which(r) for r in ("deno", "node", "bun")),
            "bgutil_provider": (not bgutil) or reachable(bgutil),
        }
        return {"serving": serving, "extraction": extraction}

    @app.get("/health/live")
    def live():
        response = jsonify({"status": "ok"})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health/ready")
    def ready():
        with lock:
            if clock() - cached["at"] > CACHE_SECONDS:
                cached["value"], cached["at"] = probe(), clock()
            value = cached["value"]
        serving_ok = all(value["serving"].values())
        extraction_ok = all(value["extraction"].values())
        body = {"status": "ok" if serving_ok and extraction_ok else ("degraded" if serving_ok else "unavailable"),
                **value}
        response = jsonify(body)
        response.status_code = 200 if serving_ok else 503
        response.headers["Cache-Control"] = "no-store"
        return response
