"""A93 owner-only HMAC provisioning rehearsal. No production DB access.

This module does not coordinate deployment/draining of workers. It proves
per-epoch key agreement with PostgreSQL without selecting a secret into Python.
All write operations are strictly restricted to provenance-tagged test clones.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from sqlalchemy import Connection, text

from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.review_rollout_audit import audit_review_schema
from kpdesk.db.review_schema_manifest import REVIEW_INACTIVE_ISSUES

_REVISION = "0031_review_schema_candidate"
_ROTATION_LOCK = text("""
    SELECT pg_advisory_xact_lock(
        hashtextextended('kpdesk/a93/review-signer-provision', 0)
    )
""")
_LATEST = text("""
    SELECT max(key_epoch) FROM public.a84_provenance_signing_secret
""")
_PROOF = text("""
    SELECT encode(public.hmac(
        :challenge, s.secret, 'sha256'
    ),'hex') FROM public.a84_provenance_signing_secret s
    WHERE s.key_epoch=:epoch AND s.key_epoch=(
        SELECT max(key_epoch) FROM public.a84_provenance_signing_secret
    )
""")
_INSERT = text("""
    INSERT INTO public.a84_provenance_signing_secret(key_epoch,secret)
    VALUES (:epoch,:secret)
""")


class ReviewSignerProvisionRejected(RuntimeError):
    """Controlled failure without exposing secret material or DB details."""


def _owner_clone(connection: Connection) -> None:
    if (
        not connection.in_transaction()
        or not is_live_accounts_http_clone(connection)
        or connection.scalar(text("SELECT current_user")) != "kpdesk"
        or connection.scalar(text(
            "SELECT version_num FROM public.alembic_version"
        )) != _REVISION
    ):
        raise ReviewSignerProvisionRejected("Review signer provisioning unavailable.")


def verify_review_signer(
    connection: Connection, credential: ReviewSigningCredential,
) -> bool:
    """Read-only HMAC challenge, checking only the latest DB epoch.

    No bytes of the DB signing key are retrieved. False means missing, stale,
    or mismatched; no fallback to an earlier epoch is permitted.
    """
    _owner_clone(connection)
    if not isinstance(credential, ReviewSigningCredential):
        return False
    challenge = secrets.token_bytes(32)
    database_digest = connection.scalar(_PROOF, {
        "challenge": challenge, "epoch": credential.epoch,
    })
    if not isinstance(database_digest, str) or len(database_digest) != 64:
        return False
    expected = hmac.new(credential.key, challenge, hashlib.sha256).hexdigest()
    return hmac.compare_digest(database_digest, expected)


def provision_review_signer_on_clone(
    connection: Connection,
    credential: ReviewSigningCredential,
    *,
    expected_latest_epoch: int | None,
) -> None:
    """Insert a new immutable signing epoch; fail closed on conflicts.

    Caller owns the transaction, and MUST coordinate worker distribution
    separately. Secrets must never be submitted using SQL logging configured
    to expose bound parameters.
    """
    _owner_clone(connection)
    if not connection.engine.hide_parameters:
        raise ReviewSignerProvisionRejected("Review signer provisioning unavailable.")
    if (
        not isinstance(credential, ReviewSigningCredential)
        or (expected_latest_epoch is not None and (
            type(expected_latest_epoch) is not int
            or not 1 <= expected_latest_epoch < 2**63 - 1
        ))
    ):
        raise ReviewSignerProvisionRejected("Review signer provisioning unavailable.")

    connection.execute(_ROTATION_LOCK)
    current = connection.scalar(_LATEST)
    schema_audit = audit_review_schema(connection, expected_revision=_REVISION)
    # A key switch invalidates signatures produced by workers still using the
    # old epoch. Require all tenant-facing SQL functions to be deactivated
    # before *any* new epoch is committed. Deactivation alone does not prove
    # that network traffic has drained; the operator must ensure that separately.
    expected_issues = REVIEW_INACTIVE_ISSUES | (
        frozenset({"invalid_signer_metadata"}) if current is None
        else frozenset()
    )
    if frozenset(schema_audit.issues) != expected_issues:
        raise ReviewSignerProvisionRejected("Review signer schema not quiesced.")
    if (
        current != expected_latest_epoch
        or credential.epoch != (1 if current is None else current + 1)
    ):
        raise ReviewSignerProvisionRejected("Review signer epoch conflict.")
    # The caller may catch a failed proof and still commit its outer transaction.
    # The nested transaction must therefore undo the candidate key atomically.
    with connection.begin_nested():
        connection.execute(_INSERT, {
            "epoch": credential.epoch,
            "secret": credential.key,
        })
        if not verify_review_signer(connection, credential):
            raise ReviewSignerProvisionRejected("Review signer verification failed.")
