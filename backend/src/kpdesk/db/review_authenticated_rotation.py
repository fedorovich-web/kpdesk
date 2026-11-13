"""A96 authenticated rotation proof for tagged disposable PostgreSQL only.

Owner provisions one per-worker attestation key through a trusted channel.
PostgreSQL checks HMAC without disclosing that key to the application.
The rotation table trigger fences even the existing low-level key inserter.
This is not a network-authenticated service or production deployment.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid

from sqlalchemy import Connection, text

from kpdesk.catalog.review_worker_admission import ReviewWorkerPublicationGate
from kpdesk.db.review_rotation_cohort import (
    ReviewCohortRejected,
    _lock_cycle,
    _owner,
    begin_rotation_cohort_on_clone,
)
from kpdesk.db.review_signer_provision import _ROTATION_LOCK

_DOMAIN = b"kpdesk/a96/drain/v1"
_VERSION = "0031_review_schema_candidate"


def _valid_uuid(value: object) -> bool:
    return isinstance(value, uuid.UUID) and value.int != 0


def _message(
    cycle_id: uuid.UUID, worker_id: uuid.UUID, instance_id: uuid.UUID,
    epoch: int, challenge: bytes,
) -> bytes:
    return b"\x1f".join((
        _DOMAIN, str(cycle_id).encode("ascii"), str(worker_id).encode("ascii"),
        str(instance_id).encode("ascii"), str(epoch).encode("ascii"),
        challenge.hex().encode("ascii"), b"drained", b"0",
    ))


def sign_worker_drain_attestation(
    *, key: bytes, cycle_id: uuid.UUID, worker_id: uuid.UUID,
    instance_id: uuid.UUID, epoch: int, challenge: bytes,
) -> str:
    """Pure worker-side signed ACK. Never send or log the signing key."""
    if (
        type(key) is not bytes or len(key) != 32
        or not all(_valid_uuid(item) for item in (cycle_id, worker_id, instance_id))
        or type(epoch) is not int or not 1 <= epoch <= 2**63 - 1
        or type(challenge) is not bytes or len(challenge) != 32
    ):
        raise ReviewCohortRejected("Invalid worker attestation input.")
    return hmac.new(
        key, _message(cycle_id, worker_id, instance_id, epoch, challenge),
        hashlib.sha256,
    ).hexdigest()


def sign_drained_worker_attestation(
    *, gate: ReviewWorkerPublicationGate, key: bytes,
    cycle_id: uuid.UUID, worker_id: uuid.UUID, instance_id: uuid.UUID,
    epoch: int, challenge: bytes,
) -> str:
    """Worker signs only after its terminal drain reached zero in-flight."""
    if (
        not isinstance(gate, ReviewWorkerPublicationGate)
        or not gate.draining or gate.active_publications != 0
        or gate.signer_epoch != epoch
    ):
        raise ReviewCohortRejected("Worker is not drained for attestation.")
    return sign_worker_drain_attestation(
        key=key, cycle_id=cycle_id, worker_id=worker_id,
        instance_id=instance_id, epoch=epoch, challenge=challenge,
    )


def install_authenticated_rotation_on_clone(connection: Connection) -> None:
    """Owner-only optional security upgrade for A95 clone cohort."""
    _owner(connection)
    connection.execute(text("""
        CREATE TABLE public.a96_review_worker_identity (
          worker_id uuid PRIMARY KEY,
          instance_id uuid NOT NULL,
          attestation_key bytea NOT NULL CHECK(octet_length(attestation_key)=32)
        )
    """))
    connection.execute(text("""
        REVOKE ALL ON public.a96_review_worker_identity
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """))
    connection.execute(text("""
        ALTER TABLE public.a96_review_worker_identity ENABLE ROW LEVEL SECURITY
    """))
    connection.execute(text("""
        ALTER TABLE public.a96_review_worker_identity FORCE ROW LEVEL SECURITY
    """))
    connection.execute(text("""
        CREATE POLICY a96_owner ON public.a96_review_worker_identity
        FOR ALL TO kpdesk USING(true) WITH CHECK(true)
    """))
    connection.execute(text("""
        ALTER TABLE public.a95_review_rotation_member
        ADD COLUMN expected_instance_id uuid,
        ADD COLUMN challenge bytea,
        ADD COLUMN verified boolean NOT NULL DEFAULT false
    """))
    connection.execute(text("""
        ALTER TABLE public.a95_review_rotation_cycle
        DROP CONSTRAINT a95_review_rotation_cycle_state_check
    """))
    connection.execute(text("""
        ALTER TABLE public.a95_review_rotation_cycle
        ADD CONSTRAINT a95_review_rotation_cycle_state_check
        CHECK (state IN ('collecting','sealed','consumed','invalidated'))
    """))
    # A SECURITY DEFINER trigger with a fixed search_path. All reviewed-
    # catalog SQL entrypoints are revoked during rotation (A94).
    connection.execute(text("""
        CREATE FUNCTION public.a96_enforce_rotation_insert()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE active_cycle uuid;
        BEGIN
          IF NEW.key_epoch=1 THEN RETURN NEW; END IF;
          SELECT c.cycle_id INTO active_cycle
          FROM public.a95_review_rotation_cycle c
          WHERE c.state='sealed'
            AND c.expected_epoch=NEW.key_epoch-1
            AND c.expires_at>clock_timestamp()
            AND EXISTS (
              SELECT 1 FROM public.a95_review_rotation_member m
              WHERE m.cycle_id=c.cycle_id
            )
            AND NOT EXISTS (
              SELECT 1 FROM public.a95_review_rotation_member m
              LEFT JOIN public.a96_review_worker_identity i
                ON i.worker_id=m.worker_id
              WHERE m.cycle_id=c.cycle_id
                AND (m.verified IS NOT TRUE
                  OR m.acknowledged_at IS NULL
                  OR m.acknowledged_at<clock_timestamp()-interval '45 seconds'
                  OR i.instance_id IS DISTINCT FROM m.instance_id
                  OR i.instance_id IS DISTINCT FROM m.expected_instance_id)
            )
          FOR UPDATE;
          IF active_cycle IS NULL THEN
            RAISE EXCEPTION 'review rotation fencing rejected'
              USING ERRCODE='23514';
          END IF;
          UPDATE public.a95_review_rotation_cycle SET state='consumed'
          WHERE cycle_id=active_cycle AND state='sealed';
          IF NOT FOUND THEN
            RAISE EXCEPTION 'review rotation fencing rejected'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
    """))
    connection.execute(text("""
        REVOKE ALL ON FUNCTION public.a96_enforce_rotation_insert() FROM PUBLIC
    """))
    connection.execute(text("""
        CREATE TRIGGER a96_rotation_fence
        BEFORE INSERT ON public.a84_provenance_signing_secret
        FOR EACH ROW EXECUTE FUNCTION public.a96_enforce_rotation_insert()
    """))


def enroll_worker_on_clone(
    connection: Connection, *, worker_id: uuid.UUID, instance_id: uuid.UUID,
    key: bytes,
) -> None:
    _owner(connection)
    if (
        not _valid_uuid(worker_id) or not _valid_uuid(instance_id)
        or type(key) is not bytes or len(key) != 32
    ):
        raise ReviewCohortRejected("Invalid registered worker identity.")
    # Registered identities must be provided out-of-band by the owner.
    connection.execute(text("""
        INSERT INTO public.a96_review_worker_identity(
            worker_id,instance_id,attestation_key
        ) VALUES (:worker_id,:instance_id,:key)
    """), {"worker_id": worker_id, "instance_id": instance_id, "key": key})


def begin_authenticated_rotation_on_clone(
    connection: Connection, *, cycle_id: uuid.UUID, expected_epoch: int,
    workers: tuple[uuid.UUID, ...],
) -> None:
    _owner(connection)
    with connection.begin_nested():
        # Original begin keeps the schema, revision and inactive SQL ACL checks.
        begin_rotation_cohort_on_clone(
            connection, cycle_id=cycle_id,
            expected_epoch=expected_epoch, workers=workers,
        )
        result = connection.execute(text("""
            UPDATE public.a95_review_rotation_member m
            SET expected_instance_id=i.instance_id,
                challenge=public.gen_random_bytes(32)
            FROM public.a96_review_worker_identity i
            WHERE m.cycle_id=:cycle AND m.worker_id=i.worker_id
            RETURNING m.worker_id
        """), {"cycle": cycle_id}).all()
        if len(result) != len(workers):
            raise ReviewCohortRejected("Rotation inventory has unknown workers.")


def read_worker_challenge_on_clone(
    connection: Connection, *, cycle_id: uuid.UUID, worker_id: uuid.UUID,
) -> tuple[uuid.UUID, int, bytes]:
    """Only trusted controller can pass challenge to the named worker."""
    _owner(connection)
    epoch, state = _lock_cycle(connection, cycle_id)
    if state != "collecting" or not _valid_uuid(worker_id):
        raise ReviewCohortRejected("Worker challenge is unavailable.")
    row = connection.execute(text("""
        SELECT expected_instance_id,challenge
        FROM public.a95_review_rotation_member
        WHERE cycle_id=:cycle AND worker_id=:worker
    """), {"cycle": cycle_id, "worker": worker_id}).one_or_none()
    if (
        row is None or not _valid_uuid(row.expected_instance_id)
        or not isinstance(row.challenge, bytes) or len(row.challenge) != 32
    ):
        raise ReviewCohortRejected("Worker challenge is unavailable.")
    return row.expected_instance_id, epoch, row.challenge


def acknowledge_authenticated_drain_on_clone(
    connection: Connection, *, cycle_id: uuid.UUID, worker_id: uuid.UUID,
    instance_id: uuid.UUID, epoch: int, signature: str,
) -> None:
    _owner(connection)
    if (
        not _valid_uuid(worker_id) or not _valid_uuid(instance_id)
        or type(epoch) is not int or not 1 <= epoch <= 2**63 - 1
        or not isinstance(signature, str) or len(signature) != 64
        or any(char not in "0123456789abcdef" for char in signature)
    ):
        raise ReviewCohortRejected("Invalid authenticated worker ACK.")
    expected_epoch, state = _lock_cycle(connection, cycle_id)
    if state != "collecting" or epoch != expected_epoch:
        raise ReviewCohortRejected("Stale authenticated worker ACK.")
    row = connection.execute(text("""
        SELECT m.challenge,m.expected_instance_id,i.instance_id
        FROM public.a95_review_rotation_member m
        JOIN public.a96_review_worker_identity i ON i.worker_id=m.worker_id
        WHERE m.cycle_id=:cycle AND m.worker_id=:worker
        FOR UPDATE OF m,i
    """), {"cycle": cycle_id, "worker": worker_id}).one_or_none()
    if (
        row is None or not isinstance(row.challenge, bytes)
        or len(row.challenge) != 32 or row.expected_instance_id != instance_id
        or row.instance_id != instance_id
    ):
        raise ReviewCohortRejected("Unrecognized worker instance.")
    message = _message(cycle_id, worker_id, instance_id, epoch, row.challenge)
    expected_mac = connection.scalar(text("""
        SELECT encode(public.hmac(:message,attestation_key,'sha256'),'hex')
        FROM public.a96_review_worker_identity
        WHERE worker_id=:worker AND instance_id=:instance
    """), {"message": message, "worker": worker_id, "instance": instance_id})
    if not isinstance(expected_mac, str) or not hmac.compare_digest(
        expected_mac, signature,
    ):
        raise ReviewCohortRejected("Worker signature verification failed.")
    result = connection.execute(text("""
        UPDATE public.a95_review_rotation_member
        SET instance_id=:instance,acknowledged_at=clock_timestamp(),verified=true
        WHERE cycle_id=:cycle AND worker_id=:worker
        AND acknowledged_at IS NULL AND verified=false
    """), {
        "cycle": cycle_id, "worker": worker_id, "instance": instance_id,
    })
    if result.rowcount != 1:
        raise ReviewCohortRejected("Duplicate authenticated worker ACK.")


def restart_worker_on_clone(
    connection: Connection, *, worker_id: uuid.UUID,
    next_instance_id: uuid.UUID, next_key: bytes,
) -> None:
    """A worker restart invalidates every pending cycle containing that worker."""
    _owner(connection)
    if (
        not _valid_uuid(worker_id) or not _valid_uuid(next_instance_id)
        or type(next_key) is not bytes or len(next_key) != 32
    ):
        raise ReviewCohortRejected("Invalid worker replacement.")
    connection.execute(_ROTATION_LOCK)
    with connection.begin_nested():
        # ACK takes the cycle row lock before the identity row lock. Keep the
        # same ordering here to avoid an ACK/restart lock inversion.
        connection.execute(text("""
            UPDATE public.a95_review_rotation_cycle c
            SET state='invalidated'
            WHERE c.state IN ('collecting','sealed')
              AND EXISTS (
                SELECT 1 FROM public.a95_review_rotation_member m
                WHERE m.cycle_id=c.cycle_id AND m.worker_id=:worker
              )
        """), {"worker": worker_id})
        count = connection.execute(text("""
            UPDATE public.a96_review_worker_identity
            SET instance_id=:instance,attestation_key=:key
            WHERE worker_id=:worker AND instance_id<>:instance
        """), {
            "instance": next_instance_id, "key": next_key, "worker": worker_id,
        }).rowcount
        if count != 1:
            raise ReviewCohortRejected("Worker replacement is not fresh.")
