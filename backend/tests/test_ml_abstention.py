"""A38: no-match calibration is honest, bounded, source-group-isolated."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from kpdesk.matching.abstention import (
    CaseScores,
    calibrate_threshold,
    evaluate_abstention,
)
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.training import split_groups
from scripts.build_synthetic_ml_hardcases import build_hardcases, main


def test_hardcase_curriculum_source_groups_and_absence_are_explicit(tmp_path: Path) -> None:
    filename = tmp_path / "hardcases.json"
    assert main(["--output", str(filename)]) == 0
    dataset = load_dataset(filename)
    assert dataset.provenance_kind == "synthetic_example"
    assert not dataset.independent_labels
    assert len(dataset.products) == 49
    assert len(dataset.queries) == 185
    assert sum(not query.relevant_skus for query in dataset.queries) == 38
    assert build_hardcases()["provenance"]["not_customer_no_match_evidence"] is True
    split = split_groups(dataset.queries, seed=42)
    assert (len(split.train), len(split.validation), len(split.test)) == (110, 38, 37)
    parts = [{record.query_group_id for record in subset} for subset in (
        split.train, split.validation, split.test
    )]
    assert all(parts)
    assert not parts[0] & parts[1] and not parts[0] & parts[2] and not parts[1] & parts[2]
    assert all(any(not query.relevant_skus for query in part) for part in (
        split.train, split.validation, split.test
    ))
    no_match = [q for q in dataset.queries if not q.relevant_skus]
    catalog = list(dataset.products.values())
    candidate_counts = [len(retrieve_candidates(query.request, catalog)) for query in no_match]
    assert any(count == 0 for count in candidate_counts)
    assert any(count > 0 for count in candidate_counts)
    with pytest.raises(FileExistsError):
        main(["--output", str(filename)])


def test_threshold_is_calibrated_only_from_given_validation_rows() -> None:
    cases = [
        CaseScores("p1", frozenset({"A"}), (("A", 0.9), ("B", 0.2))),
        CaseScores("p2", frozenset({"B"}), (("B", 0.8), ("A", 0.2))),
        CaseScores("n1", frozenset(), (("A", 0.4), ("B", 0.3))),
        CaseScores("n2", frozenset(), ()),
    ]
    threshold, score = calibrate_threshold(cases)
    assert 0.4 < threshold <= 0.8
    assert score.positive_top1_correct == 2
    assert score.no_match_correct_empty == 2
    assert score.no_match_false_suggestion == 0
    assert score.balanced_top1_and_empty == 1
    assert evaluate_abstention(cases, 0).no_match_false_suggestion == 1
    with pytest.raises(ValueError, match="both"):
        calibrate_threshold(cases[:2])


def test_abstention_rejects_invalid_scores_and_duplicate_queries() -> None:
    case = CaseScores("sample", frozenset({"A"}), (("A", 0.7),))
    with pytest.raises(ValueError, match="Duplicate"):
        evaluate_abstention((case, case), 0.5)
    with pytest.raises(ValueError, match="probability"):
        evaluate_abstention((case,), math.nan)
    with pytest.raises(ValueError, match="probability"):
        evaluate_abstention((case,), True)
    with pytest.raises(ValueError, match="Invalid"):
        evaluate_abstention(
            (CaseScores("bad", frozenset(), (("SKU", float("nan")),)),), 0.5
        )


def test_explicit_min_score_prevents_low_confidence_fallback(tmp_path: Path) -> None:
    filename = tmp_path / "training.json"
    assert main(["--output", str(filename)]) == 0
    data = load_dataset(filename)
    request = data.queries[0].request
    candidates = retrieve_candidates(request, list(data.products.values()))
    baseline = rank_candidates(request, candidates, "tfidf")
    assert baseline
    threshold = math.nextafter(baseline[0].score, 1.0)
    if threshold <= 1:
        assert rank_candidates(request, candidates, "tfidf", min_score=threshold) == []
    assert rank_candidates(request, candidates, "tfidf", min_score=0.0) == baseline
    with pytest.raises(ValueError, match="min_score"):
        rank_candidates(request, candidates, "tfidf", min_score=float("inf"))
