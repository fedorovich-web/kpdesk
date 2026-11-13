"""Committed drafts: immutable revisions, idempotency, catalog price snapshots."""

import csv
import io
import json
import os
from collections.abc import Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from postgres_clone_connection import LEGACY_TEST_OPT_IN, legacy_test_database
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import COLUMNS_V2, ImportLimits
from kpdesk.catalog.storage import publish_catalog
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.quotes.storage import (
    QuoteConflict,
    QuoteNotFound,
    create_quote_draft,
    get_quote_draft,
    revise_quote_draft,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(LEGACY_TEST_OPT_IN) != "1",
    reason="Explicit opt-in required for per-test marked disposable PostgreSQL clones",
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    with legacy_test_database() as engine:
        yield engine



def catalog_fixture(engine: Engine) -> str:
    f=io.StringIO(newline="")
    csv.writer(f,delimiter=";",lineterminator="\n").writerows([
        list(COLUMNS_V2),
        ["PENCIL-1","Карандаш графитовый HB","pencil","штука",1,"19.90",
         "Test","","","","",json.dumps({"pencil_type":"graphite","lead_grade":"HB"})],
        ["FOLDER-1","Папка для документов A4 на молнии","document_folder","штука",1,"125.50",
         "Test","","","","",json.dumps({"folder_kind":"zip","format":"A4"})],
    ])
    parsed=parse_price_list("office.csv",f.getvalue().encode(),ImportLimits())
    assert parsed.publishable,parsed.errors
    with Session(engine) as session:
        return str(publish_catalog(session,parsed,str(uuid4()),parsed.source_sha256).id)


def extracted(quantity: int = 2, *, ambiguous: bool = False) -> ExtractedRequest:
    line = {
        "source_quote":"карандаши графитовые HB",
        "description":"Карандаш графитовый HB",
        "category":"pencil","quantity":quantity,"unit":"штука",
        "brand":None,"paper_format":None,"paper_gsm":None,
        "pen_ink_color":None,"pen_ink_type":None,
        "attributes":{"pencil_type":"graphite","lead_grade":"HB"},
    }
    if ambiguous:
        line["quantity"]=None
    return ExtractedRequest.model_validate({"lines":[line]})


def test_create_quote_snapshot_replay_and_database_immutable_revision(engine: Engine) -> None:
    catalog_id=catalog_fixture(engine)
    with Session(engine) as session:
        created=create_quote_draft(session, catalog_id, extracted(), "q-create-A")
    assert created.revision==1
    assert created.ready_for_draft
    assert created.total_rub==Decimal("39.80")
    assert created.lines[0].sku=="PENCIL-1"
    assert created.lines[0].unit_price_rub==Decimal("19.90")
    assert created.lines[0].requested_quantity==2
    assert created.source_request == extracted()
    assert created.source_request.lines[0].attributes == {
        "pencil_type": "graphite", "lead_grade": "HB",
    }
    assert not created.approved
    assert created.snapshot_sha256
    with Session(engine) as session:
        replay=create_quote_draft(session,catalog_id,extracted(),"q-create-A")
        assert replay==created
        read=get_quote_draft(session,created.quote_id)
        assert read==created
    with Session(engine) as session:
        raw: dict[str, object] = session.execute(
            text("SELECT snapshot FROM quote_revisions")
        ).scalar_one()
        assert raw["source_request"] == extracted().model_dump(mode="json")
        assert session.scalar(text("SELECT COUNT(*) FROM quote_documents"))==1
        assert session.scalar(text("SELECT COUNT(*) FROM quote_revisions"))==1
        session.rollback()
        with pytest.raises(DBAPIError, match="Historical quote revisions are immutable"), session.begin():
            session.execute(text(
                "UPDATE quote_revisions SET snapshot_sha256 = 'tamper'"
            ))


def test_change_creates_new_immutable_revision_and_optimistic_conflict(engine: Engine) -> None:
    catalog_id=catalog_fixture(engine)
    with Session(engine) as session:
        first=create_quote_draft(session,catalog_id,extracted(2),"q-create-B")
    with Session(engine) as session:
        second=revise_quote_draft(
            session,first.quote_id,1,catalog_id,extracted(3),"q-change-B",
        )
    assert second.revision==2
    assert second.quote_id==first.quote_id
    assert second.total_rub==Decimal("59.70")
    with Session(engine) as session:
        assert get_quote_draft(session,first.quote_id,revision=1)==first
        assert get_quote_draft(session,first.quote_id)==second
        session.rollback()  # subsequent write opens its own transaction
        assert revise_quote_draft(session,first.quote_id,1,catalog_id,extracted(3),"q-change-B")==second
        with pytest.raises(QuoteConflict,match="stale_revision"):
            revise_quote_draft(session,first.quote_id,1,catalog_id,extracted(4),"q-change-C")
    with Session(engine) as session:
        assert session.scalar(text("SELECT COUNT(*) FROM quote_revisions"))==2


def test_idempotency_key_collision_and_missing_quote(engine: Engine) -> None:
    catalog_id=catalog_fixture(engine)
    with Session(engine) as session:
        first=create_quote_draft(session,catalog_id,extracted(2),"q-create-C")
        with pytest.raises(QuoteConflict,match="idempotency_conflict"):
            create_quote_draft(session,catalog_id,extracted(3),"q-create-C")
        with pytest.raises(QuoteNotFound):
            get_quote_draft(session,uuid4())
        session.rollback()
        with pytest.raises(QuoteConflict,match="idempotency_conflict"):
            revise_quote_draft(session,first.quote_id,1,catalog_id,extracted(2),"q-create-C")


def test_unresolved_quote_persists_without_false_total(engine: Engine) -> None:
    catalog_id=catalog_fixture(engine)
    with Session(engine) as session:
        record=create_quote_draft(session,catalog_id,extracted(ambiguous=True),"q-incomplete")
    assert record.ready_for_draft is False
    assert record.total_rub is None
    assert record.lines[0].status=="needs_review"
    assert record.lines[0].sku is None


def test_changed_catalog_blocks_new_revision_but_retains_prior_snapshot(engine: Engine) -> None:
    old_catalog=catalog_fixture(engine)
    with Session(engine) as session:
        created=create_quote_draft(session,old_catalog,extracted(2),"q-snap-A")
    _ = catalog_fixture(engine)  # new immutable catalog, old price history preserved
    with Session(engine) as session:
        assert get_quote_draft(session,created.quote_id)==created
        session.rollback()
        with pytest.raises(QuoteConflict,match="stale_catalog"):
            revise_quote_draft(
                session,created.quote_id,1,old_catalog,extracted(3),"q-snap-B",
            )
