"""Verified file provenance and group-locked real request intake, no AI calls."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kpdesk.matching.real_requests import (
    build_request_split_manifest,
    load_request_intake,
    verify_request_split_manifest,
)
from kpdesk.matching.schema import CatalogProduct


def _fixture(tmp_path: Path, *, groups: int = 7) -> Path:
    sources = tmp_path / "sources"
    sources.mkdir(parents=True)
    records = []
    categories = (
        "плотная цветная бумага формата А4",
        "тонер для лазерного принтера HP",
        "набор металлических скрепок 50 мм",
        "файлы вкладыши полипропиленовые А4",
        "картонные архивные коробки большого размера",
        "кабель USB Type-C длиной два метра",
        "степлер промышленный для толстых стопок",
    )
    for i in range(groups):
        source = sources / f"batch-{i}.txt"
        text = f"Заявка на поставку товаров: бумага формата А4, партия {i + 1}, упаковок 4."
        phrases = [
            f"Нужны {categories[i % len(categories)]}, партия {i + 1}; позиция {j + 1}"
            for j in range(2)
        ]
        source.write_text(text + "\n" + "\n".join(phrases), encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        for item in range(2):
            records.append({
                "request_id": f"request-{i}-{item}",
                "origin_declared": "customer_received" if i % 2 else "public_procurement",
                "source_batch_id": f"batch-{i}",
                "source_file": f"batch-{i}.txt",
                "source_sha256": digest,
                "source_quote": phrases[item],
                "request_text": phrases[item],
                "captured_at": "2026-10-09T09:00:00+02:00",
                "rights_status": "review_required",
                "privacy_status": "review_required",
            })
    path = tmp_path / "intake.json"
    path.write_text(json.dumps({"schema_version": "real-request-intake-v1",
                                "records": records}, ensure_ascii=False), encoding="utf-8")
    return path


def test_intake_requires_local_source_file_and_preserves_unverified_origin(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    intake = load_request_intake(path, tmp_path / "sources")
    assert len(intake.records) == 14
    assert intake.independently_verified is False
    assert intake.training_approved is False
    assert {row.origin_declared for row in intake.records} == {
        "customer_received", "public_procurement"
    }


def test_holdout_freeze_groups_together_and_never_exposes_request_text(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    intake = load_request_intake(path, tmp_path / "sources")
    manifest = build_request_split_manifest(intake, seed=42)
    assert set(manifest["partitions"]) == {"train", "validation", "test"}
    assert all(manifest["partitions"].values())
    ids = []
    groups: dict[str, str] = {}
    for partition, rows in manifest["partitions"].items():
        ids.extend(rows)
        for record in intake.records:
            if record.request_id in rows:
                old = groups.setdefault(record.source_batch_id, partition)
                assert old == partition
    assert len(ids) == len(set(ids)) == len(intake.records)
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert "Нужны плотная цветная бумага" not in serialized
    assert manifest["benchmark_approved"] is False
    assert manifest["training_approved"] is False
    assert verify_request_split_manifest(path, tmp_path / "sources", manifest)


def test_edited_source_fails_even_when_json_intake_is_unchanged(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    sources = tmp_path / "sources"
    manifest = build_request_split_manifest(load_request_intake(path, sources))
    (sources / "batch-0.txt").write_text("Подмененный исходный документ", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256|checksum|source"):
        verify_request_split_manifest(path, sources, manifest)


def test_same_document_in_different_groups_is_rejected(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["records"][2]["source_file"] = body["records"][0]["source_file"]
    body["records"][2]["source_sha256"] = body["records"][0]["source_sha256"]
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="group|source"):
        load_request_intake(path, tmp_path / "sources")


def test_near_duplicate_request_text_cross_batch_is_rejected(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    _replace_verified_excerpt(path, 2, body["records"][0]["request_text"])
    with pytest.raises(ValueError, match="duplicate|leakage"):
        load_request_intake(path, tmp_path / "sources")


def test_source_path_traversal_and_missing_file_fail_closed(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["records"][0]["source_file"] = "../secrets.txt"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="file|path|source"):
        load_request_intake(path, tmp_path / "sources")
    body["records"][0]["source_file"] = "missing.pdf"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="file|source"):
        load_request_intake(path, tmp_path / "sources")


def test_privacy_and_rights_are_not_self_certified(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["records"][0]["rights_status"] = "approved"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="rights|status"):
        load_request_intake(path, tmp_path / "sources")
    body["records"][0]["rights_status"] = "review_required"
    body["records"][0]["request_text"] = "Связь: personal@example.com, бумага А4"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="personal|email|PII"):
        load_request_intake(path, tmp_path / "sources")


def test_manifest_detects_mutation_and_only_accepts_group_count_minimum(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    sources = tmp_path / "sources"
    manifest = build_request_split_manifest(load_request_intake(path, sources))
    manifest["partitions"]["test"].append("invented")
    with pytest.raises(ValueError, match="manifest|partition|split"):
        verify_request_split_manifest(path, sources, manifest)
    few = _fixture(tmp_path / "few", groups=2)
    with pytest.raises(ValueError, match="groups"):
        build_request_split_manifest(load_request_intake(few, few.parent / "sources"))


def test_synthetic_source_not_promoted_to_real_customer_gold(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    for record in body["records"][:2]:
        record["origin_declared"] = "synthetic"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    result = load_request_intake(path, tmp_path / "sources")
    assert result.records[0].origin_declared == "synthetic"
    assert result.independently_verified is False


def test_near_duplicate_across_source_groups_is_not_split(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    before = body["records"][0]["request_text"]
    similar = before.replace("цветная", "цветнаe")
    assert similar != before
    _replace_verified_excerpt(path, 2, similar)
    with pytest.raises(ValueError, match="near.duplicate|leakage"):
        load_request_intake(path, tmp_path / "sources")


def _replace_verified_excerpt(path: Path, record_index: int, replacement: str) -> None:
    """Keep test source bytes and all same-file SHAs mutually consistent."""
    body = json.loads(path.read_text(encoding="utf-8"))
    record = body["records"][record_index]
    source = path.parent / "sources" / record["source_file"]
    old = record["source_quote"]
    original = source.read_text(encoding="utf-8")
    assert old in original
    source.write_text(original.replace(old, replacement, 1), encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    for item in body["records"]:
        if item["source_file"] == record["source_file"]:
            item["source_sha256"] = digest
    record["source_quote"] = replacement
    record["request_text"] = replacement
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def _candidate_catalog() -> dict[str, CatalogProduct]:
    return {
        sku: CatalogProduct(
            sku=sku, name=name, category="stationery_other",
            sale_unit="штука", pieces_per_sale_unit=1,
            price_rub=Decimal("40.00"), attributes={"product_kind": "item"},
        )
        for sku, name in (
            ("A4-COLOR", "Плотная цветная бумага формата А4"),
            ("TONER", "Тонер для лазерного принтера HP"),
            ("CLIPS", "Набор металлических скрепок 50 мм"),
            ("FOLDERS", "Файлы вкладыши полипропиленовые А4"),
        )
    }


def test_source_bound_request_candidates_exclude_locked_test_and_never_become_gold(
    tmp_path: Path,
) -> None:
    from kpdesk.matching.request_candidate_queue import (
        build_request_candidate_queue,
        verify_request_candidate_queue,
    )

    path = _fixture(tmp_path)
    intake = load_request_intake(path, tmp_path / "sources")
    frozen = build_request_split_manifest(intake)
    catalog = _candidate_catalog()
    queue = build_request_candidate_queue(
        intake, frozen, catalog, candidates_per_request=3,
    )
    assert queue["schema_version"] == "request-candidate-review-v1"
    tasks = queue["tasks"]
    assert isinstance(tasks, list) and tasks
    ids = {task["request_id"] for task in tasks}
    assert not ids & set(frozen["partitions"]["test"])
    assert ids == set(frozen["partitions"]["train"] + frozen["partitions"]["validation"])
    assert all(task["candidate_origin"] == "lexical_proposals_not_labels" for task in tasks)
    assert all(task["reviewed_positive_skus"] is None for task in tasks)
    assert all(task["reviewed_negative_skus"] is None for task in tasks)
    assert all(task["reviewed_no_match"] is None for task in tasks)
    assert all(len(task["candidate_skus"]) == 3 for task in tasks)
    assert all(task["request_text"] in (tmp_path / "sources" / task["source_file"]).read_text(
        encoding="utf-8",
    ) for task in tasks)
    assert queue["rights_verified"] is False
    assert queue["independent_gold"] is False
    assert queue["training_approved"] is False
    assert queue["benchmark_approved"] is False
    assert verify_request_candidate_queue(intake, frozen, catalog, queue)
    tampered = json.loads(json.dumps(queue))
    tampered["tasks"][0]["candidate_skus"][0] = "FAKE"
    with pytest.raises(ValueError, match="modified"):
        verify_request_candidate_queue(intake, frozen, catalog, tampered)
    frozen["partitions"]["test"].append(frozen["partitions"]["train"][0])
    with pytest.raises(ValueError, match="manifest|split"):
        build_request_candidate_queue(intake, frozen, catalog)


def test_independent_queue_rejects_synthetic_request_and_unverified_intake(
    tmp_path: Path,
) -> None:
    from kpdesk.matching.real_requests import RequestIntake
    from kpdesk.matching.request_candidate_queue import build_request_candidate_queue

    path = _fixture(tmp_path)
    intake = load_request_intake(path, tmp_path / "sources")
    frozen = build_request_split_manifest(intake)
    catalog = _candidate_catalog()
    with pytest.raises(ValueError, match="excerpts"):
        build_request_candidate_queue(
            RequestIntake(source_sha256=intake.source_sha256, records=intake.records),
            frozen, catalog,
        )
    body = json.loads(path.read_text(encoding="utf-8"))
    for item in body["records"][:2]:
        item["origin_declared"] = "synthetic"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    synthetic = load_request_intake(path, tmp_path / "sources")
    with pytest.raises(ValueError, match="Synthetic"):
        build_request_candidate_queue(
            synthetic, build_request_split_manifest(synthetic), catalog,
        )


def test_offline_request_candidate_cli_create_only_and_source_locked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.prepare_real_requests import main

    path = _fixture(tmp_path)
    frozen = build_request_split_manifest(load_request_intake(path, tmp_path / "sources"))
    manifest = tmp_path / "frozen.json"
    catalog_path = tmp_path / "catalog.json"
    output = tmp_path / "candidate-review.json"
    manifest.write_text(json.dumps(frozen, ensure_ascii=False), encoding="utf-8")
    catalog_path.write_text(
        json.dumps({"catalog": [
            product.model_dump(mode="json") for product in _candidate_catalog().values()
        ]}, ensure_ascii=False), encoding="utf-8",
    )
    arguments = [
        "queue", "--dataset", str(path), "--sources", str(tmp_path / "sources"),
        "--manifest", str(manifest), "--catalog", str(catalog_path),
        "--output", str(output), "--candidates-per-request", "2",
    ]
    assert main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["training_approved"] is False
    assert report["locked_test_requests_excluded"] > 0
    body = json.loads(output.read_text(encoding="utf-8"))
    assert len(body["tasks"]) == report["unreviewed_tasks"]
    assert all(len(task["candidate_skus"]) == 2 for task in body["tasks"])
    verify_args = [
        "verify-queue", "--dataset", str(path),
        "--sources", str(tmp_path / "sources"),
        "--manifest", str(manifest), "--catalog", str(catalog_path),
        "--queue", str(output),
    ]
    assert main(verify_args) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["candidate_queue_matches_originals"] is True
    assert verified["training_approved"] is False
    with pytest.raises(FileExistsError):
        main(arguments)
    body["tasks"][0]["candidate_skus"][0] = "FAKE-UNREVIEWED"
    output.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="modified"):
        main(verify_args)


def test_large_review_queue_reader_uses_separate_bounded_limit(tmp_path: Path) -> None:
    from scripts.prepare_real_requests import _read_manifest

    packet = tmp_path / "candidate-review.json"
    packet.write_text(json.dumps({"padding": "a" * 240_000}), encoding="utf-8")
    with pytest.raises(ValueError, match="too large"):
        _read_manifest(packet)
    value = _read_manifest(packet, max_bytes=2_000_000)["padding"]
    assert isinstance(value, str) and len(value) == 240_000
    packet.write_text(json.dumps({"padding": "a" * 2_000_000}), encoding="utf-8")
    with pytest.raises(ValueError, match="too large"):
        _read_manifest(packet, max_bytes=2_000_000)


def _review_packet(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, CatalogProduct]]:
    from kpdesk.matching.request_candidate_queue import build_request_candidate_queue

    dataset = _fixture(tmp_path)
    intake = load_request_intake(dataset, tmp_path / "sources")
    split = build_request_split_manifest(intake)
    catalog = _candidate_catalog()
    queue = build_request_candidate_queue(intake, split, catalog, candidates_per_request=3)
    return dataset, queue, catalog


def _completed_reviews(queue: dict[str, Any], reviewer: str) -> list[dict[str, Any]]:
    from kpdesk.matching.request_annotations import build_review_template

    reviews = build_review_template(queue)
    tasks = queue["tasks"]
    assert isinstance(tasks, list)
    ids = {task["request_id"]: task for task in tasks}
    for review in reviews:
        source = ids[review["request_id"]]
        review.update({
            "reviewed_source": "human_review",
            "reviewed_by": reviewer,
            "reviewed_at": "2026-10-22T14:00:00+02:00",
            "rationale": "Personally compared each displayed SKU with this request excerpt.",
            "accepted_skus": [],
            "rejected_skus": source["candidate_skus"],
            "unresolved_skus": [],
            "no_candidate_match": True,
        })
    return reviews


def test_source_bound_two_reviewer_candidate_consensus_is_never_gold(tmp_path: Path) -> None:
    from kpdesk.matching.request_annotations import (
        build_review_template,
        compare_review_batches,
        validate_review_batch,
    )

    _, queue, _ = _review_packet(tmp_path)
    template = build_review_template(queue)
    assert all(item["reviewed_by"] == "" and item["no_candidate_match"] is None
               for item in template)
    assert all(item["accepted_skus"] == [] and item["rejected_skus"] == []
               for item in template)
    first = _completed_reviews(queue, "reviewer-one")
    second = _completed_reviews(queue, "reviewer-two")
    reviewer, accepted = validate_review_batch(queue, first)
    assert reviewer == "reviewer-one" and len(accepted) == len(first)
    result = compare_review_batches(queue, first, second)
    assert result["agreement_count"] == len(first)
    assert result["conflicting_request_ids"] == []
    agreements = result["agreements"]
    assert isinstance(agreements, list)
    assert all(isinstance(case, dict) and case["no_candidate_match"] is True
               for case in agreements)
    assert result["all_catalog_relevance_verified"] is False
    assert result["reviewer_authentication_verified"] is False
    assert result["independent_gold"] is False
    assert result["training_approved"] is False
    assert result["benchmark_approved"] is False


def test_candidate_reviews_reject_forged_skus_missing_decisions_and_test_leakage(
    tmp_path: Path,
) -> None:
    from copy import deepcopy

    from pydantic import ValidationError

    from kpdesk.matching.request_annotations import validate_review_batch

    dataset, queue, _ = _review_packet(tmp_path)
    first = _completed_reviews(queue, "reviewer-one")
    forged = deepcopy(first)
    forged[0]["rejected_skus"] = ["fake-SKU"]
    with pytest.raises(ValueError, match="Every displayed"):
        validate_review_batch(queue, forged)
    incomplete = deepcopy(first)
    incomplete[0]["rejected_skus"] = incomplete[0]["rejected_skus"][:-1]
    with pytest.raises(ValueError, match="Every displayed"):
        validate_review_batch(queue, incomplete)
    edited_task = deepcopy(first)
    edited_task[0]["task_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA256"):
        validate_review_batch(queue, edited_task)
    fabricated_origin = deepcopy(first)
    fabricated_origin[0]["reviewed_source"] = "model_prediction"
    with pytest.raises(ValidationError):
        validate_review_batch(queue, fabricated_origin)
    unreliable_no_match = deepcopy(first)
    unreliable_no_match[0]["accepted_skus"] = ["FAKE"]
    with pytest.raises(ValidationError, match="No-candidate-match"):
        validate_review_batch(queue, unreliable_no_match)
    fake_rights = deepcopy(queue)
    fake_rights["rights_verified"] = True
    with pytest.raises(ValueError, match="unverified|permissions"):
        validate_review_batch(fake_rights, first)
    synthetic_group = json.loads(dataset.read_text(encoding="utf-8"))
    assert synthetic_group["records"]
    heldout = _completed_reviews(queue, "reviewer-one")
    heldout[0]["request_id"] = "request-id-from-locked-test"
    with pytest.raises(ValueError, match="Unknown|held-out"):
        validate_review_batch(queue, heldout)


def test_candidate_review_consensus_requires_two_distinct_complete_reviewers(
    tmp_path: Path,
) -> None:
    from copy import deepcopy

    from kpdesk.matching.request_annotations import (
        compare_review_batches,
        validate_review_batch,
    )

    _, queue, _ = _review_packet(tmp_path)
    first = _completed_reviews(queue, "reviewer-one")
    second = _completed_reviews(queue, "reviewer-two")
    with pytest.raises(ValueError, match="distinct"):
        compare_review_batches(queue, first, deepcopy(first))
    with pytest.raises(ValueError, match="identical"):
        compare_review_batches(queue, first, second[:-1])
    mixed = deepcopy(second)
    mixed[0]["reviewed_by"] = "reviewer-three"
    with pytest.raises(ValueError, match="One human reviewer"):
        validate_review_batch(queue, mixed)
    differing = deepcopy(second)
    old_rejects = differing[0]["rejected_skus"]
    differing[0]["accepted_skus"] = [old_rejects[0]]
    differing[0]["rejected_skus"] = old_rejects[1:]
    differing[0]["no_candidate_match"] = False
    result = compare_review_batches(queue, first, differing)
    assert result["agreement_count"] == len(first) - 1
    assert result["conflicting_request_ids"] == [first[0]["request_id"]]
    assert result["training_approved"] is False
    unresolved = deepcopy(second)
    unresolved[0]["unresolved_skus"] = [unresolved[0]["rejected_skus"].pop()]
    unresolved[0]["no_candidate_match"] = False
    independent = deepcopy(unresolved)
    for record in independent:
        record["reviewed_by"] = "reviewer-three"
    result = compare_review_batches(queue, unresolved, independent)
    assert result["agreement_count"] == len(first) - 1
    assert result["unresolved_request_ids"] == [first[0]["request_id"]]
    assert result["independent_gold"] is False


def test_real_request_review_cli_rejects_tampered_source_and_never_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.review_real_request_candidates import main

    dataset, queue, catalog = _review_packet(tmp_path)
    intake = load_request_intake(dataset, tmp_path / "sources")
    split = build_request_split_manifest(intake)
    manifest_file = tmp_path / "split.json"
    catalog_file = tmp_path / "catalog.json"
    queue_file = tmp_path / "queue.json"
    template_file = tmp_path / "template.json"
    first_file = tmp_path / "first.json"
    second_file = tmp_path / "second.json"
    consensus_file = tmp_path / "consensus.json"
    manifest_file.write_text(json.dumps(split), encoding="utf-8")
    catalog_file.write_text(json.dumps({"catalog": [
        item.model_dump(mode="json") for item in catalog.values()
    ]}, ensure_ascii=False), encoding="utf-8")
    queue_file.write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
    common = [
        "--dataset", str(dataset), "--sources", str(tmp_path / "sources"),
        "--manifest", str(manifest_file), "--catalog", str(catalog_file),
        "--queue", str(queue_file),
    ]
    assert main(["template", *common, "--output", str(template_file)]) == 0
    assert json.loads(capsys.readouterr().out)["training_approved"] is False
    with pytest.raises(FileExistsError):
        main(["template", *common, "--output", str(template_file)])
    first_file.write_text(json.dumps(_completed_reviews(queue, "reviewer-one")),
                          encoding="utf-8")
    second_file.write_text(json.dumps(_completed_reviews(queue, "reviewer-two")),
                           encoding="utf-8")
    assert main(["validate", *common, "--first", str(first_file)]) == 0
    assert json.loads(capsys.readouterr().out)["reviewed_requests"] > 0
    assert main(["compare", *common, "--first", str(first_file),
                 "--second", str(second_file), "--output", str(consensus_file)]) == 0
    assert json.loads(capsys.readouterr().out)["independent_gold"] is False
    with pytest.raises(FileExistsError):
        main(["compare", *common, "--first", str(first_file),
              "--second", str(second_file), "--output", str(consensus_file)])
    sources = tmp_path / "sources"
    original = sources / "batch-0.txt"
    original.write_text("SOURCE CHANGED", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256|source|excerpt"):
        main(["validate", *common, "--first", str(first_file)])
