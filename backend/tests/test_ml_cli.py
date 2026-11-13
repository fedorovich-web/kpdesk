"""CLI produces provenance-labelled training/evaluation, never fake quality claims."""

import json
from pathlib import Path

import pytest
from test_ml_training import labeled_queries, product

from kpdesk.matching.cli import main


def dataset_file(path: Path) -> Path:
    catalog = [
        product("paper-white", "Бумага A4 80 белая"),
        product("paper-color", "Бумага A4 80 цветная"),
    ]
    body = {
        "provenance": {
            "kind": "synthetic_example",
            "annotation_method": "test_fixture",
        },
        "catalog": [item.model_dump(mode="json") for item in catalog],
        "queries": [
            {
                "query_id": item.query_id,
                "query_group_id": item.query_group_id,
                "request": item.request.model_dump(mode="json"),
                "relevant_skus": sorted(item.relevant_skus),
            }
            for item in labeled_queries()
        ],
    }
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


def test_cli_prepare_train_evaluate_once_without_production_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = dataset_file(tmp_path / "dataset.json")
    trusted_root = tmp_path / "models"
    model = trusted_root / "sample.joblib"

    assert main(["prepare", "--dataset", str(dataset), "--seed", "19"]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["queries"] == 8
    assert prepared["independent_labels"] is False
    assert prepared["train_groups"] and prepared["test_groups"]

    assert main([
        "train", "--dataset", str(dataset), "--model", str(model),
        "--trusted-root", str(trusted_root), "--seed", "19",
    ]) == 0
    trained = json.loads(capsys.readouterr().out)
    assert trained["evaluation_set"] == "validation"
    assert trained["train_pairs"] > 0
    assert trained["independent_labels"] is False
    assert trained["production_benchmark"] is False
    assert set(trained["lexical_baselines"]) == {"rapidfuzz", "tfidf"}
    assert all(
        baseline["positive_queries"] == trained["metrics"]["positive_queries"]
        for baseline in trained["lexical_baselines"].values()
    )
    assert model.is_file()
    manifest = json.loads(model.with_suffix(".joblib.manifest.json").read_text(encoding="utf-8"))
    assert manifest["training_data_kind"] == "synthetic_example"
    assert manifest["production_eligible"] is False

    assert main([
        "evaluate", "--dataset", str(dataset), "--model", str(model),
        "--trusted-root", str(trusted_root),
    ]) == 0
    tested = json.loads(capsys.readouterr().out)
    assert tested["evaluation_set"] == "test"
    assert tested["independent_labels"] is False
    assert tested["production_benchmark"] is False
    assert tested["metrics"]["total_queries"] > 0
    assert set(tested["lexical_baselines"]) == {"rapidfuzz", "tfidf"}
    assert all(
        baseline["positive_queries"] == tested["metrics"]["positive_queries"]
        for baseline in tested["lexical_baselines"].values()
    )

    with pytest.raises(ValueError, match="already evaluated"):
        main([
            "evaluate", "--dataset", str(dataset), "--model", str(model),
            "--trusted-root", str(trusted_root),
        ])


def test_cli_rejects_unknown_gold_sku_and_missing_provenance(tmp_path: Path) -> None:
    dataset = dataset_file(tmp_path / "dataset.json")
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    payload["queries"][0]["relevant_skus"] = ["not-in-catalog"]
    dataset.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown SKU"):
        main(["prepare", "--dataset", str(dataset)])

    payload["queries"][0]["relevant_skus"] = ["paper-white"]
    payload.pop("provenance")
    dataset.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="provenance"):
        main(["prepare", "--dataset", str(dataset)])


def test_cli_prepare_refuses_contradictory_labels_for_same_request(tmp_path: Path) -> None:
    dataset = dataset_file(tmp_path / 'dataset.json')
    payload = json.loads(dataset.read_text(encoding='utf-8'))
    payload['queries'].append({
        **payload['queries'][0], 'query_id': 'conflicting-label', 'relevant_skus': [],
    })
    dataset.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='Conflicting relevance labels'):
        main(['prepare', '--dataset', str(dataset)])
