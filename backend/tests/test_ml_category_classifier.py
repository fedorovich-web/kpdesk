"""A39 taxonomy/category ML: source groups, abstention, artifact trust and compatibility."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpdesk.matching import category_classifier as category_ml
from kpdesk.matching.category_classifier import (
    CategoryClassifier,
    calibrate_category_confidence,
    explicitly_use_suggestion,
    load_category_model,
    save_category_model,
    stratified_category_groups,
    suggest_category,
    train_category_classifier,
)
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.retrieve import retrieve_candidates
from scripts.build_category_curriculum import build_category_curriculum, main


def test_synthetic_accessory_curriculum_retains_distinct_categories_and_group_split(
    tmp_path: Path,
) -> None:
    path = tmp_path / "category.json"
    assert main(["--output", str(path)]) == 0
    data = load_dataset(path)
    assert len(data.products) == 73
    assert len(data.queries) == 219
    assert data.provenance_kind == "synthetic_example"
    assert data.independent_labels is False
    assert len({product.category for product in data.products.values()}) == 18
    assert build_category_curriculum()["provenance"]["independent_gold"] is False
    partitions = stratified_category_groups(data.queries, seed=42)
    assert (len(partitions.train), len(partitions.validation), len(partitions.test)) == (
        111, 54, 54,
    )
    groups = [
        {record.query_group_id for record in part}
        for part in (partitions.train, partitions.validation, partitions.test)
    ]
    assert not groups[0] & groups[1]
    assert not groups[0] & groups[2]
    assert not groups[1] & groups[2]
    for part in (partitions.train, partitions.validation, partitions.test):
        assert len({record.request.category for record in part}) == 18
    with pytest.raises(FileExistsError):
        main(["--output", str(path)])


def test_canonical_category_filters_accessory_vs_primary_products(tmp_path: Path) -> None:
    path = tmp_path / "category.json"
    main(["--output", str(path)])
    data = load_dataset(path)
    pad_request = next(
        q.request for q in data.queries if q.request.category == "mouse_pad"
    )
    actual_mouse = next(
        item for item in data.products.values() if item.category == "mouse"
    )
    pad = next(
        item for item in data.products.values() if item.category == "mouse_pad"
    )
    assert check_constraints(pad_request, actual_mouse).status == "incompatible"
    assert check_constraints(pad_request, pad).status != "incompatible"
    matches = retrieve_candidates(pad_request, list(data.products.values()))
    assert matches and all(item.product.category == "mouse_pad" for item in matches)
    assert "mouse" not in {item.product.category for item in matches}


def test_category_classifier_fit_validation_and_explicit_only_suggestions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "category.json"
    main(["--output", str(path)])
    data = load_dataset(path)
    split = stratified_category_groups(data.queries, seed=42)
    trained = train_category_classifier(split.train, seed=42)
    threshold, evidence = calibrate_category_confidence(
        trained, split.validation, min_margin=0.03, min_precision=0.95,
    )
    assert 0 <= threshold <= 1
    assert evidence["incorrect_accepted"] == 0
    assert evidence["accepted"] > 0
    artifact = CategoryClassifier(
        trained, threshold, 0.03, data.source_sha256, 42,
    )
    query = next(
        row.request for row in split.validation
        if row.request.category == "mouse_pad"
    )
    wrong_parent = query.model_copy(update={"category": "mouse"})
    report = suggest_category(artifact, wrong_parent)
    assert report.best_category in {row.request.category for row in data.queries}
    assert explicitly_use_suggestion(wrong_parent, report) == wrong_parent
    if report.predicted is not None:
        corrected = explicitly_use_suggestion(
            wrong_parent, report, accepted_by_caller=True,
        )
        assert corrected.category == report.predicted
        assert corrected.category != wrong_parent.category or report.predicted == "mouse"


def test_category_training_rejects_group_overlap_and_unknown_examples(tmp_path: Path) -> None:
    path = tmp_path / "category.json"
    main(["--output", str(path)])
    data = load_dataset(path)
    records = list(data.queries)
    changed = records[1]
    first = records[0]
    assert first.query_group_id != "" and first.request.category == changed.request.category
    corrupted = changed.__class__(
        changed.query_id, first.query_group_id,
        changed.request.model_copy(update={"category": "mouse_pad"}), changed.relevant_skus,
    )
    with pytest.raises(ValueError, match="Mixed category"):
        stratified_category_groups([records[0], corrupted, *records[2:]], 42)
    with pytest.raises(ValueError, match="Duplicate"):
        stratified_category_groups([*records, records[0]], 42)


def test_category_artifact_is_create_only_and_hash_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "category.json"
    main(["--output", str(path)])
    data = load_dataset(path)
    partition = stratified_category_groups(data.queries, 42)
    fitted = train_category_classifier(partition.train)
    threshold, _ = calibrate_category_confidence(fitted, partition.validation)
    local_root = tmp_path / "private-models"
    monkeypatch.setattr(category_ml, "TRUSTED_MODEL_ROOT", local_root)
    target = local_root / "a39.joblib"
    model = CategoryClassifier(fitted, threshold, 0.03, data.source_sha256, 42)
    save_category_model(target, model)
    verified = load_category_model(target)
    assert verified.dataset_sha256 == data.source_sha256
    assert verified.threshold == threshold
    with pytest.raises(FileExistsError):
        save_category_model(target, model)
    manifest = target.with_suffix(".joblib.manifest.json")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    metadata["model_sha256"] = "0" * 64
    manifest.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest|SHA256"):
        load_category_model(target)


def test_caller_approved_category_correction_revalidates_original_profile() -> None:
    from pydantic import ValidationError

    from kpdesk.matching.category_classifier import CategorySuggestion
    from kpdesk.matching.schema import RequestedItem

    suggestion = CategorySuggestion(
        predicted="mouse_pad", best_category="mouse_pad", confidence=0.97,
        margin=0.86, threshold=0.4, abstained=False,
    )
    previous = RequestedItem(
        category="mouse", description="Тканевая подкладка под мышь",
        attributes={"connection": "usb", "dpi": 1200},
    )
    assert explicitly_use_suggestion(previous, suggestion) is previous
    with pytest.raises(ValidationError, match="Unsupported attributes"):
        explicitly_use_suggestion(previous, suggestion, accepted_by_caller=True)
    clean = RequestedItem(category="mouse", description=previous.description)
    accepted = explicitly_use_suggestion(clean, suggestion, accepted_by_caller=True)
    assert accepted.category == "mouse_pad"
    assert accepted.description == clean.description
    unknown = suggestion.__class__(
        predicted=None, best_category="pen_refill", confidence=0.07,
        margin=0.0, threshold=0.4, abstained=True,
    )
    assert explicitly_use_suggestion(clean, unknown, accepted_by_caller=True) is clean


def test_hybrid_category_features_preserve_verified_model_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "category.json"
    assert main(["--output", str(source)]) == 0
    dataset = load_dataset(source)
    split = stratified_category_groups(dataset.queries, seed=42)
    pipeline = train_category_classifier(split.train, feature_mode="hybrid")
    assert set(pipeline.named_steps["tfidf"].named_transformers) == {"character", "word"}
    threshold, _ = calibrate_category_confidence(pipeline, split.validation)
    saved = CategoryClassifier(pipeline, threshold, 0.03, dataset.source_sha256, 42)
    monkeypatch.setattr(category_ml, "TRUSTED_MODEL_ROOT", tmp_path)
    path = tmp_path / "hybrid.joblib"
    save_category_model(path, saved)
    loaded = load_category_model(path)
    request = split.validation[0].request
    assert suggest_category(loaded, request) == suggest_category(saved, request)
