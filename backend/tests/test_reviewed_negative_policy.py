"""Training must never silently label unreviewed similar products negative."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_ml_training import labeled_queries, product

from kpdesk.matching.cli import load_dataset
from kpdesk.matching.training import _pairs_for_training


def test_strict_pairs_exclude_unreviewed_competing_catalog_product() -> None:
    labeled = labeled_queries()[0]
    white = product("paper-white", "Бумага A4 80 белая")
    color = product("paper-color", "Бумага A4 80 цветная")
    recycled = product("paper-recycled", "Бумага A4 80 экологичная")
    pairs, labels = _pairs_for_training(
        [labeled], [white, color, recycled], limit=10,
        explicit_negatives={labeled.query_id: frozenset({"paper-color"})},
        reviewed_only=True,
    )
    assert {item.product.sku for item in pairs} == {"paper-white", "paper-color"}
    assert dict(zip((x.product.sku for x in pairs), labels, strict=True)) == {
        "paper-white": 1, "paper-color": 0,
    }


def test_strict_pairs_fail_closed_without_reviewed_negative_labels() -> None:
    labeled = labeled_queries()[0]
    products = [
        product("paper-white", "Бумага A4 80 белая"),
        product("paper-color", "Бумага A4 80 цветная"),
    ]
    with pytest.raises(ValueError, match="reviewed negative|missing"):
        _pairs_for_training([labeled], products, limit=10, reviewed_only=True)
    with pytest.raises(ValueError, match="conflict"):
        _pairs_for_training(
            [labeled], products, limit=10,
            reviewed_only=True,
            explicit_negatives={"q1": frozenset({"paper-white"})},
        )


def test_declared_independent_gold_requires_explicit_hard_negatives(
    tmp_path: Path,
) -> None:
    data: dict[str, Any] = {
        "provenance": {
            "kind": "independent_human_reviewed",
            "annotation_method": "independent review",
        },
        "catalog": [
            product("paper-white", "Бумага A4 80 белая").model_dump(mode="json"),
            product("paper-color", "Бумага A4 80 цветная").model_dump(mode="json"),
        ],
        "queries": [
            {
                "query_id": q.query_id, "query_group_id": q.query_group_id,
                "request": q.request.model_dump(mode="json"),
                "relevant_skus": sorted(q.relevant_skus),
            }
            for q in labeled_queries()
        ],
    }
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="reviewed_negative_skus"):
        load_dataset(path)
    for query in data["queries"]:
        query["reviewed_negative_skus"] = [
            "paper-color" if query["relevant_skus"] == ["paper-white"] else "paper-white"
        ]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    loaded = load_dataset(path)
    assert loaded.declared_independent_labels is True
    assert loaded.independent_labels is False  # Declaring provenance alone is not proof.
    assert len(loaded.explicit_hard_negatives) == 8
    data["queries"][0]["reviewed_negative_skus"] = ["paper-white"]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="conflict"):
        load_dataset(path)
