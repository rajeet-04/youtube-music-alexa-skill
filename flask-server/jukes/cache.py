"""Two-pool audio cache with persistent reservations and reader leases."""

import hashlib
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .config import CacheConfig
from .models import AudioKey, CacheCapacityError, CacheEntry, PruneResult
from .store import Store


_COORDINATOR_LOCK = threading.RLock()


def _process_start(pid: int) -> str | None:
    """Return Linux's process start tick count to protect against PID reuse."""

    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields_after_comm = stat_line[stat_line.rfind(")") + 2 :].split()
        return fields_after_comm[19]
    except (OSError, IndexError):
        return None


class CacheReservation:
    """A durable byte reservation whose writes reserve capacity before appending."""

    def __init__(self, cache: "Cache", key: AudioKey, path: Path, cached_entry: CacheEntry | None = None):
        self.cache = cache
        self.key = key
        self.path = path
        self.cached_entry = cached_entry

    @property
    def reserved_bytes(self) -> int:
        if self.cached_entry is not None:
            return self.cached_entry.size_bytes
        row = self.cache.store.get_reservation(self.key)
        return int(row["reserved_bytes"]) if row else 0

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if self.cached_entry is not None:
            raise RuntimeError("audio is already complete")
        payload = bytes(data)
        if not payload:
            return 0
        return self.cache._write_reserved(self.key, self.path, payload)


class Cache:
    def __init__(
        self,
        config: CacheConfig,
        *,
        clock: Callable[[], float] = time.time,
        store: Store | None = None,
        media_validator: Callable[[Path], bool] | None = None,
    ) -> None:
        self.config = config
        self.clock = clock
        self.audio_dir = Path(config.audio_dir)
        self.files_dir = self.audio_dir / "files"
        self.staging_dir = self.audio_dir / "staging"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.store = store or Store(config.database_path)
        self.media_validator = media_validator or self._default_media_validator
        self._pid = os.getpid()
        self._process_start_token = _process_start(self._pid) or "unknown"
        self.reconcile()

    def disk_free_bytes(self) -> int:
        return shutil.disk_usage(self.audio_dir).free

    @staticmethod
    def _default_media_validator(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _digest(key: AudioKey) -> str:
        value = f"{key.video_id}\0{key.policy}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _pool(requested: bool) -> str:
        return "requested" if requested else "warmup"

    def _pool_limit(self, pool: str) -> int:
        if pool == "requested":
            return self.config.requested_limit_bytes
        if pool == "warmup":
            return self.config.warmup_limit_bytes
        raise ValueError(f"unknown cache pool: {pool}")

    @staticmethod
    def _entry(row: dict[str, object] | None) -> CacheEntry | None:
        if row is None:
            return None
        return CacheEntry(
            key=AudioKey(str(row["video_id"]), str(row["policy"])),
            path=Path(str(row["path"])),
            pool=str(row["pool"]),
            size_bytes=int(row["size_bytes"]),
            expires_at=float(row["expires_at"]) if row["expires_at"] is not None else None,
            last_used=float(row["last_used"]),
        )

    def _is_expired(self, row: dict[str, object], now: float) -> bool:
        return row["pool"] == "warmup" and row["expires_at"] is not None and float(row["expires_at"]) <= now

    def _live_lease_rows(self, key: AudioKey | None = None) -> list[dict[str, object]]:
        rows = self.store.all_leases()
        if key is not None:
            rows = [row for row in rows if row["video_id"] == key.video_id and row["policy"] == key.policy]
        return rows

    def _is_leased(self, key: AudioKey) -> bool:
        return bool(self._live_lease_rows(key))

    def _remove_audio(self, row: dict[str, object]) -> bool:
        key = AudioKey(str(row["video_id"]), str(row["policy"]))
        if self._is_leased(key):
            return False
        path = Path(str(row["path"]))
        try:
            path.resolve().relative_to(self.audio_dir.resolve())
        except (OSError, ValueError):
            # Never unlink an arbitrary path merely because a database row names it.
            with self.store.transaction() as connection:
                connection.execute(
                    "DELETE FROM jukes_audio WHERE video_id = ? AND policy = ?",
                    (key.video_id, key.policy),
                )
            return True
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        with self.store.transaction() as connection:
            connection.execute(
                "DELETE FROM jukes_audio WHERE video_id = ? AND policy = ?",
                (key.video_id, key.policy),
            )
        return True

    @staticmethod
    def _allocated_bytes(path: Path) -> int:
        try:
            info = path.stat()
            blocks = getattr(info, "st_blocks", 0)
            return int(blocks * 512) if blocks else int(info.st_size)
        except OSError:
            return 0

    def usage(self) -> dict[str, int]:
        """Bytes in each pool including in-flight reservations (read-only)."""
        with _COORDINATOR_LOCK:
            return {pool: self._pool_usage(pool) for pool in ("requested", "warmup")}

    def _pool_usage(self, pool: str, exclude_key: AudioKey | None = None) -> int:
        key_values = (exclude_key.video_id, exclude_key.policy) if exclude_key else None
        total = 0
        for row in self.store.all_audio():
            if row["pool"] == pool and (not key_values or (row["video_id"], row["policy"]) != key_values):
                total += int(row["size_bytes"])
        for row in self.store.all_reservations():
            if row["pool"] != pool or (key_values and (row["video_id"], row["policy"]) == key_values):
                continue
            amount = int(row["size_bytes"] or 0) if row["state"] == "publishing" else int(row["reserved_bytes"])
            total += amount
        return total

    def _pending_disk_bytes(self, exclude_key: AudioKey | None = None) -> int:
        total = 0
        for row in self.store.all_reservations():
            if row["state"] != "reserved":
                continue
            if exclude_key and (row["video_id"], row["policy"]) == (exclude_key.video_id, exclude_key.policy):
                continue
            path = Path(str(row["partial_path"]))
            try:
                written = path.stat().st_size
            except OSError:
                written = 0
            total += max(0, int(row["reserved_bytes"]) - written)
        return total

    def _eligible_audio(self, excluded: set[tuple[str, str]]) -> list[dict[str, object]]:
        rows = []
        for row in self.store.all_audio():
            key_tuple = (str(row["video_id"]), str(row["policy"]))
            if key_tuple in excluded or self._is_leased(AudioKey(*key_tuple)):
                continue
            rows.append(row)
        return rows

    def _ensure_room(
        self,
        pool: str,
        pool_bytes: int,
        *,
        disk_commitment_bytes: int = 0,
        exclude_key: AudioKey | None = None,
    ) -> None:
        limit = self._pool_limit(pool)
        if pool_bytes > limit:
            code = "track_too_large" if pool == "requested" else "warmup_too_large"
            raise CacheCapacityError(f"audio exceeds the {pool} pool limit", code=code)

        now = self.clock()
        excluded = {(exclude_key.video_id, exclude_key.policy)} if exclude_key else set()
        planned: list[dict[str, object]] = []
        planned_keys: set[tuple[str, str]] = set()

        # Expired speculative audio is the first space reclaimed on every admission.
        expired = [
            row for row in self._eligible_audio(excluded)
            if row["pool"] == "warmup" and self._is_expired(row, now)
        ]
        expired.sort(key=lambda row: (float(row["expires_at"] or 0), float(row["last_used"])))
        for row in expired:
            planned.append(row)
            planned_keys.add((str(row["video_id"]), str(row["policy"])))

        def simulated_usage() -> int:
            return self._pool_usage(pool, exclude_key) - sum(
                int(row["size_bytes"]) for row in planned if row["pool"] == pool
            )

        if simulated_usage() + pool_bytes > limit:
            target_rows = [
                row for row in self._eligible_audio(excluded | planned_keys)
                if row["pool"] == pool
            ]
            target_rows.sort(key=lambda row: (float(row["last_used"]), float(row["completed_at"])))
            for row in target_rows:
                planned.append(row)
                planned_keys.add((str(row["video_id"]), str(row["policy"])))
                if simulated_usage() + pool_bytes <= limit:
                    break

        pending = self._pending_disk_bytes(exclude_key)

        def projected_free() -> int:
            reclaimed = sum(self._allocated_bytes(Path(str(row["path"]))) for row in planned)
            return self.disk_free_bytes() + reclaimed - pending - disk_commitment_bytes

        if projected_free() < self.config.min_free_disk_bytes:
            global_rows = self._eligible_audio(excluded | planned_keys)
            warmups = [row for row in global_rows if row["pool"] == "warmup"]
            requested = [row for row in global_rows if row["pool"] == "requested"]
            warmups.sort(key=lambda row: (float(row["last_used"]), float(row["completed_at"])))
            requested.sort(key=lambda row: (float(row["last_used"]), float(row["completed_at"])))
            for row in warmups + requested:
                planned.append(row)
                planned_keys.add((str(row["video_id"]), str(row["policy"])))
                if projected_free() >= self.config.min_free_disk_bytes:
                    break

        if simulated_usage() + pool_bytes > limit or projected_free() < self.config.min_free_disk_bytes:
            raise CacheCapacityError("cache capacity is pinned or reserved", code="cache_capacity")

        for row in planned:
            self._remove_audio(row)

        # Unlink can fail after planning (permissions, filesystem errors). Re-read
        # persistent accounting and available disk before letting the caller admit
        # bytes; a planned deletion only counts after its row was actually removed.
        remaining_usage = self._pool_usage(pool, exclude_key) + pool_bytes
        remaining_disk = self.disk_free_bytes() - self._pending_disk_bytes(exclude_key) - disk_commitment_bytes
        if remaining_usage > limit or remaining_disk < self.config.min_free_disk_bytes:
            raise CacheCapacityError("cache capacity is pinned or could not be reclaimed", code="cache_capacity")

    def _entry_if_valid(self, key: AudioKey, now: float | None = None) -> CacheEntry | None:
        row = self.store.get_audio(key)
        if row is None:
            return None
        if self._is_expired(row, self.clock() if now is None else now):
            return None
        path = Path(str(row["path"]))
        if not self.media_validator(path):
            if not self._is_leased(key):
                self._remove_audio(row)
            return None
        if path.stat().st_size != int(row["size_bytes"]):
            if not self._is_leased(key):
                self._remove_audio(row)
            return None
        return self._entry(row)

    def lookup(self, key: AudioKey) -> CacheEntry | None:
        with _COORDINATOR_LOCK:
            return self._entry_if_valid(key)

    def mark_used(self, key: AudioKey) -> bool:
        with _COORDINATOR_LOCK:
            entry = self._entry_if_valid(key)
            if entry is None:
                return False
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE jukes_audio SET last_used = ? WHERE video_id = ? AND policy = ?",
                    (self.clock(), key.video_id, key.policy),
                )
            return True

    def complete(self, key: AudioKey, path: Path, requested: bool) -> CacheEntry:
        source = Path(path).resolve()
        with _COORDINATOR_LOCK:
            if not source.is_file() or not self.media_validator(source):
                raise ValueError("completed audio must be a non-empty validated file")
            source_info = source.stat()
            target_device = self.files_dir.stat().st_dev
            if source_info.st_dev != target_device:
                raise ValueError("audio must be on the cache filesystem for atomic publication")

            now = self.clock()
            existing_row = self.store.get_audio(key)
            if existing_row is not None and not self._is_expired(existing_row, now):
                existing = self._entry_if_valid(key, now)
                if existing is not None:
                    if requested:
                        if existing.pool == "warmup":
                            promoted = self._promote_entry(existing_row, now)
                            self._discard_duplicate_source(source, promoted.path)
                            return promoted
                        self.mark_used(key)
                        self._discard_duplicate_source(source, existing.path)
                        return self._entry_if_valid(key, now) or existing
                    self._discard_duplicate_source(source, existing.path)
                    return existing
                if self._is_leased(key):
                    raise CacheCapacityError("cached audio is protected by an active reader")
            elif existing_row is not None:
                if self._is_leased(key):
                    if requested:
                        return self._promote_entry(existing_row, now)
                    raise CacheCapacityError("expired audio is protected by an active reader")
                self._remove_audio(existing_row)

            reservation = self.store.get_reservation(key)
            pool = "requested" if requested or (reservation and reservation["pool"] == "requested") else "warmup"
            size = int(source_info.st_size)
            expires_at = None if pool == "requested" else now + self.config.warmup_ttl_seconds
            self._ensure_room(pool, size, exclude_key=key)

            suffix = source.suffix.lower()
            if not suffix or len(suffix) > 10 or not suffix[1:].isalnum() or suffix == ".partial":
                suffix = ".audio"
            destination = self.files_dir / f"{self._digest(key)}{suffix}"
            self.store.begin_publication(
                key,
                pool=pool,
                source_path=source,
                destination_path=destination,
                size_bytes=size,
                completed_at=now,
                expires_at=expires_at,
            )
            if source != destination:
                os.replace(source, destination)
            self._fsync_directory(self.files_dir)
            self.store.finalize_publication(key)
            entry = self._entry_if_valid(key, now)
            if entry is None:
                raise OSError("published audio was not visible after commit")
            return entry

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _discard_duplicate_source(self, source: Path, retained: Path) -> None:
        if source == retained or source.parent != self.staging_dir:
            return
        try:
            source.unlink(missing_ok=True)
        except OSError:
            pass

    def _promote_entry(self, row: dict[str, object], now: float) -> CacheEntry:
        key = AudioKey(str(row["video_id"]), str(row["policy"]))
        if row["pool"] == "requested":
            self.mark_used(key)
            return self._entry_if_valid(key, now) or self._entry(row)  # type: ignore[return-value]
        self._ensure_room("requested", int(row["size_bytes"]), exclude_key=key)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE jukes_audio SET pool = 'requested', expires_at = NULL, last_used = ? "
                "WHERE video_id = ? AND policy = ?",
                (now, key.video_id, key.policy),
            )
        return self._entry_if_valid(key, now)  # type: ignore[return-value]

    def promote(self, key: AudioKey) -> CacheEntry | None:
        with _COORDINATOR_LOCK:
            now = self.clock()
            row = self.store.get_audio(key)
            if row is not None:
                if self._is_expired(row, now):
                    if not self._is_leased(key):
                        self._remove_audio(row)
                        return None
                    return self._promote_entry(row, now)
                entry = self._entry_if_valid(key, now)
                return self._promote_entry(row, now) if entry is not None else None

            reservation = self.store.get_reservation(key)
            if reservation is None or reservation["state"] != "reserved":
                return None
            if reservation["pool"] == "requested":
                return None
            amount = int(reservation["reserved_bytes"])
            partial = Path(str(reservation["partial_path"]))
            written = partial.stat().st_size if partial.exists() else 0
            self._ensure_room(
                "requested",
                amount,
                disk_commitment_bytes=max(0, amount - written),
                exclude_key=key,
            )
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE jukes_reservations SET pool = 'requested', requested = 1 "
                    "WHERE video_id = ? AND policy = ?",
                    (key.video_id, key.policy),
                )
            return None

    def reserve(self, key: AudioKey, *, requested: bool, expected_size: int | None = None) -> CacheReservation:
        with _COORDINATOR_LOCK:
            cached_row = self.store.get_audio(key)
            if cached_row is not None:
                entry = self._entry_if_valid(key)
                if entry is not None:
                    if requested and entry.pool == "warmup":
                        entry = self._promote_entry(cached_row, self.clock())
                    elif requested:
                        self.mark_used(key)
                        entry = self._entry_if_valid(key) or entry
                    return CacheReservation(self, key, entry.path, entry)
                if self._is_leased(key):
                    raise CacheCapacityError("expired audio is protected by an active reader")
                self._remove_audio(cached_row)

            if expected_size is not None and expected_size <= 0:
                raise ValueError("expected_size must be positive")
            pool = self._pool(requested)
            existing = self.store.get_reservation(key)
            if existing is not None and existing["state"] == "publishing":
                self._recover_publication(existing)
                entry = self._entry_if_valid(key)
                if entry is not None:
                    return CacheReservation(self, key, entry.path, entry)
                existing = self.store.get_reservation(key)

            if existing is not None:
                partial = Path(str(existing["partial_path"]))
                if existing["state"] == "reserved" and partial.is_file():
                    current_reserved = int(existing["reserved_bytes"])
                    written_bytes = partial.stat().st_size
                    target_pool = "requested" if requested or existing["pool"] == "requested" else "warmup"
                    wanted = max(
                        current_reserved,
                        written_bytes,
                        int(expected_size) if expected_size is not None else current_reserved,
                    )
                    if wanted > current_reserved:
                        wanted = self._rounded_reservation(wanted, current_reserved)
                    if target_pool != existing["pool"]:
                        self._ensure_room(
                            target_pool,
                            wanted,
                            disk_commitment_bytes=max(0, wanted - written_bytes),
                            exclude_key=key,
                        )
                    elif wanted > current_reserved:
                        self._ensure_room(
                            target_pool,
                            wanted,
                            disk_commitment_bytes=max(0, wanted - written_bytes),
                            exclude_key=key,
                        )
                    with self.store.transaction() as connection:
                        connection.execute(
                            "UPDATE jukes_reservations SET pool = ?, requested = ?, reserved_bytes = ?, expected_size = ? "
                            "WHERE video_id = ? AND policy = ?",
                            (target_pool, int(target_pool == "requested"), wanted,
                             expected_size if expected_size is not None else existing["expected_size"],
                             key.video_id, key.policy),
                        )
                    return CacheReservation(self, key, partial)

            amount = int(expected_size) if expected_size is not None else self.config.unknown_size_reservation_bytes
            self._ensure_room(pool, amount, disk_commitment_bytes=amount, exclude_key=key)
            partial = self.staging_dir / f"{self._digest(key)}-{uuid.uuid4().hex}.partial"
            partial.touch(exist_ok=False)
            try:
                with self.store.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO jukes_reservations (
                            video_id, policy, pool, requested, reserved_bytes,
                            expected_size, partial_path, state, created_at, owner_pid, owner_start
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
                        ON CONFLICT(video_id, policy) DO UPDATE SET
                            pool = excluded.pool,
                            requested = excluded.requested,
                            reserved_bytes = excluded.reserved_bytes,
                            expected_size = excluded.expected_size,
                            partial_path = excluded.partial_path,
                            state = 'reserved',
                            created_at = excluded.created_at,
                            owner_pid = excluded.owner_pid,
                            owner_start = excluded.owner_start,
                            publish_path = NULL,
                            size_bytes = NULL,
                            completed_at = NULL,
                            expires_at = NULL
                        """,
                        (key.video_id, key.policy, pool, int(requested), amount,
                         expected_size, str(partial), self.clock(), self._pid, self._process_start_token),
                    )
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
            return CacheReservation(self, key, partial)

    def _rounded_reservation(self, required: int, current: int) -> int:
        increment = self.config.reservation_increment_bytes
        extra = max(0, required - current)
        return current + ((extra + increment - 1) // increment) * increment

    def _write_reserved(self, key: AudioKey, path: Path, data: bytes) -> int:
        with _COORDINATOR_LOCK:
            row = self.store.get_reservation(key)
            if row is None or row["state"] != "reserved" or Path(str(row["partial_path"])) != path:
                raise RuntimeError("reservation is no longer active")
            current_size = path.stat().st_size if path.exists() else 0
            required = current_size + len(data)
            current_reserved = int(row["reserved_bytes"])
            new_reserved = current_reserved
            if required > current_reserved:
                new_reserved = self._rounded_reservation(required, current_reserved)
            delta = new_reserved - current_reserved
            self._ensure_room(
                str(row["pool"]),
                new_reserved,
                disk_commitment_bytes=max(0, new_reserved - current_size),
                exclude_key=key,
            )
            if delta:
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE jukes_reservations SET reserved_bytes = ? WHERE video_id = ? AND policy = ?",
                        (new_reserved, key.video_id, key.policy),
                    )
            with path.open("ab") as output:
                output.write(data)
                output.flush()
            return len(data)

    def open_lease(self, key: AudioKey, *, touch: bool = True) -> tuple[CacheEntry | None, Callable[[], None]]:
        """Pin a ready entry against eviction; returns ``(entry, release)``.

        ``release`` is idempotent. ``touch=False`` leaves LRU recency alone
        (HEAD and other inspections).
        """
        lease_id: str | None = None
        entry: CacheEntry | None = None
        with _COORDINATOR_LOCK:
            row = self.store.get_audio(key)
            if row is not None and not self._is_expired(row, self.clock()):
                entry = self._entry_if_valid(key)
            if entry is not None:
                lease_id = uuid.uuid4().hex
                with self.store.transaction() as connection:
                    connection.execute(
                        "INSERT INTO jukes_leases(lease_id, video_id, policy, owner_pid, owner_start, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (lease_id, key.video_id, key.policy, self._pid, self._process_start_token, self.clock()),
                    )
                    if touch:
                        connection.execute(
                            "UPDATE jukes_audio SET last_used = ? WHERE video_id = ? AND policy = ?",
                            (self.clock(), key.video_id, key.policy),
                        )
        released = threading.Event()

        def release() -> None:
            if lease_id is None or released.is_set():
                return
            released.set()
            with _COORDINATOR_LOCK, self.store.transaction() as connection:
                connection.execute("DELETE FROM jukes_leases WHERE lease_id = ?", (lease_id,))

        return entry, release

    @contextmanager
    def lease(self, key: AudioKey, *, touch: bool = True) -> Iterator[CacheEntry | None]:
        entry, release = self.open_lease(key, touch=touch)
        try:
            yield entry
        finally:
            release()

    def _remove_reservation(self, key: AudioKey) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                "DELETE FROM jukes_reservations WHERE video_id = ? AND policy = ?",
                (key.video_id, key.policy),
            )

    def release_reservation(self, key: AudioKey) -> bool:
        """Discard failed/interrupted extraction state and release its budgets."""

        with _COORDINATOR_LOCK:
            row = self.store.get_reservation(key)
            if row is None or row["state"] == "publishing":
                return False
            if row["state"] == "reserved" and self._reservation_owner_alive(row):
                owner = (int(row["owner_pid"]), str(row["owner_start"]))
                if owner != (self._pid, self._process_start_token):
                    return False
            partial = Path(str(row["partial_path"]))
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                return False
            self._remove_reservation(key)
            return True

    def _recover_publication(self, reservation: dict[str, object]) -> bool:
        key = AudioKey(str(reservation["video_id"]), str(reservation["policy"]))
        destination = Path(str(reservation["publish_path"] or ""))
        source = Path(str(reservation["partial_path"]))
        expected_size = int(reservation["size_bytes"] or 0)
        completed_at = float(reservation["completed_at"] or reservation["created_at"])
        expires_at = float(reservation["expires_at"]) if reservation["expires_at"] is not None else None
        pool = str(reservation["pool"])
        if (
            not destination.is_file()
            or destination.stat().st_size != expected_size
            or not self.media_validator(destination)
            or (pool == "warmup" and expires_at is not None and expires_at <= self.clock())
        ):
            destination.unlink(missing_ok=True)
            self._remove_staged_source(source)
            self._remove_reservation(key)
            return False
        try:
            self._ensure_room(pool, expected_size, exclude_key=key)
        except CacheCapacityError:
            destination.unlink(missing_ok=True)
            self._remove_staged_source(source)
            self._remove_reservation(key)
            return False
        self.store.finalize_publication(key)
        return True

    def _remove_staged_source(self, path: Path) -> None:
        try:
            if path.parent.resolve() == self.staging_dir.resolve():
                path.resolve().relative_to(self.staging_dir.resolve())
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def _clean_stale_leases(self) -> None:
        stale_ids = []
        for row in self.store.all_leases():
            pid = int(row["owner_pid"])
            stored_start = str(row["owner_start"])
            current_start = _process_start(pid)
            if current_start is None or current_start != stored_start:
                stale_ids.append(str(row["lease_id"]))
        if stale_ids:
            with self.store.transaction() as connection:
                connection.executemany("DELETE FROM jukes_leases WHERE lease_id = ?", [(item,) for item in stale_ids])

    def _reservation_owner_alive(self, row: dict[str, object]) -> bool:
        pid = int(row.get("owner_pid", 0) or 0)
        owner_start = str(row.get("owner_start", ""))
        return pid > 0 and bool(owner_start) and _process_start(pid) == owner_start

    def reconcile(self) -> PruneResult:
        with _COORDINATOR_LOCK:
            self._clean_stale_leases()
            removed_entries = 0
            removed_bytes = 0
            reservations = self.store.all_reservations()
            for reservation in reservations:
                key = AudioKey(str(reservation["video_id"]), str(reservation["policy"]))
                if reservation["state"] == "publishing":
                    if not self._recover_publication(reservation):
                        removed_entries += 1
                        removed_bytes += int(reservation["size_bytes"] or 0)
                    continue
                if reservation["state"] == "reserved" and self._reservation_owner_alive(reservation):
                    continue
                partial = Path(str(reservation["partial_path"]))
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE jukes_reservations SET state = 'interrupted', reserved_bytes = 0, "
                        "owner_pid = 0, owner_start = '' "
                        "WHERE video_id = ? AND policy = ?",
                        (key.video_id, key.policy),
                    )

            for row in self.store.all_audio():
                path = Path(str(row["path"]))
                valid = self.media_validator(path) and path.stat().st_size == int(row["size_bytes"])
                expired = self._is_expired(row, self.clock())
                if (not valid or expired) and self._remove_audio(row):
                    removed_entries += 1
                    removed_bytes += int(row["size_bytes"])

            tracked = {
                Path(str(row["partial_path"])).resolve()
                for row in self.store.all_reservations()
                if row["state"] == "reserved"
            }
            for partial in self.staging_dir.glob("*.partial"):
                if partial.resolve() not in tracked:
                    try:
                        partial.unlink(missing_ok=True)
                    except OSError:
                        pass
            return PruneResult(removed_entries=removed_entries, removed_bytes=removed_bytes)

    def prune(self, now: float) -> PruneResult:
        with _COORDINATOR_LOCK:
            removed_entries = 0
            removed_bytes = 0
            expired = [row for row in self.store.all_audio() if self._is_expired(row, now)]
            expired.sort(key=lambda row: (float(row["expires_at"] or 0), float(row["last_used"])))
            for row in expired:
                if self._remove_audio(row):
                    removed_entries += 1
                    removed_bytes += int(row["size_bytes"])
            tracked = {
                Path(str(row["partial_path"])).resolve()
                for row in self.store.all_reservations()
                if row["state"] == "reserved"
            }
            for partial in self.staging_dir.glob("*.partial"):
                if partial.resolve() not in tracked:
                    try:
                        partial.unlink(missing_ok=True)
                    except OSError:
                        pass
            return PruneResult(removed_entries=removed_entries, removed_bytes=removed_bytes)
