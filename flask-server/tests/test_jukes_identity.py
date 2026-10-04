from __future__ import annotations

from hashlib import sha256
import json

import pytest
from cryptography.fernet import Fernet

from jukes.credentials import Credentials
from jukes.identity import Identity, InvalidInstallationToken
from jukes.store import Store


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.value = now

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "jukes.sqlite3")


def test_installation_token_is_random_and_only_its_digest_is_persisted(store):
    identity = Identity(store)

    first = identity.issue()
    second = identity.issue()

    assert first != second
    assert len(first) >= 40
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT token_digest FROM jukes_installations ORDER BY created_at, installation_id"
        ).fetchall()
    digests = {row["token_digest"] for row in rows}
    assert sha256(first.encode("utf-8")).hexdigest() in digests
    assert sha256(second.encode("utf-8")).hexdigest() in digests
    persisted = json.dumps([dict(row) for row in rows])
    assert first not in persisted
    assert second not in persisted


def test_invalid_and_revoked_installation_tokens_are_rejected(store):
    identity = Identity(store)
    token = identity.issue()
    installation = identity.authenticate(token)

    with pytest.raises(InvalidInstallationToken):
        identity.authenticate("unknown-token")

    assert identity.revoke(installation.installation_id) is True
    with pytest.raises(InvalidInstallationToken):
        identity.authenticate(token)


def test_token_only_installation_expires_after_thirty_idle_days(store):
    clock = Clock()
    identity = Identity(store, clock=clock)
    token = identity.issue()

    clock.advance(30 * 24 * 60 * 60)

    with pytest.raises(InvalidInstallationToken):
        identity.authenticate(token)
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jukes_installations").fetchone()[0] == 0


def test_connected_installation_expires_after_180_idle_days_and_deletes_credentials(store):
    clock = Clock()
    identity = Identity(store, clock=clock)
    credentials = Credentials(
        store,
        Fernet.generate_key(),
        account_probe=lambda bundle: True,
        clock=clock,
    )
    token = identity.issue()
    installation_id = identity.authenticate(token).installation_id
    credentials.replace(
        installation_id,
        {"cookie": "SAPISID=account-session", "account_index": 0},
    )

    clock.advance(180 * 24 * 60 * 60)

    with pytest.raises(InvalidInstallationToken):
        identity.authenticate(token)
    with store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM jukes_user_credentials WHERE installation_id = ?",
            (installation_id,),
        ).fetchone()[0] == 0
