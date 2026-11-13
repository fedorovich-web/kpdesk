"""A95 actual PostgreSQL cohort fencing rehearsal, only on disposable clone."""
from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable

from sqlalchemy import Connection, text

from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.db.review_rotation_cohort import (
    ReviewCohortRejected,
    acknowledge_rotation_drain_on_clone,
    begin_rotation_cohort_on_clone,
    install_rotation_cohort_on_clone,
    rotate_review_signer_with_cohort_on_clone,
    seal_rotation_cohort_on_clone,
)
from kpdesk.db.review_signer_provision import (
    provision_review_signer_on_clone,
    verify_review_signer,
)


def verify_review_rotation_cohort(connection: Connection) -> None:
    """Do not commit coordinator fixtures or keys into the owning clone."""
    with connection.begin_nested() as rehearsal:
        install_rotation_cohort_on_clone(connection)
        first = ReviewSigningCredential(epoch=1, key=secrets.token_bytes(32))
        second = ReviewSigningCredential(epoch=2, key=secrets.token_bytes(32))
        provision_review_signer_on_clone(
            connection, first, expected_latest_epoch=None,
        )
        workers = (uuid.uuid4(), uuid.uuid4())
        instances = (uuid.uuid4(), uuid.uuid4())
        cycle = uuid.uuid4()
        begin_rotation_cohort_on_clone(
            connection, cycle_id=cycle, expected_epoch=1, workers=workers,
        )

        def ack(index: int, *, epoch: int = 1, active: int = 0) -> None:
            acknowledge_rotation_drain_on_clone(
                connection, cycle_id=cycle, worker_id=workers[index],
                instance_id=instances[index], current_epoch=epoch,
                draining=True, active_publications=active,
            )

        negative_checks: tuple[Callable[[], None], ...] = (
            lambda: seal_rotation_cohort_on_clone(connection, cycle_id=cycle),
            lambda: ack(0, active=1),
            lambda: ack(0, epoch=2),
        )
        for check in negative_checks:
            try:
                check()
            except ReviewCohortRejected:
                pass
            else:
                raise AssertionError("A95 accepted incomplete/stale cohort.")
        ack(0)
        try:
            ack(0)
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A95 accepted duplicate drain ACK.")
        try:
            acknowledge_rotation_drain_on_clone(
                connection, cycle_id=cycle, worker_id=uuid.uuid4(),
                instance_id=uuid.uuid4(), current_epoch=1,
                draining=True, active_publications=0,
            )
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A95 accepted unregistered worker.")

        try:
            seal_rotation_cohort_on_clone(connection, cycle_id=cycle)
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A95 ignored missing second worker.")
        ack(1)
        seal_rotation_cohort_on_clone(connection, cycle_id=cycle)
        try:
            ack(1)
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A95 accepted late ACK after sealing.")
        assert verify_review_signer(connection, first)
        try:
            rotate_review_signer_with_cohort_on_clone(
                connection, cycle_id=cycle, next_credential=first,
            )
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A95 reused the old epoch.")
        rotate_review_signer_with_cohort_on_clone(
            connection, cycle_id=cycle, next_credential=second,
        )
        assert verify_review_signer(connection, second)
        assert not verify_review_signer(connection, first)
        try:
            rotate_review_signer_with_cohort_on_clone(
                connection, cycle_id=cycle,
                next_credential=ReviewSigningCredential(
                    epoch=3, key=secrets.token_bytes(32),
                ),
            )
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A95 replayed consumed rotation cycle.")

        # A restarted process is NOT recognized by an older, consumed cycle.
        try:
            acknowledge_rotation_drain_on_clone(
                connection, cycle_id=cycle, worker_id=workers[0],
                instance_id=uuid.uuid4(), current_epoch=2,
                draining=True, active_publications=0,
            )
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A95 reused a consumed worker roster.")

        later = uuid.uuid4()
        begin_rotation_cohort_on_clone(
            connection, cycle_id=later, expected_epoch=2, workers=workers,
        )
        acknowledge_rotation_drain_on_clone(
            connection, cycle_id=later, worker_id=workers[0],
            instance_id=uuid.uuid4(), current_epoch=2,
            draining=True, active_publications=0,
        )
        acknowledge_rotation_drain_on_clone(
            connection, cycle_id=later, worker_id=workers[1],
            instance_id=uuid.uuid4(), current_epoch=2,
            draining=True, active_publications=0,
        )
        # Simulate coordinator/worker outage beyond DB-issued deadline.
        connection.execute(text("""
            UPDATE public.a95_review_rotation_cycle
            SET expires_at=clock_timestamp()-interval '1 second'
            WHERE cycle_id=:cycle
        """), {"cycle": later})
        try:
            seal_rotation_cohort_on_clone(connection, cycle_id=later)
        except ReviewCohortRejected:
            pass
        else:
            raise AssertionError("A95 accepted expired cycle.")
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a84_provenance_signing_secret
        """)) == 2

        # Test-only schema and all fake worker IDs/keys are rolled back.
        rehearsal.rollback()
    assert connection.scalar(text("""
        SELECT to_regclass('public.a95_review_rotation_cycle')
    """)) is None
    assert connection.scalar(text("""
        SELECT to_regclass('public.a95_review_rotation_member')
    """)) is None
    assert connection.scalar(text("""
        SELECT count(*) FROM public.a84_provenance_signing_secret
    """)) == 0
