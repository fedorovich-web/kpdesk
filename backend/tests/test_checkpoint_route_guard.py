"""Verify the explicit test-only checkpoint route registration."""
from __future__ import annotations

from fastapi import FastAPI

from kpdesk.main import create_app


def test_checkpoint_route_registration_is_test_only() -> None:
    live = create_app(tenant_sessions=True)
    isolated = create_app(testing=True, tenant_sessions=True)
    legacy = create_app(testing=True)
    suffix = "/canonical/checkpoints/"

    def checkpoint_paths(app: FastAPI) -> set[str]:
        paths: set[str] = set()
        for route in app.routes:
            nested = getattr(getattr(route, "original_router", None), "routes", ())
            for child in (route, *nested):
                path = getattr(child, "path", "")
                if suffix in path:
                    paths.add(path)
        return paths

    assert checkpoint_paths(live) == set()
    assert checkpoint_paths(legacy) == set()
    active_paths = checkpoint_paths(isolated)
    assert len(active_paths) == 5
    assert any(path.endswith("/save") for path in active_paths)
    assert any(path.endswith("/restore") for path in active_paths)
    assert any(path.endswith("/append") for path in active_paths)
    assert any(path.endswith("/restore-page") for path in active_paths)
    assert any(path.endswith("/{checkpoint_id}/publish") for path in active_paths)
