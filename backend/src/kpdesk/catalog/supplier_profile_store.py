"""Local developer-only append-only store for reviewed supplier layout profiles.

Do NOT mount as a publicly accessible SaaS user-profile store: a tenant-scoped
PostgreSQL repository with authenticated sessions, audited writes and RLS is
required before release. This offline store proves versioning and safe replay.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, fields
from pathlib import Path
from typing import Literal, cast

from kpdesk.catalog.supplier_json import _unique_object
from kpdesk.catalog.supplier_profiles import (
    PriceBasis,
    ReviewedSupplierProfile,
    _profile_checksum,
)


class AmbiguousSupplierProfiles(ValueError):
    """Multiple reviewed versions for one source layout must be resolved."""


def _profile_location(
    root: Path, organization_id: uuid.UUID,
    supplier_id: str, fingerprint: str,
) -> Path:
    if (
        not isinstance(organization_id, uuid.UUID)
        or not 1 <= len(supplier_id) <= 120 or not supplier_id.isascii()
        or any(not (x.isalnum() or x in "._-") for x in supplier_id)
        or supplier_id in (".", "..") or ".." in supplier_id
        or len(fingerprint) != 64
        or any(ch not in "0123456789abcdef" for ch in fingerprint)
    ):
        raise ValueError("Invalid supplier profile storage key.")
    return root / str(organization_id) / supplier_id / fingerprint


def save_local_reviewed_profile(root: Path, profile: ReviewedSupplierProfile) -> Path:
    """Write-once atomic local evidence; exact duplicates are idempotent."""
    target_dir = _profile_location(
        root, profile.organization_id, profile.supplier_id, profile.fingerprint,
    )
    if (
        len(profile.profile_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in profile.profile_sha256)
    ):
        raise ValueError("Invalid profile integrity identifier.")
    target = target_dir / (profile.profile_sha256 + ".json")
    if root.is_symlink() or any(
        (root / part).is_symlink() for part in (
            str(profile.organization_id),
            str(profile.organization_id) + "/" + profile.supplier_id,
        )
    ):
        raise ValueError("Symlinked local profile storage is not allowed.")
    document = asdict(profile)
    document["organization_id"] = str(profile.organization_id)
    document["approved_by"] = str(profile.approved_by)
    packed = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    # Even local cached profiles cannot exceed a strict bounded file size.
    if len(packed.encode("utf-8")) > 15_000:
        raise ValueError("Profile document outside storage limits.")
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        existing = target.read_bytes()
        if existing != packed.encode("utf-8"):
            raise ValueError("Existing immutable supplier profile changed on disk.") from error
        return target
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as sink:
            sink.write(packed)
            sink.flush()
            os.fsync(sink.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _validate_loaded_document(data: object) -> ReviewedSupplierProfile:
    if not isinstance(data, dict):
        raise TypeError("Invalid stored supplier profile JSON object.")
    expected = {record.name for record in fields(ReviewedSupplierProfile)}
    if set(data) != expected:
        raise ValueError("Unknown or missing stored supplier profile fields.")
    try:
        org = uuid.UUID(data["organization_id"])
        reviewer = uuid.UUID(data["approved_by"])
        entries = data["columns"]
        if not isinstance(entries, list) or not 2 <= len(entries) <= 7:
            raise ValueError("Malformed stored mapping entries.")
        columns: list[tuple[str, int | str]] = []
        for value in entries:
            if (
                not isinstance(value, list)
                or len(value) != 2 or not isinstance(value[0], str)
                or type(value[1]) not in (str, int)
            ):
                raise ValueError("Malformed stored supplier field.")
            columns.append((value[0], value[1]))
        if len({name for name, _ in columns}) != len(columns):
            raise ValueError("Duplicate canonical profile fields.")
        profile = ReviewedSupplierProfile(
            org, cast(str, data["supplier_id"]),
            cast(str, data["fingerprint"]),
            cast(str, data["source_format"]),
            tuple(columns), cast(int | None, data["header_row"]),
            cast(str | None, data["sheet"]),
            cast(str | None, data["delimiter"]),
            cast(str | None, data["encoding"]),
            cast(str | None, data["items_key"]),
            cast(Literal["RUB"] | None, data["confirmed_currency"]),
            cast(PriceBasis, data["price_basis"]),
            reviewer, cast(str, data["approved_source_sha256"]),
            cast(str, data["review_evidence"]),
            cast(str, data["profile_sha256"]),
        )
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError("Malformed stored profile identity.") from error
    document = asdict(profile)
    document.pop("profile_sha256")
    document["organization_id"] = str(profile.organization_id)
    document["approved_by"] = str(profile.approved_by)
    if _profile_checksum(document) != profile.profile_sha256:
        raise ValueError("Stored supplier profile checksum mismatch.")
    return profile


def load_local_reviewed_profile(
    root: Path, *, organization_id: uuid.UUID,
    supplier_id: str, fingerprint: str,
) -> ReviewedSupplierProfile | None:
    """Require an unambiguous reviewed version for one tenant/supplier/layout."""
    target_dir = _profile_location(root, organization_id, supplier_id, fingerprint)
    if not target_dir.exists():
        return None
    if root.is_symlink() or target_dir.is_symlink():
        raise ValueError("Symlinked profile storage rejected.")
    files = sorted(target_dir.glob("*.json"))
    if len(files) > 1:
        raise AmbiguousSupplierProfiles(
            "Several approved versions exist for this layout; choose one explicitly."
        )
    if not files:
        return None
    source = files[0]
    if source.is_symlink() or source.stat().st_size > 15_000:
        raise ValueError("Unsafe profile storage content.")
    try:
        data = json.loads(
            source.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Invalid stored profile document.") from error
    profile = _validate_loaded_document(data)
    if (
        profile.organization_id != organization_id
        or profile.supplier_id != supplier_id
        or profile.fingerprint != fingerprint
        or source.name != profile.profile_sha256 + ".json"
    ):
        raise ValueError("Stored supplier profile identity mismatch.")
    return profile
