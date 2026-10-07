"""Persistent cache lifecycle tests using only temporary files and SQLite."""

from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from jukes.cache import Cache  # noqa: E402
from jukes.config import CacheConfig  # noqa: E402
from jukes.models import AudioKey, CacheCapacityError  # noqa: E402
from jukes.store import Store  # noqa: E402


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.value = now

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def config(tmp_path: Path) -> CacheConfig:
    return CacheConfig(
        database_path=tmp_path / "state" / "jukes.sqlite3",
        audio_dir=tmp_path / "audio",
        requested_limit_bytes=64,
        warmup_limit_bytes=32,
        warmup_ttl_seconds=7_200,
        min_free_disk_bytes=0,
        unknown_size_reservation_bytes=4,
        reservation_increment_bytes=4,
    )


@pytest.fixture
def cache(config: CacheConfig, clock: FakeClock) -> Cache:
    return Cache(config, clock=clock.now)


@pytest.fixture
def completed_audio(cache: Cache) -> tuple[AudioKey, Path]:
    key = AudioKey("requested-track", "m4a-v1")
    path = cache.staging_dir / "requested-track.m4a"
    path.write_bytes(b"audio bytes")
    return key, path


def make_audio(cache: Cache, video_id: str, size: int, policy: str = "m4a-v1") -> tuple[AudioKey, Path]:
    key = AudioKey(video_id, policy)
    path = cache.staging_dir / f"{video_id}.m4a"
    path.write_bytes(bytes([len(video_id) % 251]) * size)
    return key, path


def test_requested_track_has_no_ttl(cache: Cache, clock: FakeClock, completed_audio: tuple[AudioKey, Path]) -> None:
    key, path = completed_audio
    cache.complete(key, path, requested=True)

    clock.advance(7_201)
    cache.prune(clock.now())

    assert cache.lookup(key) is not None


def test_cache_config_rejects_nonpositive_limits(config: CacheConfig) -> None:
    with pytest.raises(ValueError, match="reservation_increment_bytes"):
        replace(config, reservation_increment_bytes=0)


def test_warmup_expires_7200_seconds_after_completion(cache: Cache, clock: FakeClock) -> None:
    key, path = make_audio(cache, "warmup-expiry", 5)
    cache.complete(key, path, requested=False)
    entry = cache.lookup(key)
    assert entry is not None
    completed_at = clock.now()
    assert entry.expires_at == completed_at + 7_200

    clock.advance(7_199)
    assert cache.lookup(key) is not None
    clock.advance(1)
    cache.prune(clock.now())
    assert cache.lookup(key) is None


def test_requested_and_warmup_limits_are_independent(cache: Cache) -> None:
    warmup_key, warmup_path = make_audio(cache, "warmup-budget", 30)
    requested_key, requested_path = make_audio(cache, "requested-budget", 60)

    cache.complete(warmup_key, warmup_path, requested=False)
    cache.complete(requested_key, requested_path, requested=True)

    assert cache.lookup(warmup_key).pool == "warmup"
    assert cache.lookup(requested_key).pool == "requested"


def test_duplicate_warmup_does_not_reset_expiry(cache: Cache, clock: FakeClock) -> None:
    key, first_path = make_audio(cache, "duplicate-warmup", 4)
    original = cache.complete(key, first_path, requested=False)
    clock.advance(600)
    _, duplicate_path = make_audio(cache, "duplicate-warmup-copy", 4)

    duplicate = cache.complete(key, duplicate_path, requested=False)

    assert duplicate.expires_at == original.expires_at
    assert duplicate.path == original.path


def test_promotion_reuses_file_and_removes_expiry(cache: Cache, completed_audio: tuple[AudioKey, Path]) -> None:
    key, path = completed_audio
    warmup = cache.complete(key, path, requested=False)

    promoted = cache.promote(key)

    assert promoted.path == warmup.path
    assert promoted.pool == "requested"
    assert promoted.expires_at is None
    assert promoted.path.is_file()


def test_promotion_keeps_an_inflight_reservation_in_the_requested_pool(cache: Cache) -> None:
    key = AudioKey("inflight-promotion", "m4a-v1")
    reservation = cache.reserve(key, requested=False, expected_size=4)

    assert cache.promote(key) is None
    reservation.write(b"data")
    entry = cache.complete(key, reservation.path, requested=False)

    assert entry.pool == "requested"
    assert entry.expires_at is None


def test_inflight_promotion_keeps_unwritten_disk_bytes_reserved(cache: Cache) -> None:
    cache.config = replace(cache.config, requested_limit_bytes=16, min_free_disk_bytes=10)
    free_disk = [20]
    cache.disk_free_bytes = lambda: free_disk[0]
    key = AudioKey("inflight-disk-promotion", "m4a-v1")
    reservation = cache.reserve(key, requested=False, expected_size=8)
    reservation.write(b"1234")
    free_disk[0] = 10

    with pytest.raises(CacheCapacityError):
        cache.promote(key)

    assert cache.store.get_reservation(key)["pool"] == "warmup"


def test_release_reservation_removes_partial_and_frees_its_capacity(cache: Cache) -> None:
    key = AudioKey("failed-job", "m4a-v1")
    reservation = cache.reserve(key, requested=False, expected_size=4)
    reservation.write(b"partial")

    assert cache.release_reservation(key) is True

    assert not reservation.path.exists()
    assert cache.store.get_reservation(key) is None
    replacement = cache.reserve(key, requested=False, expected_size=4)
    assert replacement.path.exists()


def test_lru_eviction_uses_last_accepted_access(cache: Cache, clock: FakeClock) -> None:
    cache.config = replace(cache.config, requested_limit_bytes=8)
    older_key, older_path = make_audio(cache, "lru-old", 4)
    newer_key, newer_path = make_audio(cache, "lru-new", 4)
    cache.complete(older_key, older_path, requested=True)
    clock.advance(1)
    cache.complete(newer_key, newer_path, requested=True)
    clock.advance(1)

    assert cache.lookup(older_key) is not None  # Lookups do not refresh LRU.
    assert cache.mark_used(older_key) is True
    incoming_key, incoming_path = make_audio(cache, "lru-incoming", 4)
    cache.complete(incoming_key, incoming_path, requested=True)

    assert cache.lookup(older_key) is not None
    assert cache.lookup(newer_key) is None
    assert cache.lookup(incoming_key) is not None


def test_failed_unlink_does_not_count_as_evicted_pool_capacity(
    cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache.config = replace(cache.config, requested_limit_bytes=8)
    blocked_key, blocked_path = make_audio(cache, "unlink-blocked", 4)
    other_key, other_path = make_audio(cache, "unlink-other", 4)
    blocked_entry = cache.complete(blocked_key, blocked_path, requested=True)
    cache.complete(other_key, other_path, requested=True)
    cache.config = replace(cache.config, requested_limit_bytes=4)
    incoming_key, incoming_path = make_audio(cache, "unlink-incoming", 1)
    original_unlink = Path.unlink

    def fail_blocked_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == blocked_entry.path:
            raise PermissionError("simulated undeletable cache file")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_blocked_unlink)
    with pytest.raises(CacheCapacityError):
        cache.complete(incoming_key, incoming_path, requested=True)

    assert cache.lookup(blocked_key) is not None
    assert cache.lookup(incoming_key) is None


def test_active_reader_blocks_promotion_eviction_race(cache: Cache) -> None:
    cache.config = replace(cache.config, requested_limit_bytes=4)
    requested_key, requested_path = make_audio(cache, "leased-requested", 4)
    warmup_key, warmup_path = make_audio(cache, "promotion-candidate", 4)
    cache.complete(requested_key, requested_path, requested=True)
    cache.complete(warmup_key, warmup_path, requested=False)

    with cache.lease(requested_key) as reader:
        assert reader is not None
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(cache.promote, warmup_key)
            with pytest.raises(CacheCapacityError):
                future.result(timeout=5)
        assert cache.lookup(requested_key) is not None
        assert cache.lookup(warmup_key).pool == "warmup"

    promoted = cache.promote(warmup_key)
    assert promoted.pool == "requested"
    assert cache.lookup(requested_key) is None


def test_reconcile_removes_missing_files_and_orphan_partials(cache: Cache, config: CacheConfig, clock: FakeClock) -> None:
    missing_key, missing_path = make_audio(cache, "removed-audio", 5)
    missing_entry = cache.complete(missing_key, missing_path, requested=True)
    missing_entry.path.unlink()
    orphan = cache.staging_dir / "orphan.partial"
    orphan.write_bytes(b"orphan")
    interrupted = cache.reserve(AudioKey("interrupted", "m4a-v1"), requested=False)
    interrupted.write(b"partial")
    with cache.store.transaction() as connection:
        connection.execute(
            "UPDATE jukes_reservations SET owner_pid = 99999999, owner_start = 'dead' "
            "WHERE video_id = 'interrupted'"
        )

    restarted = Cache(config, clock=clock.now)

    assert restarted.lookup(missing_key) is None
    assert not orphan.exists()
    assert not interrupted.path.exists()
    resumed = restarted.reserve(AudioKey("interrupted", "m4a-v1"), requested=False)
    assert resumed.path.exists()


def test_second_cache_instance_keeps_live_process_leases_and_reservations(
    cache: Cache, config: CacheConfig, clock: FakeClock
) -> None:
    key, path = make_audio(cache, "live-process-audio", 4)
    entry = cache.complete(key, path, requested=True)
    in_flight = cache.reserve(AudioKey("live-process-job", "m4a-v1"), requested=False)
    in_flight.write(b"part")

    with cache.lease(key) as active_reader:
        assert active_reader is not None
        restarted = Cache(config, clock=clock.now)
        assert len(restarted.store.all_leases()) == 1

    assert restarted.lookup(key) is not None
    assert in_flight.path.exists()
    with restarted.lease(key) as reader:
        assert reader is not None
        assert reader.path == entry.path
    in_flight.write(b"ial")
    assert in_flight.path.read_bytes() == b"partial"


def test_new_tables_preserve_legacy_database_records(config: CacheConfig, clock: FakeClock) -> None:
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.database_path) as connection:
        connection.execute("CREATE TABLE legacy_tracks (id INTEGER PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO legacy_tracks(title) VALUES ('keep me')")

    Cache(config, clock=clock.now)

    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute("SELECT title FROM legacy_tracks").fetchone() == ("keep me",)
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "legacy_tracks" in names
    assert "jukes_audio" in names


def test_atomic_publication_recovers_after_rename_before_database_commit(
    cache: Cache, config: CacheConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = cache.store
    original_finalize = store.finalize_publication

    def crash_after_rename(*args: object, **kwargs: object) -> None:
        raise OSError("simulated process stop after atomic rename")

    monkeypatch.setattr(store, "finalize_publication", crash_after_rename)
    key, path = make_audio(cache, "publication-recovery", 7)
    with pytest.raises(OSError, match="simulated process stop"):
        cache.complete(key, path, requested=False)
    assert not path.exists()

    monkeypatch.setattr(store, "finalize_publication", original_finalize)
    restarted = Cache(config, clock=clock.now, store=Store(config.database_path))
    recovered = restarted.lookup(key)

    assert recovered is not None
    assert recovered.pool == "warmup"
    assert recovered.path.is_file()


def test_atomic_publication_cleans_source_when_rename_never_happened(
    cache: Cache, config: CacheConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, path = make_audio(cache, "publication-before-rename", 7)

    def fail_rename(*args: object, **kwargs: object) -> None:
        raise OSError("simulated stop before rename")

    monkeypatch.setattr("jukes.cache.os.replace", fail_rename)
    with pytest.raises(OSError, match="simulated stop before rename"):
        cache.complete(key, path, requested=False)

    monkeypatch.undo()
    Cache(config, clock=clock.now)

    assert not path.exists()
    assert cache.store.get_reservation(key) is None


def test_oversized_track_is_rejected_for_its_target_pool(cache: Cache) -> None:
    key, path = make_audio(cache, "too-large-request", 65)

    with pytest.raises(CacheCapacityError) as error:
        cache.complete(key, path, requested=True)

    assert error.value.code == "track_too_large"
    assert path.exists()


def test_partial_writes_extend_reservation_before_accepting_bytes(config: CacheConfig, clock: FakeClock) -> None:
    config = replace(config, warmup_limit_bytes=10)
    cache = Cache(config, clock=clock.now)
    reservation = cache.reserve(AudioKey("partial-budget", "m4a-v1"), requested=False)
    assert reservation.reserved_bytes == 4

    reservation.write(b"1234")
    reservation.write(b"5")
    assert reservation.reserved_bytes == 8
    assert reservation.path.read_bytes() == b"12345"
    reservation.write(b"678")
    assert reservation.reserved_bytes == 8
    with pytest.raises(CacheCapacityError):
        reservation.write(b"9")
    assert reservation.path.read_bytes() == b"12345678"


def test_reusing_reservation_never_shrinks_below_written_bytes(cache: Cache) -> None:
    key = AudioKey("reservation-reuse", "m4a-v1")
    reservation = cache.reserve(key, requested=False, expected_size=8)
    reservation.write(b"12345678")

    reused = cache.reserve(key, requested=True, expected_size=4)

    assert reused.reserved_bytes == 8
    assert reused.path.read_bytes() == b"12345678"
    assert cache.store.get_reservation(key)["pool"] == "requested"


def test_lease_pins_audio_when_free_disk_is_low(cache: Cache, clock: FakeClock) -> None:
    cache.config = replace(cache.config, requested_limit_bytes=8, min_free_disk_bytes=10)
    free_disk = [100]
    pinned_key, pinned_path = make_audio(cache, "disk-pinned", 4)
    pinned_entry = cache.complete(pinned_key, pinned_path, requested=True)
    cache.disk_free_bytes = lambda: free_disk[0] + (4 if not pinned_entry.path.exists() else 0)
    incoming_key, incoming_path = make_audio(cache, "disk-incoming", 4)

    with cache.lease(pinned_key):
        free_disk[0] = 6
        with pytest.raises(CacheCapacityError):
            cache.complete(incoming_key, incoming_path, requested=True)
        assert cache.lookup(pinned_key) is not None

    accepted = cache.complete(incoming_key, incoming_path, requested=True)
    assert accepted.key == incoming_key
    assert cache.lookup(pinned_key) is None


def test_metrics_eviction_and_expiration_are_separate(tmp_path):
    from jukes.config import CacheConfig
    from jukes.cache import Cache
    from jukes.models import AudioKey
    now=[100000.0]
    config=CacheConfig(database_path=tmp_path/'m.db',audio_dir=tmp_path/'a',
        warmup_limit_bytes=8,requested_limit_bytes=100,min_free_disk_bytes=0,
        unknown_size_reservation_bytes=4,reservation_increment_bytes=4)
    cache=Cache(config,clock=lambda:now[0])
    for vid in ('first','second'):
        key=AudioKey(vid,'p'); r=cache.reserve(key,requested=False)
        r.write(b'12345678'); cache.complete(key,r.path,False)
    total=cache.metrics.snapshot()['lifetime']
    assert total['eviction']==1 and total['warmup_eviction']==1
    now[0]+=7201
    cache.prune(now[0])
    total=cache.metrics.snapshot()['lifetime']
    assert total['warmup_expiration']==1 and total['eviction']==1


def test_writes_within_admitted_capacity_skip_eviction_scan(cache, monkeypatch):
    reservation = cache.reserve(AudioKey('write-budget', 'p'), requested=True, expected_size=16)
    original = cache._ensure_room
    scans = []
    def scan(*args, **kwargs):
        scans.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(cache, '_ensure_room', scan)
    for _ in range(4):
        reservation.write(b'1234')
    assert reservation.path.read_bytes() == b'1234' * 4
    assert scans == []
    reservation.write(b'5')  # Growth still performs full admission before writing.
    assert len(scans) == 1
    assert reservation.reserved_bytes == 20


def test_external_disk_pressure_rechecks_capacity_before_reserved_write(cache, monkeypatch):
    reservation = cache.reserve(AudioKey('write-pressure', 'p'), requested=True, expected_size=16)
    cache.config = replace(cache.config, min_free_disk_bytes=10)
    monkeypatch.setattr(cache, 'disk_free_bytes', lambda: 20)
    with pytest.raises(CacheCapacityError):
        reservation.write(b'1234')
    assert reservation.path.stat().st_size == 0


def test_eviction_candidate_scan_reads_lease_snapshot_once(cache, monkeypatch):
    for index in range(8):
        key,path=make_audio(cache,f'scan-{index}',4)
        cache.complete(key,path,requested=True)
    pinned=AudioKey('scan-0','m4a-v1')
    with cache.lease(pinned):
        reads=[]
        original=cache.store.all_leases
        def leases():
            reads.append(True)
            return original()
        monkeypatch.setattr(cache.store,'all_leases',leases)
        eligible=cache._eligible_audio(set())
        assert pinned.video_id not in {row['video_id'] for row in eligible}
        assert len(eligible)==7
        assert len(reads)==1  # Query cost stays constant as cache entry count grows.
