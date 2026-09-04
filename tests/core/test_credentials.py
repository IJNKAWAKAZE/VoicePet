import json

import pytest

import core
from core.credentials import CredentialError, DpapiCredentialStore


def test_credential_types_are_publicly_exported():
    assert core.CredentialError is CredentialError
    assert core.DpapiCredentialStore is DpapiCredentialStore


class FakeProtectionBackend:
    def protect(self, data):
        return b"encrypted:" + data[::-1]

    def unprotect(self, data):
        if not data.startswith(b"encrypted:"):
            raise RuntimeError("invalid ciphertext")
        return data.removeprefix(b"encrypted:")[::-1]


def test_credential_store_encrypts_values_and_updates_atomically(tmp_path):
    path = tmp_path / "credentials.bin"
    store = DpapiCredentialStore(path, backend=FakeProtectionBackend())

    store.set("openai_api_key", "sk-private-value")
    assert store.get("openai_api_key") == "sk-private-value"
    assert b"sk-private-value" not in path.read_bytes()
    assert not path.with_suffix(".tmp").exists()

    store.set("openai_api_key", "sk-replaced-value")
    assert store.get("openai_api_key") == "sk-replaced-value"
    assert store.delete("openai_api_key") is True
    assert store.get("openai_api_key") is None
    assert store.delete("openai_api_key") is False


def test_credential_store_rejects_corruption_without_leaking_ciphertext(tmp_path):
    path = tmp_path / "credentials.bin"
    path.write_bytes(b"private-corrupt-ciphertext")
    store = DpapiCredentialStore(path, backend=FakeProtectionBackend())

    with pytest.raises(CredentialError) as captured:
        store.get("openai_api_key")

    assert "private-corrupt-ciphertext" not in str(captured.value)


def test_credential_plaintext_payload_has_strict_version_and_names(tmp_path):
    path = tmp_path / "credentials.bin"
    backend = FakeProtectionBackend()
    invalid = json.dumps(
        {"version": 1, "credentials": {"unknown": "secret"}}
    ).encode()
    path.write_bytes(backend.protect(invalid))
    store = DpapiCredentialStore(path, backend=backend)

    with pytest.raises(CredentialError, match="字段"):
        store.get("openai_api_key")
