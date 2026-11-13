"""A96 worker-side cryptographic attestation and terminal drain guard."""
from __future__ import annotations

import uuid

import pytest

from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.catalog.review_worker_admission import ReviewWorkerPublicationGate
from kpdesk.db.review_authenticated_rotation import (
    sign_drained_worker_attestation,
    sign_worker_drain_attestation,
)
from kpdesk.db.review_rotation_cohort import ReviewCohortRejected


def _params() -> dict[str, object]:
    return {
        "key": bytes.fromhex("ab" * 32),
        "cycle_id": uuid.uuid4(),
        "worker_id": uuid.uuid4(),
        "instance_id": uuid.uuid4(),
        "epoch": 4,
        "challenge": bytes.fromhex("cd" * 32),
    }


def test_attestation_requires_terminal_drain_and_correct_epoch() -> None:
    gate = ReviewWorkerPublicationGate(
        ReviewSigningCredential(epoch=4, key=b"x" * 32),
    )
    params = _params()
    with pytest.raises(ReviewCohortRejected):
        sign_drained_worker_attestation(gate=gate, **params)  # type: ignore[arg-type]
    with gate.permit():
        gate.begin_drain()
        with pytest.raises(ReviewCohortRejected):
            sign_drained_worker_attestation(gate=gate, **params)  # type: ignore[arg-type]
    assert gate.wait_drained(0.1)
    signed = sign_drained_worker_attestation(gate=gate, **params)  # type: ignore[arg-type]
    assert len(signed) == 64
    assert signed == sign_worker_drain_attestation(**params)  # type: ignore[arg-type]
    with pytest.raises(ReviewCohortRejected):
        sign_drained_worker_attestation(
            gate=gate, **{**params, "epoch": 5},  # type: ignore[arg-type]
        )


def test_attestation_binds_instance_cycle_challenge() -> None:
    params = _params()
    signature = sign_worker_drain_attestation(**params)  # type: ignore[arg-type]
    for change in (
        {"instance_id": uuid.uuid4()},
        {"cycle_id": uuid.uuid4()},
        {"worker_id": uuid.uuid4()},
        {"challenge": b"e" * 32},
        {"epoch": 5},
    ):
        assert sign_worker_drain_attestation(
            **{**params, **change},  # type: ignore[arg-type]
        ) != signature


@pytest.mark.parametrize("bad", [b"short", b"", b"x" * 33, True, "secret"])
def test_bad_attestation_keys_rejected(bad: object) -> None:
    with pytest.raises(ReviewCohortRejected):
        sign_worker_drain_attestation(**{
            **_params(), "key": bad,
        })  # type: ignore[arg-type]
