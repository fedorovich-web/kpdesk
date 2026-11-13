"""A37: the expanded synthetic training corpus is not disguised as gold."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpdesk.matching.cli import load_dataset
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.training import split_groups
from scripts.build_synthetic_ml_curriculum import build_curriculum, main


def test_synthetic_multicategory_curriculum_has_explicit_grouped_labels(tmp_path: Path) -> None:
    dataset = tmp_path / "curriculum.json"
    assert main(["--output", str(dataset)]) == 0
    observed = load_dataset(dataset)
    assert len(observed.products) == 49
    assert len(observed.queries) == 147
    assert len({product.category for product in observed.products.values()}) == 12
    assert observed.provenance_kind == "synthetic_example"
    assert observed.independent_labels is False
    assert build_curriculum()["provenance"]["independent_gold"] is False
    assert len({query.query_group_id for query in observed.queries}) == 49
    assert len({normalize_text(query.request.description) for query in observed.queries}) == 147
    assert all(len(query.relevant_skus) == 1 for query in observed.queries)

    split = split_groups(observed.queries, seed=42)
    assert (len(split.train), len(split.validation), len(split.test)) == (87, 30, 30)
    partitions = [
        {query.query_group_id for query in records}
        for records in (split.train, split.validation, split.test)
    ]
    assert all(partitions)
    assert not (partitions[0] & partitions[1] or
                partitions[0] & partitions[2] or partitions[1] & partitions[2])
    original_catalog = list(observed.products.values())
    assert all(
        next(iter(query.relevant_skus))
        in {candidate.product.sku for candidate in retrieve_candidates(
            query.request, original_catalog, limit=20
        )}
        for query in observed.queries
    )


def test_synthetic_training_corpus_is_create_only_and_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert main(["--output", str(first)]) == 0
    assert main(["--output", str(second)]) == 0
    assert json.loads(first.read_text(encoding="utf-8")) == json.loads(
        second.read_text(encoding="utf-8")
    )
    assert load_dataset(first).source_sha256 == load_dataset(second).source_sha256
    with pytest.raises(FileExistsError):
        main(["--output", str(first)])
