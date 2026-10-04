"""Encrypted, installation-scoped YouTube browser authentication bundles."""

from __future__ import annotations

from dataclasses import dataclass, field
import binascii
import json
import time
from typing import Callable, Mapping

from cryptography.fernet import Fernet, InvalidToken

from .identity import ensure_identity_schema, installation_is_idle_expired
from .store import Store


MAX_COOKIE_LENGTH = 65_536
MAX_ACCOUNT_INDEX = 99


class CredentialError(RuntimeError):
    """Base class for safe credential lifecycle errors."""


class InvalidCredentialKey(CredentialError):
    """Raised when a configured Fernet key has an invalid representation."""


class MissingCredentialKey(CredentialError):
    """Raised when encrypted credential access is required without a key."""


class CredentialDecryptionError(CredentialError):
    """Raised when stored ciphertext cannot be authenticated with the configured key."""


class CredentialValidationError(CredentialError):
    """Raised when uncached upstream account validation fails."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class InvalidCredentialBundle(CredentialError, ValueError):
    """Raised when a caller supplies fields outside the supported auth bundle."""


class InstallationNotFound(CredentialError):
    """Raised when a credential operation is not scoped to a live installation."""


@dataclass(frozen=True)
class CredentialBundle:
    cookie: str = field(repr=False)
    account_index: int
    generation: int


@dataclass(frozen=True)
class ConnectionStatus:
    connected: bool
    credential_generation: int
    connected_at: float | None = None


class Credentials:
    """Validate, encrypt, and retrieve credentials for one installation at a time."""

    def __init__(
        self,
        store: Store,
        encryption_key: bytes | str | None,
        *,
        account_probe: Callable[[Mapping[str, object]], object] | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self._clock = clock
        self._account_probe = account_probe
        ensure_identity_schema(store)
        self._fernet: Fernet | None = None
        if encryption_key is not None:
            key_bytes = (
                encryption_key.encode("ascii")
                if isinstance(encryption_key, str)
                else encryption_key
            )
            try:
                self._fernet = Fernet(key_bytes)
            except (TypeError, ValueError, binascii.Error):
                raise InvalidCredentialKey("Invalid credential encryption key") from None
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jukes_user_credentials (
                    installation_id TEXT PRIMARY KEY
                        REFERENCES jukes_installations(installation_id) ON DELETE CASCADE,
                    encrypted_payload BLOB NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    connected_at REAL NOT NULL
                )
                """
            )

    @staticmethod
    def _normalize(bundle: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(bundle, Mapping):
            raise InvalidCredentialBundle("Credential bundle must be an object")
        allowed_fields = {"cookie", "account_index"}
        if set(bundle) - allowed_fields:
            raise InvalidCredentialBundle("Credential bundle contains unsupported fields")

        cookie = bundle.get("cookie")
        if not isinstance(cookie, str) or not cookie.strip():
            raise InvalidCredentialBundle("YouTube cookie is required")
        if len(cookie) > MAX_COOKIE_LENGTH or "\r" in cookie or "\n" in cookie:
            raise InvalidCredentialBundle("YouTube cookie is invalid")

        account_index = bundle.get("account_index", 0)
        if (
            isinstance(account_index, bool)
            or not isinstance(account_index, int)
            or not 0 <= account_index <= MAX_ACCOUNT_INDEX
        ):
            raise InvalidCredentialBundle("YouTube account index is invalid")
        return {"cookie": cookie, "account_index": account_index}

    def _require_fernet(self) -> Fernet:
        if self._fernet is None:
            raise MissingCredentialKey("Credential encryption key is unavailable")
        return self._fernet

    def _decode(self, encrypted_payload: bytes | str) -> dict[str, object]:
        fernet = self._require_fernet()
        try:
            plaintext = fernet.decrypt(
                encrypted_payload.encode("ascii")
                if isinstance(encrypted_payload, str)
                else bytes(encrypted_payload)
            )
            decoded = json.loads(plaintext.decode("utf-8"))
        except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise CredentialDecryptionError(
                "Stored YouTube credentials could not be decrypted"
            ) from None
        try:
            normalized = self._normalize(decoded)
        except InvalidCredentialBundle:
            raise CredentialDecryptionError(
                "Stored YouTube credentials could not be decrypted"
            ) from None
        return normalized

    def _installation_row(self, connection, installation_id: str, now: float):
        row = connection.execute(
            "SELECT * FROM jukes_installations WHERE installation_id = ?",
            (installation_id,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        if installation_is_idle_expired(row, now):
            connection.execute(
                "DELETE FROM jukes_installations WHERE installation_id = ?",
                (installation_id,),
            )
            return None
        return row

    def _validate_account(self, bundle: Mapping[str, object]) -> None:
        if self._account_probe is None:
            raise CredentialValidationError(
                "YouTube account validation is unavailable", retryable=True
            )
        try:
            result = self._account_probe(dict(bundle))
        except Exception:
            # Upstream exception text may include request headers or cookies.
            raise CredentialValidationError(
                "YouTube account validation failed", retryable=True
            ) from None
        if not result:
            raise CredentialValidationError("YouTube account was not validated")

    def replace(
        self,
        installation_id: str,
        bundle: Mapping[str, object],
    ) -> ConnectionStatus:
        normalized = self._normalize(bundle)
        fernet = self._require_fernet()

        now = float(self._clock())
        with self.store.transaction() as connection:
            installation = self._installation_row(connection, installation_id, now)
            if installation is None:
                raise InstallationNotFound("Installation is unavailable")
            previous = connection.execute(
                "SELECT encrypted_payload FROM jukes_user_credentials WHERE installation_id = ?",
                (installation_id,),
            ).fetchone()
            if previous is not None:
                # A valid but wrong key must never silently replace the old session.
                self._decode(previous["encrypted_payload"])

        self._validate_account(normalized)
        encrypted_payload = fernet.encrypt(
            json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        connected_at = float(self._clock())

        with self.store.transaction() as connection:
            installation = self._installation_row(connection, installation_id, connected_at)
            if installation is None:
                raise InstallationNotFound("Installation is unavailable")
            previous = connection.execute(
                "SELECT encrypted_payload FROM jukes_user_credentials WHERE installation_id = ?",
                (installation_id,),
            ).fetchone()
            if previous is not None:
                # Recheck inside the write transaction in case another update raced
                # with the network account probe.
                self._decode(previous["encrypted_payload"])
            generation = int(installation["credential_generation"]) + 1
            connection.execute(
                """
                INSERT INTO jukes_user_credentials (
                    installation_id, encrypted_payload, generation, connected_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(installation_id) DO UPDATE SET
                    encrypted_payload = excluded.encrypted_payload,
                    generation = excluded.generation,
                    connected_at = excluded.connected_at
                """,
                (installation_id, encrypted_payload, generation, connected_at),
            )
            connection.execute(
                """
                UPDATE jukes_installations
                SET credential_generation = ?, credential_connected = 1,
                    credential_connected_at = ?
                WHERE installation_id = ?
                """,
                (generation, connected_at, installation_id),
            )
        return ConnectionStatus(True, generation, connected_at)

    def lookup(self, installation_id: str) -> CredentialBundle | None:
        now = float(self._clock())
        with self.store.transaction() as connection:
            installation = self._installation_row(connection, installation_id, now)
            if installation is None or not bool(installation["credential_connected"]):
                return None
            row = connection.execute(
                "SELECT encrypted_payload, generation FROM jukes_user_credentials "
                "WHERE installation_id = ?",
                (installation_id,),
            ).fetchone()
            if row is None:
                return None
            generation = int(installation["credential_generation"])
            if int(row["generation"]) != generation:
                raise CredentialDecryptionError("Stored YouTube credential state is inconsistent")
            encrypted_payload = row["encrypted_payload"]

        decoded = self._decode(encrypted_payload)
        return CredentialBundle(
            cookie=str(decoded["cookie"]),
            account_index=int(decoded["account_index"]),
            generation=generation,
        )

    def status(self, installation_id: str) -> ConnectionStatus:
        now = float(self._clock())
        with self.store.transaction() as connection:
            installation = self._installation_row(connection, installation_id, now)
            if installation is None:
                return ConnectionStatus(False, 0, None)
            credential = connection.execute(
                "SELECT generation, connected_at FROM jukes_user_credentials "
                "WHERE installation_id = ?",
                (installation_id,),
            ).fetchone()
            connected = bool(installation["credential_connected"]) and credential is not None
            if connected and int(credential["generation"]) != int(
                installation["credential_generation"]
            ):
                connected = False
            return ConnectionStatus(
                connected=connected,
                credential_generation=int(installation["credential_generation"]),
                connected_at=float(credential["connected_at"]) if connected else None,
            )

    def delete(self, installation_id: str) -> None:
        """Disconnect one installation and advance its generation for cache invalidation."""
        with self.store.transaction() as connection:
            installation = connection.execute(
                "SELECT credential_generation FROM jukes_installations "
                "WHERE installation_id = ?",
                (installation_id,),
            ).fetchone()
            if installation is None:
                return
            generation = int(installation["credential_generation"]) + 1
            connection.execute(
                "DELETE FROM jukes_user_credentials WHERE installation_id = ?",
                (installation_id,),
            )
            connection.execute(
                """
                UPDATE jukes_installations
                SET credential_generation = ?, credential_connected = 0,
                    credential_connected_at = NULL
                WHERE installation_id = ?
                """,
                (generation, installation_id),
            )
