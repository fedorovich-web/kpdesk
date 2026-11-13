"""Offline preparation and review validation for unlabeled real supplier prices.

Usage:
  python -m scripts.prepare_real_labeling build --output PATH
  python -m scripts.prepare_real_labeling validate --queue PATH --decisions PATH

Generated requests are synthetic. No model is fit and no dataset is promoted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.matching.review_queue import (
    adjudicate_review_batches,
    build_blind_reviewer_packet,
    build_review_queue,
    validate_reviewed_decisions,
    verify_review_queue,
)

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "ml/data/real/public_supplier_prices"


def _read_json(path: Path) -> dict[str, Any] | list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Review input is missing or larger than configured limit.")
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, (dict, list)):
        raise TypeError("Expected JSON object or array.")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline supplier ML human-label queue.")
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("build", help="Read real local XLSX and write review proposals")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--per-source", type=int, default=18)
    build.add_argument("--candidates", type=int, default=8)
    validate = sub.add_parser("validate", help="Check human decisions; never create model gold")
    validate.add_argument("--queue", type=Path, required=True)
    validate.add_argument("--decisions", type=Path, required=True)
    consensus = sub.add_parser("consensus", help="Independent double-review and arbitration")
    consensus.add_argument("--queue", type=Path, required=True)
    consensus.add_argument("--first", type=Path, required=True)
    consensus.add_argument("--second", type=Path, required=True)
    consensus.add_argument("--adjudications", type=Path)
    consensus.add_argument("--output", type=Path, required=True)
    packet = sub.add_parser("packet", help="Create neutral reviewer copy with opaque case IDs")
    packet.add_argument("--queue", type=Path, required=True)
    packet.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    summary: dict[str, Any]
    if args.action == "build":
        queue = build_review_queue(
            CORPUS, per_source=args.per_source, candidate_limit=args.candidates,
        )
        body = json.dumps(queue, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(body)
        summary = {
            "task_count": len(queue["tasks"]),
            "candidate_pairs": sum(len(task["candidates"]) for task in queue["tasks"]),
            "source_files": len(queue["source_sha256"]),
            "task_queue_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "provenance": "generated_proposals_from_real_public_supplier_catalogs",
            "independent_human_gold": False,
            "model_training_permitted": False,
        }
    elif args.action == "packet":
        queue_json = _read_json(args.queue)
        if not isinstance(queue_json, dict):
            raise TypeError("Review queue must be an object.")
        packet_body = build_blind_reviewer_packet(CORPUS, queue_json)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(packet_body, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
        summary = {
            "tasks": len(packet_body["tasks"]),
            "masked_seed_ids": True,
            "independent_real_request_gold": False,
        }
    elif args.action == "validate":
        queue_json = _read_json(args.queue)
        decisions_json = _read_json(args.decisions)
        if not isinstance(queue_json, dict) or not isinstance(decisions_json, list):
            raise ValueError("Queue must be an object and reviewer decisions an array.")
        verify_review_queue(CORPUS, queue_json)
        summary = validate_reviewed_decisions(queue_json, decisions_json)
    else:
        queue_json = _read_json(args.queue)
        first = _read_json(args.first)
        second = _read_json(args.second)
        third = _read_json(args.adjudications) if args.adjudications else None
        if (not isinstance(queue_json, dict) or not isinstance(first, list)
                or not isinstance(second, list) or (third is not None and not isinstance(third, list))):
            raise ValueError("Review source format mismatch.")
        summary = adjudicate_review_batches(
            CORPUS, queue_json, first, second, adjudications=third,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(summary, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
