"""A94 process-local signer admission/drain and API transaction ordering."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.routing import APIRoute

from kpdesk.api.tenant_session_routes import get_unreleased_auth_repository
from kpdesk.api.tenant_supplier_publication_routes import (
    get_review_publish_signer,
    router,
)
from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.catalog.review_worker_admission import (
    ReviewWorkerNotReady,
    ReviewWorkerPublicationGate,
)


def _credential(epoch: int = 1) -> ReviewSigningCredential:
    return ReviewSigningCredential(epoch=epoch, key=bytes([epoch]) * 32)


def test_worker_gate_rejects_new_work_after_drain() -> None:
    gate = ReviewWorkerPublicationGate(_credential())
    with gate.permit() as pin:
        assert pin.epoch == 1
        assert gate.active_publications == 1
        gate.begin_drain()
        assert gate.wait_drained(0) is False
        with pytest.raises(ReviewWorkerNotReady), gate.permit():
            raise AssertionError("A draining worker admitted new work")
    assert gate.wait_drained(0) is True
    assert gate.active_publications == 0
    gate.begin_drain()
    with gate.optional_permit() as unavailable:
        assert unavailable is None
    with pytest.raises(ReviewWorkerNotReady), gate.permit():
        raise AssertionError("Terminal drain was reversed")


def test_worker_releases_slot_when_publication_raises() -> None:
    gate = ReviewWorkerPublicationGate(_credential())
    with pytest.raises(ValueError, match="synthetic failure"), gate.permit():
        raise ValueError("synthetic failure")
    gate.begin_drain()
    assert gate.wait_drained(0)


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), True, "1"])
def test_invalid_drain_timeout_is_rejected(timeout: object) -> None:
    gate = ReviewWorkerPublicationGate(_credential())
    gate.begin_drain()
    with pytest.raises(ValueError):
        gate.wait_drained(timeout)  # type: ignore[arg-type]


def test_drain_requires_explicit_closure() -> None:
    gate = ReviewWorkerPublicationGate(_credential())
    with pytest.raises(ReviewWorkerNotReady):
        gate.wait_drained(0.1)


def test_two_workers_require_explicit_global_orchestration() -> None:
    first = ReviewWorkerPublicationGate(_credential(1))
    second = ReviewWorkerPublicationGate(_credential(1))
    entered = threading.Event()
    release = threading.Event()

    def in_flight() -> int:
        with first.permit() as pin:
            entered.set()
            assert release.wait(timeout=4)
            return pin.epoch

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(in_flight)
        assert entered.wait(timeout=4)
        first.begin_drain()
        assert not first.wait_drained(0.01)
        with pytest.raises(ReviewWorkerNotReady), first.permit():
            raise AssertionError("Draining first worker admitted a request")
        # A second process is independent and MUST be drained by its owner.
        with second.permit() as pin:
            assert pin.epoch == 1
        release.set()
        assert future.result(timeout=4) == 1
    assert first.wait_drained(0.1)
    second.begin_drain()
    assert second.wait_drained(0.1)


def test_signer_dependency_outlives_database_commit_dependency() -> None:
    route = next(
        endpoint for endpoint in router.routes
        if isinstance(endpoint, APIRoute) and endpoint.path.endswith("/publish")
    )
    dependencies = [item.call for item in route.dependant.dependencies]
    assert get_review_publish_signer in dependencies
    assert get_unreleased_auth_repository in dependencies
    assert dependencies.index(get_review_publish_signer) < (
        dependencies.index(get_unreleased_auth_repository)
    )
    # FastAPI closes yield dependencies in reverse order: repository commit
    # happens while signer admission is still held.
