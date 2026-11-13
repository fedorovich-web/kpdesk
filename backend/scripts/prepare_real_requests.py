"""Local provenance checks and source-batch holdout IDs for procurement requests.

Does not submit source texts to ML/LLM, make remote calls, or certify licenses.
Source files stay on disk, no output content contains request text.

Usage:
 python -m scripts.prepare_real_requests inspect --dataset requests.json --sources sources
 python -m scripts.prepare_real_requests freeze --dataset requests.json --sources sources --output split.json
 python -m scripts.prepare_real_requests verify --dataset requests.json --sources sources --manifest split.json
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from kpdesk.matching.real_requests import (
    build_request_split_manifest,
    load_request_intake,
    verify_request_split_manifest,
)
from kpdesk.matching.request_candidate_queue import (
    build_request_candidate_queue,
    verify_request_candidate_queue,
)
from kpdesk.matching.schema import CatalogProduct


def _read_manifest(path: Path, *, max_bytes: int = 200_000) -> dict[str, object]:
    if (path.is_symlink() or not path.is_file()
            or not 0 < path.stat().st_size <= max_bytes):
        raise ValueError("Local split/queue document is missing or too large.")
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("Split manifest must be a JSON object.")
    return data


def _read_catalog(path: Path) -> dict[str, CatalogProduct]:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 5_000_000:
        raise ValueError("Catalog source missing, redirected or excessive.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"catalog"}:
        raise ValueError("Catalog candidate input must contain only 'catalog'.")
    raw = data["catalog"]
    if not isinstance(raw, list) or not 2 <= len(raw) <= 5000:
        raise ValueError("Candidate catalog size unsupported.")
    try:
        products = [CatalogProduct.model_validate(item) for item in raw]
    except ValidationError as error:
        raise ValueError("Candidate catalog schema invalid.") from error
    indexed = {product.sku: product for product in products}
    if len(indexed) != len(products):
        raise ValueError("Candidate catalog contains duplicate SKUs.")
    return indexed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline request source verification and group-only holdout planning."
    )
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("inspect", "freeze", "verify", "queue", "verify-queue"):
        command = sub.add_parser(name)
        command.add_argument("--dataset", required=True, type=Path)
        command.add_argument("--sources", required=True, type=Path)
        if name == "freeze":
            command.add_argument("--output", required=True, type=Path)
            command.add_argument("--seed", type=int, default=42)
        if name in ("verify", "queue", "verify-queue"):
            command.add_argument("--manifest", required=True, type=Path)
        if name in ("queue", "verify-queue"):
            command.add_argument("--catalog", required=True, type=Path)
        if name == "verify-queue":
            command.add_argument("--queue", required=True, type=Path)
        if name == "queue":
            command.add_argument("--output", required=True, type=Path)
            command.add_argument("--max-requests", type=int, default=50)
            command.add_argument("--candidates-per-request", type=int, default=6)
    args = parser.parse_args(argv)

    intake = load_request_intake(args.dataset, args.sources)
    if args.action == "inspect":
        report: dict[str, object] = {
            "records": len(intake.records),
            "source_groups": len({record.source_batch_id for record in intake.records}),
            "declared_origins": sorted({record.origin_declared for record in intake.records}),
            "source_hashes_checked": True,
            "independently_verified": False,
            "rights_verified": False,
            "privacy_review_complete": False,
            "training_approved": False,
        }
    elif args.action == "freeze":
        manifest = build_request_split_manifest(intake, seed=args.seed)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        report = {
            "partitions": {
                name: len(ids) for name, ids in manifest["partitions"].items()
            },
            "split_consistency_verified": True,
            "benchmark_approved": False,
            "training_approved": False,
        }
    elif args.action == "verify":
        saved_manifest = _read_manifest(args.manifest)
        verify_request_split_manifest(args.dataset, args.sources, saved_manifest)
        report = {
            "split_consistency_verified": True,
            "real_world_provenance_verified": False,
            "independent_test_verified": False,
            "training_approved": False,
        }
    elif args.action == "verify-queue":
        saved_manifest = _read_manifest(args.manifest)
        catalog = _read_catalog(args.catalog)
        queue = _read_manifest(args.queue, max_bytes=2_000_000)
        verify_request_candidate_queue(intake, saved_manifest, catalog, queue)
        report = {
            "candidate_queue_matches_originals": True,
            "rights_verified": False,
            "independent_gold": False,
            "training_approved": False,
        }
    else:
        saved_manifest = _read_manifest(args.manifest)
        catalog = _read_catalog(args.catalog)
        queue = build_request_candidate_queue(
            intake, saved_manifest, catalog,
            candidates_per_request=args.candidates_per_request,
            max_requests=args.max_requests,
        )
        # This packet contains original request text: store locally, create-only.
        # It does not contain rights approval or human labels.
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(queue, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write(chr(10))
        tasks = queue["tasks"]
        assert isinstance(tasks, list)
        report = {
            "unreviewed_tasks": len(tasks),
            "locked_test_requests_excluded": queue["excluded_test_queries"],
            "rights_verified": False,
            "independent_gold": False,
            "training_approved": False,
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
