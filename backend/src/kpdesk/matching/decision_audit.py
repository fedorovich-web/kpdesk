"""Read-only safety audit of *decisions*, not candidate ranking.

Uses the existing, provenance-tagged ML label format and the actual deterministic
production auto-draft implementation. Never trains, adjusts thresholds, or calls a
model provider. Rates are diagnostics on user-provided labels, not production SLAs.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kpdesk.matching.cli import load_dataset
from kpdesk.matching.schema import CatalogProduct
from kpdesk.matching.training import LabeledQuery, split_groups
from kpdesk.quotes.auto_draft import build_auto_draft


@dataclass(frozen=True, slots=True)
class DecisionAudit:
    total_queries: int
    positive_queries: int
    no_match_queries: int
    auto_selected: int
    correct_auto_selected: int
    false_auto_selected: int
    false_auto_selected_no_match: int
    abstained: int
    correct_abstained_no_match: int
    selection_precision: float | None
    auto_selection_coverage: float | None
    false_selection_rate: float | None
    false_no_match_rate: float | None
    abstain_reasons: dict[str, int]


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def audit_auto_selection(
    queries: Sequence[LabeledQuery],
    products: Sequence[CatalogProduct],
) -> DecisionAudit:
    """Evaluate one request at a time against gold relevant SKU sets.

    A selected SKU outside the reviewed gold set is a *false automatic
    selection*, even if the SKU is otherwise hard-constraint compatible.
    Zero auto-selections must never be reported as 100% precision.
    """
    seen_ids: set[str] = set()
    available = {product.sku for product in products}
    if len(available) != len(products):
        raise ValueError("Duplicate catalog SKU.")
    positives = no_match = selected = correct = false = false_no_match = 0
    abstained = abstained_no_match = 0
    reasons: Counter[str] = Counter()
    for query in queries:
        if query.query_id in seen_ids:
            raise ValueError("Duplicate evaluation query ID.")
        seen_ids.add(query.query_id)
        if not query.relevant_skus.issubset(available):
            raise ValueError("Gold label references unknown SKU.")
        if query.relevant_skus:
            positives += 1
        else:
            no_match += 1

        draft = build_auto_draft([query.request], list(products))
        line = draft.lines[0]
        if line.status == "auto_selected":
            selected += 1
            if line.sku in query.relevant_skus:
                correct += 1
            else:
                false += 1
                if not query.relevant_skus:
                    false_no_match += 1
        else:
            abstained += 1
            if not query.relevant_skus:
                abstained_no_match += 1
            for reason in line.reason_codes:
                reasons[reason] += 1

    count = len(queries)
    return DecisionAudit(
        total_queries=count,
        positive_queries=positives,
        no_match_queries=no_match,
        auto_selected=selected,
        correct_auto_selected=correct,
        false_auto_selected=false,
        false_auto_selected_no_match=false_no_match,
        abstained=abstained,
        correct_abstained_no_match=abstained_no_match,
        selection_precision=_ratio(correct, selected),
        auto_selection_coverage=_ratio(selected, count),
        false_selection_rate=_ratio(false, count),
        false_no_match_rate=_ratio(false_no_match, no_match),
        abstain_reasons=dict(sorted(reasons.items())),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="KPDesk safety audit: false automatic selections on reviewed SKU labels"
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--split", choices=("validation", "test"), default="validation")
    parser.add_argument(
        "--final-holdout", action="store_true",
        help="Acknowledge that the held-out test split must not guide tuning",
    )
    parser.add_argument(
        "--report", type=Path,
        help="Optional output: created atomically exclusively, never overwrites previous audits",
    )
    args = parser.parse_args(argv)
    if args.split == "test" and (not args.final_holdout or args.report is None):
        raise ValueError("A final holdout evaluation requires --final-holdout and --report.")
    dataset = load_dataset(args.dataset)
    split = split_groups(dataset.queries, seed=args.seed)
    cases = split.validation if args.split == "validation" else split.test
    stats = audit_auto_selection(cases, list(dataset.products.values()))
    report: dict[str, Any] = {
        "dataset_sha256": dataset.source_sha256,
        "dataset_provenance": dataset.provenance_kind,
        "independent_labels": dataset.independent_labels,
        "evaluation_set": args.split,
        "seed": args.seed,
        "train_groups": len({case.query_group_id for case in split.train}),
        "validation_groups": len({case.query_group_id for case in split.validation}),
        "test_groups": len({case.query_group_id for case in split.test}),
        "production_benchmark": False,
        "production_ready": False,
        "manual_approval_required": True,
        "rates_are_conditional_on_reviewed_labels": True,
        "metrics": asdict(stats),
    }
    if args.report is not None:
        # x mode intentionally refuses overwriting an existing held-out report.
        with args.report.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
