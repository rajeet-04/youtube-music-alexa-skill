"""Shared download coordinator.

One job per public audio key, executed by a bounded worker pool. Requested work
is dispatched before warmups and warmups may never occupy every worker. State
transitions are persisted in ``jukes_jobs`` so an interrupted process can
requeue its unfinished work once. HTTP callers only wait; they never own or
cancel a download.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable

from .cache import Cache, _process_start
from .extractor import ExtractionError
from .models import AudioKey, CacheCapacityError

log = logging.getLogger(__name__)

ACTIVE = ("queued", "downloading")
TERMINAL_RETENTION_SECONDS = 24 * 3600


class JobQueueFull(RuntimeError):
    code = "queue_full"
    retryable = True


@dataclass(frozen=True)
class Job:
    job_id: str
    key: AudioKey
    requested: bool
    status: str
    created_at: float
    updated_at: float
    error_code: str | None = None
    recovery_count: int = 0

    def __repr__(self) -> str:
        return f"Job({self.job_id}, {self.key.video_id}, {self.status}, {self.error_code})"


class Jobs:
    def __init__(
        self,
        cache: Cache,
        extractor: Any,
        *,
        worker_count: int = 4,
        max_warmup_workers: int | None = None,
        max_queue_size: int = 100,
        credential_provider: Callable[[], Any] | None = None,
        autostart: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cache = cache
        self.extractor = extractor
        self.worker_count = max(1, worker_count)
        self.max_warmup_workers = (
            max_warmup_workers if max_warmup_workers is not None else max(1, self.worker_count - 1)
        )
        self.max_queue_size = max_queue_size
        self._credential_provider = credential_provider
        self._clock = clock
        self._cond = threading.Condition()
        self._jobs: dict[str, Job] = {}
        self._by_key: dict[AudioKey, str] = {}
        self._requested_queue: deque[str] = deque()
        self._warmup_queue: deque[str] = deque()
        self._counted_warmup: set[str] = set()
        self._active_warmups = 0
        self._running = 0
        self._stopping = False
        self._threads: list[threading.Thread] = []
        self._pid = cache._pid
        self._owner_start = cache._process_start_token
        if autostart:
            self.start()

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        with self._cond:
            if self._threads:
                return
            self._recover()
            for index in range(self.worker_count):
                thread = threading.Thread(target=self._worker, name=f"jukes-worker-{index}", daemon=True)
                self._threads.append(thread)
                thread.start()

    def shutdown(self, grace_seconds: float = 5.0) -> None:
        """Stop admission, wait a bounded grace, then kill child process groups.

        Unfinished jobs keep their persisted ``queued``/``downloading`` status
        so the next process recovers them.
        """
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        deadline = time.monotonic() + grace_seconds
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            terminate = getattr(self.extractor, "terminate_all", None)
            if terminate is not None:
                terminate()
            for thread in self._threads:
                thread.join(2.0)

    # -- persistence ---------------------------------------------------
    def _persist(self, job: Job, *, new: bool = False) -> None:
        with self.cache.store.transaction() as connection:
            if new:
                connection.execute(
                    "INSERT INTO jukes_jobs (job_id, video_id, policy, requested, status, error_code, "
                    "created_at, updated_at, recovery_count, owner_pid, owner_start) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (job.job_id, job.key.video_id, job.key.policy, int(job.requested), job.status,
                     job.error_code, job.created_at, job.updated_at, job.recovery_count,
                     self._pid, self._owner_start),
                )
            else:
                connection.execute(
                    "UPDATE jukes_jobs SET requested = ?, status = ?, error_code = ?, updated_at = ?, "
                    "recovery_count = ?, owner_pid = ?, owner_start = ? WHERE job_id = ?",
                    (int(job.requested), job.status, job.error_code, job.updated_at,
                     job.recovery_count, self._pid, self._owner_start, job.job_id),
                )

    def _set(self, job_id: str, **changes: Any) -> Job:
        job = replace(self._jobs[job_id], updated_at=self._clock(), **changes)
        self._jobs[job_id] = job
        self._persist(job)
        self._cond.notify_all()
        return job

    def _recover(self) -> None:
        """Requeue jobs left unfinished by a dead process (once each)."""
        with self.cache.store.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM jukes_jobs WHERE status IN ('queued', 'downloading') ORDER BY created_at"
            ).fetchall()
        for row in rows:
            owner_pid, owner_start = int(row["owner_pid"]), str(row["owner_start"])
            if owner_pid > 0 and owner_start and (owner_pid, owner_start) != (self._pid, self._owner_start) \
                    and _process_start(owner_pid) == owner_start:
                continue  # another live process owns it
            key = AudioKey(row["video_id"], row["policy"])
            job = Job(row["job_id"], key, bool(row["requested"]), "queued", row["created_at"],
                      self._clock(), None, int(row["recovery_count"]))
            self.cache.release_reservation(key)
            if job.recovery_count >= 1:
                job = replace(job, status="failed", error_code="interrupted")
                self._jobs[job.job_id] = job
                self._persist(job)
                continue
            job = replace(job, recovery_count=job.recovery_count + 1)
            self._jobs[job.job_id] = job
            try:
                self.cache.reserve(key, requested=job.requested)
            except CacheCapacityError as error:
                job = replace(job, status="failed", error_code=error.code)
                self._jobs[job.job_id] = job
                self._persist(job)
                continue
            self._persist(job)
            self._by_key[key] = job.job_id
            self._enqueue(job)

    # -- public API ----------------------------------------------------
    def submit(self, key: AudioKey, requested: bool) -> Job:
        with self._cond:
            if self._stopping:
                raise JobQueueFull("coordinator is shutting down")
            existing = self._refresh(self._by_key.get(key))
            if existing is not None:
                return self._attach(existing, requested)
            if len(self._requested_queue) + len(self._warmup_queue) >= self.max_queue_size:
                raise JobQueueFull("download queue is full")
            now = self._clock()
            entry = self.cache.lookup(key)
            if entry is not None:
                if requested:
                    self.cache.promote(key)
                job = Job(uuid.uuid4().hex, key, requested, "ready", now, now)
                self._jobs[job.job_id] = job
                self._by_key[key] = job.job_id
                self._persist(job, new=True)
                return job
            self.cache.reserve(key, requested=requested)  # raises CacheCapacityError
            job = Job(uuid.uuid4().hex, key, requested, "queued", now, now)
            self._jobs[job.job_id] = job
            self._by_key[key] = job.job_id
            self._persist(job, new=True)
            self._enqueue(job)
            return job

    def _attach(self, job: Job, requested: bool) -> Job:
        """Duplicate submission: share the job, promoting it when requested."""
        if not requested or job.requested:
            if job.status == "ready" and requested:
                self.cache.promote(job.key)
            return job
        if job.status in ACTIVE:
            self.cache.reserve(job.key, requested=True)  # moves reserved bytes once
            if job.status == "queued" and job.job_id in self._warmup_queue:
                self._warmup_queue.remove(job.job_id)
                self._requested_queue.append(job.job_id)
            elif job.job_id in self._counted_warmup:
                self._counted_warmup.discard(job.job_id)
                self._active_warmups -= 1
            job = self._set(job.job_id, requested=True)
            return job
        self.cache.promote(job.key)
        return self._set(job.job_id, requested=True)

    def _refresh(self, job_id: str | None) -> Job | None:
        """Drop ready/failed jobs whose cache entry vanished; return live job."""
        if job_id is None:
            return None
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.status == "ready" and self.cache.lookup(job.key) is None:
            self._set(job_id, status="failed", error_code="cache_evicted")
            job = self._jobs[job_id]
        if job.status == "failed":
            if self._by_key.get(job.key) == job_id:
                del self._by_key[job.key]
            return None
        return job

    def has_active(self, key: AudioKey) -> bool:
        """True while a queued or running job already covers ``key``."""
        with self._cond:
            job = self._jobs.get(self._by_key.get(key, ""))
            return job is not None and job.status in ACTIVE

    def get(self, job_id: str) -> Job | None:
        with self._cond:
            if job_id not in self._jobs:
                return None
            self._refresh(job_id)
            return self._jobs.get(job_id)

    def wait(self, job_id: str, timeout: float) -> Job | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                job = self._jobs.get(job_id)
                if job is None or job.status not in ACTIVE:
                    return self.get(job_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return job
                self._cond.wait(remaining)

    def prune(self, now: float | None = None) -> int:
        """Forget terminal jobs older than 24 hours (audio is unaffected)."""
        cutoff = (now if now is not None else self._clock()) - TERMINAL_RETENTION_SECONDS
        with self._cond:
            stale = [j.job_id for j in self._jobs.values()
                     if j.status in ("ready", "failed") and j.updated_at < cutoff]
            for job_id in stale:
                job = self._jobs.pop(job_id)
                if self._by_key.get(job.key) == job_id:
                    del self._by_key[job.key]
        with self.cache.store.transaction() as connection:
            connection.execute(
                "DELETE FROM jukes_jobs WHERE status IN ('ready', 'failed') AND updated_at < ?", (cutoff,))
        return len(stale)

    # -- dispatch ------------------------------------------------------
    def _enqueue(self, job: Job) -> None:
        (self._requested_queue if job.requested else self._warmup_queue).append(job.job_id)
        self._cond.notify_all()

    def _next_job(self) -> str | None:
        if self._requested_queue:
            return self._requested_queue.popleft()
        if self._warmup_queue and self._active_warmups < self.max_warmup_workers:
            job_id = self._warmup_queue.popleft()
            self._active_warmups += 1
            self._counted_warmup.add(job_id)
            return job_id
        return None

    def _worker(self) -> None:
        while True:
            with self._cond:
                job_id = None
                while not self._stopping:
                    job_id = self._next_job()
                    if job_id is not None:
                        break
                    self._cond.wait()
                if self._stopping:
                    return
                self._running += 1
                job = self._set(job_id, status="downloading")
            try:
                self._run(job)
            finally:
                with self._cond:
                    self._running -= 1
                    if job_id in self._counted_warmup:
                        self._counted_warmup.discard(job_id)
                        self._active_warmups -= 1
                    self._cond.notify_all()

    def _snapshot(self) -> Any:
        if self._credential_provider is None:
            return None
        try:
            return self._credential_provider()
        except Exception:  # noqa: BLE001 - provider detail must stay private
            log.warning("credential provider unavailable; using anonymous extraction")
            return None

    def _run(self, job: Job) -> None:
        key = job.key
        error_code: str | None = None
        try:
            reservation = self.cache.reserve(key, requested=self._current(job.job_id).requested)
            snapshot = self._snapshot()
            result = self.extractor.download(key, reservation, snapshot)
            requested = self._current(job.job_id).requested
            self.cache.complete(key, result.path, requested=requested)
        except ExtractionError as error:
            error_code = error.code
        except CacheCapacityError as error:
            error_code = error.code
        except Exception:  # noqa: BLE001 - never surface extractor internals
            log.exception("download job %s failed", job.job_id)
            error_code = "extraction_failed"
        if error_code is not None:
            self.cache.release_reservation(key)
        with self._cond:
            if self._stopping and error_code is not None:
                return  # leave persisted state recoverable
            if error_code is None:
                self._set(job.job_id, status="ready", error_code=None)
            else:
                self._set(job.job_id, status="failed", error_code=error_code)

    def _current(self, job_id: str) -> Job:
        with self._cond:
            return self._jobs[job_id]
