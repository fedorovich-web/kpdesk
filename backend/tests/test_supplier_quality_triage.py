"""Review packet must retain immutable originals and never mint catalog or ML labels."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.audit_recent_supplier_candidates import ROOT
from scripts.prepare_supplier_quality_triage import (
    build_supplier_quality_triage,
    main,
    verify_supplier_quality_triage,
)


@pytest.fixture(scope="module")
def packet() -> dict[str, object]:
    return build_supplier_quality_triage(ROOT, per_issue=6)


def test_quarantined_quality_packet_source_and_issue_counts(
    packet: dict[str, object],
) -> None:
    assert packet["schema_version"] == "supplier-quality-triage-v1"
    assert packet["training_authorized"] is False
    assert packet["current_price_verified"] is False
    assert packet["canonical_importable"] is False
    assert packet["human_reviewed"] is False
    counts = packet["issue_population_counts"]
    assert counts == {
        "ecoplast_duplicate_sku": 4,
        "ecoplast_invalid_package": 2,
        "shveigid_missing_article": 3682,
        "shveigid_duplicate_article": 300,
        "etk_cached_formula_price": 1035,
        "technoas_currency_unverified": 354,
    }
    cases = packet["cases"]
    assert isinstance(cases, list)
    assert len(cases) == 30
    assert len({case["case_id"] for case in cases}) == 30
    assert all(len(case["original_sha256"]) == 64 for case in cases)
    assert all("source_rows" in case and "sheet" in case for case in cases)
    assert all("price_rub" not in case and "approved" not in case for case in cases)


def test_duplicate_sku_rows_and_unresolvable_articles_are_preserved(
    packet: dict[str, object],
) -> None:
    cases = packet["cases"]
    assert isinstance(cases, list)
    source_cases = [c for c in cases if c["issue"] == "ecoplast_duplicate_sku"]
    assert {
        c["source_article_raw"]: c["source_rows"] for c in source_cases
    } == {
        "73911-E110": [568, 581],
        "73911-2-E110": [569, 582],
        "42540HF": [1732, 1740, 1747],
        "42550-10HF": [1734, 1741, 1748],
    }
    missing = [c for c in cases if c["issue"] == "shveigid_missing_article"]
    assert len(missing) == 6
    assert all(c["source_article_raw"] is None for c in missing)
    assert all(c["source_item_id_raw"] is not None for c in missing)
    assert all(c["source_id"] == "shveigid_20261021" for c in missing)


def test_packet_rebuild_is_deterministic_and_detects_unauthorized_changes(
    packet: dict[str, object],
) -> None:
    assert verify_supplier_quality_triage(ROOT, packet)["case_count"] == 30
    altered = deepcopy(packet)
    cases = altered["cases"]
    assert isinstance(cases, list)
    cases[0]["source_rows"] = [999999]
    with pytest.raises(ValueError, match="tamper|source|mismatch"):
        verify_supplier_quality_triage(ROOT, altered)


def test_cli_create_only_and_integrity_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "triage.json"
    assert main(["build", "--output", str(output), "--per-issue", "2"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["case_count"] == 12
    assert report["training_authorized"] is False
    with pytest.raises(FileExistsError):
        main(["build", "--output", str(output), "--per-issue", "2"])
    assert main(["verify", "--queue", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["case_count"] == 12
    changed = json.loads(output.read_text(encoding="utf-8"))
    changed["cases"][0]["source_rows"] = [0]
    output.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="tamper|source|mismatch"):
        main(["verify", "--queue", str(output)])
