"""Real restricted-tenant HTTPS on the existing provenance-tagged clone only."""
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from kpdesk.matching.feedback_bundle import FeedbackBundle, dataset_from_feedback


def verify_training_feedback(
    manager: TestClient, editor: TestClient, base: str, result: dict[str, Any],
    owner_headers: dict[str, str], editor_headers: dict[str, str], outsider: UUID,
    organization: UUID,
) -> None:
    scope = {'expected_revision': result['revision'], 'source_snapshot_sha256': result['snapshot_sha256']}
    endpoint = base + '/training-feedback'
    assert editor.post(endpoint + '/preview', json=scope, headers=editor_headers).status_code == 403
    assert manager.post(endpoint + '/preview', json=scope, headers={'Origin': 'https://testserver'}).status_code == 401
    assert manager.post(endpoint.replace(str(organization), str(outsider)) + '/preview',
                        json=scope, headers=owner_headers).status_code == 403
    assert manager.post(endpoint + '/preview', json={**scope, 'expected_revision': 999},
                        headers=owner_headers).status_code == 409
    preview = manager.post(endpoint + '/preview', json=scope, headers=owner_headers)
    assert preview.status_code == 200, preview.text
    rows = preview.json()['lines']
    decisions = []
    for row in rows:
        selected = row['selected_sku']
        negative = next(p['sku'] for p in row['candidates'] if p['sku'] != selected)
        decisions.append({'line_number': row['line_number'], 'relevant_sku': selected,
                          'rejected_skus': [negative], 'no_match': False})
    response = manager.post(endpoint, json={**scope, 'reviewed': True, 'training_consent': True,
                                           'decisions': decisions}, headers=owner_headers)
    assert response.status_code == 200, response.text
    bundle = FeedbackBundle.model_validate(response.json())
    assert bundle.snapshot_sha256 == result['snapshot_sha256']
    assert all(p.price_rub == 0 for p in bundle.catalog)
    assert len(dataset_from_feedback([bundle])['queries']) == len(rows)
    assert manager.get(base).json()['snapshot_sha256'] == result['snapshot_sha256']
    directory = ('local-release-20261102' if os.environ.get('KP_DESK_E2E_RELEASE_RUNTIME') == '1'
                 else 'ml-feedback-storage-20261102' if os.environ.get('KP_DESK_E2E_FEEDBACK_STORAGE') == '1'
                 else 'ml-feedback-20261102')
    art = Path(__file__).resolve().parents[2] / 'artifacts' / directory
    art.mkdir(exist_ok=True)
    (art / 'clone-owner-feedback.json').write_text(bundle.model_dump_json(indent=2), encoding='utf-8')
    (art / 'clone-feedback-http.json').write_text(json.dumps({
        'real_restricted_tenant_postgres': True, 'https_auth_csrf': True,
        'cross_tenant_editor_anonymous_and_stale_rejected': True, 'quote_unchanged': True,
        'explicit_synthetic_decisions': len(decisions), 'provider_requests': 0,
    }, indent=2), encoding='utf-8')
    if os.environ.get('KP_DESK_E2E_FEEDBACK_STORAGE') == '1':
        from canonical_training_feedback_storage_acceptance import verify_feedback_storage

        verify_feedback_storage(manager, editor, endpoint, scope, decisions, owner_headers,
                                editor_headers, outsider, organization, art)
