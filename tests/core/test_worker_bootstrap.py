import base64
import json
from dataclasses import FrozenInstanceError

import pytest

from core.worker_bootstrap import (
    WorkerBootstrap,
    WorkerBootstrapError,
    decode_bootstrap,
    encode_bootstrap,
)


def bootstrap(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    return WorkerBootstrap(
        secret=b"k" * 32,
        audit_database=(tmp_path / "audit.db").resolve(),
        allowed_roots=(root.resolve(),),
        policy_version=3,
    )


def test_bootstrap_round_trip_is_deterministic_and_immutable(tmp_path):
    value = bootstrap(tmp_path)
    encoded = encode_bootstrap(value)

    assert decode_bootstrap(encoded) == value
    assert encode_bootstrap(value) == encoded
    assert encoded.endswith(b"\n")
    with pytest.raises(FrozenInstanceError):
        value.policy_version = 4


def test_bootstrap_encodes_secret_without_plaintext_or_environment_fields(tmp_path):
    value = bootstrap(tmp_path)
    encoded = encode_bootstrap(value)
    data = json.loads(encoded)

    assert b"k" * 32 not in encoded
    assert base64.urlsafe_b64decode(data["secret"] + "==") == b"k" * 32
    assert "environment" not in data
    assert "argv" not in data


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(extra=True),
        lambda data: data.update(version=2),
        lambda data: data.update(secret="bad"),
        lambda data: data.update(policy_version=0),
        lambda data: data.update(allowed_roots=[]),
        lambda data: data.update(audit_database="relative.db"),
    ],
)
def test_bootstrap_rejects_invalid_fields_values_and_paths(tmp_path, mutation):
    data = json.loads(encode_bootstrap(bootstrap(tmp_path)))
    mutation(data)

    with pytest.raises(WorkerBootstrapError):
        decode_bootstrap(json.dumps(data).encode() + b"\n")


def test_bootstrap_rejects_oversize_duplicate_keys_and_invalid_utf8(tmp_path):
    with pytest.raises(WorkerBootstrapError):
        decode_bootstrap(b"x" * 65537)
    with pytest.raises(WorkerBootstrapError):
        decode_bootstrap(b'{"version":1,"version":1}\n')
    with pytest.raises(WorkerBootstrapError):
        decode_bootstrap(b"\xff\n")
