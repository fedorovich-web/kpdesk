"""A82 reviewed-publish endpoint is absent outside the test-only tenant app."""
from __future__ import annotations

import uuid
from unittest.mock import Mock, patch

from fastapi import FastAPI
from starlette.routing import NoMatchFound

from kpdesk.main import create_app


def _has_publisher(app: FastAPI) -> bool:
    try:
        app.url_path_for(
            "publish_reviewed_checkpoint",
            organization_id=str(uuid.UUID(int=1)),
            supplier_id="supplier-a82",
            checkpoint_id=str(uuid.UUID(int=2)),
        )
    except NoMatchFound:
        return False
    return True


def test_review_publication_route_only_on_testing_tenant_app() -> None:
    with patch(
        "kpdesk.db.live_service_engines.live_service_engines",
        return_value=(Mock(), Mock()),
    ):
        normal = create_app(testing=False, tenant_sessions=True)
        isolated = create_app(testing=True, tenant_sessions=True)
    anonymous = create_app(testing=True, tenant_sessions=False)
    assert not _has_publisher(normal)
    assert not _has_publisher(anonymous)
    assert _has_publisher(isolated)
