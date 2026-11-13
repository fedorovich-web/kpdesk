"""Opt-in rollback-only live writer probe using kpdesk_app (no DB owner).

Creates synthetic rows only within a caller-owned transaction, then ALWAYS rolls
back and verifies original row counts. Requires no active workspace catalog.
Never imports these synthetic products into the committed workspace.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import COLUMNS_V2, ImportLimits
from kpdesk.catalog.storage import get_active_catalog, publish_catalog
from kpdesk.db.session import app_engine, assert_unprivileged_session
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.quotes.pdf import render_draft_pdf
from kpdesk.quotes.storage import create_quote_draft, get_quote_draft, revise_quote_draft

TABLES = (
    "catalog_versions", "catalog_items", "idempotency_records",
    "quote_documents", "quote_revisions", "quote_write_requests",
)


def _counts(session: Session) -> tuple[int, ...]:
    # Table names are fixed source literals, never user input.
    return tuple(
        int(session.scalar(text(f"SELECT count(*) FROM public.{table}")) or 0)
        for table in TABLES
    )


def _example_request(quantity: int) -> ExtractedRequest:
    return ExtractedRequest.model_validate({
        "lines": [{
            "source_quote": "Карандаш графитовый HB",
            "description": "Карандаш графитовый HB",
            "category": "pencil", "quantity": quantity, "unit": "штука",
            "brand": None, "paper_format": None, "paper_gsm": None,
            "pen_ink_color": None, "pen_ink_type": None,
            "attributes": {"pencil_type": "graphite", "lead_grade": "HB"},
        }],
    })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--exercise-rollback", action="store_true",
        help="Temporarily write synthetic rows inside an uncommittable outer transaction.",
    )
    args = parser.parse_args()
    engine = app_engine()
    try:
        with Session(engine) as inspection:
            assert_unprivileged_session(inspection)
            if get_active_catalog(inspection) is not None:
                raise RuntimeError("Refusing to touch workspace with active catalog.")
            original = _counts(inspection)
            print("READ_ONLY_CHECK_OK role=kpdesk_app active_catalog=none")
        if not args.exercise_rollback:
            print("DRY_RUN: no DML, pass --exercise-rollback to check write grants.")
            return

        stream = io.StringIO(newline="")
        csv.writer(stream, delimiter=";", lineterminator="\n").writerows([
            list(COLUMNS_V2),
            ["SYNTH-ROLLBACK-1", "Карандаш графитовый HB", "pencil",
             "штука", 1, "19.90", "", "", "", "", "",
             json.dumps({"pencil_type": "graphite", "lead_grade": "HB"})],
        ])
        parsed = parse_price_list("synthetic-rollback.csv", stream.getvalue().encode(), ImportLimits())
        assert parsed.publishable
        suffix = uuid.uuid4().hex
        # SQLAlchemy Session commits below do not commit the externally-owned
        # connection transaction. ALWAYS rollback that outer transaction.
        with engine.connect() as conn:
            outer = conn.begin()
            with Session(bind=conn) as session:
                try:
                    created = publish_catalog(
                        session, parsed,
                        idempotency_key=f"rollback-catalog-{suffix}",
                        request_hash=parsed.source_sha256,
                    )
                    assert outer.is_active, "Unexpected commit of externally managed transaction."
                    first = create_quote_draft(
                        session, created.id, _example_request(2),
                        key=f"rollback-quote-{suffix}",
                    )
                    assert first.total_rub is not None
                    assert str(first.total_rub) == "39.80"
                    assert len(render_draft_pdf(first)) > 1000
                    second = revise_quote_draft(
                        session, first.quote_id, first.revision,
                        created.id, _example_request(3),
                        key=f"rollback-revision-{suffix}",
                    )
                    assert str(second.total_rub) == "59.70"
                    old = get_quote_draft(session, first.quote_id, revision=1)
                    assert str(old.total_rub) == "39.80"
                    assert second.revision == 2
                    assert outer.is_active
                    print("RUNTIME_WRITE_GRANTS_OK import-to-draft-to-revision-to-PDF")
                finally:
                    # Roll back even on any exception, including a failed assertion.
                    session.close()
                    if outer.is_active:
                        outer.rollback()
        with Session(engine) as inspection:
            assert_unprivileged_session(inspection)
            assert get_active_catalog(inspection) is None
            assert _counts(inspection) == original, "Rollback did not restore row counts."
            print("ROLLBACK_VERIFIED workspace_database_unchanged=true")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
