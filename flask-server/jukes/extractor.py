"""Bounded yt-dlp process wrapper that streams public audio into a cache reservation.

Credentials never reach logs or error text: failures are classified into short
codes and stderr is discarded after classification.
"""

from __future__ import annotations

import json
import logging
import re
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import AudioKey, CacheCapacityError

log = logging.getLogger(__name__)

# tv_simply goes first, without cookies: with a bgutil PO token it downloads public m4a in
# ~3 s. web_embedded is next: it also works anonymously, but YouTube serves it a pre-roll ad
# and refuses the audio URL (403) until the ad's skip time has passed, so yt-dlp sleeps ~5 s
# first (~10 s per song). On the deployed VPN host android_vr/mweb have returned 403 and
# tv/default with rotated account cookies "page needs to be reloaded". The legacy fallbacks
# follow: default (cookie-aware), android_vr, web, tv. YTDLP_CLIENT_ORDER overrides the order
# (comma-separated) without a rebuild.
CLIENT_ORDER = ("tv_simply", "web_embedded", "default", "android_vr", "web", "tv")
COOKIELESS_CLIENTS = {"tv_simply", "web_embedded", "android_vr", "ios"}
FORMAT_SELECTOR = "140[vcodec=none]/bestaudio[ext=m4a][vcodec=none]/bestaudio[vcodec=none]"
CHUNK_BYTES = 256 * 1024
STDERR_LIMIT = 16 * 1024

_MESSAGES = {
    "rate_limited": "YouTube is rate limiting this host",
    "video_unavailable": "video is unavailable",
    "video_temporarily_unavailable": "video recently failed to download",
    "public_audio_required": "audio is not public",
    "extraction_timeout": "extraction timed out",
    "invalid_media": "downloaded media failed validation",
    "extraction_failed": "audio extraction failed",
}


class ExtractionError(RuntimeError):
    def __init__(self, code: str, *, cookie_suspect: bool = False) -> None:
        super().__init__(_MESSAGES.get(code, "audio extraction failed"))
        self.code = code
        # True when an attempt that sent account cookies was rejected in a way that points
        # at stale cookies (not at the video). Used to schedule a rare cookie refresh.
        self.cookie_suspect = cookie_suspect


@dataclass(frozen=True)
class CredentialSnapshot:
    """Immutable per-job credential view. Never persisted or logged."""

    cookie_header: str = ""
    generation: int = 0
    cookie_jar_text: str | None = None

    def __repr__(self) -> str:  # keep secrets out of accidental logging
        return f"CredentialSnapshot(generation={self.generation})"


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size_bytes: int
    media_format: str
    mime_type: str
    duration_seconds: float | None
    codec_name: str
    client: str


def client_order() -> tuple[str, ...]:
    configured = [c.strip() for c in os.environ.get("YTDLP_CLIENT_ORDER", "").split(",") if c.strip()]
    return tuple(configured) or CLIENT_ORDER


def _cookie_jar_from_header(header: str) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if not sep or not name or any(c in name for c in "\t\r\n ") or any(c in value for c in "\t\r\n"):
            continue
        lines.append("\t".join((".youtube.com", "TRUE", "/", "TRUE", "2147483647", name, value)))
    return "\n".join(lines) + "\n"


_SECRETISH = re.compile(r"(?i)(cookie|sid|token|authorization|apikey|key)[=:\s]+\S+")


def _error_hint(text: str) -> str:
    """One scrubbed yt-dlp ERROR line for operator logs (never part of API responses)."""
    for line in text.splitlines():
        if "ERROR" in line:
            return _SECRETISH.sub(r"\1=<redacted>", line.strip())[:220]
    return ""


_COOKIE_TROUBLE = (
    "page needs to be reloaded", "sign in to confirm", "login required", "cookies are no longer valid",
    "use --cookies", "account cookies",
)


def _cookie_trouble(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in _COOKIE_TROUBLE)


def _classify_stderr(text: str) -> str:
    lowered = text.lower()
    # "Requested format is not available" is a client/PO-token problem, not a dead video.
    if "requested format" in lowered:
        return "extraction_failed"
    if "429" in lowered or "too many requests" in lowered or "rate limit" in lowered or "rate-limit" in lowered:
        return "rate_limited"
    if any(token in lowered for token in (
        "private video", "video unavailable", "is not available", "has been removed",
        "account associated", "been terminated", "members-only", "members only",
    )):
        return "video_unavailable"
    return "extraction_failed"


def _ffprobe(path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, timeout=20, check=False,
        )
        data = json.loads(completed.stdout or b"{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    result = dict(data.get("format") or {})
    result["streams"] = data.get("streams") or []
    return result


def _container(format_name: str) -> tuple[str, str] | None:
    names = {part.strip() for part in format_name.split(",")}
    if names & {"mp4", "m4a", "mov"}:
        return "mp4", "audio/mp4"
    if names & {"webm", "matroska"}:
        return "webm", "audio/webm"
    if "mp3" in names:
        return "mp3", "audio/mpeg"
    if "ogg" in names:
        return "ogg", "audio/ogg"
    if "aac" in names:
        return "aac", "audio/aac"
    return None


class Extractor:
    def __init__(
        self,
        *,
        process_factory: Callable[..., Any] = subprocess.Popen,
        public_audio_probe: Callable[[AudioKey, float], bool] | None = None,
        media_probe: Callable[[Path], dict[str, Any]] = _ffprobe,
        clock: Callable[[], float] = time.time,
        total_timeout_seconds: float = 120.0,
        fallback_timeout_seconds: float = 45.0,
        cooldown_seconds: float = 90.0,
        max_cooldown_seconds: float = 300.0,
        dead_ttl_seconds: float = 3_600.0,
        flaky_ttl_seconds: float = 30.0,
        retries: int = 2,
        socket_timeout: int = 10,
    ) -> None:
        self._popen = process_factory
        if process_factory is subprocess.Popen and os.environ.get('JUKES_EXTRACTOR_FORKSERVER') == '1':
            from .fork_download import ForkDownload, warm
            warm()
            self._popen = ForkDownload
        self._public_probe = public_audio_probe or self._probe_public_audio
        self._media_probe = media_probe
        self._clock = clock
        self.total_timeout_seconds = total_timeout_seconds
        self.fallback_timeout_seconds = fallback_timeout_seconds
        self.cooldown_seconds = cooldown_seconds
        self.max_cooldown_seconds = max_cooldown_seconds
        self.dead_ttl_seconds = dead_ttl_seconds
        self.flaky_ttl_seconds = flaky_ttl_seconds
        self.retries = retries
        self.socket_timeout = socket_timeout
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._dead: dict[str, float] = {}
        self._flaky: dict[str, float] = {}
        self._processes: set[Any] = set()
        self._closing = False
        self.on_retry = None
        self.streams = None

    # -- shared failure state ------------------------------------------
    def cooldown_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until - self._clock())

    def _start_cooldown(self) -> None:
        with self._lock:
            self._cooldown_until = self._clock() + min(self.cooldown_seconds, self.max_cooldown_seconds)

    def _cached_failure(self, video_id: str) -> str | None:
        now = self._clock()
        with self._lock:
            for table, code in ((self._dead, "video_unavailable"), (self._flaky, "video_temporarily_unavailable")):
                until = table.get(video_id)
                if until is not None:
                    if until > now:
                        return code
                    del table[video_id]
            if self._cooldown_until > now:
                return "rate_limited"
        return None

    def _remember(self, table: dict[str, float], video_id: str, ttl: float) -> None:
        with self._lock:
            if len(table) > 2_000:
                table.clear()
            table[video_id] = self._clock() + ttl

    # -- commands ------------------------------------------------------
    @staticmethod
    def _js_runtime_args() -> list[str]:
        configured = (os.environ.get("YTDLP_JS_RUNTIME") or "").strip().lower()
        for runtime in [configured] if configured else ["deno", "node", "bun", "qjs"]:
            if not runtime:
                continue
            path = shutil.which(runtime)
            if path:
                return ["--js-runtimes", f"{runtime}:{path}"]
            if configured:
                return ["--js-runtimes", runtime]
        return []

    def _command(self, key: AudioKey, client: str, cookie_path: str | None) -> list[str]:
        command = [
            "yt-dlp", "--no-playlist", "--quiet", "-f", FORMAT_SELECTOR,
            "--remote-components", "ejs:github",
            "--retries", str(self.retries), "--socket-timeout", str(self.socket_timeout),
        ]
        command += self._js_runtime_args()
        if cookie_path and client not in COOKIELESS_CLIENTS:
            command += ["--cookies", cookie_path]
        player_args = [f"youtube:player_client={client}"]
        if client != "default" and (token := os.environ.get("YTDLP_PO_TOKEN")):
            player_args.append(f"youtube:po_token=mweb.gvs+{token}")
        command += ["--extractor-args", ",".join(player_args)]
        base_url = os.environ.get("YTDLP_BGUTIL_BASE_URL", "http://bgutil-provider:4416")
        if base_url:
            command += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={base_url}"]
        command += ["-o", "-", "--", f"https://music.youtube.com/watch?v={key.video_id}"]
        return command

    # -- public-scope probe --------------------------------------------
    def _probe_public_audio(self, key: AudioKey, timeout: float) -> bool:
        """True unless an unauthenticated metadata fetch shows restricted audio."""
        command = ["yt-dlp", "--no-cookies", "--dump-single-json", "--skip-download", "--no-playlist",
                   "--quiet", "--socket-timeout", str(self.socket_timeout)]
        command += self._js_runtime_args()
        base_url = os.environ.get("YTDLP_BGUTIL_BASE_URL", "http://bgutil-provider:4416")
        if base_url:
            command += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={base_url}"]
        command += ["--", f"https://music.youtube.com/watch?v={key.video_id}"]
        process = self._spawn(command)
        try:
            raw = process.stdout.read(2_000_000)
            try:
                process.wait(timeout=max(1.0, min(float(timeout), 30.0)))
            except subprocess.TimeoutExpired:
                self._kill(process)
                return True
        finally:
            self._forget(process)
        try:
            info = json.loads(raw or b"{}")
        except ValueError:
            return True  # inconclusive; extraction itself decides
        availability = info.get("availability")
        return not (info.get("is_private") or availability in {
            "private", "needs_auth", "premium_only", "subscriber_only"})

    # -- process helpers -----------------------------------------------
    def _spawn(self, command: list[str]):
        if self._closing:
            raise ExtractionError("extraction_failed")
        process = self._popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        with self._lock:
            self._processes.add(process)
        return process

    def _forget(self, process) -> None:
        with self._lock:
            self._processes.discard(process)

    @staticmethod
    def _kill(process) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                process.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                continue
            except Exception:  # noqa: BLE001 - best effort during teardown
                return

    def terminate_all(self) -> None:
        """Stop admission and kill every child process group (shutdown)."""
        with self._lock:
            self._closing = True
            running = list(self._processes)
        for process in running:
            self._kill(process)

    # -- download ------------------------------------------------------
    def download(self, key: AudioKey, destination, credential_snapshot) -> DownloadResult:
        if (code := self._cached_failure(key.video_id)) is not None:
            raise ExtractionError(code)
        deadline = time.monotonic() + self.total_timeout_seconds
        has_cookies = bool(credential_snapshot and (
            credential_snapshot.cookie_header or getattr(credential_snapshot, "cookie_jar_text", None)))
        cookie_path = None
        last_code = "extraction_failed"
        suspect = False
        try:
            for index, client in enumerate(client_order()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    last_code = "extraction_timeout"
                    break
                # Only an attempt with the server's cookies could reach non-public audio, so the
                # public-audio probe (a full metadata extraction, ~3 s) runs just before the first
                # such attempt. Anonymous clients go first and usually succeed without it.
                if has_cookies and cookie_path is None and client not in COOKIELESS_CLIENTS:
                    if not self._public_probe(key, max(1.0, remaining)):
                        raise ExtractionError("public_audio_required")
                    cookie_path = self._write_cookie_file(credential_snapshot)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        last_code = "extraction_timeout"
                        break
                attempt_timeout = min(remaining, self.fallback_timeout_seconds)
                if index and self.on_retry is not None:
                    self.on_retry()
                try:
                    return self._attempt(key, destination, client, cookie_path, attempt_timeout)
                except ExtractionError as error:
                    last_code = error.code
                    suspect = suspect or error.cookie_suspect
                    if error.code == "rate_limited":
                        self._start_cooldown()
                        raise
                    if self.streams and self.streams.published(key):
                        raise  # Never restart a generation after its bytes were published.
                    if error.code in {"video_unavailable", "invalid_media"}:
                        if error.code == "video_unavailable":
                            self._remember(self._dead, key.video_id, self.dead_ttl_seconds)
                        raise
        finally:
            if cookie_path:
                try:
                    os.unlink(cookie_path)
                except OSError:
                    pass
        if last_code == "extraction_failed":
            self._remember(self._flaky, key.video_id, self.flaky_ttl_seconds)
        raise ExtractionError(last_code, cookie_suspect=suspect)

    @staticmethod
    def _write_cookie_file(snapshot) -> str:
        text = getattr(snapshot, "cookie_jar_text", None) or _cookie_jar_from_header(snapshot.cookie_header)
        fd, path = tempfile.mkstemp(prefix="jukes-cookies-", suffix=".txt")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    def _attempt(self, key, destination, client, cookie_path, timeout) -> DownloadResult:
        attempt_started=time.monotonic()
        first_byte_seconds=None
        cache_write_seconds=stream_write_seconds=0.0
        with open(destination.path, "r+b") as partial:  # discard bytes from a failed client
            partial.truncate(0)
        command = self._command(key, client, cookie_path)
        metadata_path = Path(str(destination.path)+'.stream.json')
        if self.streams:
            metadata_path.unlink(missing_ok=True)
            command[1:1] = ['--print-to-file',
                'before_dl:{"acodec":%(acodec)j,"abr":%(abr)j,"duration":%(duration)j}',str(metadata_path)]
        process = self._spawn(command)
        stream = None
        first_chunk = True
        source_ok = False
        stderr_chunks: list[bytes] = []

        def drain_stderr() -> None:
            try:
                data = process.stderr.read(STDERR_LIMIT)
                stderr_chunks.append(data or b"")
                while process.stderr.read(4096):
                    pass
            except (OSError, ValueError):
                pass

        reader = threading.Thread(target=drain_stderr, daemon=True)
        reader.start()
        timed_out = threading.Event()

        def on_timeout() -> None:
            timed_out.set()
            self._kill(process)
            if stream and stream.process.poll() is None:
                stream.process.kill()  # Also unblock a stalled progressive stdin write.

        watchdog = threading.Timer(timeout, on_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                while True:
                    # Get startup audio promptly, then amortize persistent capacity checks.
                    wanted=self.streams and self.streams.wanted(key)
                    chunk = process.stdout.read(64 * 1024 if first_chunk and wanted else CHUNK_BYTES)
                    if not chunk:
                        break
                    if first_byte_seconds is None: first_byte_seconds=time.monotonic()-attempt_started
                    write_started=time.monotonic()
                    destination.write(chunk)
                    cache_write_seconds+=time.monotonic()-write_started
                    if first_chunk and wanted:
                        try:
                            metadata=json.loads(metadata_path.read_text())
                            stream=self.streams.begin(key,metadata,source_path=destination.path)
                        except (OSError,ValueError,TypeError):
                            pass  # A missing hint only disables progressive output.
                    first_chunk=False
                    if stream:
                        write_started=time.monotonic()
                        stream.write(chunk)
                        stream_write_seconds+=time.monotonic()-write_started
            except CacheCapacityError:
                self._kill(process)
                raise
            try:
                code = process.wait(timeout=max(0.001, timeout))
                source_ok = code == 0 and not timed_out.is_set()
            except subprocess.TimeoutExpired:
                timed_out.set()
                self._kill(process)
                raise ExtractionError("extraction_timeout") from None
        finally:
            watchdog.cancel()
            self._forget(process)
            metadata_path.unlink(missing_ok=True)
            if stream: stream.finish(source_ok)
            if os.environ.get('JUKES_TIMING_LOG')=='1':
                log.warning('audio timing video=%s client=%s first_byte=%.3f total=%.3f cache_write=%.3f stream_write=%.3f stream_ready=%s',
                    key.video_id,client,first_byte_seconds or 0,time.monotonic()-attempt_started,
                    cache_write_seconds,stream_write_seconds,stream.ready_seconds if stream else None)
        reader.join(timeout=2)
        if timed_out.is_set():
            raise ExtractionError("extraction_timeout")
        if code != 0:
            text = b"".join(stderr_chunks).decode("utf-8", "replace")
            error_code = _classify_stderr(text)
            used_cookies = bool(cookie_path) and client not in COOKIELESS_CLIENTS
            if used_cookies and _cookie_trouble(text):
                log.warning("yt-dlp %s attempt failed: %s | %s", client, error_code, _error_hint(text))
                raise ExtractionError(error_code, cookie_suspect=True)
            log.warning("yt-dlp %s attempt failed: %s | %s", client, error_code, _error_hint(text))
            raise ExtractionError(error_code)
        return self._validate(destination, client)

    def _validate(self, destination, client: str) -> DownloadResult:
        path = Path(destination.path)
        probe = self._media_probe(path) or {}
        container = _container(str(probe.get("format_name", "")))
        audio = [s for s in probe.get("streams") or [] if s.get("codec_type") == "audio"]
        if container is None or not audio or any(s.get("codec_type") == "video" for s in probe.get("streams") or []):
            raise ExtractionError("invalid_media")
        try:
            duration = float(probe.get("duration"))
        except (TypeError, ValueError):
            duration = None
        return DownloadResult(
            path=path, size_bytes=path.stat().st_size, media_format=container[0],
            mime_type=container[1], duration_seconds=duration,
            codec_name=str(audio[0].get("codec_name", "")), client=client,
        )
