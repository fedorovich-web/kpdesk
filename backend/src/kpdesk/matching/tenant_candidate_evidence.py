"""Bounded, original-source-verified ZIP evidence for authenticated human reviews.

No persistence of source text, no rights certification or gold export.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from kpdesk.matching.real_requests import load_request_intake
from kpdesk.matching.request_annotations import _sha, _tasks
from kpdesk.matching.request_candidate_queue import verify_request_candidate_queue
from kpdesk.matching.schema import CatalogProduct

_ARCHIVE_MAX = 8_000_000
_TOTAL_MAX = 24_000_000
_SOURCE = re.compile(r"sources/[A-Za-z0-9][A-Za-z0-9_.-]{0,118}\.(?:txt|csv|pdf|xlsx|docx)\Z")
_FIXED = frozenset(("dataset.json", "split.json", "catalog.json", "queue.json"))


@dataclass(frozen=True, slots=True)
class VerifiedCandidateEvidence:
    queue_sha256: str
    queue: dict[str, Any]
    tasks: dict[str, dict[str, Any]]
    intake_sha256: str
    catalog_sha256: str
    split_sha256: str
    catalog: dict[str, CatalogProduct]
    source_sha256s: frozenset[str]


def verify_candidate_archive(payload: bytes) -> VerifiedCandidateEvidence:
    if not isinstance(payload, bytes) or not 0 < len(payload) <= _ARCHIVE_MAX:
        raise ValueError("Candidate original evidence bundle size invalid.")
    with tempfile.TemporaryDirectory(prefix="kpdesk-candidate-review-") as directory:
        root = Path(directory)
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                members = archive.infolist()
                names = [item.filename for item in members]
                if not 5 <= len(members) <= 204 or len(set(names)) != len(names):
                    raise ValueError("Expected four JSON documents and bounded sources.")
                if set(names) & _FIXED != _FIXED:
                    raise ValueError("Required evidence files are missing.")
                total = 0
                for member in members:
                    name = member.filename
                    if (
                        name not in _FIXED and _SOURCE.fullmatch(name) is None
                        or member.is_dir()
                        or member.flag_bits & 0x1
                        or (member.external_attr >> 16) & 0o170000 == 0o120000
                        or member.file_size < 1
                        or member.file_size > 16_000_000
                        or member.compress_size < 1
                        or member.file_size > member.compress_size * 250
                    ):
                        raise ValueError("Unsafe original evidence ZIP entry.")
                    total += member.file_size
                    if total > _TOTAL_MAX:
                        raise ValueError("Evidence extraction budget exceeded.")
                    target = root / name
                    target.parent.mkdir(exist_ok=True)
                    with archive.open(member) as source, target.open("xb") as output:
                        count = 0
                        for block in iter(lambda: source.read(65536), b""):
                            count += len(block)
                            if count > member.file_size or count > 16_000_000:
                                raise ValueError("ZIP member expanded unexpectedly.")
                            output.write(block)
                        if count != member.file_size:
                            raise ValueError("ZIP member size inconsistent.")
        except (zipfile.BadZipFile, RuntimeError, OSError) as error:
            raise ValueError("Unreadable original source archive.") from error

        def load_document(name: str, limit: int) -> dict[str, Any]:
            path = root / name
            if path.stat().st_size > limit:
                raise ValueError("Candidate evidence JSON too large.")
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("Candidate evidence document must be an object.")
            return data

        dataset = load_document("dataset.json", 5_000_000)
        manifest = load_document("split.json", 200_000)
        catalog_body = load_document("catalog.json", 5_000_000)
        queue = load_document("queue.json", 2_000_000)
        if set(catalog_body) != {"catalog"} or not isinstance(catalog_body["catalog"], list):
            raise ValueError("Typed catalog list is required.")
        try:
            products = [
                CatalogProduct.model_validate(product) for product in catalog_body["catalog"]
            ]
        except ValidationError as error:
            raise ValueError("Candidate catalog products invalid.") from error
        catalog = {product.sku: product for product in products}
        if len(catalog) != len(products):
            raise ValueError("Duplicate source-bound product SKUs.")
        intake = load_request_intake(root / "dataset.json", root / "sources")
        if hashlib.sha256((root / "dataset.json").read_bytes()).hexdigest() != intake.source_sha256:
            raise ValueError("Candidate intake digest inconsistent.")
        if dataset.get("schema_version") != "real-request-intake-v1":
            raise ValueError("Invalid candidate intake schema.")
        expected_sources = {f"sources/{record.source_file}" for record in intake.records}
        if set(names) - _FIXED != expected_sources:
            raise ValueError("Evidence bundle has missing or unexpected original sources.")
        verify_request_candidate_queue(intake, manifest, catalog, queue)
        return VerifiedCandidateEvidence(
            queue_sha256=_sha(queue), queue=queue, tasks=_tasks(queue),
            intake_sha256=intake.source_sha256,
            catalog_sha256=queue["catalog_sha256"],
            split_sha256=queue["frozen_split_sha256"],
            catalog=catalog,
            source_sha256s=frozenset(record.source_sha256 for record in intake.records),
        )
