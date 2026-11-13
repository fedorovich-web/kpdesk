"""Safety-oriented decision audit, distinct from top-K candidate ranking."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from kpdesk.matching.decision_audit import (
    audit_auto_selection,
    main,
)
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.matching.training import LabeledQuery


def pencil() -> CatalogProduct:
    return CatalogProduct(
        sku="PENCIL-HB", name="Карандаш графитовый HB",
        category="pencil", sale_unit="штука", pieces_per_sale_unit=1,
        price_rub=Decimal("19.90"),
        attributes={"pencil_type": "graphite", "lead_grade": "HB"},
    )


def q(query_id: str, relevant: frozenset[str], quantity: int | None = 2) -> LabeledQuery:
    return LabeledQuery(
        query_id=query_id, query_group_id=query_id,
        request=RequestedItem(
            description="Карандаш графитовый HB",
            category="pencil", quantity=quantity, unit="штука",
            attributes={"pencil_type": "graphite", "lead_grade": "HB"},
        ),
        relevant_skus=relevant,
    )


def test_counts_unsafe_auto_selections_separately_from_review_abstentions() -> None:
    # Adversarial gold disagreement is deliberate: verify it is counted as unsafe.
    other = pencil().model_copy(update={
        "sku": "OTHER-REVIEWED-SKU", "name": "Карандаш графитовый 2B",
        "attributes": {"pencil_type": "graphite", "lead_grade": "2B"},
    })
    products = [pencil(), other]
    records = [
        q("correct", frozenset({"PENCIL-HB"})),
        q("wrong-label", frozenset({"OTHER-REVIEWED-SKU"})),
        q("no-match", frozenset()),
        q("review", frozenset({"PENCIL-HB"}), quantity=None),
    ]
    report = audit_auto_selection(records, products)
    assert report.total_queries == 4
    assert report.positive_queries == 3
    assert report.no_match_queries == 1
    assert report.auto_selected == 3
    assert report.correct_auto_selected == 1
    assert report.false_auto_selected == 2
    assert report.false_auto_selected_no_match == 1
    assert report.abstained == 1
    assert report.correct_abstained_no_match == 0
    assert report.selection_precision == pytest.approx(1 / 3)
    assert report.auto_selection_coverage == pytest.approx(3 / 4)
    assert report.false_selection_rate == pytest.approx(2 / 4)
    assert report.false_no_match_rate == 1
    assert report.abstain_reasons == {"missing_quantity_or_unit": 1}


def test_empty_and_all_abstain_have_undefined_selection_precision() -> None:
    empty = audit_auto_selection([], [pencil()])
    assert empty.total_queries == 0
    assert empty.selection_precision is None
    assert empty.auto_selection_coverage is None
    abstain = audit_auto_selection([q("review", frozenset(), quantity=None)], [pencil()])
    assert abstain.auto_selected == 0
    assert abstain.selection_precision is None
    assert abstain.false_no_match_rate == 0
    assert abstain.correct_abstained_no_match == 1


def test_duplicate_query_ids_and_bad_gold_sku_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        audit_auto_selection([q("same", frozenset()), q("same", frozenset())], [pencil()])
    with pytest.raises(ValueError, match="unknown"):
        audit_auto_selection([q("bad", frozenset({"NO-SUCH-SKU"}))], [pencil()])


def test_cli_validation_cannot_be_called_a_production_benchmark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from test_ml_cli import dataset_file

    dataset = dataset_file(tmp_path / "synthetic.json")
    assert main(["--dataset", str(dataset), "--split", "validation", "--seed", "19"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["evaluation_set"] == "validation"
    assert result["independent_labels"] is False
    assert result["production_benchmark"] is False
    assert result["production_ready"] is False
    assert result["manual_approval_required"] is True
    assert "dataset_sha256" in result
    assert "false_auto_selected" in result["metrics"]
    assert "queries" not in result
    with pytest.raises(ValueError, match="final holdout"):
        main(["--dataset", str(dataset), "--split", "test"])
    report_path = tmp_path / "once-only.json"
    assert main([
        "--dataset", str(dataset), "--split", "test", "--final-holdout",
        "--report", str(report_path),
    ]) == 0
    evaluated = json.loads(capsys.readouterr().out)
    assert evaluated["evaluation_set"] == "test"
    assert evaluated["production_ready"] is False
    assert report_path.is_file()
    with pytest.raises(FileExistsError):
        main([
            "--dataset", str(dataset), "--split", "test", "--final-holdout",
            "--report", str(report_path),
        ])
