"""A93 safety tests for external file-based review signer provisioning."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kpdesk.catalog.review_signing_keyfile import (
    ReviewSigningKeyUnavailable,
    load_review_signing_key,
    parse_review_signing_key,
)

_KEY = "ab" * 32


def _envelope(epoch: int = 9, key: str = _KEY) -> bytes:
    return json.dumps({"epoch": epoch, "key_hex": key}).encode("utf-8")


def test_signer_secret_is_not_exposed_by_repr() -> None:
    credential = parse_review_signing_key(_envelope(), expected_epoch=9)
    assert credential.epoch == 9
    assert credential.key == bytes.fromhex(_KEY)
    assert _KEY not in repr(credential)
    assert "b'\\xab" not in repr(credential)


@pytest.mark.parametrize("bad_payload", [
    b"", b"not-json", b"\xff", b"[]", b"null", b"{}", b"x" * 257,
    b'{"epoch":9,"epoch":9,"key_hex":"' + _KEY.encode() + b'"}',
    b'{"epoch":true,"key_hex":"' + _KEY.encode() + b'"}',
    json.dumps({"epoch": 9, "key_hex": "AB" * 32}).encode(),
    json.dumps({"epoch": 9, "key_hex": "00" * 16}).encode(),
    json.dumps({"epoch": 9, "key_hex": _KEY, "unknown": 1}).encode(),
    _envelope(epoch=8),
])
def test_invalid_secret_envelopes_fail_closed(bad_payload: bytes) -> None:
    with pytest.raises(ReviewSigningKeyUnavailable, match="unavailable") as exc:
        parse_review_signing_key(bad_payload, expected_epoch=9)
    assert _KEY not in str(exc.value)
    assert _KEY not in repr(exc.value)


@pytest.mark.parametrize("epoch", [0, -1, 2**63, True, "9"])
def test_invalid_expected_epoch_fails_closed(epoch: object) -> None:
    with pytest.raises(ReviewSigningKeyUnavailable):
        parse_review_signing_key(_envelope(), expected_epoch=epoch)  # type: ignore[arg-type]


def test_load_provisioned_regular_file(tmp_path: Path) -> None:
    secret = tmp_path / "review-secret.json"
    secret.write_bytes(_envelope())
    if os.name == "posix":
        secret.chmod(0o600)
    credential = load_review_signing_key(secret, expected_epoch=9)
    assert credential.key == bytes.fromhex(_KEY)
    with pytest.raises(ReviewSigningKeyUnavailable):
        load_review_signing_key(secret, expected_epoch=10)


def test_missing_file_rejected_without_exposing_path(tmp_path: Path) -> None:
    filename = tmp_path / "secret-should-not-appear.json"
    with pytest.raises(ReviewSigningKeyUnavailable) as exc:
        load_review_signing_key(filename, expected_epoch=9)
    assert str(filename) not in str(exc.value)


def test_nonabsolute_path_and_directory_rejected(tmp_path: Path) -> None:
    for path in (Path("signing.json"), tmp_path):
        with pytest.raises(ReviewSigningKeyUnavailable):
            load_review_signing_key(path, expected_epoch=9)


def test_oversized_file_rejected(tmp_path: Path) -> None:
    secret = tmp_path / "oversized.json"
    secret.write_bytes(_envelope() + b" " * 300)
    if os.name == "posix":
        secret.chmod(0o600)
    with pytest.raises(ReviewSigningKeyUnavailable):
        load_review_signing_key(secret, expected_epoch=9)


def test_world_readable_file_rejected_on_posix(tmp_path: Path) -> None:
    secret = tmp_path / "too-public.json"
    secret.write_bytes(_envelope())
    secret.chmod(0o644)
    if os.name == "posix":
        with pytest.raises(ReviewSigningKeyUnavailable):
            load_review_signing_key(secret, expected_epoch=9)
    else:
        assert load_review_signing_key(secret, expected_epoch=9).epoch == 9
