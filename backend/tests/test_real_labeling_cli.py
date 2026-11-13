"""CLI must preserve real supplier source and forbid invented quality claims."""
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.prepare_real_labeling import main


def test_build_review_queue_real_sources_and_no_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    result = tmp_path / "review-queue.json"
    assert main(["build", "--output", str(result), "--per-source", "3",
                 "--candidates", "5"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["source_files"] == 6
    assert info["task_count"] == 18
    assert info["candidate_pairs"] == 90
    assert info["model_training_permitted"] is False
    queue = json.loads(result.read_text(encoding="utf-8"))
    assert queue["training_approved"] is False
    assert all(task["query_origin"] == "synthetic_from_real_catalog"
               for task in queue["tasks"])
    with pytest.raises(FileExistsError):
        main(["build", "--output", str(result), "--per-source", "3"])


def test_validate_raises_if_false_review_data(
    tmp_path: Path,
) -> None:
    result = tmp_path / "queue.json"
    decisions = tmp_path / "decisions.json"
    main(["build", "--output", str(result), "--per-source", "1"])
    decisions.write_text('[{"task_id":"unknown"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown"):
        main(["validate", "--queue", str(result), "--decisions", str(decisions)])


def test_validate_refuses_tampered_queue_against_source_files(
    tmp_path: Path,
) -> None:
    queue_file = tmp_path / "review-queue.json"
    decisions_file = tmp_path / "reviews.json"
    main(["build", "--output", str(queue_file), "--per-source", "1", "--candidates", "3"])
    altered = json.loads(queue_file.read_text(encoding="utf-8"))
    altered["tasks"][0]["request_text"] = "Altered request not from original price"
    queue_file.write_text(json.dumps(altered, ensure_ascii=False), encoding="utf-8")
    decisions_file.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="source|mismatch|tamper"):
        main([
            "validate", "--queue", str(queue_file), "--decisions", str(decisions_file),
        ])


def test_consensus_cli_writes_create_only_unapproved_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    queue_file = tmp_path / "review-queue.json"
    reviewer_a = tmp_path / "reviewer-a.json"
    reviewer_b = tmp_path / "reviewer-b.json"
    evidence = tmp_path / "consensus.json"
    main(["build", "--output", str(queue_file), "--per-source", "1", "--candidates", "3"])
    capsys.readouterr()
    queue = json.loads(queue_file.read_text(encoding="utf-8"))

    def reviewer_events(reviewer: str) -> list[dict[str, Any]]:
        result = []
        for task in queue["tasks"]:
            accepted = task["seed_product_id"]
            result.append({
                "task_id": task["task_id"],
                "request_text": task["request_text"],
                "query_origin": task["query_origin"],
                "reviewed_source": "human_review",
                "reviewed_by": reviewer,
                "reviewed_at": "2026-10-09T11:00:00+02:00",
                "accepted_product_ids": [accepted],
                "rejected_product_ids": [
                    c["product_id"] for c in task["candidates"]
                    if c["product_id"] != accepted
                ],
                "no_match": False,
                "rationale": "Explicit test fixture; no actual human review occurred.",
            })
        return result

    reviewer_a.write_text(json.dumps(reviewer_events("test-reviewer-A")), encoding="utf-8")
    reviewer_b.write_text(json.dumps(reviewer_events("test-reviewer-B")), encoding="utf-8")
    assert main([
        "consensus", "--queue", str(queue_file),
        "--first", str(reviewer_a), "--second", str(reviewer_b),
        "--output", str(evidence),
    ]) == 0
    result = json.loads(evidence.read_text(encoding="utf-8"))
    assert result["reviewed_task_count"] == 6
    assert result["agreement_count"] == 6
    assert result["independent_real_request_gold"] is False
    assert result["training_approved"] is False
    assert result["consensus"][0]["reviewer_ids"] == [
        "test-reviewer-A", "test-reviewer-B",
    ]
    with pytest.raises(FileExistsError):
        main([
            "consensus", "--queue", str(queue_file),
            "--first", str(reviewer_a), "--second", str(reviewer_b),
            "--output", str(evidence),
        ])


def test_packet_cli_produces_neutral_cases_for_independent_reviewers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    queue_file = tmp_path / "queue.json"
    packet_file = tmp_path / "reviewer-packet.json"
    main(["build", "--output", str(queue_file), "--per-source", "1", "--candidates", "3"])
    capsys.readouterr()
    assert main([
        "packet", "--queue", str(queue_file), "--output", str(packet_file),
    ]) == 0
    packet = json.loads(packet_file.read_text(encoding="utf-8"))
    original = json.loads(queue_file.read_text(encoding="utf-8"))
    assert len(packet["tasks"]) == 6
    for original_task, neutral in zip(original["tasks"], packet["tasks"], strict=True):
        assert neutral["task_id"].startswith("case-")
        assert original_task["seed_product_id"] not in neutral["task_id"]
        assert "seed_product_id" not in neutral
        assert "query_group_id" not in neutral
        assert "reviewed_label" not in json.dumps(neutral, ensure_ascii=False)
    with pytest.raises(FileExistsError):
        main(["packet", "--queue", str(queue_file), "--output", str(packet_file)])
