"""Offline request→SKU human opinion packets, not tenant-authenticated gold.

Always verify original source files, frozen split and the candidate queue before
accepting any self-attested reviewer decisions. Output is create-only.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.matching.real_requests import load_request_intake
from kpdesk.matching.request_annotations import (
    build_review_template,
    compare_review_batches,
    validate_review_batch,
)
from kpdesk.matching.request_candidate_queue import verify_request_candidate_queue
from scripts.prepare_real_requests import _read_catalog, _read_manifest


def _read_reviews(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 2_000_000:
        raise ValueError("Human review file missing, redirected or excessive.")
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data or len(data) > 200:
        raise ValueError("Human review batch must be a bounded nonempty list.")
    if any(not isinstance(item, dict) for item in data):
        raise TypeError("Human review records must be objects.")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline human candidate review with original-source verification.",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("template", "validate", "compare"):
        command = commands.add_parser(action)
        command.add_argument("--dataset", type=Path, required=True)
        command.add_argument("--sources", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--catalog", type=Path, required=True)
        command.add_argument("--queue", type=Path, required=True)
        if action == "template":
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--first", type=Path, required=True)
        if action == "compare":
            command.add_argument("--second", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    # Re-read original local bytes; never trust a claimed request provenance,
    # arbitrary request ID, renamed SKU, or an edited locked test partition.
    intake = load_request_intake(args.dataset, args.sources)
    frozen = _read_manifest(args.manifest)
    catalog = _read_catalog(args.catalog)
    queue = _read_manifest(args.queue, max_bytes=2_000_000)
    verify_request_candidate_queue(intake, frozen, catalog, queue)

    if args.action == "template":
        template = build_review_template(queue)
        output: object = template
        report: dict[str, object] = {
            "unfilled_tasks": len(template),
            "human_review_authenticated": False,
            "independent_gold": False, "training_approved": False,
        }
    elif args.action == "validate":
        reviewer, decisions = validate_review_batch(queue, _read_reviews(args.first))
        report = {
            "reviewed_requests": len(decisions),
            "self_attested_reviewer": reviewer,
            "human_review_authenticated": False,
            "independent_gold": False, "training_approved": False,
        }
        output = None
    else:
        consensus = compare_review_batches(
            queue, _read_reviews(args.first), _read_reviews(args.second),
        )
        output = consensus
        conflicts = consensus["conflicting_request_ids"]
        assert isinstance(conflicts, list)
        report = {
            "reviewed_requests": consensus["reviewed_request_count"],
            "agreement_count": consensus["agreement_count"],
            "conflict_count": len(conflicts),
            "human_review_authenticated": False,
            "independent_gold": False, "training_approved": False,
        }
    if args.action != "validate":
        # Never create parent directories here: output location must already
        # be intentionally selected, with source texts in private storage.
        with args.output.open("x", encoding="utf-8") as file:
            json.dump(output, file, ensure_ascii=False, sort_keys=True, indent=2)
            file.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
