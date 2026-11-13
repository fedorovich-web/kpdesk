"""Real-request source intake CLI must write split IDs only, never raw requests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_real_request_intake import _fixture

from scripts.prepare_real_requests import main


def test_cli_inspect_freeze_verify_and_no_gold_promotion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = _fixture(tmp_path)
    sources = tmp_path / "sources"
    target = tmp_path / "source-locked-split.json"
    assert main(["inspect", "--dataset", str(dataset), "--sources", str(sources)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["records"] == 14
    assert inspected["source_groups"] == 7
    assert inspected["independently_verified"] is False
    assert inspected["training_approved"] is False
    assert "Нужны бумажные товары" not in json.dumps(inspected, ensure_ascii=False)

    assert main(["freeze", "--dataset", str(dataset), "--sources", str(sources),
                 "--output", str(target), "--seed", "13"]) == 0
    frozen_info = json.loads(capsys.readouterr().out)
    assert frozen_info["partitions"]["test"] >= 1
    assert frozen_info["benchmark_approved"] is False
    split = json.loads(target.read_text(encoding="utf-8"))
    assert split["seed"] == 13
    assert split["independent_test_verified"] is False
    assert "Нужны бумажные товары" not in target.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        main(["freeze", "--dataset", str(dataset), "--sources", str(sources),
              "--output", str(target)])

    assert main(["verify", "--dataset", str(dataset), "--sources", str(sources),
                 "--manifest", str(target)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["split_consistency_verified"] is True
    assert verified["real_world_provenance_verified"] is False
    assert verified["independent_test_verified"] is False


def test_cli_rejects_changed_data_and_never_rewrites_existing_split(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = _fixture(tmp_path)
    target = tmp_path / "split.json"
    sources = tmp_path / "sources"
    assert main(["freeze", "--dataset", str(dataset), "--sources", str(sources),
                 "--output", str(target)]) == 0
    capsys.readouterr()
    before = target.read_bytes()
    content: dict[str, Any] = json.loads(dataset.read_text(encoding="utf-8"))
    content["records"][0]["captured_at"] = "2026-10-09T09:10:00+02:00"
    dataset.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest|mismatch"):
        main(["verify", "--dataset", str(dataset), "--sources", str(sources),
              "--manifest", str(target)])
    assert target.read_bytes() == before
