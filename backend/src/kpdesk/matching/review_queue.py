"""Offline review tasks from genuine supplier XLSX; proposals are never gold.

Candidate product IDs are audit IDs, NOT supplier SKUs or catalog IDs.
The source title of a real product is used to generate a synthetic request,
never impersonating a real customer inquiry or independent test example.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openpyxl import load_workbook
from rapidfuzz import fuzz

from kpdesk.catalog.russian_store_policy import inspect_russian_store_price
from kpdesk.matching.normalize import normalize_text


def _products(path: Path, source: dict[str, Any]) -> list[dict[str, Any]]:
    raw = (path / source["file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise ValueError("Supplier source checksum mismatch.")
    workbook = load_workbook(path / source["file"], read_only=True, data_only=True)
    products: list[dict[str, Any]] = []
    try:
        sheet = workbook.worksheets[0]
        if sheet.max_row > 100_000:
            raise ValueError("Supplier worksheet exceeds supported row limit.")
        for line_no, row in enumerate(
            sheet.iter_rows(max_col=8, values_only=True), start=1
        ):
            if source["source_id"] == "svih_lighting":
                sku, name, price = row[2], row[3], row[4]
                identifiable = isinstance(sku, str) and bool(sku.strip())
            else:
                sku, name, price = row[1], row[2], row[5]
                link = row[3]
                identifiable = (
                    isinstance(link, str)
                    and urlparse(link).hostname == "stroymaterialno.ru"
                )
            if (
                not identifiable
                or not isinstance(name, str) or not name.strip()
                or isinstance(price, bool)
                or not isinstance(price, (int, float)) or price <= 0
            ):
                continue
            title = name.strip()
            identity = hashlib.sha256(
                f"{source['sha256']}:{line_no}:{title}".encode()
            ).hexdigest()[:24]
            products.append({
                "product_id": f"{source['source_id']}:{identity}",
                "source_id": source["source_id"],
                "source_row": line_no,
                "name": title[:500],
                "supplier_sku": sku.strip() if isinstance(sku, str) and sku.strip() else None,
                "reviewed_label": "unknown",
            })
    finally:
        workbook.close()
    return products


def build_review_queue(
    corpus: Path, *, per_source: int = 12, candidate_limit: int = 6
) -> dict[str, Any]:
    """Produce deterministic close-call comparisons, without assigning truth."""
    if not 1 <= per_source <= 100 or not 2 <= candidate_limit <= 20:
        raise ValueError("Review queue size or candidate limit is unsupported.")
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("dataset_type") != "unlabeled_real_supplier_price_format_research":
        raise ValueError("Source provenance is not an unlabeled real supplier corpus.")
    tasks: list[dict[str, Any]] = []
    digests: dict[str, str] = {}
    for source in manifest["sources"]:
        if (
            source.get("provenance") != "real_public_supplier_file"
            or source.get("model_training_authorized") is not False
            or source.get("labels") != "not_human_reviewed"
        ):
            raise ValueError("Unexpected source provenance or training eligibility.")
        source_id = source["source_id"]
        inspect_russian_store_price(corpus / source["file"], source)
        digests[source_id] = source["sha256"]
        entries = _products(corpus, source)
        if len(entries) < 2:
            raise ValueError("Source has too few unreviewed products.")
        positions = [
            (index * len(entries)) // min(len(entries), per_source)
            for index in range(min(len(entries), per_source))
        ]
        for position in positions:
            target = entries[position]
            similar = sorted(
                (other for other in entries if other["product_id"] != target["product_id"]),
                key=lambda candidate: (
                    -fuzz.WRatio(
                        normalize_text(target["name"]), normalize_text(candidate["name"])
                    ),
                    candidate["product_id"],
                ),
            )
            candidates = [target, *similar[:candidate_limit - 1]]
            candidates.sort(key=lambda p: p["product_id"])
            tasks.append({
                "task_id": "review-" + target["product_id"],
                "source_id": source_id,
                "source_file_sha256": source["sha256"],
                "query_group_id": target["product_id"],
                "seed_product_id": target["product_id"],
                "query_origin": "synthetic_from_real_catalog",
                "request_text": "Нужно " + target["name"],
                "review_state": "unreviewed",
                "candidates": candidates,
                "explanation": (
                    "Near-miss retrieval proposals; no product has been "
                    "independently verified as relevant or irrelevant."
                ),
            })
    return {
        "schema_version": "supplier-review-v1",
        "corpus_kind": "real_public_supplier_files",
        "training_approved": False,
        "independent_gold": False,
        "review_state": "unreviewed_proposals",
        "source_sha256": digests,
        "tasks": tasks,
    }


def validate_reviewed_decisions(
    queue: dict[str, Any], decisions: list[dict[str, Any]],
) -> dict[str, int | bool]:
    """Fail closed on incomplete labels, counterfeit IDs or generated-query gold.

    Human attestation is a data-validation gate, not cryptographic proof.
    Real-world benchmark approval additionally requires independently sourced
    customer requests, source-data rights and locked annotation protocol.
    """
    tasks = {task["task_id"]: task for task in queue["tasks"]}
    blind_aliases = {_blind_id(task_id): task_id for task_id in tasks}
    seen: set[str] = set()
    rejected_count = no_match_count = 0
    for record in decisions:
        if not isinstance(record, dict):
            raise TypeError("Invalid reviewer decision.")
        task_id = record.get("task_id")
        if not isinstance(task_id, str):
            raise TypeError("Review references unknown task ID.")
        actual_id = blind_aliases.get(task_id, task_id)
        if actual_id not in tasks:
            raise ValueError("Review references unknown task ID.")
        if actual_id in seen:
            raise ValueError("Review contains duplicate task ID.")
        seen.add(actual_id)
        task = tasks[actual_id]
        if (
            record.get("query_origin") != task["query_origin"]
            or record.get("request_text") != task["request_text"]
        ):
            raise ValueError("Review request origin or source text differs.")
        reviewer = record.get("reviewed_by")
        rationale = record.get("rationale")
        timestamp = record.get("reviewed_at")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("Human reviewer identification is required.")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("Review rationale is required.")
        if not isinstance(timestamp, str):
            raise TypeError("Timezone-aware reviewed_at is required.")
        try:
            reviewed_at = datetime.fromisoformat(timestamp)
        except ValueError as error:
            raise ValueError("Invalid reviewed_at timestamp.") from error
        if reviewed_at.utcoffset() is None:
            raise ValueError("Reviewed timestamp must include a timezone.")
        accepted = record.get("accepted_product_ids")
        rejected = record.get("rejected_product_ids")
        if (
            not isinstance(accepted, list) or not isinstance(rejected, list)
            or not all(isinstance(x, str) for x in [*accepted, *rejected])
        ):
            raise ValueError("Review must specify accepted and rejected candidate IDs.")
        if len(accepted) != len(set(accepted)) or len(rejected) != len(set(rejected)):
            raise ValueError("Review contains duplicate candidate IDs.")
        if set(accepted) & set(rejected):
            raise ValueError("Conflicting accepted/rejected decision.")
        universe = {candidate["product_id"] for candidate in task["candidates"]}
        if (set(accepted) | set(rejected)) - universe:
            raise ValueError("Review references unknown candidate ID outside task.")
        if set(accepted) | set(rejected) != universe:
            raise ValueError("Review incomplete: unreviewed near-miss candidates.")
        no_match = record.get("no_match")
        if type(no_match) is not bool or no_match == bool(accepted):
            raise ValueError("Review must explicitly select positive(s) or no_match.")
        rejected_count += len(rejected)
        no_match_count += no_match
    return {
        "reviewed_count": len(seen),
        "hard_negative_count": rejected_count,
        "no_match_count": no_match_count,
        "independent_gold_count": 0,
        "training_approved": False,
        "independent_benchmark_approved": False,
    }


def verify_review_queue(corpus: Path, queue: dict[str, Any]) -> dict[str, int]:
    """Regenerate review queue against original XLSX; reject modified task/candidate text.

    Local source manifest hashes are asserted against downloaded XLSX content.
    This is an offline data-integrity check, not a cryptographic proof of publisher rights.
    """
    if not isinstance(queue, dict) or queue.get("schema_version") != "supplier-review-v1":
        raise ValueError("Review source schema mismatch.")
    tasks = queue.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Review source tasks missing.")
    counts: dict[str, int] = {}
    candidate_limits: set[int] = set()
    ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("source_id"), str):
            raise TypeError("Review source task shape mismatch.")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or task_id in ids:
            raise ValueError("Review source task identity mismatch.")
        ids.add(task_id)
        candidates = task.get("candidates")
        if not isinstance(candidates, list) or len(candidates) < 2:
            raise ValueError("Review source candidate shape mismatch.")
        counts[task["source_id"]] = counts.get(task["source_id"], 0) + 1
        candidate_limits.add(len(candidates))
    if len(candidate_limits) != 1 or len(set(counts.values())) != 1:
        raise ValueError("Review source task structure mismatch.")
    limit = candidate_limits.pop()
    per_source = max(counts.values())
    expected = build_review_queue(
        corpus, per_source=per_source, candidate_limit=limit,
    )
    if queue != expected:
        raise ValueError("Review source mismatch: queue or original XLSX was tampered.")
    return {"tasks": len(tasks), "verified_originals": len(expected["source_sha256"])}


def _batch(
    queue: dict[str, Any], decisions: list[dict[str, Any]],
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Validate a single independently authored human review batch."""
    if not decisions:
        raise ValueError("Review batch has no human decisions.")
    reviewers: set[str] = set()
    for record in decisions:
        if not isinstance(record, dict) or record.get("reviewed_source") != "human_review":
            raise ValueError("Only human review is eligible for consensus.")
        reviewer = record.get("reviewed_by")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("Human reviewer is missing.")
        reviewers.add(reviewer.strip())
    if len(reviewers) != 1:
        raise ValueError("One review batch must have one human reviewer.")
    validate_reviewed_decisions(queue, decisions)
    return reviewers.pop(), {record["task_id"]: record for record in decisions}


def _verdict(record: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    return (
        tuple(sorted(record["accepted_product_ids"])),
        tuple(sorted(record["rejected_product_ids"])),
        record["no_match"],
    )


def adjudicate_review_batches(
    corpus: Path,
    queue: dict[str, Any],
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
    *,
    adjudications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Double-review agreement and optional independent third-party adjudication.

    Even unanimous annotations of generated title-based requests are NOT
    independent real request benchmark gold or license to train on supplier data.
    """
    verify_review_queue(corpus, queue)
    first_reviewer, a = _batch(queue, first)
    second_reviewer, b = _batch(queue, second)
    if first_reviewer == second_reviewer:
        raise ValueError("Two distinct human reviewers are required.")
    if a.keys() != b.keys():
        raise ValueError("Review batches must cover the same tasks.")
    conflicts: list[str] = []
    consensus: list[dict[str, Any]] = []
    for task_id in sorted(a):
        if _verdict(a[task_id]) != _verdict(b[task_id]):
            conflicts.append(task_id)
            continue
        consensus.append({
            "task_id": task_id,
            "query_origin": a[task_id]["query_origin"],
            "accepted_product_ids": sorted(a[task_id]["accepted_product_ids"]),
            "rejected_product_ids": sorted(a[task_id]["rejected_product_ids"]),
            "no_match": a[task_id]["no_match"],
            "annotation_method": "two_human_reviewers_agreed",
            "reviewer_ids": [first_reviewer, second_reviewer],
        })
    initial_conflicts = len(conflicts)
    if adjudications is not None:
        if not conflicts and adjudications:
            raise ValueError("Third-party adjudication cannot add unrequested tasks.")
        if conflicts:
            adjudicator, third = _batch(queue, adjudications)
            if adjudicator in (first_reviewer, second_reviewer):
                raise ValueError("Third reviewer must be distinct from original reviewers.")
            if set(third) != set(conflicts):
                raise ValueError("Third reviewer must resolve exactly the conflicting tasks.")
            for task_id in sorted(third):
                consensus.append({
                    "task_id": task_id,
                    "query_origin": third[task_id]["query_origin"],
                    "accepted_product_ids": sorted(third[task_id]["accepted_product_ids"]),
                    "rejected_product_ids": sorted(third[task_id]["rejected_product_ids"]),
                    "no_match": third[task_id]["no_match"],
                    "annotation_method": "third_human_reviewer_adjudicated",
                    "reviewer_ids": [first_reviewer, second_reviewer, adjudicator],
                })
            conflicts = []
    consensus.sort(key=lambda record: record["task_id"])
    return {
        "schema_version": "supplier-review-consensus-v1",
        "queue_sha256": hashlib.sha256(
            json.dumps(queue, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "reviewed_task_count": len(a),
        "agreement_count": len(a) - initial_conflicts,
        "adjudicated_count": initial_conflicts - len(conflicts),
        "conflict_count": len(conflicts),
        "unresolved_task_ids": conflicts,
        "consensus": consensus,
        "independent_real_request_gold": False,
        "training_approved": False,
    }


def _blind_id(task_id: str) -> str:
    """Opaque review case ID: the seed product cannot be read from this identifier."""
    return "case-" + hashlib.sha256(f"kpdesk-blind-review-v1:{task_id}".encode()).hexdigest()[:24]


def build_blind_reviewer_packet(
    corpus: Path, queue: dict[str, Any],
) -> dict[str, Any]:
    """Create review-only view without exposing seed labels or grouping keys.

    The generated request text itself is synthetic from a product name, and
    therefore this packet is NOT an independent or blind real-world benchmark.
    """
    checked = verify_review_queue(corpus, queue)
    output = []
    for task in queue["tasks"]:
        output.append({
            "task_id": _blind_id(task["task_id"]),
            "request_text": task["request_text"],
            "query_origin": task["query_origin"],
            "source_id": task["source_id"],
            "candidates": [
                {
                    "product_id": candidate["product_id"],
                    "name": candidate["name"],
                    "supplier_sku": candidate["supplier_sku"],
                    "source_row": candidate["source_row"],
                }
                for candidate in task["candidates"]
            ],
        })
    return {
        "schema_version": "blind-review-packet-v1",
        "human_labels": False,
        "source_count": checked["verified_originals"],
        "tasks": output,
    }
