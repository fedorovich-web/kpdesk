from decimal import Decimal

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import CatalogProduct, RequestedItem


def test_identical_tfidf_vectors_keep_scores_in_api_bounds() -> None:
    wanted = RequestedItem(
        description="Office paper A4 80", category="paper", quantity=1, unit="пачка",
        paper_format="A4", paper_gsm=80,
    )
    products = [paper(sku, 80).model_copy(update={"name": wanted.description})
                for sku in ("A", "B")]
    ranked = rank_candidates(wanted, retrieve_candidates(wanted, products), "tfidf")
    assert len(ranked) == 2
    # Floating point cosine sums can be 1.0000000000000002 for equal vectors.
    assert all(0 <= candidate.score <= 1 for candidate in ranked)


def paper(sku: str, gsm: int | None) -> CatalogProduct:
    return CatalogProduct(
        sku=sku,
        name=f"Бумага офисная A4 {gsm or 'плотность неизвестна'}",
        category="paper",
        sale_unit="пачка",
        pieces_per_sale_unit=1,
        price_rub=Decimal("349.90"),
        paper_format="A4",
        paper_gsm=gsm,
    )


def test_normalize_preserves_format_and_weight() -> None:
    assert normalize_text("  БУМАГА    А4  80   г/м²  ") == "бумага а4 80 г/м²"


def test_exact_paper_attributes_are_compatible() -> None:
    wanted = RequestedItem(
        description="Бумага офисная А4 80 г/м²",
        category="paper",
        quantity=20,
        unit="пачка",
        paper_format="A4",
        paper_gsm=80,
    )
    result = check_constraints(wanted, paper("000001", 80))
    assert result.status == "compatible"
    assert result.reason_codes == ()


def test_conflicting_paper_density_is_rejected() -> None:
    wanted = RequestedItem(
        description="Бумага офисная А4 80 г/м²",
        category="paper",
        quantity=20,
        unit="пачка",
        paper_format="A4",
        paper_gsm=80,
    )
    result = check_constraints(wanted, paper("000002", 65))
    assert result.status == "incompatible"
    assert "paper_gsm_mismatch" in result.reason_codes


def test_missing_critical_attribute_requires_review() -> None:
    wanted = RequestedItem(
        description="Бумага офисная А4 80 г/м²",
        category="paper",
        quantity=20,
        unit="пачка",
        paper_format="A4",
        paper_gsm=80,
    )
    result = check_constraints(wanted, paper("000003", None))
    assert result.status == "needs_review"
    assert "paper_gsm_unknown" in result.reason_codes


def test_retrieval_keeps_sku_and_never_returns_explicit_conflict() -> None:
    wanted = RequestedItem(
        description="Бумага А4 80",
        category="paper",
        quantity=20,
        unit="пачка",
        paper_format="A4",
        paper_gsm=80,
    )
    results = retrieve_candidates(
        wanted,
        [paper("000001", 80), paper("000002", 65), paper("000003", None)],
        limit=20,
    )
    assert [candidate.product.sku for candidate in results] == ["000001", "000003"]
    assert results[0].constraints.status == "compatible"
    assert results[1].constraints.status == "needs_review"
