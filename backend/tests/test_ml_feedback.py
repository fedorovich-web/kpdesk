"""Independent reviewed corrections become training labels, never live self-training."""
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kpdesk.matching.feedback import MatchFeedback, curate_feedback
from kpdesk.matching.schema import CatalogProduct, RequestedItem

CATALOG_VERSION = uuid4()


def product(sku: str, color: str) -> CatalogProduct:
    return CatalogProduct(
        sku=sku, name="Маркер перманентный",
        category="marker", sale_unit="штука", pieces_per_sale_unit=1,
        price_rub=Decimal("50.00"),
        attributes={"ink_color": color, "marker_type": "permanent"},
    )


def request() -> RequestedItem:
    return RequestedItem(
        description="Маркер перманентный черный", category="marker", quantity=3,
        unit="штука", attributes={"ink_color":"black","marker_type":"permanent"},
    )


def feedback(query_id: str, sku: str | None, verdict: str) -> MatchFeedback:
    return MatchFeedback.model_validate({
        "event_id": str(uuid4()),
        "catalog_version_id": str(CATALOG_VERSION),
        "query_id": query_id, "query_group_id": query_id,
        "request": request().model_dump(mode="json"),
        "sku": sku, "verdict": verdict,
        "reviewed_by": "human-reviewer",
        "reviewed_at": datetime(2026, 10, 8, tzinfo=UTC).isoformat(),
        "source": "human_review",
    })


def test_verified_corrections_retain_positive_and_explicit_hard_negative() -> None:
    catalog={"M-B": product("M-B","black"),"M-R":product("M-R","red")}
    batch=curate_feedback([
        feedback("q1","M-B","relevant"),
        feedback("q1","M-R","irrelevant"),
        feedback("q2",None,"no_match"),
    ], catalog, CATALOG_VERSION)
    assert len(batch.labels)==2
    assert batch.labels[0].relevant_skus==frozenset({"M-B"})
    assert batch.explicit_negatives["q1"]==frozenset({"M-R"})
    assert batch.labels[1].relevant_skus==frozenset()
    assert len(batch.provenance_sha256)==64


def test_unsafe_self_label_and_missing_reviewer_are_rejected() -> None:
    event=feedback("q1","M-B","relevant").model_dump(mode="json")
    event["source"]="model_prediction"
    with pytest.raises(ValidationError):
        MatchFeedback.model_validate(event)
    event["source"]="human_review"
    event["reviewed_by"]=""
    with pytest.raises(ValidationError):
        MatchFeedback.model_validate(event)


def test_contradictory_labels_and_incompatible_positive_are_rejected() -> None:
    catalog={"M-B":product("M-B","black"),"M-R":product("M-R","red")}
    with pytest.raises(ValueError,match="conflict"):
        curate_feedback([
            feedback("q1","M-B","relevant"),
            feedback("q1","M-B","irrelevant"),
        ],catalog,CATALOG_VERSION)
    with pytest.raises(ValueError,match="incompatible"):
        curate_feedback([feedback("q1","M-R","relevant")],catalog,CATALOG_VERSION)


def test_unresolved_negatives_are_not_mislabeled_as_no_match() -> None:
    catalog={"M-B":product("M-B","black")}
    with pytest.raises(ValueError,match="final"):
        curate_feedback([feedback("q1","M-B","irrelevant")],catalog,CATALOG_VERSION)


def test_human_rejected_hard_negative_enters_train_pairs_only() -> None:
    from kpdesk.matching.training import _pairs_for_training

    catalog=[product("M-B","black"),product("M-R","red")]
    record=curate_feedback([
        feedback("q1","M-B","relevant"),
        feedback("q1","M-R","irrelevant"),
    ],{p.sku:p for p in catalog},CATALOG_VERSION)
    pairs,labels=_pairs_for_training(
        record.labels,catalog,limit=1,
        explicit_negatives=record.explicit_negatives,
    )
    assert len(pairs)==2
    assert {p.product.sku for p in pairs}=={"M-B","M-R"}
    assert dict(zip((p.product.sku for p in pairs),labels,strict=True))=={
        "M-B":1,"M-R":0,
    }
