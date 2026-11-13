"""A93 clone-restricted SQL activation, independent of schema installation.

This is NOT a production rollout mechanism; operational worker drainage,
distribution, credentials and deploy authorization remain separate blockers.
"""
from __future__ import annotations

from sqlalchemy import Connection, text

from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.db.review_rollout_audit import audit_review_schema
from kpdesk.db.review_schema_manifest import REVIEW_FUNCTIONS, REVIEW_INACTIVE_ISSUES
from kpdesk.db.review_signer_provision import (
    _ROTATION_LOCK,
    ReviewSignerProvisionRejected,
    _owner_clone,
    verify_review_signer,
)

_REVISION = "0031_review_schema_candidate"
_RUNTIME = "kpdesk_tenant_runtime"
_ENTRY_POINTS = tuple(
    signature for signature in REVIEW_FUNCTIONS
    if signature != "a77_apply_immutable_decisions()"
)


def activate_review_schema_on_clone(
    connection: Connection, credential: ReviewSigningCredential,
) -> None:
    """Activate only fully audited SQL ACLs with a matching current signer."""
    _owner_clone(connection)
    connection.execute(_ROTATION_LOCK)
    # An active or invalidated authenticated rotation must not be bypassed
    # by simply re-enabling tenant EXECUTE with the previously valid signer.
    # An invalidated cycle requires explicit owner recovery, not auto-resume.
    if connection.scalar(text(
        "SELECT to_regclass('public.a96_review_worker_identity')"
    )) is not None:
        pending = connection.scalar(text("""
            SELECT count(*) FROM public.a95_review_rotation_cycle
            WHERE state IN ('collecting','sealed','invalidated')
        """))
        if pending != 0:
            raise ReviewSignerProvisionRejected(
                "Review activation blocked by unresolved worker cohort."
            )
    if not verify_review_signer(connection, credential):
        raise ReviewSignerProvisionRejected("Review activation signer unavailable.")
    audit = audit_review_schema(connection, expected_revision=_REVISION)
    if frozenset(audit.issues) != REVIEW_INACTIVE_ISSUES:
        raise ReviewSignerProvisionRejected("Review activation preflight rejected.")
    # A nested transaction ensures partial GRANT or postflight failures roll back.
    with connection.begin_nested():
        for signature in _ENTRY_POINTS:
            connection.execute(text(
                f"GRANT EXECUTE ON FUNCTION public.{signature} TO {_RUNTIME}"
            ))
        audit_review_schema(connection, expected_revision=_REVISION).require_ready()


def deactivate_review_schema_on_clone(connection: Connection) -> None:
    """Idempotent ACL deactivation; never touches immutable review data."""
    _owner_clone(connection)
    connection.execute(_ROTATION_LOCK)
    with connection.begin_nested():
        for signature in _ENTRY_POINTS:
            connection.execute(text(
                f"REVOKE EXECUTE ON FUNCTION public.{signature} FROM {_RUNTIME}"
            ))
        issues = audit_review_schema(connection, expected_revision=_REVISION).issues
        if frozenset(issues) != REVIEW_INACTIVE_ISSUES:
            raise ReviewSignerProvisionRejected("Review deactivation failed.")
