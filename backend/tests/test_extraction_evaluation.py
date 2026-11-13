"""LLM extraction audit uses only supplied human gold and offline predictions."""
import json
from pathlib import Path

import pytest

from kpdesk.extraction.evaluation import (
    ExtractionCase,
    evaluate_extractions,
    main,
)
from kpdesk.extraction.schema import ExtractedRequest

SOURCE = "Нужно 2 пачки бумаги А4 и синяя ручка"
PAPER: dict[str, object] = {
    "source_quote": "2 пачки бумаги А4",
    "description": "Бумага А4", "category": "paper",
    "quantity": 2, "unit": "пачка", "brand": None,
    "paper_format": "A4", "paper_gsm": None,
    "pen_ink_color": None, "pen_ink_type": None,
    "attributes": {},
}
PEN: dict[str, object] = {
    "source_quote": "синяя ручка",
    "description": "Ручка синяя", "category": "pen",
    "quantity": None, "unit": None, "brand": None,
    "paper_format": None, "paper_gsm": None,
    "pen_ink_color": "синий", "pen_ink_type": None,
    "attributes": {},
}


def request(lines: list[dict[str, object]]) -> ExtractedRequest:
    return ExtractedRequest.model_validate({"lines": lines})


def test_field_errors_missing_lines_hallucinations_and_denominators() -> None:
    wrong = {**PAPER, "quantity": 3, "brand": "Выдуманный бренд"}
    one = ExtractionCase(
        case_id="local-01", source_text=SOURCE,
        expected=request([PAPER, PEN]),
        predicted=request([wrong]),
    )
    result = evaluate_extractions([one])
    assert result.total_cases == 1
    assert result.gold_lines == 2
    assert result.predicted_lines == 1
    assert result.aligned_lines == 1
    assert result.missed_lines == 1
    assert result.unexpected_lines == 0
    assert result.provider_failures == 0
    assert result.hallucinated_values == 1
    assert result.changed_values == 1
    assert result.lines_with_critical_errors == 1
    assert result.line_recall == 0.5
    assert result.line_precision == 1.0
    assert result.value_precision is not None and result.value_precision < 1
    assert result.value_recall is not None and result.value_recall < 1


def test_provider_failure_reduces_recall_without_invented_predicted_data() -> None:
    failed = ExtractionCase(
        case_id="local-02", source_text=SOURCE,
        expected=request([PAPER, PEN]),
        predicted=None, provider_error="provider_timeout",
    )
    result = evaluate_extractions([failed])
    assert result.provider_failures == 1
    assert result.gold_lines == 2
    assert result.missed_lines == 2
    assert result.aligned_lines == 0
    assert result.line_precision is None
    assert result.value_precision is None
    assert result.gold_present_values > 0
    assert result.value_recall == 0.0


def test_extra_predicted_line_counts_against_fact_precision() -> None:
    case = ExtractionCase(
        case_id="extra", source_text=SOURCE,
        expected=request([PAPER]), predicted=request([PEN]),
    )
    result = evaluate_extractions([case])
    assert result.gold_lines == 1
    assert result.predicted_lines == 1
    assert result.aligned_lines == 0
    assert result.missed_lines == 1
    assert result.unexpected_lines == 1
    assert result.gold_present_values > 0
    assert result.predicted_present_values > 0
    assert result.value_precision == 0.0
    assert result.value_recall == 0.0


def test_rejects_ambiguous_evidence_and_inconsistent_failure_state() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        evaluate_extractions([
            ExtractionCase(case_id="one", source_text=SOURCE, expected=request([PAPER]),
                           predicted=request([PAPER])),
            ExtractionCase(case_id="one", source_text=SOURCE, expected=request([PAPER]),
                           predicted=request([PAPER])),
        ])
    with pytest.raises(ValueError, match="source evidence"):
        evaluate_extractions([
            ExtractionCase(case_id="unknown", source_text="другое",
                           expected=request([PAPER]), predicted=request([PAPER])),
        ])
    with pytest.raises(ValueError, match="provider_error"):
        ExtractionCase(case_id="bad", source_text=SOURCE,
                       expected=request([PAPER]), predicted=None)


def test_cli_never_discloses_source_text_or_calls_a_synthetic_corpus_production(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    data = {
        "provenance": {"kind": "synthetic_example", "annotation_method": "unit_test"},
        "cases": [{
            "case_id": "a1",
            "source_text": SOURCE,
            "expected": {"lines": [PAPER]},
            "predicted": {"lines": [{**PAPER, "brand": "Гипотеза"}]},
            "provider_error": None,
        }],
    }
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    report_path = tmp_path / "report.json"
    assert main(["--dataset", str(path), "--report", str(report_path)]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["independent_labels"] is False
    assert report["production_ready"] is False
    assert report["production_benchmark"] is False
    assert report["metrics"]["hallucinated_values"] == 1
    assert SOURCE not in output
    assert SOURCE not in report_path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        main(["--dataset", str(path), "--report", str(report_path)])
