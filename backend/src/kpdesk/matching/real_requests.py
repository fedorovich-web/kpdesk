"""Offline intake of separately sourced procurement requests with split provenance.

This module verifies LOCAL source file hashes and split consistency, not claimed
real-world origin, reuse rights, identity, redaction, or human quality labels.
No DB writes, model training, external calls, or hidden gold inference.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from rapidfuzz import fuzz

from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.source_evidence import (
    AUDITED_XLSX_QUANTITY_LAYOUTS,
    extract_xlsx_row_amount,
    locate_source_excerpt,
    normalize_source_quote,
)

_MAX_DATASET_BYTES = 5_000_000
_MAX_SOURCE_BYTES = 16_000_000
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,118}\.(?:txt|csv|pdf|xlsx|docx)$")
_PII_EMAIL = re.compile(r"(?<![\w.-])[\w.+-]+@[A-Za-z\d.-]+\.[A-Za-z]{2,}", re.ASCII)
_PII_PHONE = re.compile(r"(?<!\d)(?:\+7|8)[\s(\)-]*\d{3}[\s)\-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")


class XlsxQuantityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quantity: int = Field(gt=0, le=1_000_000)
    unit_original: str | None = Field(min_length=1, max_length=80)
    unit_cell: str | None = Field(min_length=3, max_length=150)
    quantity_cell: str = Field(min_length=3, max_length=150)
    verified_as: Literal["raw_adjacent_cells"]


class RequestIntakeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    origin_declared: Literal["customer_received", "public_procurement", "synthetic"]
    source_batch_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    buyer_group_id: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"
    )
    source_file: str
    source_sha256: str
    source_quote: str = Field(min_length=12, max_length=2000)
    quantity_evidence: XlsxQuantityEvidence | None = None
    request_text: str = Field(min_length=12, max_length=2000)
    captured_at: datetime
    rights_status: Literal["review_required"]
    privacy_status: Literal["review_required"]

    @field_validator("source_file")
    @classmethod
    def safe_source_file(cls, value: str) -> str:
        if ".." in value or not _FILE_RE.fullmatch(value):
            raise ValueError("Unsafe source file path or extension.")
        return value

    @field_validator("source_sha256")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        if not _HASH_RE.fullmatch(value):
            raise ValueError("Source checksum must be lowercase SHA256.")
        return value

    @field_validator("captured_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Captured date must have a timezone.")
        return value

    @field_validator("source_quote", "request_text")
    @classmethod
    def no_obvious_pii(cls, value: str) -> str:
        if not value.strip() or _PII_EMAIL.search(value) or _PII_PHONE.search(value):
            raise ValueError("Request text contains obvious personal contact PII.")
        return value


@dataclass(frozen=True, slots=True)
class RequestIntake:
    source_sha256: str
    records: tuple[RequestIntakeRecord, ...]
    independently_verified: bool = False
    training_approved: bool = False
    source_excerpts_verified: bool = False


class RequestSplitManifest(TypedDict):
    schema_version: str
    dataset_sha256: str
    seed: int
    partitions: dict[str, list[str]]
    group_counts: dict[str, int]
    origin_status: str
    rights_status: str
    privacy_status: str
    independent_test_verified: bool
    benchmark_approved: bool
    training_approved: bool


def _read_source(directory: Path, filename: str, expected_sha: str) -> None:
    source = directory / filename
    if source.is_symlink() or not source.is_file():
        raise ValueError("Request source file is missing or is a symbolic link.")
    size = source.stat().st_size
    if size < 1 or size > _MAX_SOURCE_BYTES:
        raise ValueError("Request source file size outside supported limits.")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for part in iter(lambda: stream.read(65536), b""):
            digest.update(part)
    if digest.hexdigest() != expected_sha:
        raise ValueError("Request source file SHA256 checksum mismatch.")


def load_request_intake(dataset: Path, sources: Path) -> RequestIntake:
    """Require source evidence but never claim self-declared origin as confirmed."""
    if not dataset.is_file() or not 1 <= dataset.stat().st_size <= _MAX_DATASET_BYTES:
        raise ValueError("Intake dataset missing or exceeds size limits.")
    raw = dataset.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Request intake must be valid UTF-8 JSON.") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "records"}
        or payload["schema_version"] != "real-request-intake-v1"
        or not isinstance(payload["records"], list)
        or not 1 <= len(payload["records"]) <= 1000
    ):
        raise ValueError("Request intake schema mismatch.")
    try:
        records = tuple(RequestIntakeRecord.model_validate(row) for row in payload["records"])
    except ValidationError as exc:
        raise ValueError("Request intake source record validation failed: " + str(exc)) from exc
    ids: set[str] = set()
    source_groups: dict[str, str] = {}
    normalized_groups: dict[str, str] = {}
    seen_request_texts: list[tuple[str, str]] = []
    batch_origins: dict[str, str] = {}
    batch_buyers: dict[str, str] = {}
    checked: set[tuple[str, str]] = set()
    for record in records:
        if record.request_id in ids:
            raise ValueError("Duplicate request ID.")
        ids.add(record.request_id)
        if normalize_source_quote(record.request_text) != normalize_source_quote(record.source_quote):
            raise ValueError("request_text must be a verbatim source_quote excerpt.")
        locate_source_excerpt(
            sources / record.source_file, record.source_quote,
            expected_sha256=record.source_sha256,
        )
        if record.quantity_evidence is not None:
            layout = AUDITED_XLSX_QUANTITY_LAYOUTS.get(record.source_file)
            if layout is None:
                raise ValueError("XLSX quantity evidence not permitted for this source layout.")
            expected_amount = extract_xlsx_row_amount(
                sources / record.source_file, record.source_quote,
                expected_sha256=record.source_sha256,
                title_column=layout[0], quantity_column=layout[1], unit_column=layout[2],
            )
            if record.quantity_evidence.model_dump() != expected_amount:
                raise ValueError("Declared quantity evidence does not match source adjacent cells.")
        owner = source_groups.setdefault(record.source_sha256, record.source_batch_id)
        if owner != record.source_batch_id:
            raise ValueError("Request source document leaks across source batch groups.")
        key = normalize_text(record.request_text)
        previous = normalized_groups.setdefault(key, record.source_batch_id)
        if previous != record.source_batch_id:
            raise ValueError("Duplicate request text leakage across batches.")
        if len(key) >= 32 and any(
            other_group != record.source_batch_id and len(other_text) >= 32
            and fuzz.ratio(key, other_text) >= 98.0
            for other_text, other_group in seen_request_texts
        ):
            raise ValueError("Near-duplicate request leakage across source groups.")
        seen_request_texts.append((key, record.source_batch_id))
        buyer = record.buyer_group_id or record.source_batch_id
        old_buyer = batch_buyers.setdefault(record.source_batch_id, buyer)
        if old_buyer != buyer:
            raise ValueError("One source batch must belong to one buyer group.")
        origin = batch_origins.setdefault(record.source_batch_id, record.origin_declared)
        if origin != record.origin_declared:
            raise ValueError("Mixed declared origins within one request source batch.")
        source_key = (record.source_file, record.source_sha256)
        if source_key not in checked:
            _read_source(sources, record.source_file, record.source_sha256)
            checked.add(source_key)
    return RequestIntake(
        source_sha256=hashlib.sha256(raw).hexdigest(), records=records,
        source_excerpts_verified=True,
    )


def build_request_split_manifest(intake: RequestIntake, *, seed: int = 42) -> RequestSplitManifest:
    """Group entire source batches into deterministic train/validation/locked test IDs.

    This split is a preparation artifact, not evidence the test has never been
    accessed or a security boundary against people reading local raw files.
    """
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("Split seed outside permitted integer range.")
    groups: dict[str, list[str]] = defaultdict(list)
    for item in intake.records:
        groups[item.buyer_group_id or item.source_batch_id].append(item.request_id)
    if len(groups) < 5:
        raise ValueError("At least five independent request source groups are required.")
    ordered = sorted(groups)
    random.Random(seed).shuffle(ordered)
    holdout_count = max(1, len(ordered) // 5)
    train = ordered[:len(ordered) - 2 * holdout_count]
    validation = ordered[len(ordered) - 2 * holdout_count:-holdout_count]
    test = ordered[-holdout_count:]
    partitions = {
        "train": sorted(request for group in train for request in groups[group]),
        "validation": sorted(request for group in validation for request in groups[group]),
        "test": sorted(request for group in test for request in groups[group]),
    }
    return {
        "schema_version": "real-request-split-v1",
        "dataset_sha256": intake.source_sha256,
        "seed": seed,
        "partitions": partitions,
        "group_counts": {name: len(group_ids) for name, group_ids in zip(
            ("train", "validation", "test"), (train, validation, test), strict=True
        )},
        "origin_status": "declared_only_not_independently_verified",
        "rights_status": "review_required",
        "privacy_status": "review_required",
        "independent_test_verified": False,
        "benchmark_approved": False,
        "training_approved": False,
    }


def verify_request_split_manifest(
    dataset: Path, sources: Path, split_manifest: Mapping[str, object],
) -> bool:
    intake = load_request_intake(dataset, sources)
    seed = split_manifest.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("Invalid split manifest seed.")
    expected = build_request_split_manifest(intake, seed=seed)
    if split_manifest != expected:
        raise ValueError("Split manifest mismatch: dataset source or partition changed.")
    return True
