"""A95 guard tests: inventory and ACK validation before any SQL mutation."""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from unittest.mock import Mock

import pytest

from kpdesk.db import review_rotation_cohort as cohort
from kpdesk.db.review_signer_provision import ReviewSignerProvisionRejected


@pytest.mark.parametrize("workers", [
    (),
    (uuid.UUID(int=0),),
    (uuid.UUID(int=1), uuid.UUID(int=1)),
    tuple(uuid.UUID(int=index + 1) for index in range(33)),
])
def test_rejects_bad_worker_inventory_before_sql(
    monkeypatch: pytest.MonkeyPatch, workers: Sequence[uuid.UUID],
) -> None:
    connection = Mock()
    monkeypatch.setattr(cohort, "_owner", lambda unused: None)
    with pytest.raises(cohort.ReviewCohortRejected):
        cohort.begin_rotation_cohort_on_clone(
            connection, cycle_id=uuid.uuid4(), expected_epoch=1,
            workers=workers,
        )
    connection.execute.assert_not_called()


@pytest.mark.parametrize("epoch", [True, 0, -1, 2**63])
def test_rejects_invalid_epoch_before_sql(
    monkeypatch: pytest.MonkeyPatch, epoch: object,
) -> None:
    connection = Mock()
    monkeypatch.setattr(cohort, "_owner", lambda unused: None)
    with pytest.raises(cohort.ReviewCohortRejected):
        cohort.begin_rotation_cohort_on_clone(
            connection, cycle_id=uuid.uuid4(), expected_epoch=epoch,  # type: ignore[arg-type]
            workers=(uuid.uuid4(),),
        )
    connection.execute.assert_not_called()


@pytest.mark.parametrize("kwargs", [
    {"draining": False, "active_publications": 0, "current_epoch": 1},
    {"draining": True, "active_publications": 1, "current_epoch": 1},
    {"draining": True, "active_publications": -1, "current_epoch": 1},
    {"draining": True, "active_publications": True, "current_epoch": 1},
    {"draining": True, "active_publications": 0, "current_epoch": True},
])
def test_rejects_invalid_worker_ack_before_sql(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object],
) -> None:
    connection = Mock()
    monkeypatch.setattr(cohort, "_owner", lambda unused: None)
    with pytest.raises(cohort.ReviewCohortRejected):
        cohort.acknowledge_rotation_drain_on_clone(
            connection, cycle_id=uuid.uuid4(), worker_id=uuid.uuid4(),
            instance_id=uuid.uuid4(), **kwargs,  # type: ignore[arg-type]
        )
    connection.execute.assert_not_called()


def test_rotation_rejects_unmarked_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Mock()
    connection.in_transaction.return_value = False
    with pytest.raises(ReviewSignerProvisionRejected):
        cohort.install_rotation_cohort_on_clone(connection)
    connection.execute.assert_not_called()
