"""Private installation tokens for optional JUKES personalisation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import secrets
import time
import uuid
from typing import Callable

from .store import Store


TOKEN_ONLY_IDLE_SECONDS = 30 * 24 * 60 * 60
CONNECTED_IDLE_SECONDS = 180 * 24 * 60 * 60


class InvalidInstallationToken(PermissionError):
    """Raised when a bearer token is invalid, revoked, or idle-expired."""


@dataclass(frozen=True)
class Installation:
    installation_id: str
    created_at: float
    last_seen_at: float
    credential_generation: int
    connected: bool


def ensure_identity_schema(store: Store) -> None:
    """Create the prefixed installation table without changing legacy tables."""
    with store.transaction() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jukes_installations (
                installation_id TEXT PRIMARY KEY,
                token_digest TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                revoked_at REAL,
                credential_generation INTEGER NOT NULL DEFAULT 0
                    CHECK (credential_generation >= 0),
                credential_connected INTEGER NOT NULL DEFAULT 0
                    CHECK (credential_connected IN (0, 1)),
                credential_connected_at REAL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS jukes_installations_idle "
            "ON jukes_installations(credential_connected, last_seen_at)"
        )


def installation_is_idle_expired(row: object, now: float) -> bool:
    """Return whether an installation has passed its connected or token-only TTL."""
    connected = bool(row["credential_connected"])
    ttl = CONNECTED_IDLE_SECONDS if connected else TOKEN_ONLY_IDLE_SECONDS
    return now - float(row["last_seen_at"]) >= ttl


class Identity:
    """Issues one-time bearer tokens and persists only their SHA-256 digests."""

    def __init__(self, store: Store, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self._clock = clock
        ensure_identity_schema(store)

    def issue(self) -> str:
        """Create an installation and return its 256-bit bearer token once."""
        token = secrets.token_urlsafe(32)
        now = float(self._clock())
        installation_id = uuid.uuid4().hex
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO jukes_installations (
                    installation_id, token_digest, created_at, last_seen_at
                ) VALUES (?, ?, ?, ?)
                """,
                (installation_id, digest, now, now),
            )
        return token

    def authenticate(self, token: str) -> Installation:
        """Validate a bearer token, touch idle activity, and return its safe identity."""
        if not isinstance(token, str) or not token or len(token) > 512:
            raise InvalidInstallationToken("Invalid installation token")

        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = float(self._clock())
        installation: Installation | None = None
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jukes_installations WHERE token_digest = ?",
                (digest,),
            ).fetchone()
            if row is not None and row["revoked_at"] is None:
                if installation_is_idle_expired(row, now):
                    # The credential table uses ON DELETE CASCADE, so expiry removes
                    # any encrypted session in the same transaction.
                    connection.execute(
                        "DELETE FROM jukes_installations WHERE installation_id = ?",
                        (row["installation_id"],),
                    )
                else:
                    connection.execute(
                        "UPDATE jukes_installations SET last_seen_at = ? "
                        "WHERE installation_id = ?",
                        (now, row["installation_id"]),
                    )
                    installation = Installation(
                        installation_id=str(row["installation_id"]),
                        created_at=float(row["created_at"]),
                        last_seen_at=now,
                        credential_generation=int(row["credential_generation"]),
                        connected=bool(row["credential_connected"]),
                    )

        if installation is None:
            raise InvalidInstallationToken("Invalid installation token")
        return installation

    def revoke(self, installation_id: str) -> bool:
        """Revoke an installation and cascade-delete its credential record."""
        with self.store.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM jukes_installations WHERE installation_id = ?",
                (installation_id,),
            )
        return cursor.rowcount > 0

    def delete(self, installation_id: str) -> bool:
        """Delete an installation; this is the persisted revocation operation."""
        return self.revoke(installation_id)
