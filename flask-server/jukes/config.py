from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CacheConfig:
    database_path: Path = Path("/data/jukes.sqlite3")
    audio_dir: Path = Path("/tmp/ytm_audio_cache")
    requested_limit_bytes: int = 10_000_000_000
    warmup_limit_bytes: int = 1_000_000_000
    warmup_ttl_seconds: float = 7_200
    min_free_disk_bytes: int = 2_000_000_000
    unknown_size_reservation_bytes: int = 16_000_000
    reservation_increment_bytes: int = 16_000_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_path", Path(self.database_path))
        object.__setattr__(self, "audio_dir", Path(self.audio_dir))
        for name in (
            "requested_limit_bytes",
            "warmup_limit_bytes",
            "unknown_size_reservation_bytes",
            "reservation_increment_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.warmup_ttl_seconds <= 0:
            raise ValueError("warmup_ttl_seconds must be positive")
        if self.min_free_disk_bytes < 0:
            raise ValueError("min_free_disk_bytes cannot be negative")
