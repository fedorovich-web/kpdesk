"""A95 isolated-clone prototype of a closed-world signer rotation cohort.

The owner records acknowledgements received through a *trusted external*
channel. This module DOES NOT authenticate remote worker acknowledgements and
MUST NOT be used as production service discovery or as a production rollout
controller. No migrations or persistent production SQL objects are installed.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Connection, text

from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.db.review_rollout_audit import audit_review_schema
from kpdesk.db.review_schema_manifest import REVIEW_INACTIVE_ISSUES
from kpdesk.db.review_signer_provision import (
    _LATEST,
    _ROTATION_LOCK,
    _owner_clone,
    provision_review_signer_on_clone,
)

_MAX_WORKERS = 32
_REVISION = "0031_review_schema_candidate"


class ReviewCohortRejected(RuntimeError):
    """A stale, unknown, incomplete, or unsafe cohort cannot rotate a key."""


def _owner(connection: Connection) -> None:
    _owner_clone(connection)
    if not connection.engine.hide_parameters:
        raise ReviewCohortRejected("Review rotation cohort unavailable.")


def _lock_cycle(connection: Connection, cycle_id: uuid.UUID) -> tuple[int, str]:
    if not isinstance(cycle_id, uuid.UUID) or cycle_id.int == 0:
        raise ReviewCohortRejected("Review rotation cohort unavailable.")
    row = connection.execute(text("""
        SELECT expected_epoch,state
        FROM public.a95_review_rotation_cycle
        WHERE cycle_id=:cycle_id AND expires_at>clock_timestamp()
        FOR UPDATE
    """), {"cycle_id": cycle_id}).one_or_none()
    if row is None:
        raise ReviewCohortRejected("Review rotation cohort unavailable.")
    return int(row.expected_epoch), str(row.state)


def install_rotation_cohort_on_clone(connection: Connection) -> None:
    """Test-only DDL, always wrapped in a disposable-clone SAVEPOINT."""
    _owner(connection)
    connection.execute(text("""
        CREATE TABLE public.a95_review_rotation_cycle (
          cycle_id uuid PRIMARY KEY,
          expected_epoch bigint NOT NULL CHECK (expected_epoch BETWEEN 1 AND 9223372036854775806),
          state text NOT NULL DEFAULT 'collecting'
            CHECK (state IN ('collecting','sealed','consumed')),
          expires_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )
    """))
    connection.execute(text("""
        CREATE TABLE public.a95_review_rotation_member (
          cycle_id uuid NOT NULL REFERENCES public.a95_review_rotation_cycle(cycle_id),
          worker_id uuid NOT NULL,
          instance_id uuid,
          acknowledged_at timestamptz,
          PRIMARY KEY(cycle_id,worker_id),
          CHECK ((instance_id IS NULL) = (acknowledged_at IS NULL))
        )
    """))
    for table_name in ("a95_review_rotation_cycle", "a95_review_rotation_member"):
        connection.execute(text(f"""
            REVOKE ALL ON public.{table_name}
            FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
        """))
        connection.execute(text(f"""
            ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY
        """))
        connection.execute(text(f"""
            ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY
        """))
        connection.execute(text(f"""
            CREATE POLICY a95_owner ON public.{table_name}
            FOR ALL TO kpdesk USING (true) WITH CHECK (true)
        """))


def begin_rotation_cohort_on_clone(
    connection: Connection, *, cycle_id: uuid.UUID, expected_epoch: int,
    workers: Sequence[uuid.UUID],
) -> None:
    """Freeze an explicit inventory. Unknown or replacement workers are unsafe."""
    _owner(connection)
    if (
        not isinstance(cycle_id, uuid.UUID) or cycle_id.int == 0
        or type(expected_epoch) is not int or not 1 <= expected_epoch < 2**63 - 1
        or not 1 <= len(workers) <= _MAX_WORKERS
        or any(not isinstance(w, uuid.UUID) or w.int == 0 for w in workers)
        or len(set(workers)) != len(workers)
    ):
        raise ReviewCohortRejected("Invalid review rotation inventory.")
    connection.execute(_ROTATION_LOCK)
    issues = frozenset(
        audit_review_schema(connection, expected_revision=_REVISION).issues
    )
    if (
        issues != REVIEW_INACTIVE_ISSUES
        or connection.scalar(_LATEST) != expected_epoch
        or connection.scalar(text("""
            SELECT count(*) FROM public.a95_review_rotation_cycle
            WHERE state IN ('collecting','sealed')
        """)) != 0
    ):
        raise ReviewCohortRejected("Review rotation preflight rejected.")
    # PostgreSQL's clock is authoritative; no worker-provided wall clock.
    connection.execute(text("""
        INSERT INTO public.a95_review_rotation_cycle(
          cycle_id,expected_epoch,expires_at
        ) VALUES (:cycle_id,:epoch,clock_timestamp()+interval '45 seconds')
    """), {"cycle_id": cycle_id, "epoch": expected_epoch})
    connection.execute(text("""
        INSERT INTO public.a95_review_rotation_member(cycle_id,worker_id)
        VALUES (:cycle_id,:worker_id)
    """), [{"cycle_id": cycle_id, "worker_id": w} for w in workers])


def acknowledge_rotation_drain_on_clone(
    connection: Connection, *, cycle_id: uuid.UUID, worker_id: uuid.UUID,
    instance_id: uuid.UUID, current_epoch: int, draining: bool,
    active_publications: int,
) -> None:
    """Record ONE externally verified drain ACK; never trust a claimed count."""
    _owner(connection)
    if (
        not isinstance(worker_id, uuid.UUID) or worker_id.int == 0
        or not isinstance(instance_id, uuid.UUID) or instance_id.int == 0
        or type(current_epoch) is not int
        or draining is not True
        or type(active_publications) is not int
        or active_publications != 0
    ):
        raise ReviewCohortRejected("Invalid review worker drain acknowledgement.")
    if connection.scalar(text(
        "SELECT to_regclass('public.a96_review_worker_identity')"
    )) is not None:
        raise ReviewCohortRejected("Unsigned review ACK is disabled.")
    epoch, state = _lock_cycle(connection, cycle_id)
    if state != "collecting" or epoch != current_epoch:
        raise ReviewCohortRejected("Stale review worker acknowledgement.")
    updated = connection.execute(text("""
        UPDATE public.a95_review_rotation_member
        SET instance_id=:instance_id, acknowledged_at=clock_timestamp()
        WHERE cycle_id=:cycle_id AND worker_id=:worker_id
          AND acknowledged_at IS NULL AND instance_id IS NULL
    """), {
        "cycle_id": cycle_id, "worker_id": worker_id,
        "instance_id": instance_id,
    })
    if updated.rowcount != 1:
        raise ReviewCohortRejected("Unexpected or duplicate review worker ACK.")


def seal_rotation_cohort_on_clone(
    connection: Connection, *, cycle_id: uuid.UUID,
) -> None:
    """Only the complete, unexpired frozen inventory may be marked fenced."""
    _owner(connection)
    epoch, state = _lock_cycle(connection, cycle_id)
    if state != "collecting" or connection.scalar(_LATEST) != epoch:
        raise ReviewCohortRejected("Review rotation state changed.")
    missing = connection.scalar(text("""
        SELECT count(*) FROM public.a95_review_rotation_member
        WHERE cycle_id=:cycle_id AND (
          acknowledged_at IS NULL
          OR acknowledged_at<clock_timestamp()-interval '45 seconds'
        )
    """), {"cycle_id": cycle_id})
    if missing != 0:
        raise ReviewCohortRejected("Review rotation inventory is not drained.")
    if connection.scalar(text(
        "SELECT to_regclass('public.a96_review_worker_identity')"
    )) is not None:
        invalid_attestations = connection.scalar(text("""
            SELECT count(*) FROM public.a95_review_rotation_member m
            LEFT JOIN public.a96_review_worker_identity i
              ON i.worker_id=m.worker_id
            WHERE m.cycle_id=:cycle_id
              AND (m.verified IS NOT TRUE OR m.challenge IS NULL
                OR m.instance_id IS DISTINCT FROM m.expected_instance_id
                OR i.instance_id IS DISTINCT FROM m.expected_instance_id)
        """), {"cycle_id": cycle_id})
        if invalid_attestations != 0:
            raise ReviewCohortRejected("Authenticated roster is incomplete.")
    # Guard against the table being empty or altered between calls.
    if connection.scalar(text("""
        SELECT count(*) FROM public.a95_review_rotation_member
        WHERE cycle_id=:cycle_id
    """), {"cycle_id": cycle_id}) == 0:
        raise ReviewCohortRejected("Review rotation inventory is empty.")
    connection.execute(text("""
        UPDATE public.a95_review_rotation_cycle
        SET state='sealed' WHERE cycle_id=:cycle_id AND state='collecting'
    """), {"cycle_id": cycle_id})


def rotate_review_signer_with_cohort_on_clone(
    connection: Connection, *, cycle_id: uuid.UUID,
    next_credential: ReviewSigningCredential,
) -> None:
    """Consume the sealed cohort and change epoch atomically within a savepoint."""
    _owner(connection)
    connection.execute(_ROTATION_LOCK)
    epoch, state = _lock_cycle(connection, cycle_id)
    if (
        state != "sealed"
        or not isinstance(next_credential, ReviewSigningCredential)
        or next_credential.epoch != epoch + 1
    ):
        raise ReviewCohortRejected("Review rotation cohort cannot be consumed.")
    with connection.begin_nested():
        provision_review_signer_on_clone(
            connection, next_credential, expected_latest_epoch=epoch,
        )
        # A96's DB trigger consumes the sealed cycle *during the key INSERT*.
        # For A95-only clone rehearsals preserve the legacy wrapper behavior.
        if connection.scalar(text(
            "SELECT to_regclass('public.a96_review_worker_identity')"
        )) is not None:
            status = connection.scalar(text("""
                SELECT state FROM public.a95_review_rotation_cycle
                WHERE cycle_id=:cycle_id
            """), {"cycle_id": cycle_id})
            if status != "consumed":
                raise ReviewCohortRejected("DB rotation fence was not consumed.")
        else:
            updated = connection.execute(text("""
                UPDATE public.a95_review_rotation_cycle
                SET state='consumed'
                WHERE cycle_id=:cycle_id AND state='sealed'
            """), {"cycle_id": cycle_id})
            if updated.rowcount != 1:
                raise ReviewCohortRejected("Review rotation cohort has been consumed.")
