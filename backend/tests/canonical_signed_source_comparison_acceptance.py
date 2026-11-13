"""End-to-end signed review → discovery → selected supplier quote on a tagged clone.

Called only from the existing A45 disposable HTTPS/PostgreSQL acceptance. All
prices and signed bindings are produced by real domain services and SQL.
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from canonical_checkpoint_acceptance import MAP, PUT, SOURCE, STAGED, _decisions
from canonical_review_publication_acceptance import _canonical_preview
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.catalog.reviewed_publication import publish_verified_supplier_review
from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_inference import inspect_supplier_source
from kpdesk.catalog.supplier_mapping_storage import TenantSupplierMappingStore
from kpdesk.catalog.supplier_profiles import approve_supplier_profile
from kpdesk.catalog.supplier_staging import mapping_digest
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.review_source_readers import (
    install_readonly_source_readers_on_clone,
)
from kpdesk.db.tenant_access import AuthenticatedUser
from kpdesk.matching.profiles import TAXONOMY_VERSION


def _keyboard_request() -> dict[str, object]:
    return {
        "lines": [{
            "source_quote": "Проводная клавиатура 104 клавиши",
            "description": "Wired keyboard", "category": "keyboard",
            "quantity": 1, "unit": "штука", "brand": None,
            "paper_format": None, "paper_gsm": None,
            "pen_ink_color": None, "pen_ink_type": None,
            "attributes": {"connection": "wired", "keys": 104},
        }],
    }


def verify_signed_source_to_supplier_quote(
    tenant: Engine,
    owner: Engine,
    organization: uuid.UUID,
    outsider: uuid.UUID,
    reviewer_id: uuid.UUID,
    session: IssuedSession,
    signer: ReviewSigningCredential,
    app_factory: Callable[[str], FastAPI],
) -> None:
    """Run a full positive signed-catalog scenario, plus isolation failures."""
    actor = AuthenticatedUser(reviewer_id)
    source_mapping = replace(MAP, confirmed_currency="RUB")
    canonical = _canonical_preview(organization, reviewer_id)
    assert canonical.csv_sha256 != STAGED.sha256

    with owner.begin() as connection:
        install_readonly_source_readers_on_clone(connection)

    # Both supplier mappings are independently reviewed and persisted under
    # the authenticated reviewer, with source bytes bound to each profile.
    source_inspection = inspect_supplier_source("supplier.csv", SOURCE)
    first_profile = approve_supplier_profile(
        source_inspection, organization_id=organization,
        supplier_id="supplier-a79", reviewer=actor,
        source_sha256=STAGED.sha256,
        selected_columns={
            "sku": 0, "name": 1, "price": 2, "currency": 3,
            "category": 4, "sale_unit": 5, "pieces_per_sale_unit": 6,
        },
        price_basis="standard", confirmed_currency="RUB",
        review_evidence="Independent reviewer confirmed original supplier columns.",
        explicitly_confirmed=True,
    )
    assert mapping_digest(source_mapping, "reject") != mapping_digest(MAP, "reject")
    assert source_inspection.source_sha256 == STAGED.sha256
    assert first_profile.approved_source_sha256 == STAGED.sha256

    # A different, valid canonical price list is independently approved for
    # supplier B. Its price differs, so catalog digest and version also differ.
    other_bytes = canonical.csv_content.replace(b"1250.00", b"1199.00", 1)
    assert other_bytes != canonical.csv_content
    other_import = parse_price_list("second.csv", other_bytes, ImportLimits())
    assert other_import.publishable and len(other_import.products) == 2
    other_inspection = inspect_supplier_source("second.csv", other_bytes)
    columns = other_inspection.observed_columns
    second_profile = approve_supplier_profile(
        other_inspection, organization_id=organization,
        supplier_id="supplier-second", reviewer=actor,
        source_sha256=hashlib.sha256(other_bytes).hexdigest(),
        selected_columns={
            "sku": columns.index("sku"), "name": columns.index("name"),
            "price": columns.index("price_rub"),
        },
        price_basis="standard", confirmed_currency="RUB",
        review_evidence="Independent human review of second supplier price column.",
        explicitly_confirmed=True,
    )

    # Independent human approval for a revised source of supplier A. The
    # schema/fingerprint differs from its original signed CSV; its original
    # SHA belongs to the separately published revised version. Nothing from
    # supplier B's profile is reused as supplier A's price authorization.
    updated_profile = approve_supplier_profile(
        other_inspection, organization_id=organization,
        supplier_id="supplier-a79", reviewer=actor,
        source_sha256=hashlib.sha256(other_bytes).hexdigest(),
        selected_columns={
            "sku": columns.index("sku"), "name": columns.index("name"),
            "price": columns.index("price_rub"),
        },
        price_basis="standard", confirmed_currency="RUB",
        review_evidence="Reviewer independently confirmed supplier A's revised source.",
        explicitly_confirmed=True,
    )
    assert updated_profile.fingerprint != first_profile.fingerprint
    with tenant.begin() as connection:
        mapping_store = TenantSupplierMappingStore(
            connection, actor, organization, session.session_token,
            session.csrf_token,
        )
        mapping_store.save(first_profile)
        mapping_store.save(second_profile)
        mapping_store.save(updated_profile)
        scope: dict[str, Any] = {
            "org": organization, "supplier": "supplier-a79",
            "source": STAGED.sha256, "mapping": mapping_digest(source_mapping, "reject"),
            "taxonomy": TAXONOMY_VERSION, "basis": "standard",
            "idempotency": "signed-source-discovery-confirmed",
            "expected": None, "decisions": _decisions(),
        }
        from kpdesk.db.authenticated_tenant import bind_authenticated_tenant

        bind_authenticated_tenant(
            connection, session_token=session.session_token,
            organization_id=organization, operation="write", csrf=session.csrf_token,
        )
        checkpoint = connection.scalar(PUT, scope)
        assert isinstance(checkpoint, uuid.UUID)

    with tenant.begin() as connection:
        signed = publish_verified_supplier_review(
            connection, organization_id=organization, identity=actor,
            session_token=session.session_token, csrf_token=session.csrf_token,
            supplier_id="supplier-a79", checkpoint_id=checkpoint,
            source_filename="supplier.csv", source_bytes=SOURCE,
            mapping=source_mapping, formula_policy="reject",
            price_basis="standard", expected_canonical_sha256=canonical.csv_sha256,
            idempotency_key="signed-source-discovery-publish",
            signing_credential=signer,
        )
        assert signed.source_sha256 == canonical.csv_sha256
        assert signed.source_sha256 != STAGED.sha256
    with tenant.begin() as connection:
        catalog = TenantCatalogRepository(
            connection, actor, organization,
            session_token=session.session_token, csrf_token=session.csrf_token,
        )
        second = catalog.publish(
            other_import, "second-supplier-source-publish", other_import.source_sha256,
        )

    root = f"/api/session/organizations/{organization}"
    compare_path = root + "/quotes/supplier-comparison"
    headers = {
        "Origin": "https://testserver",
        "X-CSRF-Token": session.csrf_token,
    }
    with TestClient(app_factory("https://testserver"), base_url="https://testserver") as client:
        assert client.post(
            compare_path + "/sources",
            json={"supplier_ids": ["supplier-a79", "supplier-second"]},
            headers=headers,
        ).status_code == 401
        client.cookies.set(SESSION_COOKIE, session.session_token)
        no_csrf = client.post(
            compare_path + "/sources",
            json={"supplier_ids": ["supplier-a79", "supplier-second"]},
            headers={"Origin": "https://testserver"},
        )
        assert no_csrf.status_code == 401
        denied = client.post(
            f"/api/session/organizations/{outsider}/quotes/supplier-comparison/sources",
            json={"supplier_ids": ["supplier-a79", "supplier-second"]},
            headers=headers,
        )
        assert denied.status_code == 403

        discovered = client.post(
            compare_path + "/sources",
            json={"supplier_ids": ["supplier-a79", "supplier-second"]},
            headers=headers,
        )
        assert discovered.status_code == 200, discovered.text
        groups = discovered.json()["suppliers"]
        assert [item["supplier_id"] for item in groups] == [
            "supplier-a79", "supplier-second",
        ]
        assert all(not item["truncated"] for item in groups)
        signed_option = next(
            item for item in groups[0]["options"]
            if item["catalog_version_id"] == str(signed.id)
        )
        assert signed_option["evidence"] == "reviewed_canonical"
        assert signed_option["source_sha256"] == canonical.csv_sha256
        assert signed_option["profile_fingerprint"] == first_profile.fingerprint
        original_option = next(
            item for item in groups[1]["options"]
            if item["catalog_version_id"] == str(second.id)
        )
        assert original_option["evidence"] == "original"
        assert original_option["profile_fingerprint"] == second_profile.fingerprint

        # A101: the SAME supplier has two independently reviewed price
        # sources. The signed canonical version and revised original version
        # are both checked again by the real HTTPS diff endpoint.
        supplier_history = client.post(
            compare_path + "/supplier-sources",
            headers=headers, json={"supplier_id": "supplier-a79"},
        )
        assert supplier_history.status_code == 200, supplier_history.text
        assert supplier_history.json()["supplier_id"] == "supplier-a79"
        revised_option = next(
            item for item in supplier_history.json()["options"]
            if item["catalog_version_id"] == str(second.id)
        )
        assert revised_option["profile_fingerprint"] == updated_profile.fingerprint
        assert revised_option["evidence"] == "original"
        before_ref = {
            field: signed_option[field]
            for field in ("supplier_id", "profile_fingerprint", "catalog_version_id")
        }
        after_ref = {
            field: revised_option[field]
            for field in ("supplier_id", "profile_fingerprint", "catalog_version_id")
        }
        diff_payload = {"before": before_ref, "after": after_ref}
        no_diff_csrf = client.post(
            compare_path + "/source-diff",
            headers={"Origin": "https://testserver"}, json=diff_payload,
        )
        assert no_diff_csrf.status_code == 401
        cross_tenant_diff = client.post(
            f"/api/session/organizations/{outsider}/quotes/supplier-comparison/source-diff",
            headers=headers, json=diff_payload,
        )
        assert cross_tenant_diff.status_code == 403
        changed_catalog = client.post(
            compare_path + "/source-diff",
            headers=headers, json={**diff_payload, "limit": 1},
        )
        assert changed_catalog.status_code == 200, changed_catalog.text
        changes = changed_catalog.json()
        assert changes["requires_manager_review"] is True
        assert changes["supplier_id"] == "supplier-a79"
        assert changes["before_source_sha256"] == canonical.csv_sha256
        assert changes["after_source_sha256"] == other_import.source_sha256
        assert changes["counts"]["price_changed"] == 1
        assert changes["total_changes"] == 1
        assert changes["next_offset"] is None
        assert len(changes["changes"]) == 1
        updated_price = changes["changes"][0]
        assert updated_price["kind"] == "price_changed"
        assert updated_price["before_price_rub"] == "1250.00"
        assert updated_price["after_price_rub"] == "1199.00"
        assert updated_price["comparable_price_delta_rub"] == "-51.00"
        untrusted_reference = client.post(
            compare_path + "/source-diff",
            headers=headers, json={
                "before": {**before_ref, "profile_fingerprint": "0" * 64},
                "after": after_ref,
            },
        )
        assert untrusted_reference.status_code == 422


        references = [
            {
                "supplier_id": item["supplier_id"],
                "profile_fingerprint": item["profile_fingerprint"],
                "catalog_version_id": item["catalog_version_id"],
            }
            for item in (signed_option, original_option)
        ]
        request = _keyboard_request()
        compared = client.post(
            compare_path + "/preview",
            headers=headers, json={"sources": references, "extracted_request": request},
        )
        assert compared.status_code == 200, compared.text
        preview = compared.json()
        assert preview["requires_manager_confirmation"] is True
        assert preview["catalog_source_count"] == 2
        offers = preview["lines"][0]["offers"]
        assert any(item["supplier_id"] == "supplier-a79"
                   and item["catalog_version_id"] == str(signed.id)
                   and item["source_sha256"] == canonical.csv_sha256
                   for item in offers)
        assert any(item["supplier_id"] == "supplier-second" for item in offers)
        selected = next(item for item in offers if item["supplier_id"] == "supplier-a79")
        # No automatic selection. Explicit SKU from a reviewed source only.
        creation = client.post(
            compare_path,
            headers={**headers, "Idempotency-Key": "signed-compare-selected-001"},
            json={
                "sources": references,
                "active_catalog_version_id": str(second.id),
                "extracted_request": request,
                "selections": [{
                    "line_number": 1,
                    "supplier_id": selected["supplier_id"],
                    "catalog_version_id": selected["catalog_version_id"],
                    "sku": selected["sku"],
                }],
            },
        )
        # The A45 rehearsal deliberately runs on the released 0030 schema;
        # audit migration 0032 is not installed until after A96. Refuse to
        # persist supplier-attributed decisions before the FK/trigger exists.
        assert creation.status_code == 503, creation.text
        assert creation.json()["code"] == "quote_store_unavailable"



def verify_signed_quote_after_audit_migration(
    organization: uuid.UUID,
    session: IssuedSession,
    app_factory: Callable[[str], FastAPI],
) -> None:
    """Persist only after the *real* 0032 FK/trigger migration is installed."""
    root = f"/api/session/organizations/{organization}"
    path = root + "/quotes/supplier-comparison"
    headers = {
        "Origin": "https://testserver",
        "X-CSRF-Token": session.csrf_token,
    }
    with TestClient(app_factory("https://testserver"), base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, session.session_token)
        result = client.post(
            path + "/sources",
            json={"supplier_ids": ["supplier-a79", "supplier-second"]},
            headers=headers,
        )
        assert result.status_code == 200, result.text
        groups = result.json()["suppliers"]
        a = next(item for item in groups[0]["options"]
                 if item["evidence"] == "reviewed_canonical")
        b = next(item for item in groups[1]["options"]
                 if item["evidence"] == "original")
        references = [
            {
                "supplier_id": item["supplier_id"],
                "profile_fingerprint": item["profile_fingerprint"],
                "catalog_version_id": item["catalog_version_id"],
            }
            for item in (a, b)
        ]
        active = client.get(root + "/catalog/current")
        assert active.status_code == 200, active.text
        current_id = active.json()["version_id"]
        request = _keyboard_request()
        compared = client.post(
            path + "/preview",
            headers=headers, json={
                "sources": references, "extracted_request": request,
            },
        )
        assert compared.status_code == 200, compared.text
        offers = compared.json()["lines"][0]["offers"]
        assert len({offer["supplier_id"] for offer in offers}) == 2
        selected = next(offer for offer in offers
                        if offer["supplier_id"] == "supplier-a79"
                        and offer["catalog_version_id"] == a["catalog_version_id"])
        assert selected["source_sha256"] == a["source_sha256"]
        payload = {
            "sources": references,
            "active_catalog_version_id": current_id,
            "extracted_request": request,
            "selections": [{
                "line_number": 1,
                "supplier_id": selected["supplier_id"],
                "catalog_version_id": selected["catalog_version_id"],
                "sku": selected["sku"],
            }],
        }
        publish_headers = {
            **headers,
            "Idempotency-Key": "signed-source-comparison-verified",
        }
        creation = client.post(path, json=payload, headers=publish_headers)
        assert creation.status_code == 201, creation.text
        quote = creation.json()
        assert quote["ready_for_draft"] is True
        assert quote["lines"][0]["supplier_id"] == "supplier-a79"
        assert quote["lines"][0]["source_catalog_version_id"] == a["catalog_version_id"]
        assert quote["lines"][0]["source_sha256"] == a["source_sha256"]
        assert quote["lines"][0]["sku"] == selected["sku"]
        assert quote["total_rub"] == selected["selling_total_rub"]
        repeated = client.post(path, json=payload, headers=publish_headers)
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["quote_id"] == quote["quote_id"]
        pdf = client.get(root + "/quotes/" + quote["quote_id"] + "/pdf")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
        # The selected revision must contain the exact catalog provenance,
        # even if a different active catalog was published by A96.
        opened = client.get(root + "/quotes/" + quote["quote_id"])
        assert opened.status_code == 200
        assert opened.json()["lines"][0]["source_catalog_version_id"] == a["catalog_version_id"]
