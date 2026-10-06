"""HTTP surface: versioned warmup/prepare/jobs/audio plus the legacy /audio/ endpoint."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from flask import Flask, Response, jsonify, request, send_file

from .credentials import (
    CredentialDecryptionError, CredentialValidationError, InstallationNotFound, InvalidCredentialBundle,
    MissingCredentialKey,
)
from .identity import InvalidInstallationToken
from .jobs import Job, JobQueueFull
from .limits import RateLimiter, client_address, is_trusted_peer
from .models import AudioKey, CacheCapacityError
from .music import (
    VIDEO_ID_RE, ANONYMOUS, MusicError, Track, TrackSelector, UserContext,
)

AUDIO_POLICY = "public-m4a-v1"

# error code -> (http status, retryable)
JOB_ERRORS = {
    "video_unavailable": (404, False),
    "public_audio_required": (422, False),
    "invalid_media": (502, False),
    "track_too_large": (413, False),
    "warmup_too_large": (413, False),
    "rate_limited": (503, True),
    "cache_capacity": (503, True),
    "cache_evicted": (503, True),
    "warmup_expired": (503, True),
    "interrupted": (503, True),
    "video_temporarily_unavailable": (503, True),
    "extraction_timeout": (502, True),
    "extraction_failed": (502, True),
    "queue_full": (429, True),
}
MUSIC_STATUS = {"no_match": 404, "match_rejected": 422, "upstream_error": 502}
RETRY_AFTER = "3"


class InvalidToken(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False, retry_after: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retryable, self.retry_after = retryable, retry_after


@dataclass
class Services:
    cache: Any
    jobs: Any
    music: Any
    identity: Any = None
    credentials: Any = None
    limiter: Any = None
    server_cookies: Any = None
    # Optional hook for admin/readiness code; routes themselves use identity/credentials.
    context_provider: Callable[[str], UserContext] | None = None


@dataclass(frozen=True)
class Settings:
    legacy_wait_seconds: float = 25.0
    public_base_url: str = ""
    trusted_proxies: tuple = ()
    warmup_per_minute: int = 30
    requested_per_minute: int = 60
    poll_per_minute: int = 120
    max_job_wait_seconds: float = 10.0
    issue_per_minute: int = 5
    http_per_minute: int = 600


def error_response(status: int, code: str, message: str, retryable: bool = False,
                   retry_after: str | None = None) -> Response:
    response = jsonify({"error": {"code": code, "message": message, "retryable": retryable}})
    response.status_code = status
    if retryable:
        response.headers["Retry-After"] = retry_after or RETRY_AFTER
    response.headers["Cache-Control"] = "no-store"
    return response


def sniff_mime(path) -> str:
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError:
        return "audio/mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "audio/mpeg"
    return "audio/mp4"


_YT_HOSTS = ("youtube.com", "youtu.be", "music.youtube.com", "m.youtube.com")


def video_id_from_url(url: str) -> str | None:
    url = (url or "").strip()
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in _YT_HOSTS):
        return None
    query = parse_qs(parsed.query)
    parts = [p for p in unquote(parsed.path).split("/") if p]
    candidate = None
    if query.get("v"):
        candidate = query["v"][0]
    elif host.endswith("youtu.be") and parts:
        candidate = parts[0]
    elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "watch"}:
        candidate = parts[1]
    return candidate if candidate and VIDEO_ID_RE.match(candidate) else None


class LeaseIterator:
    """Response body wrapper that releases a reader lease when the server closes it.

    ``Response.call_on_close`` is skipped for direct-passthrough file responses,
    so the release is tied to the WSGI iterable's own ``close``.
    """

    def __init__(self, inner, release: Callable[[], None]):
        self._inner = inner
        self._iterator = iter(inner)
        self._release = release

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iterator)

    def close(self) -> None:
        try:
            close = getattr(self._inner, "close", None)
            if close is not None:
                close()
        finally:
            self._release()


def register_routes(app: Flask, services: Services, settings: Settings) -> None:
    cache, jobs, music = services.cache, services.jobs, services.music
    limiter = services.limiter or RateLimiter()
    app.extensions["jukes_limiter"] = limiter

    def caller() -> str:
        return client_address(
            request.remote_addr,
            {"cf": request.headers.get("CF-Connecting-IP"), "xff": request.headers.get("X-Forwarded-For")},
            settings.trusted_proxies,
        )

    def throttle(bucket: str, limit: int) -> None:
        wait = limiter.check(bucket, caller(), limit)
        if wait:
            raise ApiError(429, "rate_limited", "too many requests", True, str(wait))

    def bearer() -> str | None:
        header = request.headers.get("Authorization")
        if header is None:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ApiError(401, "invalid_token", "invalid installation token")
        return token.strip()

    def personalization_enabled() -> None:
        if services.identity is None or services.credentials is None:
            raise ApiError(503, "personalization_unavailable", "personalisation is not configured", True)

    def installation():
        """Authenticate the bearer token for /v1/me routes (token required)."""
        personalization_enabled()
        token = bearer()
        if token is None:
            raise ApiError(401, "invalid_token", "installation token required")
        try:
            return services.identity.authenticate(token)
        except InvalidInstallationToken:
            raise ApiError(401, "invalid_token", "invalid installation token") from None

    # -- helpers -------------------------------------------------------
    def base_url() -> str:
        if settings.public_base_url:
            return settings.public_base_url.rstrip("/")
        if is_trusted_peer(request.remote_addr, settings.trusted_proxies):
            # Behind our own proxy/tunnel: honour its scheme and host so a changing
            # *.trycloudflare.com address still yields correct https audio URLs.
            proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
            try:  # Cloudflare states the visitor's real scheme even if a hop rewrote X-Forwarded-Proto
                visitor = json.loads(request.headers.get("CF-Visitor") or "{}").get("scheme")
            except (ValueError, AttributeError):
                visitor = None
            if visitor in ("http", "https"):
                proto = visitor
            host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
            if proto in ("http", "https") and re.fullmatch(r"[A-Za-z0-9.-]+(:\d{1,5})?", host or ""):
                return f"{proto}://{host}"
        return request.url_root.rstrip("/")

    def context() -> UserContext:
        token = bearer()
        if token is None:
            return ANONYMOUS
        if services.identity is None:
            raise ApiError(401, "invalid_token", "invalid installation token")
        try:
            inst = services.identity.authenticate(token)
        except InvalidInstallationToken:
            raise ApiError(401, "invalid_token", "invalid installation token") from None
        status = services.credentials.status(inst.installation_id) if services.credentials else None
        return music.context(inst.installation_id, status)

    def selector_from_body() -> TrackSelector:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_request", "expected a JSON object")
        unknown = set(body) - {"video_id", "title", "artist", "duration_ms"}
        if unknown:
            raise ApiError(400, "invalid_request", "unsupported field")
        duration = body.get("duration_ms")
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0):
            raise ApiError(400, "invalid_request", "duration_ms must be a positive integer")
        if "video_id" in body:
            if len(body) != 1 or not isinstance(body["video_id"], str) or not VIDEO_ID_RE.match(body["video_id"]):
                raise ApiError(400, "invalid_request", "video_id must be a lone 11-character id")
            return TrackSelector(video_id=body["video_id"])
        title, artist = body.get("title"), body.get("artist")
        if not (isinstance(title, str) and title.strip() and isinstance(artist, str) and artist.strip()):
            raise ApiError(400, "invalid_request", "provide video_id, or title and artist")
        if len(title) > 300 or len(artist) > 300:
            raise ApiError(413, "input_too_large", "title or artist is too long")
        return TrackSelector(title=title.strip(), artist=artist.strip(), duration_ms=duration)

    def resolve(selector: TrackSelector, ctx: UserContext) -> Track:
        try:
            return music.resolve(selector, ctx)
        except MusicError as error:
            raise ApiError(MUSIC_STATUS.get(error.code, 502), error.code, str(error), error.retryable) from None

    metrics = cache.metrics

    def observed_submit(key, started_at):
        observation = metrics.begin_preparation(started_at)
        entry = cache.lookup(key)
        joined = jobs.has_active(key)
        category = 'warmed' if entry and entry.pool=='warmup' else ('cached' if entry else ('joined' if joined else 'cold'))
        metrics.record('cache_hit' if entry else 'cache_miss')
        if entry:
            metrics.record('warmup_hit' if entry.pool=='warmup' else 'main_hit')
            row = cache.store.get_audio(key)
            if row:
                metrics.consume_cached(key,row['completed_at'])
        elif joined:
            metrics.record('joined')
        try:
            job = submit(key, True)
        except ApiError:
            metrics.record('admission_rejected')
            with cache.store.transaction() as c:
                c.execute('DELETE FROM jukes_metrics_pending WHERE id=?',(observation,))
            raise
        metrics.attach_preparation(observation,job.job_id,category)
        return job

    def consume_ready(key):
        row = cache.store.get_audio(key)
        if row:
            metrics.consume_cached(key,row['completed_at'])

    def submit(key: AudioKey, requested: bool) -> Job:
        try:
            return jobs.submit(key, requested=requested)
        except JobQueueFull:
            raise ApiError(429, "queue_full", "download queue is full", True) from None
        except CacheCapacityError as error:
            status, retryable = JOB_ERRORS.get(error.code, (503, True))
            raise ApiError(status, error.code, "cache cannot admit this track", retryable) from None

    def job_error(job: Job) -> ApiError:
        status, retryable = JOB_ERRORS.get(job.error_code or "", (502, True))
        return ApiError(status, job.error_code or "extraction_failed", "audio is unavailable", retryable)

    def pool_of(job: Job) -> str:
        entry = cache.lookup(job.key) if job.status == "ready" else None
        if entry is not None:
            return entry.pool
        return "requested" if job.requested else "warmup"

    def job_view(job: Job) -> dict[str, Any]:
        view: dict[str, Any] = {"job_id": job.job_id, "video_id": job.key.video_id,
                                "status": job.status, "pool": pool_of(job)}
        if job.status == "ready":
            view["audio_url"] = f"{base_url()}/v1/audio/{job.key.video_id}"
        if job.status in ("failed", "evicted"):
            _, retryable = JOB_ERRORS.get(job.error_code or "", (502, True))
            view["error"] = {"code": job.error_code, "retryable": retryable}
        return view

    def serve(key: AudioKey, *, touch: bool, headers: dict[str, str] | None = None) -> Response | None:
        """Serve a completed file; the reader lease lives until the response closes."""
        entry, release = cache.open_lease(key, touch=touch)
        if entry is None:
            release()
            return None
        try:
            size = entry.path.stat().st_size
            response = send_file(entry.path, mimetype=sniff_mime(entry.path), conditional=True,
                                 etag=f"{key.video_id}-{size}", max_age=None)
        except BaseException:
            release()
            raise
        response.response = LeaseIterator(response.response, release)
        response.headers["Cache-Control"] = "private, no-cache"
        response.headers["X-Video-Id"] = key.video_id
        for name, value in (headers or {}).items():
            response.headers[name] = value
        return response

    # -- errors --------------------------------------------------------
    @app.errorhandler(ApiError)
    def _api_error(error: ApiError):
        return error_response(error.status, error.code, error.message, error.retryable, error.retry_after)

    @app.errorhandler(413)
    def _too_large(_error):
        return error_response(413, "input_too_large", "request body is too large")

    @app.errorhandler(416)
    def _bad_range(error):
        return error.get_response()

    @app.errorhandler(404)
    def _not_found(_error):
        return error_response(404, "not_found", "not found")

    @app.errorhandler(405)
    def _bad_method(_error):
        return error_response(405, "method_not_allowed", "method not allowed")

    @app.after_request
    def _no_store(response: Response):
        if request.path.startswith("/v1/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    # -- versioned API -------------------------------------------------
    def start(requested: bool):
        throttle("http", settings.http_per_minute)
        ctx = context()
        selector = selector_from_body()
        started_at = time.time()
        try:
            track = resolve(selector, ctx)
        except ApiError as error:
            if requested:
                observation = metrics.begin_preparation(started_at)
                metrics.finish_preparation(observation,success=False,duration_seconds=time.time()-started_at,retryable=error.retryable)
            raise
        key = AudioKey(track.video_id, AUDIO_POLICY)
        if not jobs.has_active(key) and cache.lookup(key) is None:  # only genuinely new work
            if requested:
                throttle("requested", settings.requested_per_minute)
            else:
                throttle("warmup", settings.warmup_per_minute)
        job = observed_submit(key, started_at) if requested else submit(key, False)
        body = {**track.as_dict(), **job_view(job), "personalization_status": ctx.personalization_status}
        response = jsonify(body)
        response.status_code = 200 if job.status == "ready" else 202
        return response

    @app.post("/v1/warmup")
    def warmup():
        return start(False)

    @app.post("/v1/audio/prepare")
    def prepare():
        return start(True)

    @app.get("/v1/jobs/<job_id>")
    def job_status(job_id: str):
        throttle("poll", settings.poll_per_minute)
        job = jobs.get(job_id) if re.fullmatch(r"[0-9a-f]{32}|[\w-]{1,64}", job_id) else None
        if job is None:
            raise ApiError(404, "job_not_found", "unknown job")
        # Long poll: ?wait=<seconds> answers as soon as the job is ready or failed, so a client
        # learns of a 3 s download at 3 s instead of at its next backoff tick.
        wait = request.args.get("wait", type=float)
        if wait and wait > 0 and job.status in ("queued", "downloading"):
            job = jobs.wait(job_id, min(wait, settings.max_job_wait_seconds)) or job
        return jsonify(job_view(job))

    @app.route("/v1/audio/<video_id>", methods=["GET", "HEAD"])
    def audio(video_id: str):
        if not VIDEO_ID_RE.match(video_id):
            raise ApiError(400, "invalid_request", "invalid video id")
        key = AudioKey(video_id, AUDIO_POLICY)
        if request.method == "HEAD":
            response = serve(key, touch=False)
            if response is None:
                raise ApiError(404, "not_ready", "audio is not cached")
            return response
        response = serve(key, touch=True)
        if response is not None:
            consume_ready(key)
            return response
        job = observed_submit(key, time.time())
        if job.status == "ready":  # published between the lookup and submit
            if (response := serve(key, touch=True)) is not None:
                return response
        if job.status in ("failed", "evicted"):
            raise job_error(job)
        raise ApiError(503, "pending", "audio is being prepared", True)

    # -- installations and optional personalisation --------------------
    @app.post("/v1/installations")
    def issue_installation():
        personalization_enabled()
        throttle("issue", settings.issue_per_minute)
        response = jsonify({"token": services.identity.issue()})
        response.status_code = 201
        return response

    def connection_view(status) -> dict[str, Any]:
        view = {"connected": bool(status.connected)}
        if status.connected:
            view["credential_generation"] = status.credential_generation
            view["connected_at"] = status.connected_at
        return view

    @app.put("/v1/me/youtube")
    def connect_youtube():
        throttle("http", settings.http_per_minute)
        inst = installation()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_request", "expected a JSON object")
        try:
            status = services.credentials.replace(inst.installation_id, body)
        except InvalidCredentialBundle as error:
            raise ApiError(400, "invalid_request", str(error)) from None
        except MissingCredentialKey:
            raise ApiError(503, "personalization_unavailable", "credential storage is not configured", True) from None
        except CredentialDecryptionError:
            raise ApiError(503, "credential_store_error", "stored credentials are unreadable", True) from None
        except CredentialValidationError as error:
            if error.retryable:
                raise ApiError(502, "upstream_error", "YouTube account validation failed", True) from None
            raise ApiError(422, "session_rejected", "YouTube session was not accepted") from None
        except InstallationNotFound:
            raise ApiError(401, "invalid_token", "invalid installation token") from None
        return jsonify(connection_view(status))

    @app.get("/v1/me/youtube")
    def youtube_status():
        throttle("poll", settings.poll_per_minute)
        inst = installation()
        return jsonify(connection_view(services.credentials.status(inst.installation_id)))

    @app.delete("/v1/me/youtube")
    def disconnect_youtube():
        inst = installation()
        services.credentials.delete(inst.installation_id)
        return Response(status=204)

    @app.delete("/v1/me")
    def delete_me():
        inst = installation()
        services.credentials.delete(inst.installation_id)
        services.identity.revoke(inst.installation_id)
        return Response(status=204)

    @app.post("/v1/recommendations")
    def recommendations():
        throttle("http", settings.http_per_minute)
        ctx = context()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {"video_id", "limit"}:
            raise ApiError(400, "invalid_request", "expected video_id and optional limit")
        video_id, limit = body.get("video_id"), body.get("limit", 25)
        if not isinstance(video_id, str) or not VIDEO_ID_RE.match(video_id):
            raise ApiError(400, "invalid_request", "video_id must be an 11-character id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ApiError(400, "invalid_request", "limit must be an integer from 1 to 100")
        try:
            result = music.radio(video_id, limit, ctx)
        except MusicError as error:
            raise ApiError(MUSIC_STATUS.get(error.code, 502), error.code, str(error), error.retryable) from None
        response = jsonify({"video_id": video_id, "tracks": [t.as_dict() for t in result.tracks],
                            "personalization_status": result.personalization_status})
        return response

    # -- legacy /audio/ ------------------------------------------------
    @app.route("/audio/", methods=["GET", "HEAD"])
    def legacy_audio():
        started_at = time.time()
        args = request.args
        meta: Track | None = None
        video_id = args.get("video_id")
        if not video_id and args.get("url"):
            video_id = video_id_from_url(args["url"])
            if not video_id:
                raise ApiError(400, "invalid_request", '"url" is not a YouTube video link')
        if not video_id and args.get("q"):
            try:
                seconds = max(0.0, float(args.get("duration") or 0))
            except ValueError:
                raise ApiError(400, "invalid_request", 'invalid "duration"') from None
            if request.method == "GET" or args.get("info"):
                meta = resolve(TrackSelector(query=args["q"], duration_ms=int(seconds * 1000) or None), ANONYMOUS)
                video_id = meta.video_id
            else:  # HEAD never needs a lookup that could hit upstream
                raise ApiError(400, "invalid_request", "HEAD requires video_id or url")
        if not video_id or not VIDEO_ID_RE.match(video_id):
            raise ApiError(400, "invalid_request", 'provide "video_id", "url" or "q"')
        key = AudioKey(video_id, AUDIO_POLICY)
        headers = {"X-Cache": "HIT" if cache.lookup(key) else "MISS"}
        if meta:
            headers["X-Title"] = quote(meta.title, safe="")
            headers["X-Artist"] = quote(" and ".join(meta.artists), safe="")
            if meta.duration_ms:
                headers["X-Duration-Ms"] = str(meta.duration_ms)

        if (args.get("info") or "").strip().lower() in {"1", "true", "yes", "on"}:
            body = meta.as_dict() if meta else {"video_id": video_id}
            body["cached"] = headers["X-Cache"] == "HIT"
            body["audio_url"] = f"{base_url()}/audio/?video_id={video_id}"
            response = jsonify(body)
            response.headers["Cache-Control"] = "no-store"
            return response

        if request.method == "HEAD":
            response = serve(key, touch=False, headers=headers)
            if response is None:
                raise ApiError(404, "not_ready", "audio is not cached")
            return response

        response = serve(key, touch=True, headers=headers)
        if response is not None:
            consume_ready(key)
            return response
        job = observed_submit(key, started_at)
        job = jobs.wait(job.job_id, settings.legacy_wait_seconds) or job
        if job.status == "ready":
            headers["X-Cache"] = "MISS"
            if (response := serve(key, touch=True, headers=headers)) is not None:
                return response
        if job.status in ("failed", "evicted"):
            raise job_error(job)
        raise ApiError(503, "pending", "audio is being prepared", True)
