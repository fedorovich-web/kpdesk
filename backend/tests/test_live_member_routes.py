"""A17 member API exposure and cookie authorization negative checks."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from kpdesk.main import create_app


def test_membership_routes_are_not_present_in_default_application() -> None:
    app = create_app(testing=True)
    group = uuid.uuid4()
    with TestClient(app) as client:
        assert client.get("/api/session/invitations").status_code == 404
        assert client.post(
            "/api/session/invitations/register",
            headers={"Origin": "http://localhost:5173"},
            json={"token": "a" * 43, "password": "StrongPassword!123"},
        ).status_code == 404
        assert client.get(
            f"/api/session/organizations/{group}/members"
        ).status_code == 404
        assert client.get(
            f"/api/session/organizations/{group}/quotes"
        ).status_code == 404
        assert client.post(
            f"/api/session/organizations/{group}/requests/extract",
            headers={"Origin": "http://localhost:5173"},
            json={"text": "Бумага А4 2 пачки",
                  "consent_to_external_processing": True},
        ).status_code == 404


def test_unverified_cookie_never_accesses_members_or_invitations() -> None:
    app = create_app(testing=True, tenant_sessions=True)
    group = uuid.uuid4()
    target = uuid.uuid4()
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/session/invitations").status_code == 401
        assert client.post(
            "/api/session/invitations/register",
            headers={"Origin": "https://localhost:5173"},
            json={"token": "a" * 43, "password": "StrongPassword!123"},
        ).status_code == 400
        assert client.get(
            f"/api/session/organizations/{group}/members"
        ).status_code == 401
        assert client.get(
            f"/api/session/organizations/{group}/quotes"
        ).status_code == 401
        assert client.post(
            f"/api/session/organizations/{group}/requests/extract",
            headers={"Origin": "https://localhost:5173"},
            json={"text": "Бумага А4 2 пачки",
                  "consent_to_external_processing": True},
        ).status_code == 401
        assert client.post(
            f"/api/session/organizations/{group}/invitations",
            headers={"Origin": "https://localhost:5173"},
            json={"email": "verified@example.test", "role": "editor"},
        ).status_code == 401
        assert client.post(
            f"/api/session/organizations/{group}/invitations/email",
            headers={"Origin": "https://localhost:5173"},
            json={"email": "never-registered@example.test", "role": "viewer"},
        ).status_code == 401
        assert client.patch(
            f"/api/session/organizations/{group}/members/{target}",
            headers={"Origin": "https://localhost:5173"},
            json={"role": "viewer"},
        ).status_code == 401
        assert client.delete(
            f"/api/session/organizations/{group}/members/{target}",
            headers={"Origin": "https://localhost:5173"},
        ).status_code == 401
