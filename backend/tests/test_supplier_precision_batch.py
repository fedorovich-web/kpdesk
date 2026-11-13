"""Human price-precision batch reviews do not publish or fabricate catalog prices."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from kpdesk.catalog.supplier_staging import SupplierMapping, SupplierStaging, stage_supplier_prices
from scripts.supplier_precision_batch import (
    adjudicate_precision_batches,
    build_precision_packet,
    build_reviewer_template,
    validate_precision_batch,
    verify_precision_packet,
)


@pytest.fixture()
def source() -> tuple[SupplierStaging, dict[str, Any]]:
    content = (
        "sku;name;price;currency;unit;pack\n"
        "A-1;One pack;10.125;RUB;уп;12\n"
        "B-2;Other product;12.335;RUB;шт;1\n"
        "C-3;Currency unclear;15.125;EUR;шт;1\n"
        "D-4;Already cents;15.50;RUB;шт;1\n"
    ).encode()
    staged = stage_supplier_prices(
        "vendor.csv", content,
        SupplierMapping(
            sku=0, name=1, price=2, currency=3,
            sale_unit=4, pieces_per_sale_unit=5, header_row=1,
        ),
    )
    return staged, build_precision_packet(staged, source_id="fixture-vendor")


def _decisions(
    packet: dict[str, Any], reviewer: str, *,
    action: str = "needs_supplier_confirmation",
) -> list[dict[str, Any]]:
    return [{
        "case_id": case["case_id"],
        "source_row_sha256": case["source_row_sha256"],
        "reviewer_id": reviewer,
        "reviewed_at": "2026-10-21T21:00:00+02:00",
        "reviewed_source": "human_review",
        "finding": action,
        "rounding_mode": None,
        "rounded_price_rub": None,
        "rationale": "Need original supplier price calculation rules before changing the offer.",
    } for case in packet["cases"]]


def test_packet_immutable_source_evidence_and_unfilled_templates(
    source: tuple[SupplierStaging, dict[str, Any]],
) -> None:
    staged, packet = source
    assert packet["schema_version"] == "supplier-precision-review-v1"
    assert len(packet["cases"]) == 3
    assert packet["cases"][0]["source_price_raw"] == "10.125"
    assert packet["cases"][0]["source_unit_raw"] == "уп"
    assert packet["cases"][0]["source_pack_size"] == 12
    assert packet["cases"][2]["currency_confirmed_rub"] is False
    assert packet["supplier_source_reuse_verified"] is False
    assert packet["canonical_importable"] is False
    assert packet["precision_review_completed"] is False
    assert verify_precision_packet(staged, packet) == 3
    blank = build_reviewer_template(packet, start=0, limit=2)
    assert len(blank) == 2
    assert all(x["finding"] is None and x["reviewer_id"] == "" for x in blank)
    assert all(x["rounded_price_rub"] is None for x in blank)
    with pytest.raises(ValueError, match="human|reviewer|finding"):
        validate_precision_batch(staged, packet, blank)
    tampered = deepcopy(packet)
    tampered["cases"][0]["source_price_raw"] = "100.125"
    with pytest.raises(ValueError, match="mismatch"):
        verify_precision_packet(staged, tampered)
    stale = deepcopy(packet)
    stale["source_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="mismatch"):
        verify_precision_packet(staged, stale)


def test_independent_batch_reuses_exact_rounding_math_and_blocks_unsafe_rows(
    source: tuple[SupplierStaging, dict[str, Any]],
) -> None:
    staged, packet = source
    reviews = _decisions(packet, "reviewer-a")
    first = reviews[0]
    first.update(
        finding="approve_rounding", rounding_mode="half_up",
        rounded_price_rub="10.13",
        rationale="Supplier price policy separately confirmed as half up on this specific row.",
    )
    result = validate_precision_batch(staged, packet, reviews)
    assert result["reviewed_count"] == 3
    assert result["reviewed_rounding_count"] == 1
    assert result["pending_count"] == 2
    assert result["human_identity_authenticated"] is False
    assert result["canonical_importable"] is False
    for field, invalid in (
        ("rounded_price_rub", "10.12"),
        ("rounded_price_rub", "100.00"),
        ("rounded_price_rub", "10.130"),
        ("rounding_mode", "bankers_unknown"),
    ):
        altered = deepcopy(reviews)
        altered[0][field] = invalid
        with pytest.raises(ValueError, match="precision|rounding"):
            validate_precision_batch(staged, packet, altered)
    altered = deepcopy(reviews)
    altered[2].update(
        finding="approve_rounding", rounding_mode="half_up", rounded_price_rub="15.13",
    )
    with pytest.raises(ValueError, match="currency|precision|rounding"):
        validate_precision_batch(staged, packet, altered)
    altered = deepcopy(reviews)
    altered[0]["rounded_price_rub"] = 10.13
    with pytest.raises((ValueError, TypeError), match="rounded_price"):
        validate_precision_batch(staged, packet, altered)


def test_untrusted_identity_bad_schema_and_partial_case_sets_refused(
    source: tuple[SupplierStaging, dict[str, Any]],
) -> None:
    staged, packet = source
    reviews = _decisions(packet, "reviewer-a")
    for field, invalid in (
        ("source_row_sha256", "0" * 64),
        ("reviewed_source", "model_prediction"),
        ("reviewer_id", ""),
        ("reviewed_at", "2026-10-21T21:00:00"),
        ("rationale", "bad"),
    ):
        altered = deepcopy(reviews)
        altered[0][field] = invalid
        with pytest.raises((TypeError, ValueError)):
            validate_precision_batch(staged, packet, altered)
    with pytest.raises(ValueError, match="duplicate"):
        validate_precision_batch(staged, packet, [reviews[0], reviews[0]])
    bad = deepcopy(reviews)
    bad[0]["training_approved"] = True
    with pytest.raises(ValueError, match="unexpected"):
        validate_precision_batch(staged, packet, bad)
    mixed = deepcopy(reviews)
    mixed[1]["reviewer_id"] = "reviewer-b"
    with pytest.raises(ValueError, match="one reviewer"):
        validate_precision_batch(staged, packet, mixed)


def test_two_human_batches_allow_partial_chunks_but_conflicts_need_arbitration(
    source: tuple[SupplierStaging, dict[str, Any]],
) -> None:
    staged, packet = source
    a = _decisions(packet, "reviewer-a")[:2]
    b = _decisions(packet, "reviewer-b")[:2]
    a[0].update(
        finding="approve_rounding", rounding_mode="half_up", rounded_price_rub="10.13",
        rationale="Supplier confirmed the half-up rule for this original record.",
    )
    consensus = adjudicate_precision_batches(staged, packet, a, b)
    assert consensus["reviewed_case_count"] == 2
    assert consensus["conflict_count"] == 1
    assert consensus["unresolved_case_ids"] == [a[0]["case_id"]]
    assert consensus["approved_rounding_count"] == 0
    assert consensus["canonical_importable"] is False
    with pytest.raises(ValueError, match="distinct"):
        adjudicate_precision_batches(staged, packet, a, b, adjudications=[a[0]])
    third = deepcopy(a[0])
    third["reviewer_id"] = "reviewer-c"
    resolved = adjudicate_precision_batches(
        staged, packet, a, b, adjudications=[third],
    )
    assert resolved["conflict_count"] == 0
    assert resolved["approved_rounding_count"] == 1
    assert resolved["consensus"][0]["source_row_sha256"] == a[0]["source_row_sha256"]
    assert resolved["consensus"][0]["rounded_price_rub"] == "10.13"
    assert resolved["consensus"][0]["finding"] == "approve_rounding"
    assert resolved["human_identity_authenticated"] is False
    with pytest.raises(ValueError, match="same cases"):
        adjudicate_precision_batches(staged, packet, a, b[:-1])
    with pytest.raises(ValueError, match="distinct"):
        adjudicate_precision_batches(staged, packet, a, deepcopy(a))


def test_templates_bounded_and_gaps_not_automatically_approved(
    source: tuple[SupplierStaging, dict[str, Any]],
) -> None:
    staged, packet = source
    with pytest.raises(ValueError, match="range|limit|start"):
        build_reviewer_template(packet, start=-1, limit=10)
    with pytest.raises(ValueError, match="range|limit|start"):
        build_reviewer_template(packet, start=0, limit=0)
    with pytest.raises(ValueError, match="range|limit|start"):
        build_reviewer_template(packet, start=0, limit=101)
    assert build_reviewer_template(packet, start=2, limit=100)[0]["case_id"] == packet["cases"][2]["case_id"]
    a = _decisions(packet, "reviewer-a")[:1]
    b = _decisions(packet, "reviewer-b")[:1]
    result = adjudicate_precision_batches(staged, packet, a, b)
    assert result["pending_count"] == 1
    assert result["total_packet_cases"] == 3
    assert result["untouched_case_count"] == 2
    assert result["published"] is False


def test_real_ecoplast_precision_packet_matches_immutable_original() -> None:
    from scripts.supplier_precision_batch import load_pinned_ecoplast_staging

    staged = load_pinned_ecoplast_staging()
    packet = build_precision_packet(staged, source_id="avt_el_ecoplast_20260615")
    assert len(packet["cases"]) == 3481
    assert verify_precision_packet(staged, packet) == 3481
    assert packet["source_sha256"] == staged.sha256
    assert all("source_row_sha256" in case for case in packet["cases"])
    assert not packet["precision_review_completed"]
