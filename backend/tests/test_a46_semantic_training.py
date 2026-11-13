from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.training import split_groups
from scripts.build_a46_semantic_training import build_semantic_training, main
from scripts.build_synthetic_ml_hardcases import build_hardcases


def test_train_only_synthetic_hard_negatives_are_isolated_from_previous_holdout() -> None:
    source = build_hardcases()
    document = build_semantic_training()
    original = source["queries"]
    added = document["queries"][len(original):]
    assert document["catalog"] == source["catalog"]
    assert document["queries"][:len(original)] == original
    assert len(added) == 48
    assert len({item["query_group_id"] for item in added}) == 24
    assert all(not item["relevant_skus"] for item in added)
    assert all(item["query_id"].startswith("semantic-") for item in added)
    assert document["provenance"]["synthetic_only"] is True
    assert document["provenance"]["external_holdout_used_for_training"] is False
    frozen = json.loads((
        PROJECT_ROOT / "ml/data/synthetic/ml/no-match-holdout-a46.json"
    ).read_text(encoding="utf-8"))
    training_text = {
        normalize_text(item["request"]["description"])
        for item in document["queries"]
    }
    heldout_text = {
        normalize_text(item["description"])
        for item in frozen["cases"]
    }
    assert training_text.isdisjoint(heldout_text)


def test_training_dataset_has_group_isolated_positive_and_absent_queries(
    tmp_path: Path,
) -> None:
    target = tmp_path / "synthetic.json"
    assert main(["--output", str(target)]) == 0
    data = load_dataset(target)
    assert data.provenance_kind == "synthetic_example"
    assert len(data.queries) == 233
    split = split_groups(data.queries, 42)
    groups = [
        {item.query_group_id for item in part}
        for part in (split.train, split.validation, split.test)
    ]
    assert all(groups)
    assert not groups[0] & groups[1]
    assert not groups[0] & groups[2]
    assert not groups[1] & groups[2]
    assert all(
        any(item.relevant_skus for item in part)
        and any(not item.relevant_skus for item in part)
        for part in (split.train, split.validation, split.test)
    )
    with pytest.raises(FileExistsError):
        main(["--output", str(target)])
