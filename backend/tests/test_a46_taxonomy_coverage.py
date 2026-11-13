from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from kpdesk.matching.category_classifier import stratified_category_groups
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from scripts.build_a46_taxonomy_coverage import build_taxonomy_coverage, main
from scripts.build_category_curriculum_v2 import build_natural_category_curriculum
from scripts.evaluate_a46_taxonomy_coverage import category_quality_checks


def test_taxonomy_expansion_covers_every_class_without_query_leakage(tmp_path: Path) -> None:
    target = tmp_path / "taxonomy-coverage.json"
    assert main(["--output", str(target)]) == 0
    data = load_dataset(target)
    assert data.provenance_kind == "synthetic_example"
    assert data.independent_labels is False
    assert {row.request.category for row in data.queries} == set(CATEGORY_ATTRIBUTES)
    assert {row.category for row in data.products.values()} == set(CATEGORY_ATTRIBUTES)
    assert len(data.queries) == 439
    missing = set(CATEGORY_ATTRIBUTES) - {
        item["category"] for item in build_natural_category_curriculum()["catalog"]
    }
    counts = Counter(item.request.category for item in data.queries)
    assert len(missing) == 22
    assert all(counts[category] >= 10 for category in missing)
    assert len({row.query_id for row in data.queries}) == len(data.queries)
    assert len({normalize_text(row.request.description) for row in data.queries}) == len(data.queries)
    split = stratified_category_groups(data.queries, seed=42)
    partitions = (split.train, split.validation, split.test)
    assert all({row.request.category for row in partition} == set(CATEGORY_ATTRIBUTES)
               for partition in partitions)
    groups = [{row.query_group_id for row in partition} for partition in partitions]
    assert not groups[0] & groups[1]
    assert not groups[0] & groups[2]
    assert not groups[1] & groups[2]
    assert len(split.train) + len(split.validation) + len(split.test) == len(data.queries)
    with pytest.raises(FileExistsError):
        main(["--output", str(target)])


def test_synthetic_coverage_protects_neighboring_categories(tmp_path: Path) -> None:
    target = tmp_path / "taxonomy-coverage.json"
    assert main(["--output", str(target)]) == 0
    data = load_dataset(target)
    category_products = {
        category: next(p for p in data.products.values() if p.category == category)
        for category in CATEGORY_ATTRIBUTES
    }
    for requested, competing in (
        ("pencil", "mechanical_pencil"),
        ("marker", "highlighter"),
        ("paper", "photo_paper"),
        ("printer", "mfp"),
        ("printer", "copier"),
        ("toner_cartridge", "ink_cartridge"),
        ("document_folder", "binder"),
        ("notebook", "notepad"),
        ("stapler", "staple"),
        ("mouse", "mouse_pad"),
        ("pen", "pen_refill"),
        ("usb_drive", "usb_adapter"),
    ):
        request = next(q.request for q in data.queries if q.request.category == requested)
        assert check_constraints(request, category_products[competing]).status == "incompatible"


def test_expanded_corpus_is_deterministic_and_disjoint_from_prior_no_match_holdout(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    assert main(["--output", str(first)]) == 0
    assert main(["--output", str(second)]) == 0
    one, two = load_dataset(first), load_dataset(second)
    assert one.source_sha256 == two.source_sha256
    assert build_taxonomy_coverage()["provenance"]["source_customer_gold"] is False
    holdout = Path(__file__).resolve().parents[2] / "ml/data/synthetic/ml/no-match-holdout-a46.json"
    known_no_match = {
        normalize_text(case["description"])
        for case in json.loads(holdout.read_text(encoding="utf-8"))["cases"]
    }
    assert not known_no_match & {
        normalize_text(row.request.description) for row in one.queries
    }


@pytest.mark.parametrize(
    ("missing_class", "precision", "group_precision", "coverage", "group_coverage",
     "unknown_count", "expected"),
    [
        (False, 0.95, 0.95, 0.50, 0.50, 0, True),
        (True, 0.95, 0.95, 0.50, 0.50, 0, False),
        (False, 0.94, 0.95, 0.50, 0.50, 0, False),
        (False, 0.95, 0.94, 0.50, 0.50, 0, False),
        (False, 0.95, 0.95, 0.49, 0.50, 0, False),
        (False, 0.95, 0.95, 0.50, 0.49, 0, False),
        (False, 0.95, 0.95, 0.50, 0.50, 1, False),
        (False, None, 0.95, 0.50, 0.50, 0, False),
    ],
)
def test_category_quality_gate_rejects_unsafe_candidates(
    missing_class: bool,
    precision: float | None,
    group_precision: float,
    coverage: float,
    group_coverage: float,
    unknown_count: int,
    expected: bool,
) -> None:
    classes = set(CATEGORY_ATTRIBUTES)
    if missing_class:
        classes.remove("copier")
    checks = category_quality_checks(
        trained_categories=classes,
        accepted_precision=precision,
        accepted_group_precision=group_precision,
        coverage=coverage,
        group_coverage=group_coverage,
        unknown_false_suggestions=unknown_count,
    )
    assert all(checks.values()) is expected
