"""Track metadata resolution against YouTube Music (anonymous by default).

``Music.resolve`` turns either a video id or a title/artist (optional duration)
into a verified ``Track``. Candidate matching is strict: wrong artists,
unrequested renditions (live, remix, karaoke...) and out-of-tolerance durations
are rejected instead of silently substituted.
"""

from __future__ import annotations

import difflib
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

VIDEO_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{11}\Z")

_MODIFIER_TOKENS = {
    "cover", "covers", "karaoke", "remix", "remixes", "instrumental", "acoustic", "unplugged",
    "live", "lofi", "slowed", "reverb", "mashup", "megamix", "medley", "remake", "parody",
    "8d", "nightcore", "tribute", "reprise", "sped",
}
_PROMO_TOKENS = {"teaser", "trailer", "promo", "preview", "snippet", "announcement"}


class MusicError(Exception):
    code = "music_error"
    retryable = False


class NoMatch(MusicError):
    code = "no_match"


class RejectedMatch(MusicError):
    code = "match_rejected"


class UpstreamError(MusicError):
    code = "upstream_error"
    retryable = True


@dataclass(frozen=True)
class TrackSelector:
    video_id: str | None = None
    title: str | None = None
    artist: str | None = None
    duration_ms: int | None = None
    query: str | None = None  # legacy free-text lookup (/audio/?q=)

    @property
    def cache_key(self) -> tuple:
        return (self.video_id, self.title, self.artist, self.duration_ms, self.query)


@dataclass(frozen=True)
class UserContext:
    installation_id: str | None = None
    generation: int = 0
    locale: str = "en-IN"
    region: str = "IN"
    personalization_status: str = "anonymous"

    @property
    def cache_key(self) -> tuple:
        return (self.installation_id, self.generation, self.locale, self.region)


ANONYMOUS = UserContext()


@dataclass(frozen=True)
class Track:
    video_id: str
    title: str
    artists: tuple[str, ...] = ()
    album: str | None = None
    duration_ms: int | None = None
    artwork_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "artists": list(self.artists),
            "artist": " and ".join(self.artists),
            "album": self.album,
            "duration_ms": self.duration_ms,
            "artwork_url": self.artwork_url,
        }


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower().replace("&", " and "))


def _score(query: str, text: str) -> float:
    """0..1 share of the query's tokens present (fuzzily) in ``text``."""
    result = set(_tokens(text))
    wanted = [t for t in _tokens(query) if t != "by"]
    if not result or not wanted:
        return 0.0
    hit = 0.0
    for token in wanted:
        if token in result:
            hit += 1
        else:
            close = difflib.get_close_matches(token, result, n=1, cutoff=0.8)
            if close:
                hit += difflib.SequenceMatcher(None, token, close[0]).ratio()
    return hit / len(wanted)


def _clean_artist(name: str | None) -> str:
    name = (name or "").strip()
    return re.sub(r"\s*-\s*topic$", "", name, flags=re.I).strip()


def _artists(item: dict[str, Any]) -> tuple[str, ...]:
    names = [_clean_artist(a.get("name")) for a in item.get("artists") or [] if isinstance(a, dict)]
    return tuple(n for n in names if n)


def _artwork(thumbnails: Any) -> str | None:
    if isinstance(thumbnails, list) and thumbnails and isinstance(thumbnails[-1], dict):
        return thumbnails[-1].get("url") or None
    return None


def _duration_ms(item: dict[str, Any]) -> int | None:
    for key in ("duration_seconds", "lengthSeconds"):
        try:
            seconds = int(float(item.get(key)))
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds * 1000
    text = item.get("duration") or item.get("length")
    if isinstance(text, str):
        try:
            seconds = 0
            for part in text.strip().split(":"):
                seconds = seconds * 60 + int(part)
        except ValueError:
            return None
        return seconds * 1000 if seconds > 0 else None
    return None


def duration_tolerance_ms(target_ms: int) -> float:
    return max(8_000.0, target_ms * 0.07)


@dataclass(frozen=True)
class RadioResult:
    tracks: tuple[Track, ...]
    personalization_status: str


def _is_auth_error(error: BaseException) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return any(token in text for token in (
        "401", "403", "unauthor", "forbidden", "authenticat", "logged out", "login", "sign in", "cookie"))


class Music:
    """``client_factory(context)`` returns an object with ``search`` and ``get_song``."""

    def __init__(
        self,
        client_factory: Callable[[UserContext], Any],
        *,
        clock: Callable[[], float] = time.time,
        cache_size: int = 5_000,
        cache_ttl: float = 600.0,
    ) -> None:
        self._client_factory = client_factory
        self._clock = clock
        self._cache_size = cache_size
        self._cache_ttl = cache_ttl
        self._cache: OrderedDict[tuple, tuple[float, Track]] = OrderedDict()
        self._lock = threading.Lock()

    # -- cache ---------------------------------------------------------
    def _cached(self, key: tuple) -> Track | None:
        with self._lock:
            hit = self._cache.get(key)
            if hit is None:
                return None
            if self._clock() - hit[0] > self._cache_ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return hit[1]

    def _store(self, key: tuple, track: Track) -> None:
        with self._lock:
            self._cache[key] = (self._clock(), track)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    # -- personalisation -----------------------------------------------
    @staticmethod
    def context(installation_id: str | None, status: Any = None) -> UserContext:
        """UserContext for an installation; ``status`` is a ``ConnectionStatus``."""
        if installation_id is None:
            return ANONYMOUS
        connected = bool(status is not None and status.connected)
        return UserContext(
            installation_id=installation_id,
            generation=int(getattr(status, "credential_generation", 0) or 0),
            personalization_status="connected" if connected else "anonymous",
        )

    def radio(self, video_id: str, limit: int, context: UserContext = ANONYMOUS) -> RadioResult:
        """Ordered radio for ``video_id``; expired sessions fall back to anonymous.

        Filtering, dedup and queue reseeding stay in the app's own engine.
        """
        status = context.personalization_status
        attempts = [context] if context.personalization_status == "connected" else []
        attempts.append(ANONYMOUS)
        last: BaseException | None = None
        for attempt in attempts:
            try:
                client = self._client_factory(attempt)
                data = client.get_watch_playlist(videoId=video_id, radio=True, limit=limit)
            except Exception as error:  # noqa: BLE001
                last = error
                if attempt is not ANONYMOUS:
                    status = "reconnect_required" if _is_auth_error(error) else "personalization_unavailable"
                continue
            tracks = []
            for item in (data or {}).get("tracks") or []:
                if isinstance(item, dict) and VIDEO_ID_RE.match(str(item.get("videoId") or "")):
                    tracks.append(self._track(item))
            if attempt is ANONYMOUS and status == "connected":
                status = "anonymous"
            return RadioResult(tuple(tracks[:limit]), status)
        raise UpstreamError("radio lookup failed") from last

    # -- resolution ----------------------------------------------------
    def resolve(self, selector: TrackSelector, context: UserContext = ANONYMOUS) -> Track:
        key = (selector.cache_key, context.cache_key)
        if (track := self._cached(key)) is not None:
            return track
        client = self._client_factory(context)
        if selector.video_id:
            track = self._by_video_id(client, selector.video_id)
        elif selector.query:
            track = self._by_query(client, selector)
        elif selector.title:
            track = self._by_title(client, selector)
        else:
            raise NoMatch("empty selector")
        self._store(key, track)
        return track

    def _by_video_id(self, client: Any, video_id: str) -> Track:
        try:
            info = client.get_song(video_id)
        except Exception as error:  # noqa: BLE001
            raise UpstreamError("metadata lookup failed") from error
        details = (info or {}).get("videoDetails") if isinstance(info, dict) else None
        status = ((info or {}).get("playabilityStatus") or {}).get("status") if isinstance(info, dict) else None
        if not details or details.get("videoId") not in (None, video_id) or status not in (None, "OK"):
            raise NoMatch("video not found")
        author = _clean_artist(details.get("author"))
        return Track(
            video_id=video_id,
            title=str(details.get("title") or ""),
            artists=(author,) if author else (),
            album=None,
            duration_ms=_duration_ms(details),
            artwork_url=_artwork((details.get("thumbnail") or {}).get("thumbnails")),
        )

    def _search(self, client: Any, query: str) -> list[dict[str, Any]]:
        try:
            results = client.search(query, filter="songs", limit=8, ignore_spelling=True)
        except Exception as error:  # noqa: BLE001
            raise UpstreamError("search failed") from error
        return [r for r in results or [] if isinstance(r, dict) and VIDEO_ID_RE.match(str(r.get("videoId") or ""))]

    @staticmethod
    def _track(item: dict[str, Any]) -> Track:
        album = item.get("album")
        return Track(
            video_id=item["videoId"],
            title=str(item.get("title") or ""),
            artists=_artists(item),
            album=(album.get("name") or None) if isinstance(album, dict) else None,
            duration_ms=_duration_ms(item),
            artwork_url=_artwork(item.get("thumbnails") or item.get("thumbnail")),
        )

    def _by_query(self, client: Any, selector: TrackSelector) -> Track:
        """Legacy lookup: best song, nudged by an expected duration (never rejected)."""
        items = self._search(client, selector.query or "")[:5]
        if not items:
            raise NoMatch("no matching song")
        tracks = [self._track(i) for i in items]
        best = tracks[0]
        if selector.duration_ms:
            tolerance = duration_tolerance_ms(selector.duration_ms)
            timed = [t for t in tracks if t.duration_ms]
            within = [t for t in timed if abs(t.duration_ms - selector.duration_ms) <= tolerance]
            if within:
                best = within[0]
            elif timed:
                best = min(timed, key=lambda t: abs(t.duration_ms - selector.duration_ms))
        return best

    def _by_title(self, client: Any, selector: TrackSelector) -> Track:
        title, artist = selector.title or "", selector.artist or ""
        items = self._search(client, f"{title} {artist}".strip())
        if not items:
            raise NoMatch("no matching song")
        requested = set(_tokens(title)) | set(_tokens(artist))
        ranked: list[tuple[float, float, int, Track]] = []
        considered = 0
        for rank, item in enumerate(items):
            track = self._track(item)
            title_score = _score(title, track.title)
            artist_score = _score(artist, " ".join(track.artists)) if artist else 1.0
            if title_score < 0.8 or artist_score < 0.6:
                continue  # a different song entirely: no match, not a rejection
            considered += 1
            tokens = set(_tokens(track.title)) | set(_tokens(" ".join(track.artists + ((track.album or ""),))))
            if (_MODIFIER_TOKENS & tokens) - requested or (_PROMO_TOKENS & tokens) - requested:
                continue
            gap = 0.0
            if selector.duration_ms and track.duration_ms:
                gap = abs(track.duration_ms - selector.duration_ms)
                if gap > duration_tolerance_ms(selector.duration_ms):
                    continue
            ranked.append((-(title_score + artist_score), gap, rank, track))
        if not ranked:
            if considered:
                raise RejectedMatch("candidates differ in version or duration")
            raise NoMatch("no matching song")
        ranked.sort(key=lambda r: r[:3])
        return ranked[0][3]
