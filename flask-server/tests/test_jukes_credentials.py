from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from jukes.credentials import (
    CredentialDecryptionError,
    CredentialValidationError,
    Credentials,
    InvalidCredentialBundle,
    MissingCredentialKey,
)
from jukes.identity import Identity
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


@pytest.fixture
def installation_id(store):
    token = Identity(store).issue()
    return Identity(store).authenticate(token).installation_id


def test_replacement_probes_uncached_bundle_and_encrypts_it_at_rest(store, installation_id):
    key = Fernet.generate_key()
    calls = []

    def probe(bundle):
        calls.append(dict(bundle))
        return True

    credentials = Credentials(store, key, account_probe=probe)
    bundle = {"cookie": "SAPISID=private-cookie", "account_index": 2}

    status = credentials.replace(installation_id, bundle)

    assert calls == [bundle]
    assert status.connected is True
    assert status.credential_generation == 1
    with store.connection() as connection:
        row = connection.execute(
            "SELECT encrypted_payload FROM jukes_user_credentials WHERE installation_id = ?",
            (installation_id,),
        ).fetchone()
    assert row is not None
    assert b"private-cookie" not in row["encrypted_payload"]
    assert credentials.lookup(installation_id).cookie == "SAPISID=private-cookie"
    assert credentials.lookup(installation_id).account_index == 2


def test_each_replacement_performs_a_new_account_probe(store, installation_id):
    calls = []
    credentials = Credentials(
        store,
        Fernet.generate_key(),
        account_probe=lambda bundle: calls.append(bundle["account_index"]) or True,
    )

    credentials.replace(installation_id, {"cookie": "SAPISID=one", "account_index": 1})
    credentials.replace(installation_id, {"cookie": "SAPISID=two", "account_index": 3})

    assert calls == [1, 3]
    assert credentials.lookup(installation_id).account_index == 3


def test_failed_upstream_validation_preserves_the_previous_credential(store, installation_id):
    key = Fernet.generate_key()
    credentials = Credentials(store, key, account_probe=lambda bundle: True)
    credentials.replace(installation_id, {"cookie": "SAPISID=working", "account_index": 0})

    def unavailable_probe(bundle):
        raise RuntimeError(f"upstream failed while using {bundle['cookie']}")

    reconnect = Credentials(store, key, account_probe=unavailable_probe)
    with pytest.raises(CredentialValidationError) as error:
        reconnect.replace(
            installation_id,
            {"cookie": "SAPISID=secret-new-value", "account_index": 1},
        )

    assert "secret-new-value" not in str(error.value)
    assert reconnect.lookup(installation_id).cookie == "SAPISID=working"
    assert reconnect.status(installation_id).credential_generation == 1


def test_rejected_account_probe_does_not_connect_or_advance_generation(store, installation_id):
    credentials = Credentials(
        store,
        Fernet.generate_key(),
        account_probe=lambda bundle: False,
    )

    with pytest.raises(CredentialValidationError):
        credentials.replace(installation_id, {"cookie": "SAPISID=rejected", "account_index": 0})

    status = credentials.status(installation_id)
    assert status.connected is False
    assert status.credential_generation == 0
    assert credentials.lookup(installation_id) is None


def test_bundle_rejects_caller_selected_origins_and_unrecognized_headers(store, installation_id):
    calls = []
    credentials = Credentials(
        store,
        Fernet.generate_key(),
        account_probe=lambda bundle: calls.append(bundle) or True,
    )

    with pytest.raises(InvalidCredentialBundle):
        credentials.replace(
            installation_id,
            {
                "cookie": "SAPISID=private-cookie",
                "account_index": 0,
                "origin": "https://attacker.invalid",
            },
        )

    assert calls == []
    assert credentials.lookup(installation_id) is None


def test_credentials_are_isolated_by_installation_and_disconnect_advances_generation(store):
    identity = Identity(store)
    first_id = identity.authenticate(identity.issue()).installation_id
    second_id = identity.authenticate(identity.issue()).installation_id
    credentials = Credentials(store, Fernet.generate_key(), account_probe=lambda bundle: True)
    credentials.replace(first_id, {"cookie": "SAPISID=first-user", "account_index": 0})

    assert credentials.lookup(second_id) is None
    assert credentials.status(second_id).connected is False

    credentials.delete(first_id)

    first_status = credentials.status(first_id)
    assert first_status.connected is False
    assert first_status.credential_generation == 2
    assert credentials.lookup(first_id) is None
    assert credentials.lookup(second_id) is None


def test_missing_or_wrong_key_cannot_read_or_replace_existing_credentials(store, installation_id):
    key = Fernet.generate_key()
    original = Credentials(store, key, account_probe=lambda bundle: True)
    original.replace(installation_id, {"cookie": "SAPISID=preserve-me", "account_index": 0})

    missing = Credentials(store, None, account_probe=lambda bundle: True)
    with pytest.raises(MissingCredentialKey):
        missing.lookup(installation_id)
    with pytest.raises(MissingCredentialKey):
        missing.replace(installation_id, {"cookie": "SAPISID=new", "account_index": 0})

    wrong = Credentials(store, Fernet.generate_key(), account_probe=lambda bundle: True)
    with pytest.raises(CredentialDecryptionError):
        wrong.lookup(installation_id)
    with pytest.raises(CredentialDecryptionError):
        wrong.replace(installation_id, {"cookie": "SAPISID=new", "account_index": 0})

    assert original.lookup(installation_id).cookie == "SAPISID=preserve-me"
    assert original.status(installation_id).credential_generation == 1


@pytest.mark.parametrize(
    "bundle",
    [
        {"cookie": "SAPISID=value\r\nOrigin: https://attacker.invalid", "account_index": 0},
        {"cookie": "SAPISID=value", "account_index": True},
        {"cookie": "SAPISID=value", "account_index": -1},
    ],
)
def test_bundle_rejects_header_injection_and_invalid_account_indexes(store, installation_id, bundle):
    credentials = Credentials(store, Fernet.generate_key(), account_probe=lambda _: True)

    with pytest.raises(InvalidCredentialBundle):
        credentials.replace(installation_id, bundle)
