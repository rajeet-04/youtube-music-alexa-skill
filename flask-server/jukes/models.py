from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudioKey:
    video_id: str
    policy: str


@dataclass(frozen=True)
class CacheEntry:
    key: AudioKey
    path: Path
    pool: str
    size_bytes: int
    expires_at: float | None
    last_used: float


@dataclass(frozen=True)
class PruneResult:
    removed_entries: int = 0
    removed_bytes: int = 0


class CacheCapacityError(RuntimeError):
    def __init__(self, message: str, *, code: str = "cache_capacity") -> None:
        super().__init__(message)
        self.code = code
