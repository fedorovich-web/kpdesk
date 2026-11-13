"""Fail-closed reader for externally provisioned reviewed-publication signing keys.

This does not install keys in PostgreSQL or activate production publication.
The operator must provision the same epoch/key through an independently
authorized owner-only procedure before any worker can use it.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from kpdesk.catalog.review_signing import ReviewSigningCredential

_MAX_BYTES = 256
_KEY_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ReviewSigningKeyUnavailable(RuntimeError):
    """A signer is unavailable; details and secret bytes must not reach APIs."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate key field")
        result[name] = value
    return result


def parse_review_signing_key(raw: bytes, *, expected_epoch: int) -> ReviewSigningCredential:
    """Decode a bounded, strict JSON secret envelope, without logging input."""
    if (
        type(expected_epoch) is not int
        or not 1 <= expected_epoch <= 2**63 - 1
        or type(raw) is not bytes
        or not 1 <= len(raw) <= _MAX_BYTES
    ):
        raise ReviewSigningKeyUnavailable("Review signing key unavailable.")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"epoch", "key_hex"}
            or type(payload["epoch"]) is not int
            or payload["epoch"] != expected_epoch
            or type(payload["key_hex"]) is not str
            or _KEY_PATTERN.fullmatch(payload["key_hex"]) is None
        ):
            raise ValueError("Invalid or stale signing key")
        return ReviewSigningCredential(
            epoch=expected_epoch, key=bytes.fromhex(payload["key_hex"]),
        )
    except (UnicodeError, ValueError, TypeError, KeyError):
        raise ReviewSigningKeyUnavailable("Review signing key unavailable.") from None


def load_review_signing_key(path: Path, *, expected_epoch: int) -> ReviewSigningCredential:
    """Read an owner-restricted regular file; reject symlinks and stale epochs.

    Docker/Linux secret mounts must be configured with 0400 or 0600 permissions.
    On Windows, mode/ACL security must be enforced by the deploying environment;
    this reader does not claim to validate Windows ACLs.
    """
    if not isinstance(path, Path) or not path.is_absolute():
        raise ReviewSigningKeyUnavailable("Review signing key unavailable.")
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
            raise OSError("Symbolic links are not permitted")
        descriptor = os.open(path, flags)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > _MAX_BYTES:
            raise OSError("Not a bounded regular secret file")
        if os.name == "posix" and (
            file_stat.st_mode & 0o077
            or file_stat.st_uid not in (0, getattr(os, "geteuid", lambda: -1)())
        ):
            raise OSError("Secret file owner or permissions are unsafe")
        raw = os.read(descriptor, _MAX_BYTES + 1)
    except OSError:
        raise ReviewSigningKeyUnavailable("Review signing key unavailable.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return parse_review_signing_key(raw, expected_epoch=expected_epoch)
