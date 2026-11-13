"""A93 safe refusal of unsafe owner-only signer operations."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.db import review_signer_provision as provision


def _credential(epoch: int = 1) -> ReviewSigningCredential:
    return ReviewSigningCredential(epoch=epoch, key=b"z" * 32)


@pytest.mark.parametrize(
    ("is_clone", "owner", "revision", "transaction"),
    [
        (False, "kpdesk", "0031_review_schema_candidate", True),
        (True, "kpdesk_tenant_runtime", "0031_review_schema_candidate", True),
        (True, "kpdesk", "0030_tenant_llm_cache", True),
        (True, "kpdesk", "0031_review_schema_candidate", False),
    ],
)
def test_rejects_unsafe_target_before_secret_write(
    monkeypatch: pytest.MonkeyPatch,
    is_clone: bool,
    owner: str,
    revision: str,
    transaction: bool,
) -> None:
    connection = Mock()
    connection.in_transaction.return_value = transaction
    connection.scalar.side_effect = [owner, revision]
    monkeypatch.setattr(
        provision, "is_live_accounts_http_clone", lambda unused: is_clone,
    )
    with pytest.raises(provision.ReviewSignerProvisionRejected):
        provision.provision_review_signer_on_clone(
            connection, _credential(), expected_latest_epoch=None,
        )
    connection.execute.assert_not_called()


def test_requires_redacted_sql_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Mock()
    connection.engine.hide_parameters = False
    monkeypatch.setattr(provision, "_owner_clone", lambda unused: None)
    with pytest.raises(provision.ReviewSignerProvisionRejected):
        provision.provision_review_signer_on_clone(
            connection, _credential(), expected_latest_epoch=None,
        )
    connection.execute.assert_not_called()


@pytest.mark.parametrize("expected", [True, 0, -1, 2**63, "1"])
def test_rejects_invalid_epoch_precondition(
    monkeypatch: pytest.MonkeyPatch, expected: object,
) -> None:
    connection = Mock()
    connection.engine.hide_parameters = True
    monkeypatch.setattr(provision, "_owner_clone", lambda unused: None)
    with pytest.raises(provision.ReviewSignerProvisionRejected):
        provision.provision_review_signer_on_clone(
            connection, _credential(), expected_latest_epoch=expected,  # type: ignore[arg-type]
        )
    connection.execute.assert_not_called()


def test_read_only_challenge_refuses_wrong_credential_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Mock()
    monkeypatch.setattr(provision, "_owner_clone", lambda unused: None)
    assert provision.verify_review_signer(connection, object()) is False  # type: ignore[arg-type]
    connection.scalar.assert_not_called()
    connection.execute.assert_not_called()
