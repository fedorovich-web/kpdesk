"""A96: full-taxonomy HTTPS catalog → ML suggestions → human quote → PDF.

Executed ONLY after A45 history/scaling on its provenance-marked disposable
PostgreSQL clone. No fixtures are asserted to be real supplier gold labels.
"""
from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from canonical_a97_taxonomy_migration_rehearsal import (
    assert_downgrade_denied_with_history,
    rehearse_catalog_category_migration,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Engine, text
from test_multicategory_canonical_scale import (
    _canonical_csv,
    _verified_synthetic_canonical_rows,
)

from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import ImportLimits
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from kpdesk.matching.schema import CatalogProduct
from scripts.build_a46_taxonomy_coverage import build_taxonomy_coverage


def _synthetic_multicategory_catalog() -> tuple[bytes, tuple[CatalogProduct, ...]]:
    body = build_taxonomy_coverage()
    provenance = body.get("provenance")
    if not isinstance(provenance, dict) or (
        provenance.get("kind") != "synthetic_example"
        or provenance.get("independent_gold") is not False
        or provenance.get("production_training") is not False
    ):
        raise ValueError("A96 requires declared synthetic-only catalog provenance")
    if len(body["catalog"]) != 183:
        raise ValueError("A96 original hand-authored seed catalog changed")
    # The A46 ML examples are not all strict canonical-importable (four pen
    # examples omit pen_ink_type). Use the separately validated synthetic
    # canonical fixture, which supplies all mandatory product facts.
    source = _canonical_csv(_verified_synthetic_canonical_rows(183))
    parsed = parse_price_list("a96-full-taxonomy.csv", source, ImportLimits())
    if not parsed.publishable:
        raise AssertionError(f"A96 strict canonical CSV rejected: {parsed.errors[:5]}")
    originals = parsed.products
    if (
        len(originals) != 183
        or {product.category for product in originals} != set(CATEGORY_ATTRIBUTES)
        or len({product.sku for product in originals}) != 183
    ):
        raise ValueError("A96 synthetic 40-category canonical inventory changed")
    return source, originals


def _request_line(item: CatalogProduct, *, quantity: int, description: str) -> dict[str, Any]:
    return {
        "source_quote": description,
        "description": description,
        "category": item.category,
        "quantity": quantity,
        "unit": item.sale_unit,
        "brand": item.brand,
        "paper_format": item.paper_format,
        "paper_gsm": item.paper_gsm,
        "pen_ink_color": item.pen_ink_color,
        "pen_ink_type": item.pen_ink_type,
        "attributes": item.attributes,
    }


def verify_full_taxonomy_quote_journey(
    *, owner: Engine, organization: uuid.UUID, outsider: uuid.UUID,
    session: IssuedSession, factory: Callable[[str], FastAPI],
) -> dict[str, object]:
    """Real tenant HTTP + SQL RLS + immutable snapshots; one disposable clone."""
    with owner.connect() as connection:
        if not is_live_accounts_http_clone(connection):
            raise RuntimeError("A96 may run only on the provenance-marked A45 clone")
        if connection.scalar(text("SELECT current_database()")) == "kpdesk":
            raise RuntimeError("A96 refuses live database")
        if connection.scalar(text("SELECT version_num FROM public.alembic_version")) != (
            "0030_tenant_llm_cache"
        ):
            raise RuntimeError("A96 requires the released 0030 database schema")
        prior_quotes = connection.scalar(text("""
            SELECT count(*) FROM public.quote_documents WHERE organization_id=:org
        """), {"org": organization})
        # P4 intentionally commits one revision-less head *after* the 1000
        # valid documents, to test the guarded list query under a real orphan.
        if prior_quotes != 1001:
            raise RuntimeError("A96 requires 1000 P4 quotes plus its orphan head")

    source, originals = _synthetic_multicategory_catalog()
    # As-shipped DB: preflight/import MUST reject the six unsupported
    # categories without accidentally changing the active catalog.
    root = f"/api/session/organizations/{organization}"
    headers = {
        "Origin": "https://testserver", "X-CSRF-Token": session.csrf_token,
    }
    with TestClient(factory("https://testserver"), base_url="https://testserver") as check:
        check.cookies.set(SESSION_COOKIE, session.session_token)
        baseline = check.get(root + "/catalog/current")
        assert baseline.status_code == 200
        rejected_preflight = check.post(
            root + "/catalog/preflight", headers=headers,
            files={"file": ("a96-full-taxonomy.csv", source, "text/csv")},
        )
        assert rejected_preflight.status_code == 200, rejected_preflight.text
        assert rejected_preflight.json()["publishable"] is False
        assert rejected_preflight.json()["rows_count"] == 183
        blocked_issues = rejected_preflight.json()["errors"]
        assert len(blocked_issues) == 1
        assert blocked_issues[0]["code"] == "database_category_unavailable"
        rejected_import = check.post(
            root + "/catalog/import", headers={
                **headers, "Idempotency-Key": "a96-release-schema-mismatch",
                "X-Source-SHA256": hashlib.sha256(source).hexdigest(),
            },
            files={"file": ("a96-full-taxonomy.csv", source, "text/csv")},
        )
        assert rejected_import.status_code == 409, rejected_import.text
        assert rejected_import.json()["code"] == "catalog_schema_taxonomy_mismatch"
        assert check.get(root + "/catalog/current").json() == baseline.json()
    # REHEARSAL ONLY. The release DB still rejects six Python V4 categories.
    # No working database DDL/migration is authorized in this task.
    missing_from_release = rehearse_catalog_category_migration(owner)
    assert all(name in blocked_issues[0]["message"] for name in missing_from_release)
    chosen = tuple(next(
        p for p in originals if p.category == category
    ) for category in ("paper", "pencil", "keyboard", "usb_drive"))
    counts = (2, 3, 4, 5)
    expected_total = sum(
        (p.price_rub * quantity for p, quantity in zip(chosen, counts, strict=True)),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"))
    # Each requested quantity is expressed in its published sale unit;
    # package multiplicity remains preserved in the canonical catalog.
    prefix = f"/api/session/organizations/{organization}"
    cross = f"/api/session/organizations/{outsider}"
    headers = {
        "Origin": "https://testserver", "X-CSRF-Token": session.csrf_token,
    }
    digest = hashlib.sha256(source).hexdigest()
    filename = "a96-full-taxonomy.csv"
    with TestClient(factory("https://testserver"), base_url="https://testserver") as client:
        assert client.get(prefix + "/catalog/current").status_code == 401
        client.cookies.set(SESSION_COOKIE, session.session_token)
        preflight = client.post(
            prefix + "/catalog/preflight", headers=headers,
            files={"file": (filename, source, "text/csv")},
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.json() == {
            "source_sha256": digest,
            "publishable": True,
            "rows_count": 183,
            "errors": [],
            "total_errors": 0,
        }
        assert client.post(
            cross + "/catalog/preflight", headers=headers,
            files={"file": (filename, source, "text/csv")},
        ).status_code == 403
        imported = client.post(
            prefix + "/catalog/import", headers={
                **headers, "Idempotency-Key": "a96-full-taxonomy-publish",
                "X-Source-SHA256": digest,
            },
            files={"file": (filename, source, "text/csv")},
        )
        assert imported.status_code == 201, imported.text
        publication = imported.json()
        assert publication["source_sha256"] == digest
        assert publication["rows_count"] == 183
        version = publication["version_id"]
        active = client.get(prefix + "/catalog/current")
        assert active.status_code == 200
        assert active.json()["version_id"] == version

        request_lines = [
            _request_line(item, quantity=quantity, description=item.name)
            for item, quantity in zip(chosen, counts, strict=True)
        ]
        for item, line in zip(chosen, request_lines, strict=True):
            matched = client.post(
                prefix + "/matches/preview", headers=headers,
                json={
                    "catalog_version_id": version,
                    "requested_item": {
                        "description": line["description"],
                        "category": line["category"],
                        "quantity": line["quantity"], "unit": line["unit"],
                        "brand": line["brand"], "paper_format": line["paper_format"],
                        "paper_gsm": line["paper_gsm"],
                        "pen_ink_color": line["pen_ink_color"],
                        "pen_ink_type": line["pen_ink_type"],
                        "attributes": line["attributes"],
                    },
                    "limit": 20,
                },
            )
            assert matched.status_code == 200, matched.text
            candidates = matched.json()
            assert candidates["calibrated_no_match"] is False
            assert candidates["requires_manager_confirmation"] is True
            assert any(candidate["sku"] == item.sku
                       for candidate in candidates["candidates"])
        # A similar name must not cause a silent fallback to the wrong SKU.
        broken = [dict(row) for row in request_lines]
        broken[1]["description"] += " другое исполнение"
        broken[1]["source_quote"] = broken[1]["description"]
        broken.append({
            **request_lines[0],
            "description": "Бумага особая A0 900 г/м2 несуществующая",
            "source_quote": "Бумага особая A0 900 г/м2 несуществующая",
            "quantity": 1, "paper_format": "A0", "paper_gsm": 900,
        })
        quote_api = prefix + "/quotes"
        created = client.post(
            quote_api, headers={
                **headers, "Idempotency-Key": "a96-incomplete-five-categories",
            }, json={
                "catalog_version_id": version,
                "extracted_request": {"lines": broken},
            },
        )
        assert created.status_code == 201, created.text
        incomplete = created.json()
        quote_id = incomplete["quote_id"]
        assert incomplete["revision"] == 1
        assert incomplete["ready_for_draft"] is False
        assert incomplete["total_rub"] is None
        assert incomplete["lines"][1]["status"] == "needs_review"
        assert incomplete["lines"][1]["sku"] is None
        assert set(incomplete["lines"][1]["reason_codes"]).intersection({
            "unverified_product_identity", "multiple_compatible_skus",
        })
        assert incomplete["lines"][4]["status"] in ("no_match", "needs_review")
        assert incomplete["lines"][4]["sku"] is None
        assert client.get(f"{quote_api}/{quote_id}/pdf").status_code == 409
        assert client.get(f"{cross}/quotes/{quote_id}/pdf").status_code == 403

        # Human corrects the unsupported text, drops absent SKU, then explicitly
        # selects all four source-bound SKUs; no ML/LLM approval is trusted.
        fixed = client.post(
            f"{quote_api}/{quote_id}/revisions",
            headers={
                **headers, "Idempotency-Key": "a96-human-reviewed-four-lines",
            },
            json={
                "expected_revision": 1, "catalog_version_id": version,
                "extracted_request": {"lines": request_lines},
                "manager_selections": [
                    {"line_number": n, "sku": product.sku}
                    for n, product in enumerate(chosen, 1)
                ],
            },
        )
        assert fixed.status_code == 201, fixed.text
        corrected = fixed.json()
        assert corrected["revision"] == 2
        assert corrected["ready_for_draft"] is True
        assert corrected["approved"] is False
        assert Decimal(corrected["total_rub"]) == expected_total
        assert all(line["status"] == "manager_selected"
                   for line in corrected["lines"])
        assert [line["sku"] for line in corrected["lines"]] == [
            product.sku for product in chosen
        ]
        for line, product, qty in zip(
            corrected["lines"], chosen, counts, strict=True,
        ):
            assert Decimal(line["unit_price_rub"]) == product.price_rub
            assert Decimal(line["line_total_rub"]) == product.price_rub * qty
        old = client.get(f"{quote_api}/{quote_id}", params={"revision": 1})
        assert old.status_code == 200
        assert old.json()["snapshot_sha256"] == incomplete["snapshot_sha256"]
        assert old.json()["total_rub"] is None
        assert client.get(
            f"{quote_api}/{quote_id}/pdf", params={"revision": 1},
        ).status_code == 409

        audit = client.get(f"{quote_api}/{quote_id}/manager-decisions/2")
        assert audit.status_code == 200, audit.text
        assert [entry["sku"] for entry in audit.json()["items"]] == [
            item.sku for item in chosen
        ]
        assert all(entry["approved_by_user_id"] == str(session.user_id)
                   for entry in audit.json()["items"])
        pdf_response = client.get(f"{quote_api}/{quote_id}/pdf")
        assert pdf_response.status_code == 200, pdf_response.text[:400]
        assert pdf_response.headers["content-type"].startswith("application/pdf")
        document = PdfReader(io.BytesIO(pdf_response.content))
        rendered = "\n".join(page.extract_text() for page in document.pages)
        assert all(item.sku in rendered for item in chosen)
        assert format(expected_total, ".2f") in rendered
        assert quote_id in rendered
        assert "Не утверждено" in rendered
        assert client.get(f"{cross}/quotes/{quote_id}").status_code == 403

    with owner.connect() as connection:
        assert is_live_accounts_http_clone(connection)
        metrics = connection.execute(text("""
            SELECT COUNT(*),COUNT(DISTINCT category),COUNT(DISTINCT sku)
            FROM public.catalog_items
            WHERE organization_id=:org AND catalog_version_id=:catalog
        """), {"org": organization, "catalog": uuid.UUID(version)}).one()
        assert metrics == (183, 40, 183)
        assert connection.scalar(text("""
            SELECT count(*) FROM public.quote_documents
            WHERE organization_id=:org
        """), {"org": organization}) == prior_quotes + 1
        assert connection.scalar(text("""
            SELECT count(*) FROM public.tenant_quote_manager_decisions
            WHERE organization_id=:org AND quote_id=:quote AND revision=2
        """), {"org": organization, "quote": uuid.UUID(quote_id)}) == 4

    assert_downgrade_denied_with_history(owner)

    return {
        "kind": "a96_multicategory_disposable_clone_e2e",
        "catalog_skus": 183,
        "taxonomy_categories": 40,
        "request_categories": [item.category for item in chosen],
        "source_sha256": digest,
        "first_revision_incomplete": True,
        "unverified_title_rejected": True,
        "missing_product_rejected": True,
        "historical_pdf_rejected": True,
        "manager_attested_skus": 4,
        "completed_revision": 2,
        "total_rub": format(expected_total, ".2f"),
        "pdf_verified_real_render": True,
        "foreign_tenant_forbidden": True,
        "production_eligible": False,
        "release_schema_tenants_support_all_categories": False,
        "as_shipped_preflight_rejects_unsupported_categories": True,
        "as_shipped_import_returns_controlled_409_without_mutation": True,
        "prototype_ddl_disposable_clone_only": True,
        "live_migration_not_applied": True,
        "categories_missing_from_live_release_schema": list(missing_from_release),
        "independent_supplier_data": False,
        "browser_engine": "TestClient; real HTTP routing, no external TCP/TLS",
    }
