"""Offline comparison of LLM extraction against independently supplied gold facts.

No model calls, hidden corrections, or PII in console/JSON reports. Exact evidence
alignment is deliberately conservative: changed source spans count as a missed
gold line plus an extra predicted line, not an inferred semantic match.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from kpdesk.extraction.schema import ExtractedLine, ExtractedRequest

type Provenance = Literal["synthetic_example", "independent_human_reviewed"]
_FIELDS = (
    "description", "category", "quantity", "unit", "brand", "paper_format",
    "paper_gsm", "pen_ink_color", "pen_ink_type",
)
_PROVIDER_CODES = frozenset({
    "provider_timeout", "rate_limited", "invalid_output",
    "provider_error", "provider_auth_failed", "provider_unreachable",
    "incomplete_output", "provider_not_configured",
})


@dataclass(frozen=True, slots=True)
class ExtractionCase:
    case_id: str
    source_text: str
    expected: ExtractedRequest
    predicted: ExtractedRequest | None
    provider_error: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.case_id):
            raise ValueError("Case ID must be an opaque, short identifier.")
        if not 1 <= len(self.source_text) <= 12_000:
            raise ValueError("Source length is outside configured limits.")
        if (self.predicted is None) != (self.provider_error is not None):
            raise ValueError("provider_error must be set iff prediction is missing.")
        if self.provider_error is not None and self.provider_error not in _PROVIDER_CODES:
            raise ValueError("Unsupported safe provider_error code.")


@dataclass(frozen=True, slots=True)
class ExtractionAudit:
    total_cases: int
    provider_failures: int
    gold_lines: int
    predicted_lines: int
    aligned_lines: int
    missed_lines: int
    unexpected_lines: int
    hallucinated_values: int
    omitted_values: int
    changed_values: int
    lines_with_critical_errors: int
    matched_present_values: int
    gold_present_values: int
    predicted_present_values: int
    line_precision: float | None
    line_recall: float | None
    value_precision: float | None
    value_recall: float | None


def _evidence_index(
    lines: Sequence[ExtractedLine], source: str,
) -> dict[str, ExtractedLine]:
    indexed: dict[str, ExtractedLine] = {}
    for line in lines:
        if line.source_quote not in source:
            raise ValueError("Invalid source evidence in evaluation dataset.")
        if line.source_quote in indexed:
            raise ValueError("Duplicate source evidence is ambiguous.")
        indexed[line.source_quote] = line
    return indexed


def _values(line: ExtractedLine) -> dict[str, object]:
    result = {name: getattr(line, name) for name in _FIELDS}
    result.update(line.attributes)
    return result


def _ratio(n: int, d: int) -> float | None:
    return n / d if d else None


def evaluate_extractions(cases: Sequence[ExtractionCase]) -> ExtractionAudit:
    """Count missed/extra lines and factual hallucinations on exact aligned spans.

    Null values do not inflate agreement. A changed non-null value is both a
    false positive assertion and a missing correct assertion.
    """
    ids: set[str] = set()
    gold_lines = predicted_lines = aligned = missed = unexpected = failures = 0
    hallucinated = omitted = changed = bad_critical = 0
    matched_present = gold_present = predicted_present = 0
    for case in cases:
        if case.case_id in ids:
            raise ValueError("Duplicate evaluation case ID.")
        ids.add(case.case_id)
        gold = _evidence_index(case.expected.lines, case.source_text)
        prediction = (
            _evidence_index(case.predicted.lines, case.source_text)
            if case.predicted is not None else {}
        )
        failures += case.predicted is None
        gold_lines += len(gold)
        predicted_lines += len(prediction)
        matched_keys = gold.keys() & prediction.keys()
        aligned += len(matched_keys)
        missed += len(gold) - len(matched_keys)
        unexpected += len(prediction) - len(matched_keys)
        # Include entirely missing gold lines in fact recall, and entirely
        # unexpected predicted lines in assertion precision. Otherwise a
        # failed provider could produce an undefined/optimistic field score.
        for quote in gold.keys() - matched_keys:
            gold_present += sum(v is not None for v in _values(gold[quote]).values())
        for quote in prediction.keys() - matched_keys:
            predicted_present += sum(
                v is not None for v in _values(prediction[quote]).values()
            )
        for quote in matched_keys:
            expected = _values(gold[quote])
            observed = _values(prediction[quote])
            mismatched_critical = False
            for field in expected.keys() | observed.keys():
                correct = expected.get(field)
                actual = observed.get(field)
                gold_present += correct is not None
                predicted_present += actual is not None
                if correct is not None and correct == actual:
                    matched_present += 1
                elif correct is None and actual is not None:
                    hallucinated += 1
                elif actual is None and correct is not None:
                    omitted += 1
                elif correct != actual:
                    changed += 1
                if field != "description" and correct != actual:
                    mismatched_critical = True
            bad_critical += mismatched_critical
    return ExtractionAudit(
        total_cases=len(cases),
        provider_failures=failures,
        gold_lines=gold_lines,
        predicted_lines=predicted_lines,
        aligned_lines=aligned,
        missed_lines=missed,
        unexpected_lines=unexpected,
        hallucinated_values=hallucinated,
        omitted_values=omitted,
        changed_values=changed,
        lines_with_critical_errors=bad_critical,
        matched_present_values=matched_present,
        gold_present_values=gold_present,
        predicted_present_values=predicted_present,
        line_precision=_ratio(aligned, predicted_lines),
        line_recall=_ratio(aligned, gold_lines),
        value_precision=_ratio(matched_present, predicted_present),
        value_recall=_ratio(matched_present, gold_present),
    )


def _read_dataset(path: Path) -> tuple[Provenance, str, tuple[ExtractionCase, ...]]:
    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Evaluation dataset missing or above 10 MiB.")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("Evaluation dataset must be valid UTF-8 JSON.") from error
    if not isinstance(data, dict) or set(data) != {"provenance", "cases"}:
        raise ValueError("Evaluation dataset requires provenance and cases.")
    origin = data["provenance"]
    if not isinstance(origin, dict) or not isinstance(origin.get("annotation_method"), str):
        raise TypeError("Dataset annotation provenance is required.")
    if not origin["annotation_method"].strip():
        raise ValueError("Dataset annotation provenance must not be empty.")
    kind = origin.get("kind")
    if kind not in ("synthetic_example", "independent_human_reviewed"):
        raise ValueError("Dataset provenance kind is unsupported.")
    cases = data["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1_000:
        raise ValueError("Evaluation dataset requires 1–1000 cases.")
    reviewed: list[ExtractionCase] = []
    for entry in cases:
        if not isinstance(entry, dict) or set(entry) != {
            "case_id", "source_text", "expected", "predicted", "provider_error"
        }:
            raise ValueError("Invalid evaluation case structure.")
        try:
            gold = ExtractedRequest.model_validate(entry["expected"])
            prediction = (
                ExtractedRequest.model_validate(entry["predicted"])
                if entry["predicted"] is not None else None
            )
        except (ValidationError, TypeError):
            raise ValueError("Invalid extraction schema in evaluation case.") from None
        reviewed.append(ExtractionCase(
            case_id=entry["case_id"], source_text=entry["source_text"],
            expected=gold, predicted=prediction,
            provider_error=entry["provider_error"],
        ))
    return kind, hashlib.sha256(raw).hexdigest(), tuple(reviewed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline human-gold vs LLM-output extraction safety report."
    )
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    provenance, digest, cases = _read_dataset(args.dataset)
    metrics = evaluate_extractions(cases)
    report: dict[str, Any] = {
        "dataset_sha256": digest,
        "dataset_provenance": provenance,
        "independent_labels": provenance == "independent_human_reviewed",
        "production_benchmark": False,
        "production_ready": False,
        "manual_approval_required": True,
        "alignment": "source_quote_exact",
        "metrics": asdict(metrics),
    }
    if args.report is not None:
        with args.report.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
