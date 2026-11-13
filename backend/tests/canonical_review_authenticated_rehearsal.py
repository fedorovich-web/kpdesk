"""A96 authenticated HMAC ACK + PostgreSQL insert fencing on marked clone."""
from __future__ import annotations

import secrets
import uuid

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.db.review_authenticated_rotation import (
    acknowledge_authenticated_drain_on_clone,
    begin_authenticated_rotation_on_clone,
    enroll_worker_on_clone,
    install_authenticated_rotation_on_clone,
    read_worker_challenge_on_clone,
    restart_worker_on_clone,
    sign_worker_drain_attestation,
)
from kpdesk.db.review_rotation_cohort import (
    ReviewCohortRejected,
    acknowledge_rotation_drain_on_clone,
    install_rotation_cohort_on_clone,
    rotate_review_signer_with_cohort_on_clone,
    seal_rotation_cohort_on_clone,
)
from kpdesk.db.review_schema_activation import activate_review_schema_on_clone
from kpdesk.db.review_signer_provision import (
    ReviewSignerProvisionRejected,
    provision_review_signer_on_clone,
    verify_review_signer,
)


def verify_authenticated_rotation_on_clone(connection: Connection) -> None:
    with connection.begin_nested() as rehearsal:
        install_rotation_cohort_on_clone(connection)
        first = ReviewSigningCredential(epoch=1, key=secrets.token_bytes(32))
        second = ReviewSigningCredential(epoch=2, key=secrets.token_bytes(32))
        provision_review_signer_on_clone(
            connection, first, expected_latest_epoch=None,
        )
        install_authenticated_rotation_on_clone(connection)
        workers = (uuid.uuid4(), uuid.uuid4())
        instances = (uuid.uuid4(), uuid.uuid4())
        keys = (secrets.token_bytes(32), secrets.token_bytes(32))
        for worker, instance, key in zip(workers, instances, keys, strict=True):
            enroll_worker_on_clone(
                connection, worker_id=worker, instance_id=instance, key=key,
            )
        cycle = uuid.uuid4()
        begin_authenticated_rotation_on_clone(
            connection, cycle_id=cycle, expected_epoch=1, workers=workers,
        )
        try:
            activate_review_schema_on_clone(connection, first)
        except ReviewSignerProvisionRejected:
            pass
        else:
            raise AssertionError("A96 reactivated runtime during a pending cohort.")
        attestation = []
        for worker, instance, key in zip(workers, instances, keys, strict=True):
            returned_instance, epoch, challenge = read_worker_challenge_on_clone(
                connection, cycle_id=cycle, worker_id=worker,
            )
            assert returned_instance == instance
            assert epoch == 1
            attestation.append(sign_worker_drain_attestation(
                key=key, cycle_id=cycle, worker_id=worker,
                instance_id=instance, epoch=epoch, challenge=challenge,
            ))

        # SQL trigger rejects the OLD lower-level provisioner even with a
        # completely valid epoch and signer key, unless cohort was sealed.
        with connection.begin_nested() as attempt:
            try:
                provision_review_signer_on_clone(
                    connection, second, expected_latest_epoch=1,
                )
            except DBAPIError as error:
                assert "23514" in str(error.orig) or "rotation fencing" in str(error.orig)
            else:
                raise AssertionError("A96 permitted raw key rotation bypass.")
            attempt.rollback()
        assert verify_review_signer(connection, first)
        try:
            acknowledge_rotation_drain_on_clone(
                connection, cycle_id=cycle, worker_id=workers[0],
                instance_id=instances[0], current_epoch=1,
                draining=True, active_publications=0,
            )
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A96 accepted unsigned A95 compatibility ACK.")

        # An unrelated instance, wrong key, or wrong challenge cannot sign.
        for invalid in (
            "0" * 64,
            sign_worker_drain_attestation(
                key=keys[1], cycle_id=cycle, worker_id=workers[0],
                instance_id=instances[0], epoch=1,
                challenge=read_worker_challenge_on_clone(
                    connection, cycle_id=cycle, worker_id=workers[0],
                )[2],
            ),
        ):
            try:
                acknowledge_authenticated_drain_on_clone(
                    connection, cycle_id=cycle, worker_id=workers[0],
                    instance_id=instances[0], epoch=1, signature=invalid,
                )
            except ReviewCohortRejected:
                pass
            else:
                raise AssertionError("A96 accepted a forged worker signature.")
        acknowledge_authenticated_drain_on_clone(
            connection, cycle_id=cycle, worker_id=workers[0],
            instance_id=instances[0], epoch=1, signature=attestation[0],
        )
        try:
            acknowledge_authenticated_drain_on_clone(
                connection, cycle_id=cycle, worker_id=workers[0],
                instance_id=instances[0], epoch=1, signature=attestation[0],
            )
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A96 accepted duplicate authenticated ACK.")
        try:
            seal_rotation_cohort_on_clone(connection, cycle_id=cycle)
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A96 sealed with missing second worker.")
        acknowledge_authenticated_drain_on_clone(
            connection, cycle_id=cycle, worker_id=workers[1],
            instance_id=instances[1], epoch=1, signature=attestation[1],
        )
        seal_rotation_cohort_on_clone(connection, cycle_id=cycle)
        rotate_review_signer_with_cohort_on_clone(
            connection, cycle_id=cycle, next_credential=second,
        )
        assert verify_review_signer(connection, second)
        assert not verify_review_signer(connection, first)
        assert connection.scalar(text("""
            SELECT state FROM public.a95_review_rotation_cycle
            WHERE cycle_id=:cycle
        """), {"cycle": cycle}) == "consumed"

        # New cycle ACK is invalidated by a known worker restart.
        later = uuid.uuid4()
        begin_authenticated_rotation_on_clone(
            connection, cycle_id=later, expected_epoch=2, workers=workers,
        )
        restart_worker_on_clone(
            connection, worker_id=workers[0],
            next_instance_id=uuid.uuid4(), next_key=secrets.token_bytes(32),
        )
        assert connection.scalar(text("""
            SELECT state FROM public.a95_review_rotation_cycle
            WHERE cycle_id=:cycle
        """), {"cycle": later}) == "invalidated"
        try:
            activate_review_schema_on_clone(connection, second)
        except ReviewSignerProvisionRejected:
            pass
        else:
            raise AssertionError("A96 reactivated runtime after invalidation.")
        try:
            seal_rotation_cohort_on_clone(connection, cycle_id=later)
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A96 sealed invalidated worker generation.")

        rehearsal.rollback()
    assert connection.scalar(text("""
        SELECT to_regclass('public.a96_review_worker_identity')
    """)) is None
    assert connection.scalar(text("""
        SELECT to_regprocedure('public.a96_enforce_rotation_insert()')
    """)) is None
    assert connection.scalar(text("""
        SELECT to_regclass('public.a95_review_rotation_cycle')
    """)) is None
    assert connection.scalar(text("""
        SELECT count(*) FROM public.a84_provenance_signing_secret
    """)) == 0
