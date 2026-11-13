"""A79: atomic review-to-catalog publication proof on a disposable database."""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import Literal

from canonical_checkpoint_acceptance import MAP, SOURCE, STAGED, _decisions
from canonical_large_review_publication_http_acceptance import verify_large_review_publication_https
from canonical_review_publication_clone_schema import install_publication_provenance
from canonical_review_publication_http_acceptance import verify_review_publication_http
from fastapi import FastAPI
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.catalog.reviewed_publication import publish_verified_supplier_review
from kpdesk.catalog.supplier_canonical_preview import (
    CanonicalPreview,
    ReviewedExclusion,
    ReviewedProductDecision,
    preview_canonical_catalog,
)
from kpdesk.catalog.supplier_staging import SupplierMapping, mapping_digest
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.tenant_access import AuthenticatedUser
from kpdesk.matching.profiles import TAXONOMY_VERSION


def _canonical_preview(organization: uuid.UUID, reviewer: uuid.UUID) -> CanonicalPreview:
    decisions: list[ReviewedProductDecision | ReviewedExclusion] = []
    for item in _decisions():
        if item["action"] == "exclude":
            decisions.append(ReviewedExclusion(
                line=item["line"], source_row_sha256=item["source_row_sha256"],
                confirmed=True, reason=item["reason"],
            ))
        else:
            decisions.append(ReviewedProductDecision(
                line=item["line"], source_row_sha256=item["source_row_sha256"],
                confirmed=True, category=item["category"],
                sale_unit=item["sale_unit"], pieces_per_sale_unit=item["pieces_per_sale_unit"],
                attributes=item["attributes"],
            ))
    return preview_canonical_catalog(
        STAGED, tuple(decisions), organization_id=organization,
        reviewer=AuthenticatedUser(reviewer), expected_source_sha256=STAGED.sha256,
        price_basis="standard", explicitly_confirmed_price_basis=True,
    )


def verify_atomic_review_publication(
    tenant: Engine, owner: Engine, organization: uuid.UUID,
    outsider: uuid.UUID, user: uuid.UUID, session: IssuedSession,
    app_factory: Callable[[str], FastAPI],
) -> ReviewSigningCredential:
    assert hashlib.sha256(SOURCE).hexdigest() == STAGED.sha256
    preview = _canonical_preview(organization, user)
    assert preview.csv_sha256 != STAGED.sha256
    assert preview.validation.publishable
    with owner.begin() as connection:
        signing_credential = install_publication_provenance(connection)

    scope = {
        "org": organization, "supplier": "supplier-a79",
        "source": STAGED.sha256, "mapping": mapping_digest(MAP, "reject"),
        "taxonomy": TAXONOMY_VERSION, "basis": "standard",
    }
    decisions = _decisions()
    from canonical_checkpoint_acceptance import PUT
    with tenant.begin() as connection:
        from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
        bind_authenticated_tenant(
            connection, session_token=session.session_token,
            organization_id=organization, operation="write", csrf=session.csrf_token,
        )
        checkpoint = connection.scalar(PUT, {
            **scope, "idempotency": "a79-reviewed-checkpoint-1",
            "expected": None, "decisions": decisions,
        })
        assert isinstance(checkpoint, uuid.UUID)

    def publish(
        connection: Connection, key: str, csv_hash: str, cp: uuid.UUID,
        *, raw: bytes = SOURCE, selected_mapping: SupplierMapping = MAP,
        selected_formula: Literal["reject", "quarantine_rows"] = "reject",
    ) -> uuid.UUID:
        result = publish_verified_supplier_review(
            connection, organization_id=organization,
            identity=AuthenticatedUser(user), session_token=session.session_token,
            csrf_token=session.csrf_token, supplier_id="supplier-a79",
            checkpoint_id=cp, source_filename="supplier.csv",
            source_bytes=raw, mapping=selected_mapping, formula_policy=selected_formula,
            price_basis="standard", expected_canonical_sha256=csv_hash,
            idempotency_key=key, signing_credential=signing_credential,
        )
        return result.id

    # Refuse a modified canonical upload and a nonexistent checkpoint without
    # reserving an idempotency key or altering the active catalog.
    for key, digest, checkpoint_id in (
        ("a79-changed-content", "f" * 64, checkpoint),
        ("a79-missing-review", preview.csv_sha256, uuid.uuid4()),
    ):
        try:
            with tenant.begin() as connection:
                publish(connection, key, digest, checkpoint_id)
        except ValueError:
            pass
        else:
            raise AssertionError("Unverified source was accepted.")
        with owner.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM public.tenant_catalog_idempotency
                WHERE organization_id=:org AND key=:key
            """), {"org": organization, "key": key}) == 0

    # Replaying changed supplier bytes or a changed mapping must never bind
    # the old checkpoint, even if the client supplies a valid canonical hash.
    for key, original, changed_mapping in (
        ("a80-modified-original", SOURCE + b"\n", MAP),
        ("a80-modified-mapping", SOURCE, replace(MAP, delimiter=",")),
    ):
        try:
            with tenant.begin() as connection:
                publish(
                    connection, key, preview.csv_sha256, checkpoint,
                    raw=original, selected_mapping=changed_mapping,
                )
        except ValueError:
            pass
        else:
            raise AssertionError("Changed supplier evidence was accepted.")
        with owner.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM public.tenant_catalog_idempotency
                WHERE organization_id=:org AND key=:key
            """), {"org": organization, "key": key}) == 0

    # Even with a legitimate session, a caller must not bind another tenant.
    with tenant.begin() as connection:
        repository = TenantCatalogRepository(
            connection, AuthenticatedUser(user), organization,
            session_token=session.session_token, csrf_token=session.csrf_token,
        )
        repository._authorize("write")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert connection.scalar(text("""
                SELECT has_table_privilege(
                    current_user,'public.a79_catalog_review_provenance',:privilege
                )
            """), {"privilege": privilege}) is False
        assert connection.execute(text("""
            SELECT reviewer,decisions FROM public.a79_review_snapshot(
              :org,:supplier,:source,:mapping,:taxonomy,:basis,:checkpoint)
        """), {**scope, "org": outsider, "checkpoint": checkpoint}).one_or_none() is None

    # Independent concurrent PostgreSQL connections must converge on exactly
    # one catalog version for an identical idempotency key.
    barrier = Barrier(2, timeout=15)

    def concurrent_publish() -> uuid.UUID:
        with tenant.begin() as connection:
            barrier.wait(timeout=15)
            return publish(connection, "a79-verified-publish", preview.csv_sha256, checkpoint)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(concurrent_publish)
        second_future = executor.submit(concurrent_publish)
        first = first_future.result(timeout=30)
        assert second_future.result(timeout=30) == first

    with tenant.begin() as connection:
        assert publish(connection, "a79-verified-publish", preview.csv_sha256, checkpoint) == first

    # Force a failed provenance insert after repository.publish: the entire
    # catalog publication must roll back, including its idempotency reservation.
    with owner.begin() as connection:
        connection.execute(text("""
            ALTER TABLE public.a79_catalog_review_provenance
            ADD CONSTRAINT a79_injected_rejection
            CHECK (catalog_version_id='00000000-0000-0000-0000-000000000000'::uuid) NOT VALID
        """))
    try:
        try:
            with tenant.begin() as connection:
                publish(connection, "a79-rollback-key", preview.csv_sha256, checkpoint)
        except DBAPIError as error:
            assert getattr(error.orig, "sqlstate", None) == "23514"
        else:
            raise AssertionError("Fault injection was not exercised.")
        with owner.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM public.tenant_catalog_idempotency
                WHERE organization_id=:org AND key='a79-rollback-key'
            """), {"org": organization}) == 0
            assert connection.scalar(text("""
                SELECT catalog_version_id FROM public.tenant_catalog_active
                WHERE organization_id=:org
            """), {"org": organization}) == first
            assert connection.scalar(text("""
                SELECT count(*) FROM public.catalog_versions
                WHERE organization_id=:org AND source_sha256=:csv
            """), {"org": organization, "csv": preview.csv_sha256}) == 1
            assert connection.scalar(text("""
                SELECT count(*) FROM public.catalog_items i
                JOIN public.catalog_versions v
                  ON v.organization_id=i.organization_id
                 AND v.id=i.catalog_version_id
                WHERE v.organization_id=:org AND v.source_sha256=:csv
            """), {"org": organization, "csv": preview.csv_sha256}) == len(preview.validation.products)
    finally:
        with owner.begin() as connection:
            connection.execute(text("""
                ALTER TABLE public.a79_catalog_review_provenance
                DROP CONSTRAINT a79_injected_rejection
            """))

    second, rotated_version, active_signer = verify_review_publication_http(
        app_factory, organization, outsider, checkpoint, session, preview.csv_sha256,
        signing_credential, owner,
    )
    assert second != first
    with owner.connect() as connection:
        rows = connection.execute(text("""
            SELECT catalog_version_id,checkpoint_id,original_source_sha256,
                   mapping_sha256,canonical_csv_sha256,reviewer_user_id
            FROM public.a79_catalog_review_provenance
            WHERE organization_id=:org ORDER BY created_at
        """), {"org": organization}).all()
        assert len(rows) == 3
        assert {row.catalog_version_id for row in rows} == {
            first, second, rotated_version,
        }
        assert connection.scalar(text("""
            SELECT count(*) FROM public.tenant_catalog_idempotency
            WHERE organization_id=:org
              AND key='a84-incorrect-server-signature'
        """), {"org": organization}) == 0
        assert all(
            row.checkpoint_id == checkpoint
            and row.original_source_sha256 == STAGED.sha256
            and row.mapping_sha256 == scope["mapping"]
            and row.canonical_csv_sha256 == preview.csv_sha256
            and row.reviewer_user_id == user
            for row in rows
        )
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a79_catalog_review_provenance
            WHERE organization_id=:outsider
        """), {"outsider": outsider}) == 0

    # Once a new reviewed revision exists, the old checkpoint must not be
    # published again under a new idempotency key.
    from canonical_checkpoint_acceptance import PUT

    from kpdesk.db.authenticated_tenant import bind_authenticated_tenant

    with tenant.begin() as connection:
        bind_authenticated_tenant(
            connection, session_token=session.session_token,
            organization_id=organization, operation="write", csrf=session.csrf_token,
        )
        next_checkpoint = connection.scalar(PUT, {
            **scope, "idempotency": "a79-new-confirmed-revision",
            "expected": checkpoint, "decisions": decisions,
        })
        assert isinstance(next_checkpoint, uuid.UUID) and next_checkpoint != checkpoint

    try:
        with tenant.begin() as connection:
            publish(connection, "a79-stale-revision", preview.csv_sha256, checkpoint)
    except ValueError:
        pass
    else:
        raise AssertionError("Stale review checkpoint allowed publication.")
    with owner.connect() as connection:
        assert connection.scalar(text("""
            SELECT count(*) FROM public.tenant_catalog_idempotency
            WHERE organization_id=:org AND key='a79-stale-revision'
        """), {"org": organization}) == 0
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a79_catalog_review_provenance
            WHERE organization_id=:org
        """), {"org": organization}) == 3

    # Immutable provenance is enforced in PostgreSQL even for the clone owner.
    with owner.begin() as connection:
        try:
            with connection.begin_nested():
                connection.execute(text("""
                    UPDATE public.a79_catalog_review_provenance
                    SET mapping_sha256=:altered
                    WHERE organization_id=:org AND catalog_version_id=:version
                """), {"altered": "0" * 64, "org": organization, "version": first})
        except DBAPIError as error:
            assert "review_audit_immutable" in str(error.orig)
        else:
            raise AssertionError("A79 audit link was mutable.")
    # A review writer and a publisher operate on separate PostgreSQL sessions.
    # The writer may commit a new head before the publisher obtains FOR UPDATE;
    # publication of the previous head must fail, not hang or silently regress.
    interleave = Barrier(2, timeout=15)

    def publish_during_edit() -> bool:
        interleave.wait(timeout=15)
        try:
            with tenant.begin() as connection:
                publish(connection, "a80-competing-review", preview.csv_sha256, next_checkpoint)
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=1) as executor:
        with tenant.begin() as connection:
            bind_authenticated_tenant(
                connection, session_token=session.session_token,
                organization_id=organization, operation="write", csrf=session.csrf_token,
            )
            newest = connection.scalar(PUT, {
                **scope, "idempotency": "a80-racing-new-review",
                "expected": next_checkpoint, "decisions": decisions,
            })
            assert isinstance(newest, uuid.UUID)
            future = executor.submit(publish_during_edit)
            interleave.wait(timeout=15)
        assert future.result(timeout=30) is False
    with owner.connect() as connection:
        assert connection.scalar(text("""
            SELECT count(*) FROM public.tenant_catalog_idempotency
            WHERE organization_id=:org AND key='a80-competing-review'
        """), {"org": organization}) == 0

    # A82: a privileged function caller cannot retrospectively attest a
    # catalog created by an earlier committed transaction, even when all
    # version/source/checkpoint hashes match the current confirmed review.
    with tenant.begin() as connection:
        plain_repository = TenantCatalogRepository(
            connection, AuthenticatedUser(user), organization,
            session_token=session.session_token, csrf_token=session.csrf_token,
        )
        ordinary = plain_repository.publish(
            preview.validation, "a82-unreviewed-ordinary-catalog", preview.csv_sha256,
        )
        # A84: old A82 same-transaction xmin/active checks are insufficient.
        # Even an authorized runtime catalog writer may not invent provenance.
        assert connection.scalar(text("""
            SELECT public.a79_bind_published_review(
              :org,:version,:checkpoint,:supplier,:source,:mapping,
              :csv,:taxonomy,:basis,:reviewer,:key_epoch,:signature
            )
        """), {
            **scope, "version": ordinary.id, "checkpoint": newest,
            "csv": preview.csv_sha256, "reviewer": user,
            "key_epoch": signing_credential.epoch,
            "signature": "0" * 64,
        }) is False
    with tenant.begin() as connection:
        verify_repository = TenantCatalogRepository(
            connection, AuthenticatedUser(user), organization,
            session_token=session.session_token, csrf_token=session.csrf_token,
        )
        verify_repository._authorize("write")
        assert connection.scalar(text("""
            SELECT public.a79_bind_published_review(
              :org,:version,:checkpoint,:supplier,:source,:mapping,
              :csv,:taxonomy,:basis,:reviewer,:key_epoch,:signature
            )
        """), {
            **scope, "version": ordinary.id, "checkpoint": newest,
            "csv": preview.csv_sha256, "reviewer": user,
            "key_epoch": signing_credential.epoch,
            "signature": "0" * 64,
        }) is False
    with owner.connect() as connection:
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a79_catalog_review_provenance
            WHERE organization_id=:org AND catalog_version_id=:version
        """), {"org": organization, "version": ordinary.id}) == 0

    # A90: 121-row paginated human review, independent SQL sessions, complete
    # replay and an actual HTTPS signed publication on the marked clone.
    verify_large_review_publication_https(
        tenant, owner, organization, outsider, user, session,
        app_factory, active_signer,
    )

    # A98: a genuine signed canonical version must also be usable through
    # production discovery/preview/compose routes on this disposable clone.
    from canonical_signed_source_comparison_acceptance import (
        verify_signed_source_to_supplier_quote,
    )

    verify_signed_source_to_supplier_quote(
        tenant, owner, organization, outsider, user, session,
        active_signer, app_factory,
    )

    # Revoke the real server-side session. The clone-only service must then
    # reject a new publish without reserving any idempotency or provenance.
    with owner.begin() as connection:
        changed = connection.execute(text("""
            UPDATE public.auth_sessions SET revoked_at=clock_timestamp()
            WHERE token_sha256=:digest AND revoked_at IS NULL
        """), {
            "digest": hashlib.sha256(session.session_token.encode("ascii")).hexdigest(),
        })
        assert changed.rowcount == 1
    from kpdesk.db.tenant_access import TenantAccessDenied

    try:
        with tenant.begin() as connection:
            publish(connection, "a80-revoked-session", preview.csv_sha256, newest)
    except TenantAccessDenied:
        pass
    else:
        raise AssertionError("Revoked session published a catalog.")
    with owner.connect() as connection:
        assert connection.scalar(text("""
            SELECT count(*) FROM public.tenant_catalog_idempotency
            WHERE organization_id=:org AND key='a80-revoked-session'
        """), {"org": organization}) == 0
    return active_signer
