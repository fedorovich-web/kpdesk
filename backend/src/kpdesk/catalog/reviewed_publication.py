"""A80 verified source-to-catalog publication, callable only on disposable clones.

Replays uploaded supplier bytes and stored human decisions inside a single
caller-owned SQLAlchemy transaction. Not installed in the production API.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import Connection, text

from kpdesk.api.tenant_supplier_canonical_routes import reviewed_decisions
from kpdesk.api.tenant_supplier_checkpoint_routes import SaveDecision
from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.catalog.storage import PublishedCatalog
from kpdesk.catalog.supplier_canonical_preview import preview_canonical_catalog
from kpdesk.catalog.supplier_profiles import PriceBasis
from kpdesk.catalog.supplier_staging import (
    SupplierMapping,
    mapping_digest,
    stage_supplier_prices,
)
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.tenant_access import AuthenticatedUser
from kpdesk.matching.profiles import TAXONOMY_VERSION

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SNAPSHOT = text("""
    SELECT reviewer,decisions FROM public.a79_review_snapshot(
      :org,:supplier,:source,:mapping,:taxonomy,:basis,:checkpoint
    )
""")
_BIND = text("""
    SELECT public.a79_bind_published_review(
      :org,:version,:checkpoint,:supplier,:source,:mapping,
      :csv,:taxonomy,:basis,:reviewer,:key_epoch,:signature
    )
""")


class VerifiedReviewPublishRejected(ValueError):
    """The original supplier source and confirmed checkpoint do not match."""


def publish_verified_supplier_review(
    connection: Connection,
    *,
    organization_id: uuid.UUID,
    identity: AuthenticatedUser,
    session_token: str,
    csrf_token: str,
    supplier_id: str,
    checkpoint_id: uuid.UUID,
    source_filename: str,
    source_bytes: bytes,
    mapping: SupplierMapping,
    formula_policy: Literal["reject", "quarantine_rows"],
    price_basis: PriceBasis,
    expected_canonical_sha256: str,
    idempotency_key: str,
    signing_credential: ReviewSigningCredential,
) -> PublishedCatalog:
    """Atomic catalog and immutable provenance, without committing the connection.

    This clone-only service is deliberately not an HTTP endpoint. Its callers
    must own a transaction and must not commit after a rejected publication.
    """
    if (
        not connection.in_transaction() or not is_live_accounts_http_clone(connection)
        or connection.scalar(text("SELECT current_user")) != "kpdesk_tenant_runtime"
    ):
        raise RuntimeError("A80 publisher is restricted to the marked test clone.")
    if (
        not isinstance(identity, AuthenticatedUser)
        or not isinstance(checkpoint_id, uuid.UUID)
        or not isinstance(supplier_id, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", supplier_id) is None
        or not isinstance(source_filename, str)
        or len(source_filename) > 250
        or not source_filename.casefold().endswith((".csv", ".xlsx", ".xls"))
        or any(c in source_filename for c in ("/", "\\"))
        or any(ord(c) < 32 for c in source_filename)
        or not isinstance(source_bytes, bytes)
        or len(source_bytes) > 10 * 1024 * 1024
        or not isinstance(signing_credential, ReviewSigningCredential)
        or not isinstance(mapping, SupplierMapping)
        or formula_policy not in ("reject", "quarantine_rows")
        or price_basis not in ("standard", "retail", "wholesale", "rrp", "vat_included")
        or not isinstance(expected_canonical_sha256, str)
        or _SHA256.fullmatch(expected_canonical_sha256) is None
    ):
        raise VerifiedReviewPublishRejected("Invalid reviewed supplier input.")

    repo = TenantCatalogRepository(
        connection, identity, organization_id,
        session_token=session_token, csrf_token=csrf_token,
    )
    repo._authorize("write")
    # Parsing untrusted XLS/XLSX/CSV may be expensive. Do not hold the
    # organization's publication mutex during bounded source intake.
    try:
        staging = stage_supplier_prices(
            source_filename, source_bytes, mapping, formula_policy=formula_policy,
        )
    except (TypeError, ValueError) as error:
        raise VerifiedReviewPublishRejected("Invalid original supplier source.") from error
    if staging.sha256 != hashlib.sha256(source_bytes).hexdigest():
        raise VerifiedReviewPublishRejected("Supplier source hash mismatch.")

    mapping_sha256 = mapping_digest(mapping, formula_policy)
    scope = {
        "org": organization_id, "supplier": supplier_id,
        "source": staging.sha256, "mapping": mapping_sha256,
        "taxonomy": TAXONOMY_VERSION, "basis": price_basis,
        "checkpoint": checkpoint_id,
    }
    # Acquire the organization mutex before the checkpoint head's FOR UPDATE
    # lock. A concurrent checkpoint append cannot silently replace this review.
    repo.lock_for_publish()
    snapshot = connection.execute(_SNAPSHOT, scope).one_or_none()
    if snapshot is None or snapshot.reviewer != identity.user_id:
        raise VerifiedReviewPublishRejected("Checkpoint is stale or reviewer differs.")
    try:
        decisions = [
            SaveDecision.model_validate(raw).root for raw in snapshot.decisions
        ]
        canonical = preview_canonical_catalog(
            staging, reviewed_decisions(decisions),
            organization_id=organization_id, reviewer=identity,
            expected_source_sha256=staging.sha256,
            price_basis=price_basis, explicitly_confirmed_price_basis=True,
        )
    except (ValidationError, TypeError, ValueError) as error:
        raise VerifiedReviewPublishRejected("Saved review is invalid for these bytes.") from error
    if canonical.csv_sha256 != expected_canonical_sha256:
        raise VerifiedReviewPublishRejected("Canonical CSV changed after review.")

    published = repo.publish(
        canonical.validation, idempotency_key, canonical.csv_sha256,
    )
    # A84: only the server that replayed original bytes can attest the
    # published version. Bind proof to this transaction and verified session.
    txid, session_digest = connection.execute(text("""
        SELECT txid_current()::text,
               current_setting('kpdesk.session_sha256',true)
    """)).one()
    if (not isinstance(txid, str) or not txid.isdecimal()
            or not isinstance(session_digest, str) or not _SHA256.fullmatch(session_digest)):
        raise VerifiedReviewPublishRejected("Missing verified session binding.")
    payload = "\x1f".join((
        "kpdesk/a85/review-publish/v1", str(signing_credential.epoch),
        str(organization_id), str(published.id), str(checkpoint_id),
        supplier_id, staging.sha256, mapping_sha256, canonical.csv_sha256,
        TAXONOMY_VERSION, price_basis, str(identity.user_id),
        txid, session_digest,
    ))
    signature = hmac.new(signing_credential.key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    linked = connection.scalar(_BIND, {
        **scope, "version": published.id, "csv": canonical.csv_sha256,
        "reviewer": identity.user_id, "key_epoch": signing_credential.epoch,
        "signature": signature,
    })
    if linked is not True:
        raise VerifiedReviewPublishRejected("Cannot bind the catalog to the review.")
    return published
