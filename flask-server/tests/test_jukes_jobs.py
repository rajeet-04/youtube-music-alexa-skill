"""Shared download coordinator tests with a deterministic fake extractor."""

from __future__ import annotations

import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from jukes.cache import Cache  # noqa: E402
from jukes.config import CacheConfig  # noqa: E402
from jukes.extractor import DownloadResult  # noqa: E402
from jukes.jobs import JobQueueFull, Jobs  # noqa: E402
from jukes.models import AudioKey, CacheCapacityError  # noqa: E402


@pytest.fixture
def config(tmp_path: Path) -> CacheConfig:
    return CacheConfig(
        database_path=tmp_path / "state" / "jukes.sqlite3",
        audio_dir=tmp_path / "audio",
        requested_limit_bytes=64,
        warmup_limit_bytes=24,
        warmup_ttl_seconds=7_200,
        min_free_disk_bytes=0,
        unknown_size_reservation_bytes=4,
        reservation_increment_bytes=4,
    )


@pytest.fixture
def cache(config: CacheConfig) -> Cache:
    return Cache(config)


@dataclass
class FakeExtractor:
    entered: threading.Event | None = None
    release: threading.Event | None = None
    fail: bool = False
    output: bytes = b"audio"
    started: list[str] | None = None
    active: int = 0
    max_active: int = 0
    lock: threading.Lock = threading.Lock()

    def download(self, key, destination, credential_snapshot):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.started is not None:
                self.started.append(key.video_id)
        if self.entered:
            self.entered.set()
        try:
            if self.release:
                assert self.release.wait(3)
            destination.write(self.output)
            if self.fail:
                raise RuntimeError("secret cookie value must not escape")
            return DownloadResult(
                path=destination.path,
                size_bytes=len(self.output),
                media_format="m4a",
                mime_type="audio/mp4",
                duration_seconds=12.5,
                codec_name="aac",
                client="default",
            )
        finally:
            with self.lock:
                self.active -= 1


def test_twenty_simultaneous_submissions_share_one_job(cache: Cache) -> None:
    entered = threading.Event()
    release = threading.Event()
    extractor = FakeExtractor(entered=entered, release=release)
    jobs = Jobs(cache, extractor, worker_count=1)
    key = AudioKey("same-public-track", "public-m4a-v1")
    barrier = threading.Barrier(20)

    def submit():
        barrier.wait(timeout=2)
        return jobs.submit(key, requested=False)

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda _: submit(), range(20)))
        assert entered.wait(2)
        assert len({job.job_id for job in results}) == 1
        assert cache.store.get_reservation(key) is not None
        release.set()
        ready = jobs.wait(results[0].job_id, 2)
        assert ready.status == "ready"
        assert extractor.max_active == 1
    finally:
        release.set()
        jobs.shutdown()


def test_requested_promotion_of_queued_warmup_wins_dispatch_priority(cache: Cache) -> None:
    entered = threading.Event()
    release = threading.Event()
    started: list[str] = []
    extractor = FakeExtractor(entered=entered, release=release, started=started)
    jobs = Jobs(cache, extractor, worker_count=1, max_warmup_workers=1)
    try:
        first = jobs.submit(AudioKey("first", "p1"), requested=True)
        assert entered.wait(2)
        stale = jobs.submit(AudioKey("stale-warmup", "p1"), requested=False)
        promoted = jobs.submit(AudioKey("promoted-warmup", "p1"), requested=False)
        promoted_requested = jobs.submit(promoted.key, requested=True)
        assert promoted_requested.job_id == promoted.job_id
        assert promoted_requested.requested is True
        release.set()

        assert jobs.wait(first.job_id, 2).status == "ready"
        assert jobs.wait(promoted.job_id, 2).status == "ready"
        assert jobs.wait(stale.job_id, 2).status == "ready"
        assert started[:3] == ["first", "promoted-warmup", "stale-warmup"]
        assert cache.lookup(promoted.key).pool == "requested"
    finally:
        release.set()
        jobs.shutdown()


def test_worker_limit_and_reserved_requested_slot(cache: Cache) -> None:
    entered = threading.Event()
    release = threading.Event()
    extractor = FakeExtractor(entered=entered, release=release)
    jobs = Jobs(cache, extractor, worker_count=4)
    try:
        warmups = [jobs.submit(AudioKey(f"w{i}", "p1"), requested=False) for i in range(4)]
        assert entered.wait(2)
        # Warmups occupy at most three workers, leaving a requested slot available.
        requested = jobs.submit(AudioKey("foreground", "p1"), requested=True)
        assert jobs.wait(requested.job_id, 0.05).status == "downloading"
        assert extractor.max_active <= 4
        assert extractor.max_active >= 1
        assert jobs._active_warmups <= 3
    finally:
        release.set()
        jobs.shutdown()


def test_queue_capacity_is_bounded_and_requested_pool_is_independent(cache: Cache) -> None:
    entered = threading.Event()
    release = threading.Event()
    extractor = FakeExtractor(entered=entered, release=release)
    jobs = Jobs(cache, extractor, worker_count=1, max_queue_size=1)
    try:
        active = jobs.submit(AudioKey("active", "p1"), requested=True)
        assert entered.wait(2)
        queued = jobs.submit(AudioKey("queued", "p1"), requested=False)
        with pytest.raises(JobQueueFull):
            jobs.submit(AudioKey("over-capacity", "p1"), requested=False)
        # A separate pool can still admit requested work; queue policy is global.
        with pytest.raises(JobQueueFull):
            jobs.submit(AudioKey("requested-over-capacity", "p1"), requested=True)
        release.set()
        assert jobs.wait(active.job_id, 2).status == "ready"
        assert jobs.wait(queued.job_id, 2).status == "ready"
    finally:
        release.set()
        jobs.shutdown()


def test_cache_reservation_refuses_capacity_before_queueing(cache: Cache) -> None:
    entered = threading.Event()
    release = threading.Event()
    extractor = FakeExtractor(entered=entered, release=release)
    jobs = Jobs(cache, extractor, worker_count=1, max_queue_size=4)
    try:
        jobs.submit(AudioKey("warmup-budget-full", "p1"), requested=False)
        assert entered.wait(2)
        # Six bytes fit; an unknown second job reserves four more and cannot fit.
        full_key = AudioKey("large-warmup", "p1")
        cache.store.transaction  # keep the test focused on cache admission below
        from jukes.cache import CacheReservation

        reservation = cache.reserve(full_key, requested=False, expected_size=20)
        reservation.write(b"x" * 20)
        cache.complete(full_key, reservation.path, requested=False)
        with cache.lease(full_key):  # protected files cannot be evicted for the new reservation
            with pytest.raises(CacheCapacityError):
                jobs.submit(AudioKey("cannot-reserve", "p1"), requested=False)
        assert jobs.get(next(iter(jobs._by_key.values()))) is not None
    finally:
        release.set()
        jobs.shutdown()


def test_failed_extraction_releases_partial_reservation_and_hides_error(cache: Cache) -> None:
    extractor = FakeExtractor(fail=True)
    jobs = Jobs(cache, extractor, worker_count=1)
    key = AudioKey("failure", "p1")
    try:
        job = jobs.submit(key, requested=True)
        result = jobs.wait(job.job_id, 2)
        assert result.status == "failed"
        assert result.error_code == "extraction_failed"
        assert "secret cookie" not in str(result)
        assert cache.store.get_reservation(key) is None
        assert not list(cache.staging_dir.glob("*.partial"))
    finally:
        jobs.shutdown()


def test_recovery_requeues_interrupted_job_once_after_cleaning_partial(cache: Cache) -> None:
    extractor = FakeExtractor()
    first_jobs = Jobs(cache, extractor, worker_count=1, autostart=False)
    key = AudioKey("interrupted", "p1")
    old_reservation = cache.reserve(key, requested=False)
    old_reservation.write(b"partial")
    try:
        with cache.store.transaction() as connection:
            connection.execute(
                """INSERT INTO jukes_jobs
                   (job_id, video_id, policy, requested, status, created_at, updated_at,
                    recovery_count, owner_pid, owner_start)
                   VALUES ('job-recovered', ?, ?, 0, 'downloading', 1, 1, 0, 0, '')""",
                (key.video_id, key.policy),
            )
        recovered = Jobs(cache, extractor, worker_count=1)
        try:
            job = recovered.wait("job-recovered", 2)
            assert job.status == "ready"
            assert job.recovery_count == 1
            assert not old_reservation.path.exists()
            assert cache.lookup(key) is not None
        finally:
            recovered.shutdown()
    finally:
        first_jobs.shutdown()


def test_cancelling_waiter_does_not_cancel_shared_download(cache: Cache) -> None:
    entered = threading.Event()
    release = threading.Event()
    jobs = Jobs(cache, FakeExtractor(entered=entered, release=release), worker_count=1)
    key = AudioKey("caller-cancel", "p1")
    try:
        job = jobs.submit(key, requested=True)
        assert entered.wait(2)
        timed_out = jobs.wait(job.job_id, 0.01)
        assert timed_out.status == "downloading"
        release.set()
        assert jobs.wait(job.job_id, 2).status == "ready"
        assert cache.lookup(key) is not None
    finally:
        release.set()
        jobs.shutdown()


def test_ready_job_becomes_evicted_if_cache_entry_was_evicted(cache: Cache) -> None:
    jobs = Jobs(cache, FakeExtractor(), worker_count=1)
    key = AudioKey("evicted", "p1")
    try:
        original = jobs.submit(key, requested=False)
        ready = jobs.wait(original.job_id, 2)
        assert ready.status == "ready"
        cached = cache.store.get_audio(key)
        assert cached is not None
        Path(cached["path"]).unlink()
        cache.reconcile()
        assert jobs.get(original.job_id).status == "evicted"
        assert jobs.get(original.job_id).error_code == "cache_evicted"

        replacement = jobs.submit(key, requested=False)
        assert replacement.job_id != original.job_id
        assert jobs.wait(replacement.job_id, 2).status == "ready"
    finally:
        jobs.shutdown()


def test_cookie_provider_snapshot_is_captured_once_and_never_persisted(cache: Cache) -> None:
    entered = threading.Event()
    release = threading.Event()
    snapshots = []

    @dataclass(frozen=True)
    class Snapshot:
        cookie_header: str
        generation: int

    def provider():
        snapshots.append(Snapshot(f"SID=session-{len(snapshots)}", len(snapshots)))
        return snapshots[-1]

    class CapturingExtractor(FakeExtractor):
        def download(self, key, destination, credential_snapshot):
            self.seen_snapshot = credential_snapshot
            return super().download(key, destination, credential_snapshot)

    extractor = CapturingExtractor(entered=entered, release=release)
    jobs = Jobs(cache, extractor, worker_count=1, credential_provider=provider)
    key = AudioKey("snapshot-stable", "p1")
    try:
        first = jobs.submit(key, requested=True)
        assert entered.wait(2)
        duplicate = jobs.submit(key, requested=False)
        assert duplicate.job_id == first.job_id
        assert len(snapshots) == 1
        assert extractor.seen_snapshot == snapshots[0]
        with cache.store.connection() as connection:
            rows = connection.execute("SELECT * FROM jukes_jobs").fetchall()
        assert "session-0" not in repr(rows)
        release.set()
        assert jobs.wait(first.job_id, 2).status == "ready"
    finally:
        release.set()
        jobs.shutdown()


def test_provider_unavailable_uses_anonymous_snapshot(cache: Cache) -> None:
    class CapturingExtractor(FakeExtractor):
        def download(self, key, destination, credential_snapshot):
            self.seen_snapshot = credential_snapshot
            return super().download(key, destination, credential_snapshot)

    extractor = CapturingExtractor()

    def provider():
        raise RuntimeError("provider detail must stay private")

    jobs = Jobs(cache, extractor, worker_count=1, credential_provider=provider)
    try:
        job = jobs.submit(AudioKey("anonymous", "p1"), requested=True)
        assert jobs.wait(job.job_id, 2).status == "ready"
        assert extractor.seen_snapshot is None
    finally:
        jobs.shutdown()


def test_evicted_job_is_not_failed(cache):
    jobs = Jobs(cache, FakeExtractor(), worker_count=1)
    try:
        job = jobs.submit(AudioKey('evict-me', 'p1'), requested=True)
        assert jobs.wait(job.job_id, 2).status == 'ready'
        entry = cache.lookup(job.key)
        entry.path.unlink()
        assert jobs.get(job.job_id).status == 'evicted'
        stats = jobs.stats()
        assert stats['jobs']['evicted'] == 1
        assert stats['jobs']['failed'] == 0
        assert stats['active_workers'] == 0
        assert stats['worker_utilization'] == 0
        assert jobs.submit(job.key, True).job_id != job.job_id
    finally:
        jobs.shutdown()


def test_old_result_eviction_preserves_new_job(cache):
    jobs = Jobs(cache, FakeExtractor(), worker_count=1)
    try:
        old = jobs.submit(AudioKey('replace', 'p1'), True)
        jobs.wait(old.job_id, 2)
        cache.lookup(old.key).path.unlink()
        new = jobs.submit(old.key, True)
        jobs.wait(new.job_id, 2)
        assert jobs.get(old.job_id).status == 'evicted'
        assert jobs.get(new.job_id).status == 'ready'
    finally:
        jobs.shutdown()


def test_evicted_state_migration_preserves_legacy_tables(tmp_path):
    from jukes.store import Store
    path = tmp_path / 'old.db'
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE unrelated (value TEXT); INSERT INTO unrelated VALUES ('keep');")
    db.close()
    store = Store(path)
    with store.transaction() as c:
        c.execute("INSERT INTO jukes_jobs VALUES ('j','v','p',1,'failed','cache_evicted',1,2,0,0,'')")
    store = Store(path)
    with store.connection() as c:
        assert c.execute("SELECT status FROM jukes_jobs WHERE job_id='j'").fetchone()[0] == 'evicted'
        assert c.execute('SELECT value FROM unrelated').fetchone()[0] == 'keep'


def test_migration_upgrades_actual_old_check_constraint(tmp_path):
    from jukes.store import Store
    path=tmp_path/'old-check.db'
    db=sqlite3.connect(path)
    db.execute("CREATE TABLE jukes_jobs(job_id TEXT PRIMARY KEY,video_id TEXT NOT NULL,policy TEXT NOT NULL,requested INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('queued', 'downloading', 'ready', 'failed')),error_code TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,recovery_count INTEGER NOT NULL DEFAULT 0,owner_pid INTEGER NOT NULL DEFAULT 0,owner_start TEXT NOT NULL DEFAULT '')")
    db.execute("INSERT INTO jukes_jobs VALUES ('old','v','p',1,'failed','cache_evicted',1,2,0,0,'')")
    db.commit();db.close()
    store=Store(path)
    with store.connection() as c:
        assert c.execute("SELECT status FROM jukes_jobs WHERE job_id='old'").fetchone()[0]=='evicted'
        assert "'evicted'" in c.execute("SELECT sql FROM sqlite_master WHERE name='jukes_jobs'").fetchone()[0]


def test_job_totals_survive_worker_restart(cache):
    jobs=Jobs(cache,FakeExtractor(),worker_count=1)
    job=jobs.submit(AudioKey('persist','p'),True)
    jobs.wait(job.job_id,2);jobs.shutdown()
    jobs=Jobs(cache,FakeExtractor(),worker_count=1)
    try:
        assert jobs.stats()['jobs']['ready']==1
        assert cache.metrics.snapshot()['lifetime']['job_completed']==1
    finally:jobs.shutdown()


def test_unexpected_extraction_exception_is_redacted_in_logs(cache,caplog):
    jobs=Jobs(cache,FakeExtractor(fail=True),worker_count=1)
    try:
        job=jobs.submit(AudioKey('log-redaction','p'),True)
        assert jobs.wait(job.job_id,2).status=='failed'
        assert 'secret cookie value' not in caplog.text
    finally:jobs.shutdown()


def test_job_queue_and_extraction_timings_both_recorded(cache):
    jobs=Jobs(cache,FakeExtractor(),worker_count=1)
    try:
        job=jobs.submit(AudioKey('timings','p'),True)
        jobs.wait(job.job_id,2)
        latency=cache.metrics.snapshot()['windows']['15m']['latency']
        assert latency['queue']['sample_count']==1
        assert latency['extraction']['sample_count']==1
    finally:jobs.shutdown()


def test_rejected_warmup_promotion_is_not_consumption(cache):
    from dataclasses import replace
    jobs=Jobs(cache,FakeExtractor(output=b'12345'),worker_count=1)
    try:
        job=jobs.submit(AudioKey('oversize-promotion','p'),False)
        jobs.wait(job.job_id,2)
        cache.config=replace(cache.config,requested_limit_bytes=2)
        with pytest.raises(CacheCapacityError):jobs.submit(job.key,True)
        assert cache.metrics.snapshot()['lifetime'].get('warmup_consumed',0)==0
    finally:jobs.shutdown()


def test_restart_after_publication_finalizes_without_redownload(cache):
    jobs=Jobs(cache,FakeExtractor(),autostart=False)
    job=jobs.submit(AudioKey('published-before-crash','p'),True)
    reservation=cache.reserve(job.key,requested=True)
    reservation.write(b'published-audio')
    entry=cache.complete(job.key,reservation.path,True)
    original=entry.path.read_bytes()
    restarted=Jobs(cache,FakeExtractor(output=b'corrupt'),worker_count=1)
    try:
        recovered=restarted.wait(job.job_id,2)
        assert recovered.status=='ready'
        assert entry.path.read_bytes()==original
        totals=cache.metrics.snapshot()['lifetime']
        assert totals['job_completed']==1 and totals.get('job_failed',0)==0
    finally:restarted.shutdown()
