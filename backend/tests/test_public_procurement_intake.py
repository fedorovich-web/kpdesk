"""Real, untouched 2026 procurement source excerpts are not ML gold labels."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpdesk.matching.real_requests import load_request_intake
from scripts.prepare_public_procurement_intake import (
    build_unreviewed_procurement_intake,
    main,
)

PROJECT = Path(__file__).resolve().parents[2]
SOURCES = PROJECT / "ml/data/real/public_procurement_requests"


def test_real_procurement_intake_uses_actual_2026_sources_and_verifiable_quotes(
    tmp_path: Path,
) -> None:
    dataset = build_unreviewed_procurement_intake(SOURCES)
    assert dataset["schema_version"] == "real-request-intake-v1"
    assert 20 <= len(dataset["records"]) <= 40
    assert len({r["source_batch_id"] for r in dataset["records"]}) == 2
    assert {r["source_file"].split(".")[-1] for r in dataset["records"]} == {"xlsx", "docx"}
    assert len({r["request_id"] for r in dataset["records"]}) == len(dataset["records"])
    assert all(r["source_quote"] == r["request_text"] for r in dataset["records"])
    assert all(r["origin_declared"] == "public_procurement" for r in dataset["records"])
    assert all(r["rights_status"] == "review_required" for r in dataset["records"])
    assert all(r["privacy_status"] == "review_required" for r in dataset["records"])
    assert all("accepted_product_ids" not in r for r in dataset["records"])
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
    verified = load_request_intake(path, SOURCES)
    assert verified.source_excerpts_verified is True
    assert verified.independently_verified is False
    assert verified.training_approved is False


def test_real_procurement_cli_create_only_no_labeling(tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "unreviewed-real-requests.json"
    assert main(["--sources", str(SOURCES), "--output", str(target)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["source_files"] == 2
    assert summary["verbatim_source_excerpts_verified"] is True
    assert summary["independently_verified"] is False
    assert summary["training_approved"] is False
    assert summary["human_labels"] == 0
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        main(["--sources", str(SOURCES), "--output", str(target)])
    assert target.read_bytes() == before


def test_producer_does_not_publish_if_intake_fails_final_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.prepare_public_procurement_intake as module

    def reject(*_args: object, **_kwargs: object) -> None:
        raise ValueError("intake rejected during final validation")

    monkeypatch.setattr(module, "load_request_intake", reject)
    target = tmp_path / "unverified-do-not-publish.json"
    with pytest.raises(ValueError, match="final validation"):
        module.main(["--sources", str(SOURCES), "--output", str(target)])
    assert not target.exists()


def test_two_real_procurement_batches_cannot_be_claimed_independent_holdout(
    tmp_path: Path,
) -> None:
    from kpdesk.matching.real_requests import build_request_split_manifest

    dataset = build_unreviewed_procurement_intake(SOURCES)
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
    verified = load_request_intake(path, SOURCES)
    assert len({row.source_batch_id for row in verified.records}) == 2
    with pytest.raises(ValueError, match="five|groups"):
        build_request_split_manifest(verified)
