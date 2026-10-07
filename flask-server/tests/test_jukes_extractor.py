"""Deterministic tests for the bounded yt-dlp process wrapper."""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import time
import threading
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from jukes.cache import Cache  # noqa: E402
from jukes.config import CacheConfig  # noqa: E402
from jukes.extractor import (  # noqa: E402
    CredentialSnapshot,
    ExtractionError,
    Extractor,
)
from jukes.models import AudioKey, CacheCapacityError  # noqa: E402


class FakeProcess:
    next_pid = 30_000

    def __init__(self, args, payload=b"audio-data", stderr=b"", returncode=0):
        self.args = list(args)
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.killed = True
        self.returncode = -signal.SIGTERM

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    config = CacheConfig(
        database_path=tmp_path / "state.sqlite3",
        audio_dir=tmp_path / "audio",
        requested_limit_bytes=128,
        warmup_limit_bytes=64,
        min_free_disk_bytes=0,
        unknown_size_reservation_bytes=4,
        reservation_increment_bytes=4,
    )
    return Cache(config)


def test_ytdlp_client_order_and_current_compatible_format_policy(monkeypatch, cache: Cache):
    monkeypatch.setenv("YTDLP_BGUTIL_BASE_URL", "http://provider:4416")
    monkeypatch.setenv("YTDLP_JS_RUNTIME", "deno")
    commands = []

    def process_factory(args, **kwargs):
        commands.append(list(args))
        if len(commands) <= 3:  # tv_simply, web_embedded and default fail; android_vr succeeds
            return FakeProcess(args, payload=b"partial", stderr=b"ERROR: client blocked", returncode=1)
        return FakeProcess(args, payload=b"valid-audio")

    extractor = Extractor(
        process_factory=process_factory,
        public_audio_probe=lambda key, timeout: True,
        media_probe=lambda path: {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "12.5",
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
        },
    )
    key = AudioKey("fallback-order", "public-m4a-v1")
    reservation = cache.reserve(key, requested=True)

    result = extractor.download(
        key,
        reservation,
        CredentialSnapshot(cookie_header="SID=private-secret", generation=7),
    )

    assert [command[command.index("--extractor-args") + 1].split("=")[-1]
            if "--extractor-args" in command else "default" for command in commands] == [
        "tv_simply", "web_embedded", "default", "android_vr"
    ]
    assert "140[vcodec=none]/bestaudio[ext=m4a][vcodec=none]/bestaudio[vcodec=none]" in commands[0]
    assert commands[0][-1] == "https://music.youtube.com/watch?v=fallback-order"
    assert "--remote-components" in commands[0]
    assert "ejs:github" in commands[0]
    assert "--js-runtimes" in commands[0]
    assert "--cookies" not in commands[0]  # tv_simply is tried anonymously first
    assert "--cookies" not in commands[1]  # then web_embedded, also anonymous
    assert "--cookies" in commands[2]      # default is cookie-aware
    assert "--cookies" not in commands[3]  # android_vr is cookie-free
    assert "--extractor-args" in commands[0]
    assert result.mime_type == "audio/mp4"
    assert result.media_format == "mp4"
    assert result.duration_seconds == 12.5
    assert reservation.path.read_bytes() == b"valid-audio"


def test_validation_rejects_combined_video_and_audio(cache):
    extractor = Extractor(media_probe=lambda path: {
        "format_name": "mp4", "duration": "10", "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "h264"},
        ]})
    reservation = cache.reserve(AudioKey("combined", "p"), requested=True)
    reservation.write(b"combined-media")
    with pytest.raises(ExtractionError, match="validation"):
        extractor._validate(reservation, "tv_simply")


def test_public_probe_uses_music_url():
    commands = []
    def spawn(args, **kwargs):
        commands.append(args)
        return FakeProcess(args, payload=b'{"availability":"public"}')
    extractor = Extractor(process_factory=spawn)
    assert extractor._probe_public_audio(AudioKey("public-song", "p"), 5)
    assert commands[0][-1] == "https://music.youtube.com/watch?v=public-song"


def test_progressive_download_keeps_capacity_checks_batched(tmp_path):
    payload=b'a'*1_048_576
    class Destination:
        path=tmp_path/'source.partial'
        writes=0
        def write(self,data):
            self.writes+=1
            with self.path.open('ab') as output: output.write(data)
    destination=Destination(); destination.path.touch()
    extractor=Extractor(process_factory=lambda args,**kw: FakeProcess(args,payload=payload),
        media_probe=lambda path: {'format_name':'mp4','streams':[{'codec_type':'audio','codec_name':'aac'}]})
    extractor._attempt(AudioKey('song','p'),destination,'tv_simply',None,5)
    assert destination.path.read_bytes()==payload
    assert destination.writes<=6  # Avoid 32 capacity/database scans per MiB.


def test_old_clients_do_not_start_a_progressive_encoder(cache):
    class Streams:
        def wanted(self,key): return False
        def begin(self,*args): raise AssertionError('old client started an encoder')
    def spawn(args,**kwargs):
        if '--print-to-file' in args:
            Path(args[args.index('--print-to-file')+2]).write_text('{"duration":12,"acodec":"aac","abr":128}')
        return FakeProcess(args,payload=b'audio')
    extractor=Extractor(process_factory=spawn,media_probe=lambda path:{
        'format_name':'mp4','streams':[{'codec_type':'audio','codec_name':'aac'}]})
    extractor.streams=Streams()
    reservation=cache.reserve(AudioKey('old-client','p'),requested=True)
    extractor._attempt(reservation.key,reservation,'tv_simply',None,5)


def test_rate_limit_after_publication_still_applies_cooldown(cache):
    class Streams:
        def published(self,key): return True
    extractor=Extractor(clock=lambda:100)
    extractor.streams=Streams()
    def rate_limit(*args): raise ExtractionError('rate_limited')
    extractor._attempt=rate_limit
    reservation=cache.reserve(AudioKey('limited','p'),requested=True)
    with pytest.raises(ExtractionError): extractor.download(reservation.key,reservation,None)
    assert extractor.cooldown_remaining()>0


def test_cookie_snapshot_path_is_unique_private_and_removed_after_download(monkeypatch, cache: Cache):
    cookie_paths = []
    cookie_contents = []

    def process_factory(args, **kwargs):
        if any(c in a for a in args for c in ("tv_simply", "web_embedded")):  # anonymous attempts fail; cookie-aware default runs
            return FakeProcess(args, payload=b"", stderr=b"ERROR: blocked", returncode=1)
        if "--cookies" in args:
            path = Path(args[args.index("--cookies") + 1])
            cookie_paths.append(path)
            cookie_contents.append(path.read_text())
            assert path.stat().st_mode & 0o777 == 0o600
        return FakeProcess(args)

    extractor = Extractor(
        process_factory=process_factory,
        public_audio_probe=lambda key, timeout: True,
        media_probe=lambda path: {
            "format_name": "webm",
            "duration": "9",
            "streams": [{"codec_type": "audio", "codec_name": "opus"}],
        },
    )
    key = AudioKey("cookie-snapshot", "policy")
    first = cache.reserve(key, requested=True)
    extractor.download(
        key,
        first,
        CredentialSnapshot(cookie_header="SID=one; SAPISID=two", generation=1),
    )
    second_key = AudioKey("cookie-snapshot-two", "policy")
    second = cache.reserve(second_key, requested=True)
    extractor.download(
        second_key,
        second,
        CredentialSnapshot(cookie_header="SID=three", generation=2),
    )

    assert len(cookie_paths) == 2
    assert cookie_paths[0] != cookie_paths[1]
    assert all(not path.exists() for path in cookie_paths)
    assert "SID\tone" in cookie_contents[0]
    assert "SAPISID\ttwo" in cookie_contents[0]
    assert "SID=one" not in cookie_contents[0]


def test_rate_limit_cooldown_is_bounded_and_suppresses_repeat_attempts(cache: Cache):
    now = [100.0]
    process_calls = []

    def process_factory(args, **kwargs):
        process_calls.append(list(args))
        return FakeProcess(
            args,
            payload=b"",
            stderr=b"ERROR: [youtube] HTTP Error 429: Too Many Requests",
            returncode=1,
        )

    extractor = Extractor(
        process_factory=process_factory,
        public_audio_probe=lambda key, timeout: True,
        media_probe=lambda path: {},
        clock=lambda: now[0],
        cooldown_seconds=1_000,
        max_cooldown_seconds=120,
    )
    first = cache.reserve(AudioKey("rate-limited", "p"), requested=True)
    with pytest.raises(ExtractionError) as failure:
        extractor.download(AudioKey("rate-limited", "p"), first, None)
    assert failure.value.code == "rate_limited"
    assert extractor.cooldown_remaining() <= 120
    calls_after_first = len(process_calls)

    second_key = AudioKey("second-rate-limited", "p")
    second = cache.reserve(second_key, requested=True)
    with pytest.raises(ExtractionError) as cooled:
        extractor.download(second_key, second, None)
    assert cooled.value.code == "rate_limited"
    assert len(process_calls) == calls_after_first


def test_private_or_account_restricted_video_is_rejected_before_cookie_fallback(cache: Cache):
    calls = []
    probes = []

    def process_factory(args, **kwargs):
        calls.append(list(args))  # anonymous clients cannot reach the private audio
        return FakeProcess(args, payload=b"", stderr=b"ERROR: client blocked", returncode=1)

    def probe(key, timeout):
        probes.append(len(calls))
        return False

    extractor = Extractor(process_factory=process_factory, public_audio_probe=probe)
    key = AudioKey("private-video", "p")
    reservation = cache.reserve(key, requested=True)

    with pytest.raises(ExtractionError) as failure:
        extractor.download(
            key,
            reservation,
            CredentialSnapshot(cookie_header="SID=operator-cookie", generation=4),
        )

    assert failure.value.code == "public_audio_required"
    assert probes == [2]  # after tv_simply and web_embedded, before the first cookie attempt
    assert not any("--cookies" in command for command in calls)


def test_public_probe_is_skipped_when_an_anonymous_client_succeeds(cache: Cache):
    probes = []
    extractor = Extractor(
        process_factory=lambda args, **kwargs: FakeProcess(args, payload=b"valid-audio"),
        public_audio_probe=lambda key, timeout: probes.append(key) or True,
        media_probe=lambda path: {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "12.5",
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
        },
    )
    key = AudioKey("public-video", "p")
    reservation = cache.reserve(key, requested=True)

    extractor.download(key, reservation, CredentialSnapshot(cookie_header="SID=operator-cookie", generation=4))

    assert probes == []


def test_dead_video_and_flaky_failures_receive_different_backoff(monkeypatch, cache: Cache):
    command_count = 0

    def dead_factory(args, **kwargs):
        nonlocal command_count
        command_count += 1
        return FakeProcess(args, payload=b"", stderr=b"ERROR: Private video", returncode=1)

    dead = Extractor(
        process_factory=dead_factory,
        public_audio_probe=lambda key, timeout: True,
        media_probe=lambda path: {},
        clock=lambda: 100.0,
    )
    dead_key = AudioKey("dead", "p")
    with pytest.raises(ExtractionError):
        dead.download(dead_key, cache.reserve(dead_key, requested=True), None)
    dead_calls = command_count
    with pytest.raises(ExtractionError) as cached_dead:
        dead.download(dead_key, cache.reserve(dead_key, requested=True), None)
    assert cached_dead.value.code == "video_unavailable"
    assert command_count == dead_calls

    flaky_factory = lambda args, **kwargs: FakeProcess(  # noqa: E731
        args, payload=b"", stderr=b"ERROR: socket reset", returncode=1
    )
    flaky = Extractor(
        process_factory=flaky_factory,
        public_audio_probe=lambda key, timeout: True,
        media_probe=lambda path: {},
        clock=lambda: 100.0,
        flaky_ttl_seconds=2,
    )
    flaky_key = AudioKey("flaky", "p")
    with pytest.raises(ExtractionError):
        flaky.download(flaky_key, cache.reserve(flaky_key, requested=True), None)
    with pytest.raises(ExtractionError) as cached_flaky:
        flaky.download(flaky_key, cache.reserve(flaky_key, requested=True), None)
    assert cached_flaky.value.code == "video_temporarily_unavailable"


def test_timeout_terminates_the_child_process_group(monkeypatch, cache: Cache):
    processes = []
    killed_groups = []

    class TimedOutProcess(FakeProcess):
        def poll(self):
            return None

        def wait(self, timeout=None):
            import subprocess

            if timeout is not None:
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.returncode = -signal.SIGTERM
            return self.returncode

    def process_factory(args, **kwargs):
        process = TimedOutProcess(args, payload=b"")
        processes.append(process)
        return process

    monkeypatch.setattr("jukes.extractor.os.killpg", lambda pid, sig: killed_groups.append((pid, sig)))
    extractor = Extractor(
        process_factory=process_factory,
        public_audio_probe=lambda key, timeout: True,
        media_probe=lambda path: {},
        total_timeout_seconds=0.03,
        fallback_timeout_seconds=0.02,
    )
    key = AudioKey("slow-child", "p")
    with pytest.raises(ExtractionError) as failure:
        extractor.download(key, cache.reserve(key, requested=True), None)

    assert failure.value.code == "extraction_timeout"
    assert processes
    assert killed_groups


def test_output_limit_raises_cache_capacity_error_before_accepting_more_bytes(cache: Cache):
    extractor = Extractor(
        process_factory=lambda args, **kwargs: FakeProcess(args, payload=b"z" * 200),
        public_audio_probe=lambda key, timeout: True,
        media_probe=lambda path: {},
    )
    key = AudioKey("oversized", "p")
    reservation = cache.reserve(key, requested=False)

    with pytest.raises(CacheCapacityError):
        extractor.download(key, reservation, None)

    assert reservation.path.stat().st_size <= cache.config.unknown_size_reservation_bytes + cache.config.reservation_increment_bytes


def test_invalid_media_is_rejected_and_actual_container_mime_is_reported(cache: Cache):
    extractor = Extractor(
        process_factory=lambda args, **kwargs: FakeProcess(args, payload=b"webm-bytes"),
        public_audio_probe=lambda key, timeout: True,
        media_probe=lambda path: {
            "format_name": "matroska,webm",
            "duration": "42.25",
            "streams": [{"codec_type": "audio", "codec_name": "opus"}],
        },
    )
    key = AudioKey("actual-webm", "m4a-policy-v1")
    result = extractor.download(key, cache.reserve(key, requested=True), None)

    assert result.media_format == "webm"
    assert result.mime_type == "audio/webm"
    assert result.duration_seconds == 42.25
    assert result.codec_name == "opus"


def test_invalid_media_without_audio_stream_fails_safely(cache: Cache):
    extractor = Extractor(
        process_factory=lambda args, **kwargs: FakeProcess(args),
        public_audio_probe=lambda key, timeout: True,
        media_probe=lambda path: {
            "format_name": "mp4",
            "duration": "8",
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
        },
    )
    key = AudioKey("not-audio", "p")

    with pytest.raises(ExtractionError) as failure:
        extractor.download(key, cache.reserve(key, requested=True), None)
    assert failure.value.code == "invalid_media"


def test_public_probe_command_is_unauthenticated_and_scope_parser_rejects_restricted():
    command_seen = []

    def process_factory(args, **kwargs):
        command_seen.append(list(args))
        return FakeProcess(
            args,
            payload=json.dumps({"availability": "needs_auth", "is_private": True}).encode(),
        )

    extractor = Extractor(process_factory=process_factory, media_probe=lambda path: {})
    assert extractor._probe_public_audio(AudioKey("restricted", "p"), time.monotonic() + 1) is False
    assert command_seen
    command = command_seen[0]
    assert "--no-cookies" in command
    assert "--dump-single-json" in command
    assert "--cookies" not in command


def test_failure_text_never_includes_secret_bearing_stderr(cache: Cache):
    extractor = Extractor(
        process_factory=lambda args, **kwargs: FakeProcess(
            args,
            payload=b"",
            stderr=b"ERROR: header Cookie: SID=super-secret",
            returncode=1,
        ),
        public_audio_probe=lambda key, timeout: True,
        media_probe=lambda path: {},
    )
    key = AudioKey("redacted-error", "p")

    with pytest.raises(ExtractionError) as failure:
        extractor.download(
            key,
            cache.reserve(key, requested=True),
            CredentialSnapshot(cookie_header="SID=super-secret", generation=9),
        )

    assert "super-secret" not in str(failure.value)
    assert "Cookie" not in str(failure.value)


def test_requested_format_error_is_not_classified_as_a_dead_video(cache: Cache):
    extractor = Extractor(
        process_factory=lambda args, **kwargs: FakeProcess(
            args, payload=b"", stderr=b"ERROR: [youtube] x: Requested format is not available.", returncode=1),
        public_audio_probe=lambda key, timeout: True,
        media_probe=lambda path: {},
    )
    key = AudioKey("fmt-missing", "p")
    with pytest.raises(ExtractionError) as failure:
        extractor.download(key, cache.reserve(key, requested=True), None)
    assert failure.value.code == "extraction_failed"  # not video_unavailable, so no hour-long dead cache
    from jukes.extractor import _error_hint
    assert "SID=" not in _error_hint("ERROR: bad Cookie: SID=abc123 failed") and "abc123" not in _error_hint("ERROR: Cookie: SID=abc123")


def test_client_order_can_be_overridden_without_a_rebuild(monkeypatch):
    from jukes import extractor as module

    assert module.client_order()[:2] == ("tv_simply", "web_embedded")
    monkeypatch.setenv("YTDLP_CLIENT_ORDER", " web_embedded , default ")
    assert module.client_order() == ("web_embedded", "default")
    monkeypatch.setenv("YTDLP_CLIENT_ORDER", " , ")
    assert module.client_order() == module.CLIENT_ORDER
