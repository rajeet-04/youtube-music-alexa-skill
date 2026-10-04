"""Operator-owned download cookies, encrypted at rest.

These cookies only help yt-dlp extract public audio. They are never used for
recommendations or metadata, so they cannot personalise results.
"""

from __future__ import annotations

import binascii
import time
from dataclasses import dataclass
from typing import Callable

from cryptography.fernet import Fernet, InvalidToken

from .credentials import CredentialError, InvalidCredentialKey, MissingCredentialKey
from .extractor import CredentialSnapshot
from .store import Store

MAX_JAR_BYTES = 1_000_000
MAX_COOKIES = 512
AUTH_COOKIES = {"SAPISID", "__Secure-3PAPISID", "__Secure-3PSID", "SID", "__Secure-1PSID"}
ALLOWED_DOMAINS = ("youtube.com", "google.com")


class CookieFormatError(ValueError):
    """The supplied jar is malformed, oversized or lacks YouTube authentication."""


class CookieValidationError(CredentialError):
    """The upstream account probe rejected the cookies."""


@dataclass(frozen=True)
class CookieStatus:
    connected: bool
    generation: int = 0
    cookie_count: int = 0
    updated_at: float | None = None


def _domain_allowed(domain: str) -> bool:
    host = domain.lstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def parse_netscape(text: str) -> list[tuple[str, ...]]:
    """Return validated cookie rows (7 tab fields; HttpOnly prefix kept)."""
    if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > MAX_JAR_BYTES:
        raise CookieFormatError("cookie file is too large")
    rows: list[tuple[str, ...]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or (stripped.startswith("#") and not stripped.startswith("#HttpOnly_")):
            continue
        fields = line.rstrip("\r\n").split("\t")
        if len(fields) != 7:
            raise CookieFormatError("cookie file is not in Netscape format")
        domain = fields[0].removeprefix("#HttpOnly_")
        if not _domain_allowed(domain):
            continue  # silently drop unrelated sites; only YouTube/Google auth is kept
        if fields[2] != "/" and not fields[2].startswith("/"):
            raise CookieFormatError("cookie path is invalid")
        if fields[3] not in ("TRUE", "FALSE") or fields[1] not in ("TRUE", "FALSE"):
            raise CookieFormatError("cookie flags are invalid")
        if not fields[5] or any(c in fields[5] + fields[6] for c in "\r\n"):
            raise CookieFormatError("cookie name is invalid")
        rows.append(tuple(fields))
    if not rows or len(rows) > MAX_COOKIES:
        raise CookieFormatError("no usable YouTube cookies found")
    if not any(r[5] in AUTH_COOKIES for r in rows):
        raise CookieFormatError("no YouTube authentication cookie found")
    return rows


def serialize(rows: list[tuple[str, ...]]) -> str:
    return "# Netscape HTTP Cookie File\n" + "\n".join("\t".join(r) for r in rows) + "\n"


def cookie_header(rows: list[tuple[str, ...]]) -> str:
    return "; ".join(f"{r[5]}={r[6]}" for r in rows if r[0].removeprefix("#HttpOnly_").lstrip(".").endswith("youtube.com"))


class ServerCookies:
    def __init__(self, store: Store, encryption_key: bytes | str | None, *,
                 probe: Callable[[str], bool] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self._probe = probe
        self._clock = clock
        self._fernet = None
        if encryption_key:
            try:
                self._fernet = Fernet(encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key)
            except (TypeError, ValueError, binascii.Error):
                raise InvalidCredentialKey("invalid credential encryption key") from None
        with self.store.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS jukes_server_cookies (id INTEGER PRIMARY KEY CHECK (id = 1), "
                "payload BLOB NOT NULL, generation INTEGER NOT NULL, cookie_count INTEGER NOT NULL, "
                "updated_at REAL NOT NULL)")

    def _need_key(self) -> Fernet:
        if self._fernet is None:
            raise MissingCredentialKey("credential encryption key is unavailable")
        return self._fernet

    def replace(self, netscape_text: str) -> CookieStatus:
        """Validate, probe upstream, then atomically replace; failures keep the old jar."""
        fernet = self._need_key()
        rows = parse_netscape(netscape_text)
        if self._probe is not None:
            try:
                ok = self._probe(cookie_header(rows))
            except Exception:  # noqa: BLE001 - never echo upstream detail
                raise CookieValidationError("account validation failed") from None
            if not ok:
                raise CookieValidationError("cookies are not signed in")
        payload = fernet.encrypt(serialize(rows).encode("utf-8"))
        now = float(self._clock())
        with self.store.transaction() as connection:
            row = connection.execute("SELECT generation, payload FROM jukes_server_cookies WHERE id = 1").fetchone()
            if row is not None:
                try:  # a wrong key must never silently replace an existing jar
                    fernet.decrypt(bytes(row["payload"]))
                except InvalidToken:
                    raise CredentialError("stored cookies cannot be decrypted with the configured key") from None
            generation = (int(row["generation"]) if row else 0) + 1
            connection.execute(
                "INSERT INTO jukes_server_cookies (id, payload, generation, cookie_count, updated_at) "
                "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, "
                "generation = excluded.generation, cookie_count = excluded.cookie_count, "
                "updated_at = excluded.updated_at", (payload, generation, len(rows), now))
        return CookieStatus(True, generation, len(rows), now)

    def status(self) -> CookieStatus:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT generation, cookie_count, updated_at FROM jukes_server_cookies WHERE id = 1").fetchone()
        if row is None:
            return CookieStatus(False)
        return CookieStatus(True, int(row["generation"]), int(row["cookie_count"]), float(row["updated_at"]))

    def snapshot(self) -> CredentialSnapshot | None:
        """Immutable copy for one job; later replacements never change it."""
        if self._fernet is None:
            return None
        with self.store.connection() as connection:
            row = connection.execute("SELECT payload, generation FROM jukes_server_cookies WHERE id = 1").fetchone()
        if row is None:
            return None
        try:
            text = self._fernet.decrypt(bytes(row["payload"])).decode("utf-8")
        except InvalidToken:
            return None
        return CredentialSnapshot(cookie_header="", generation=int(row["generation"]), cookie_jar_text=text)

    def clear(self) -> None:
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM jukes_server_cookies WHERE id = 1")
