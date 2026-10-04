"""HTTP surface: versioned warmup/prepare/jobs/audio plus the legacy /audio/ endpoint."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from flask import Flask, Response, jsonify, request, send_file

from .jobs import Job, JobQueueFull
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
    context_provider: Callable[[str], UserContext] | None = None


@dataclass(frozen=True)
class Settings:
    legacy_wait_seconds: float = 25.0
    public_base_url: str = ""


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

    # -- helpers -------------------------------------------------------
    def base_url() -> str:
        return (settings.public_base_url or request.url_root).rstrip("/")

    def context() -> UserContext:
        header = request.headers.get("Authorization")
        if header is None:
            return ANONYMOUS
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip() or services.context_provider is None:
            raise ApiError(401, "invalid_token", "invalid installation token")
        try:
            return services.context_provider(token.strip())
        except InvalidToken:
            raise ApiError(401, "invalid_token", "invalid installation token") from None

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
        if job.status == "failed":
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
        ctx = context()
        selector = selector_from_body()
        track = resolve(selector, ctx)
        job = submit(AudioKey(track.video_id, AUDIO_POLICY), requested)
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
        job = jobs.get(job_id) if re.fullmatch(r"[0-9a-f]{32}|[\w-]{1,64}", job_id) else None
        if job is None:
            raise ApiError(404, "job_not_found", "unknown job")
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
            return response
        job = submit(key, True)
        if job.status == "ready":  # published between the lookup and submit
            if (response := serve(key, touch=True)) is not None:
                return response
        if job.status == "failed":
            raise job_error(job)
        raise ApiError(503, "pending", "audio is being prepared", True)

    # -- legacy /audio/ ------------------------------------------------
    @app.route("/audio/", methods=["GET", "HEAD"])
    def legacy_audio():
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
            return response
        job = submit(key, True)
        job = jobs.wait(job.job_id, settings.legacy_wait_seconds) or job
        if job.status == "ready":
            headers["X-Cache"] = "MISS"
            if (response := serve(key, touch=True, headers=headers)) is not None:
                return response
        if job.status == "failed":
            raise job_error(job)
        raise ApiError(503, "pending", "audio is being prepared", True)
