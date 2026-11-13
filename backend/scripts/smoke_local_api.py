"""Non-mutating runtime smoke test (no credentials or catalog content printed)."""
from __future__ import annotations

import httpx

DIRECT = "http://127.0.0.1:8000"
PROXY = "http://127.0.0.1:5173"

def main() -> None:
    with httpx.Client(timeout=7.0) as client:
        for label, base in (("api", DIRECT), ("vite_proxy", PROXY)):
            for path in ("/api/health/live", "/api/health/ready", "/api/capabilities"):
                response = client.get(base + path)
                print(label, path, response.status_code)
                assert response.status_code == 200
            current = client.get(base + "/api/catalog/current")
            items = client.get(base + "/api/catalog/items")
            print(label, "catalog/current", current.status_code)
            print(label, "catalog/items", items.status_code)
            assert current.status_code in (200, 404)
            assert items.status_code == 200
            rows = items.json()["items"]
            assert isinstance(rows, list)
            if current.status_code == 404:
                assert current.json()["code"] == "no_active_catalog"
                assert not rows, "Catalog must be empty when no version is published."
            else:
                assert current.json()["rows_count"] >= len(rows) >= 1

        denied = client.post(
            DIRECT + "/api/quotes/drafts",
            headers={"Origin": "http://localhost:5173"},
            json={"catalog_version_id": "invalid"},
        )
        print("draft_unauthorized", denied.status_code)
        assert denied.status_code == 401
        invalid_origin = client.post(
            DIRECT + "/api/requests/extract",
            headers={"Origin": "https://untrusted.example"},
            json={"text": "example"},
        )
        print("invalid_origin", invalid_origin.status_code)
        assert invalid_origin.status_code == 403
        print("LIVE_API_SMOKE_PASS", 12, "non-mutating HTTP checks")

if __name__ == "__main__":
    main()
