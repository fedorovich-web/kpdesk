"""A101 real XLSX price update, independent source review and HTTPS diff.

Run last on the disposable A45 clone: catalog.publish updates the active
catalog, so no subsequent benchmark or existing quote workflow may rely on it.
"""
from __future__ import annotations

import csv
import hashlib
import io
import uuid
from collections.abc import Callable

from canonical_review_publication_acceptance import _canonical_preview
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import Engine, text

from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_inference import inspect_supplier_source
from kpdesk.catalog.supplier_mapping_storage import TenantSupplierMappingStore
from kpdesk.catalog.supplier_profiles import approve_supplier_profile
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.tenant_access import AuthenticatedUser


def verify_source_update_diff_https(
    *, tenant: Engine, owner: Engine,
    organization: uuid.UUID, outsider: uuid.UUID,
    editor_id: uuid.UUID, session: IssuedSession,
    app_factory: Callable[[str], FastAPI],
) -> None:
    root = f"/api/session/organizations/{organization}"
    route = root + "/quotes/supplier-comparison"
    headers = {"Origin": "https://testserver", "X-CSRF-Token": session.csrf_token}
    actor = AuthenticatedUser(editor_id)
    canonical = _canonical_preview(organization, editor_id)
    original_csv = canonical.csv_content.replace(b"1250.00", b"1199.00", 1)
    assert original_csv != canonical.csv_content
    changed_csv = original_csv.replace(b"1199.00", b"1111.00", 1)
    assert changed_csv != original_csv
    source_rows = list(csv.reader(
        io.StringIO(changed_csv.decode("utf-8-sig"), newline=""),
        delimiter=";",
    ))
    assert source_rows and source_rows[0][0] == "sku"
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Products"
    for row in source_rows:
        sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    updated_bytes = stream.getvalue()
    updated_import = parse_price_list("updated-source.xlsx", updated_bytes, ImportLimits())
    assert updated_import.publishable and len(updated_import.products) == 2
    inspection = inspect_supplier_source("updated-source.xlsx", updated_bytes)
    columns = inspection.observed_columns
    assert inspection.source_format == "xlsx"
    profile = approve_supplier_profile(
        inspection, organization_id=organization, supplier_id="supplier-second",
        reviewer=actor, source_sha256=hashlib.sha256(updated_bytes).hexdigest(),
        selected_columns={
            "sku": columns.index("sku"), "name": columns.index("name"),
            "price": columns.index("price_rub"),
        }, price_basis="standard", confirmed_currency="RUB",
        review_evidence="Independent human confirmation of updated XLSX price columns.",
        explicitly_confirmed=True,
    )
    with TestClient(app_factory("https://testserver"), base_url="https://testserver") as client:
        assert client.post(route + "/supplier-sources", headers=headers, json={
            "supplier_id": "supplier-second",
        }).status_code == 401
        client.cookies.set(SESSION_COOKIE, session.session_token)
        assert client.post(route + "/supplier-sources", json={
            "supplier_id": "supplier-second",
        }, headers={"Origin": "https://testserver"}).status_code == 401
        assert client.post(
            f"/api/session/organizations/{outsider}/quotes/supplier-comparison/supplier-sources",
            headers=headers, json={"supplier_id": "supplier-second"},
        ).status_code == 403
        initial = client.post(route + "/supplier-sources", headers=headers, json={
            "supplier_id": "supplier-second",
        })
        assert initial.status_code == 200, initial.text
        original = next(item for item in initial.json()["options"]
                        if item["evidence"] == "original")
        assert original["supplier_id"] == "supplier-second"

        with tenant.begin() as connection:
            TenantSupplierMappingStore(
                connection, actor, organization,
                session_token=session.session_token, csrf_token=session.csrf_token,
            ).save(profile)
            updated = TenantCatalogRepository(
                connection, actor, organization,
                session_token=session.session_token, csrf_token=session.csrf_token,
            ).publish(
                updated_import, "a101-independent-reviewed-xlsx-update",
                updated_import.source_sha256,
            )
        assert updated.id != uuid.UUID(original["catalog_version_id"])
        assert updated.source_sha256 == inspection.source_sha256

        recent = client.post(route + "/supplier-sources", headers=headers, json={
            "supplier_id": "supplier-second",
        })
        assert recent.status_code == 200, recent.text
        assert recent.headers["Cache-Control"] == "no-store"
        assert recent.json()["truncated"] is False
        newer = next(
            item for item in recent.json()["options"]
            if item["catalog_version_id"] == str(updated.id)
        )
        assert newer["profile_fingerprint"] == profile.fingerprint
        assert newer["evidence"] == "original"
        assert newer["source_sha256"] != original["source_sha256"]

        def ref(option: dict[str, object]) -> dict[str, object]:
            return {
                "supplier_id": option["supplier_id"],
                "profile_fingerprint": option["profile_fingerprint"],
                "catalog_version_id": option["catalog_version_id"],
            }

        pair = {"before": ref(original), "after": ref(newer), "limit": 1}
        compared = client.post(route + "/source-diff", headers=headers, json=pair)
        assert compared.status_code == 200, compared.text
        assert compared.headers["Cache-Control"] == "no-store"
        report = compared.json()
        assert report["requires_manager_review"] is True
        assert report["supplier_id"] == "supplier-second"
        assert report["total_changes"] == 1
        assert report["counts"]["price_changed"] == 1
        assert report["counts"]["product_changed"] == 0
        assert report["counts"]["product_and_price_changed"] == 0
        assert len(report["changes"]) == 1
        delta = report["changes"][0]
        assert delta["before_price_rub"] == "1199.00"
        assert delta["after_price_rub"] == "1111.00"
        assert delta["comparable_price_delta_rub"] == "-88.00"
        assert report["next_offset"] is None
        assert client.post(route + "/source-diff", headers=headers, json={
            "before": ref(original), "after": ref(original),
        }).status_code == 422
        assert client.post(route + "/source-diff", headers=headers, json={
            **pair, "offset": 10001,
        }).status_code == 422
        assert client.post(route + "/source-diff", headers={
            "Origin": "https://testserver",
        }, json=pair).status_code == 401
        assert client.post(
            f"/api/session/organizations/{outsider}/quotes/supplier-comparison/source-diff",
            headers=headers, json=pair,
        ).status_code == 403

    # Comparison never writes a new quote revision or mutates old snapshots.
    with owner.connect() as connection:
        assert connection.scalar(text("""
            SELECT count(*) FROM public.quote_revisions
            WHERE organization_id=:org
              AND snapshot::text LIKE '%supplier-a79%'
              AND snapshot::text LIKE '%source_catalog_version_id%'
        """), {"org": organization}) >= 1

    verify_same_layout_historical_diff_https(
        tenant=tenant, owner=owner,
        organization=organization, outsider=outsider, editor_id=editor_id,
        session=session, app_factory=app_factory,
        prior_xlsx=updated_bytes, prior_version_id=updated.id,
        prior_profile_sha256=profile.profile_sha256,
        prior_fingerprint=profile.fingerprint,
    )


def verify_same_layout_historical_diff_https(
    *, tenant: Engine, owner: Engine, organization: uuid.UUID,
    outsider: uuid.UUID, editor_id: uuid.UUID, session: IssuedSession,
    app_factory: Callable[[str], FastAPI], prior_xlsx: bytes,
    prior_version_id: uuid.UUID, prior_profile_sha256: str,
    prior_fingerprint: str,
) -> None:
    """One supplier, exact same XLSX layout, two independently reviewed bytes."""
    actor = AuthenticatedUser(editor_id)
    book = load_workbook(io.BytesIO(prior_xlsx), read_only=False, data_only=False)
    try:
        sheet = book.active
        assert sheet is not None and sheet.title == "Products"
        changed = False
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                if str(cell.value) == "1111.00":
                    cell.value = "990.00"
                    changed = True
        assert changed
        stream = io.BytesIO()
        book.save(stream)
        next_bytes = stream.getvalue()
    finally:
        book.close()
    assert next_bytes != prior_xlsx
    imported = parse_price_list("same-layout-refreshed.xlsx", next_bytes, ImportLimits())
    assert imported.publishable and len(imported.products) == 2
    inspection = inspect_supplier_source("same-layout-refreshed.xlsx", next_bytes)
    names = inspection.observed_columns
    reviewed = approve_supplier_profile(
        inspection, organization_id=organization, supplier_id="supplier-second",
        reviewer=actor, source_sha256=hashlib.sha256(next_bytes).hexdigest(),
        selected_columns={
            "sku": names.index("sku"), "name": names.index("name"),
            "price": names.index("price_rub"),
        }, price_basis="standard", confirmed_currency="RUB",
        review_evidence="Separate human verification of refreshed XLSX price bytes.",
        explicitly_confirmed=True,
    )
    assert reviewed.fingerprint == prior_fingerprint
    assert reviewed.profile_sha256 != prior_profile_sha256

    with owner.connect() as connection:
        immutable_quotes_before = connection.execute(text("""
            SELECT quote_id,number,snapshot::text FROM public.quote_revisions
            WHERE organization_id=:org ORDER BY quote_id,number
        """), {"org": organization}).all()
    assert immutable_quotes_before

    with tenant.begin() as connection:
        TenantSupplierMappingStore(
            connection, actor, organization, session.session_token, session.csrf_token,
        ).save(reviewed)
        published = TenantCatalogRepository(
            connection, actor, organization,
            session_token=session.session_token, csrf_token=session.csrf_token,
        ).publish(
            imported, "a102-independent-same-layout-refresh",
            imported.source_sha256,
        )
    assert published.id != prior_version_id

    root = f"/api/session/organizations/{organization}"
    headers = {"Origin": "https://testserver", "X-CSRF-Token": session.csrf_token}
    history = root + "/supplier-price-history"
    live = root + "/quotes/supplier-comparison"
    with TestClient(app_factory("https://testserver"), base_url="https://testserver") as client:
        assert client.post(
            history + "/sources", json={"supplier_id": "supplier-second"},
            headers=headers,
        ).status_code == 401
        client.cookies.set(SESSION_COOKIE, session.session_token)
        assert client.post(
            history + "/sources", json={"supplier_id": "supplier-second"},
            headers={"Origin": "https://testserver"},
        ).status_code == 401
        assert client.post(
            f"/api/session/organizations/{outsider}/supplier-price-history/sources",
            json={"supplier_id": "supplier-second"}, headers=headers,
        ).status_code == 403

        archival = client.post(
            history + "/sources", json={"supplier_id": "supplier-second"},
            headers=headers,
        )
        assert archival.status_code == 200, archival.text
        assert archival.headers["Cache-Control"] == "no-store"
        assert archival.json()["audit_only"] is True
        assert archival.json()["truncated"] is False
        options = archival.json()["options"]
        before = next(item for item in options
                      if item["catalog_version_id"] == str(prior_version_id))
        after = next(item for item in options
                     if item["catalog_version_id"] == str(published.id))
        assert before["profile_sha256"] == prior_profile_sha256
        assert after["profile_sha256"] == reviewed.profile_sha256
        assert before["profile_fingerprint"] == after["profile_fingerprint"]
        assert before["source_sha256"] != after["source_sha256"]
        assert before["evidence"] == after["evidence"] == "original"

        # Audit-only history MUST NOT resurrect the old XLSX approval in the
        # active quote-source discovery (latest profile points at new bytes).
        current = client.post(
            live + "/supplier-sources",
            json={"supplier_id": "supplier-second"}, headers=headers,
        )
        assert current.status_code == 200, current.text
        assert not any(
            source["catalog_version_id"] == str(prior_version_id)
            and source["profile_fingerprint"] == prior_fingerprint
            for source in current.json()["options"]
        ), "Superseded review must not be offered to new quotes"

        def audit_ref(option: dict[str, object]) -> dict[str, object]:
            return {
                "supplier_id": option["supplier_id"],
                "profile_sha256": option["profile_sha256"],
                "catalog_version_id": option["catalog_version_id"],
            }

        pair = {"before": audit_ref(before), "after": audit_ref(after), "limit": 1}
        compared = client.post(history + "/diff", json=pair, headers=headers)
        assert compared.status_code == 200, compared.text
        report = compared.json()
        assert report["requires_manager_review"] is True
        assert report["total_changes"] == 1
        assert report["counts"]["price_changed"] == 1
        delta = report["changes"][0]
        assert delta["before_price_rub"] == "1111.00"
        assert delta["after_price_rub"] == "990.00"
        assert delta["comparable_price_delta_rub"] == "-121.00"
        assert report["next_offset"] is None
        assert client.post(history + "/diff", json={
            "before": {**audit_ref(before), "profile_sha256": "0" * 64},
            "after": audit_ref(after),
        }, headers=headers).status_code == 422
        assert client.post(history + "/diff", json={
            "before": audit_ref(before),
            "after": audit_ref(before),
        }, headers=headers).status_code == 422
        assert client.post(history + "/diff", json=pair, headers={
            "Origin": "https://testserver",
        }).status_code == 401
        assert client.post(
            f"/api/session/organizations/{outsider}/supplier-price-history/diff",
            json=pair, headers=headers,
        ).status_code == 403
        # No historical ref is an acceptable supplier-comparison quote ref.
        quote_preview = client.post(live + "/preview", json={
            "sources": [audit_ref(before), audit_ref(after)],
            "extracted_request": {"lines": []},
        }, headers=headers)
        assert quote_preview.status_code == 422, quote_preview.text

    with owner.connect() as connection:
        immutable_quotes_after = connection.execute(text("""
            SELECT quote_id,number,snapshot::text FROM public.quote_revisions
            WHERE organization_id=:org ORDER BY quote_id,number
        """), {"org": organization}).all()
    assert immutable_quotes_after == immutable_quotes_before
