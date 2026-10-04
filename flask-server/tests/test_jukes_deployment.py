"""Deployable anonymous service: clean import, retired routes, health, proxy trust, ownership guard."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from jukes.app import create_app  # noqa: E402
from jukes.cache import Cache  # noqa: E402
from jukes.config import CacheConfig  # noqa: E402
from jukes.guard import AlreadyRunning, acquire_exclusive, release_exclusive  # noqa: E402
from jukes.jobs import Jobs  # noqa: E402
from jukes.limits import parse_networks  # noqa: E402
from jukes.music import Music  # noqa: E402
from jukes.routes import Services, Settings  # noqa: E402
from jukes.health import register_health  # noqa: E402


def services_for(tmp_path):
    config = CacheConfig(database_path=tmp_path / "s.sqlite3", audio_dir=tmp_path / "audio", min_free_disk_bytes=0)
    cache = Cache(config)
    jobs = Jobs(cache, object(), worker_count=1)
    return Services(cache=cache, jobs=jobs, music=Music(lambda c: None)), jobs


def test_server_entry_point_imports_without_alexa_dependencies(tmp_path):
    script = textwrap.dedent("""
        import sys
        import server
        assert hasattr(server, "app")
        banned = [m for m in sys.modules if m.split(".")[0] in {"alexapy", "alexa_remote", "home_feed", "aiohttp", "qrcode"}]
        assert not banned, banned
        rules = {r.rule for r in server.app.url_map.iter_rules()}
        for gone in ("/alexa/search", "/proxy/", "/remote", "/", "/login", "/get_radio/", "/jam"):
            assert gone not in rules, gone
        for kept in ("/audio/", "/v1/warmup", "/v1/audio/prepare", "/health/live", "/admin/login"):
            assert kept in rules, kept
        print("ok")
    """)
    env = dict(os.environ, JUKES_DB_PATH=str(tmp_path / "x" / "j.sqlite3"), JUKES_AUDIO_DIR=str(tmp_path / "a"),
               PYTHONPATH=str(SERVER_DIR))
    result = subprocess.run([sys.executable, "-c", script], cwd=SERVER_DIR, env=env, capture_output=True, text=True)
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr


def test_requirements_drop_alexa_and_player_only_dependencies():
    names = {line.split("==")[0].split("[")[0].strip().lower()
             for line in (SERVER_DIR / "requirements.txt").read_text().splitlines() if line.strip()}
    assert not names & {"alexapy", "aiohttp", "qrcode"}
    assert {"flask", "waitress", "yt-dlp", "ytmusicapi", "cryptography"} <= names


def test_retired_routes_are_404_json(tmp_path):
    services, jobs = services_for(tmp_path)
    try:
        client = create_app(Settings(), services).test_client()
        for path in ("/", "/alexa/search", "/proxy/abc", "/remote", "/static/js/player.js", "/jam"):
            r = client.get(path)
            assert r.status_code == 404 and r.get_json()["error"]["code"] == "not_found"
    finally:
        jobs.shutdown()


def test_health_live_is_cheap_and_ready_separates_serving_from_extraction(tmp_path):
    services, jobs = services_for(tmp_path)
    try:
        app = create_app(Settings(), services)
        c = app.test_client()
        assert c.get("/health/live").get_json() == {"status": "ok"}
        ready = c.get("/health/ready")
        body = ready.get_json()
        assert set(body) == {"status", "serving", "extraction"} and body["serving"]["database"] is True
        # a missing extractor dependency degrades but never fails serving
        from flask import Flask
        probe_app = Flask("probe")
        calls = []
        register_health(probe_app, services, reachable=lambda url: calls.append(url) or False,
                        which=lambda name: None)
        r = probe_app.test_client().get("/health/ready")
        assert r.status_code == 200 and r.get_json()["status"] == "degraded"
        probe_app.test_client().get("/health/ready")
        assert len(calls) == 1  # cached: health checks never hammer the provider
    finally:
        jobs.shutdown()


def test_ready_reports_unavailable_when_database_is_broken(tmp_path):
    services, jobs = services_for(tmp_path)
    try:
        class Broken:
            def connection(self):
                raise RuntimeError("db down")
        services.cache.store = Broken()
        from flask import Flask
        probe_app = Flask("probe")
        register_health(probe_app, services, reachable=lambda u: True, which=lambda n: "/bin/x")
        r = probe_app.test_client().get("/health/ready")
        assert r.status_code == 503 and r.get_json()["status"] == "unavailable"
    finally:
        jobs.shutdown()


def test_exclusive_guard_blocks_second_owner(tmp_path):
    db = tmp_path / "own" / "j.sqlite3"
    acquire_exclusive(db)
    code = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {str(SERVER_DIR)!r})
        from jukes.guard import acquire_exclusive, AlreadyRunning
        try:
            acquire_exclusive({str(db)!r})
        except AlreadyRunning:
            print("blocked")
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "blocked"
    release_exclusive(db)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == ""  # lock was free again


def test_rate_limits_use_direct_peer_unless_proxy_is_trusted(tmp_path):
    services, jobs = services_for(tmp_path)
    try:
        trusted = Settings(trusted_proxies=parse_networks(["127.0.0.0/8"]), issue_per_minute=2)
        untrusted = Settings(issue_per_minute=2)
        for settings, expect_bypass in ((untrusted, False), (trusted, True)):
            from jukes.identity import Identity
            services.identity = Identity(services.cache.store)
            services.credentials = object()
            services.limiter = None
            app = create_app(settings, services)
            c = app.test_client()  # peer is 127.0.0.1
            codes = [c.post("/v1/installations", headers={"CF-Connecting-IP": f"9.9.9.{i}"}).status_code
                     for i in range(4)]
            assert (codes == [201, 201, 201, 201]) if expect_bypass else codes[2:] == [429, 429]
            if expect_bypass is False:
                pass
    finally:
        jobs.shutdown()
