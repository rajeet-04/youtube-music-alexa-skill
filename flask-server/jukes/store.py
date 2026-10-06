"""Small SQLite boundary for the persistent JUKES cache state."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import AudioKey


class Store:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _create_schema(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jukes_audio (
                    video_id TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    path TEXT NOT NULL,
                    pool TEXT NOT NULL CHECK (pool IN ('requested', 'warmup')),
                    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
                    completed_at REAL NOT NULL,
                    expires_at REAL,
                    last_used REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'ready' CHECK (state = 'ready'),
                    PRIMARY KEY (video_id, policy)
                );
                CREATE INDEX IF NOT EXISTS jukes_audio_pool_lru
                    ON jukes_audio(pool, last_used);
                CREATE INDEX IF NOT EXISTS jukes_audio_expiry
                    ON jukes_audio(pool, expires_at);
                CREATE TABLE IF NOT EXISTS jukes_reservations (
                    video_id TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    pool TEXT NOT NULL CHECK (pool IN ('requested', 'warmup')),
                    requested INTEGER NOT NULL CHECK (requested IN (0, 1)),
                    reserved_bytes INTEGER NOT NULL CHECK (reserved_bytes >= 0),
                    expected_size INTEGER,
                    partial_path TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('reserved', 'publishing', 'interrupted')),
                    created_at REAL NOT NULL,
                    owner_pid INTEGER NOT NULL DEFAULT 0,
                    owner_start TEXT NOT NULL DEFAULT '',
                    publish_path TEXT,
                    size_bytes INTEGER,
                    completed_at REAL,
                    expires_at REAL,
                    PRIMARY KEY (video_id, policy)
                );
                CREATE INDEX IF NOT EXISTS jukes_reservations_pool
                    ON jukes_reservations(pool, created_at);
                CREATE TABLE IF NOT EXISTS jukes_leases (
                    lease_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    owner_start TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jukes_leases_key
                    ON jukes_leases(video_id, policy);
                CREATE TABLE IF NOT EXISTS jukes_jobs (
                    job_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    requested INTEGER NOT NULL CHECK (requested IN (0, 1)),
                    status TEXT NOT NULL CHECK (status IN ('queued', 'downloading', 'ready', 'failed')),
                    error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    owner_pid INTEGER NOT NULL DEFAULT 0,
                    owner_start TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS jukes_jobs_status
                    ON jukes_jobs(status, updated_at);
                """
            )
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            job_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='jukes_jobs'").fetchone()[0]
            if "'evicted'" not in job_sql:
                indexes = [r[0] for r in connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name='jukes_jobs' AND type='index' AND sql IS NOT NULL")]
                connection.execute("ALTER TABLE jukes_jobs RENAME TO jukes_jobs_old")
                connection.execute(job_sql.replace("'ready', 'failed'", "'ready', 'failed', 'evicted'"))
                connection.execute("INSERT INTO jukes_jobs SELECT * FROM jukes_jobs_old")
                connection.execute("DROP TABLE jukes_jobs_old")
                for index in indexes:
                    connection.execute(index)
            connection.execute("UPDATE jukes_jobs SET status='evicted' WHERE status='failed' AND error_code='cache_evicted'")
            connection.execute("CREATE TABLE IF NOT EXISTS jukes_job_results(job_id TEXT PRIMARY KEY, completed_at REAL NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO jukes_job_results SELECT j.job_id,a.completed_at "
                "FROM jukes_jobs j JOIN jukes_audio a ON j.video_id=a.video_id AND j.policy=a.policy "
                "WHERE j.status='ready' AND a.completed_at<=j.updated_at")
            reservation_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jukes_reservations)").fetchall()
            }
            if "owner_pid" not in reservation_columns:
                connection.execute(
                    "ALTER TABLE jukes_reservations ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0"
                )
            if "owner_start" not in reservation_columns:
                connection.execute(
                    "ALTER TABLE jukes_reservations ADD COLUMN owner_start TEXT NOT NULL DEFAULT ''"
                )

    def get_audio(self, key: AudioKey) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM jukes_audio WHERE video_id = ? AND policy = ?",
                (key.video_id, key.policy),
            ).fetchone()
        return dict(row) if row else None

    def all_audio(self) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM jukes_audio").fetchall()
        return [dict(row) for row in rows]

    def all_reservations(self) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM jukes_reservations").fetchall()
        return [dict(row) for row in rows]

    def get_reservation(self, key: AudioKey) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM jukes_reservations WHERE video_id = ? AND policy = ?",
                (key.video_id, key.policy),
            ).fetchone()
        return dict(row) if row else None

    def all_leases(self) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM jukes_leases").fetchall()
        return [dict(row) for row in rows]

    def begin_publication(
        self,
        key: AudioKey,
        *,
        pool: str,
        source_path: Path,
        destination_path: Path,
        size_bytes: int,
        completed_at: float,
        expires_at: float | None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO jukes_reservations (
                    video_id, policy, pool, requested, reserved_bytes, expected_size,
                    partial_path, state, created_at, publish_path, size_bytes,
                    completed_at, expires_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, 'publishing', ?, ?, ?, ?, ?)
                ON CONFLICT(video_id, policy) DO UPDATE SET
                    pool = excluded.pool,
                    requested = excluded.requested,
                    reserved_bytes = 0,
                    partial_path = excluded.partial_path,
                    state = 'publishing',
                    publish_path = excluded.publish_path,
                    size_bytes = excluded.size_bytes,
                    completed_at = excluded.completed_at,
                    expires_at = excluded.expires_at
                """,
                (
                    key.video_id,
                    key.policy,
                    pool,
                    int(pool == "requested"),
                    size_bytes,
                    str(source_path),
                    completed_at,
                    str(destination_path),
                    size_bytes,
                    completed_at,
                    expires_at,
                ),
            )

    def finalize_publication(self, key: AudioKey) -> None:
        with self.transaction() as connection:
            reservation = connection.execute(
                "SELECT * FROM jukes_reservations WHERE video_id = ? AND policy = ? AND state = 'publishing'",
                (key.video_id, key.policy),
            ).fetchone()
            if reservation is None:
                return
            connection.execute(
                """
                INSERT INTO jukes_audio (
                    video_id, policy, path, pool, size_bytes, completed_at,
                    expires_at, last_used, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready')
                ON CONFLICT(video_id, policy) DO UPDATE SET
                    path = excluded.path,
                    pool = excluded.pool,
                    size_bytes = excluded.size_bytes,
                    completed_at = excluded.completed_at,
                    expires_at = excluded.expires_at,
                    last_used = excluded.last_used,
                    state = 'ready'
                """,
                (
                    key.video_id,
                    key.policy,
                    reservation["publish_path"],
                    reservation["pool"],
                    reservation["size_bytes"],
                    reservation["completed_at"],
                    reservation["expires_at"],
                    reservation["completed_at"],
                ),
            )
            connection.execute(
                "DELETE FROM jukes_reservations WHERE video_id = ? AND policy = ?",
                (key.video_id, key.policy),
            )
