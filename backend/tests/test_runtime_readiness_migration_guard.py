"""Metadata deployment stays explicitly gated; no DDL under default invocation."""
import importlib
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('action',['upgrade','downgrade'])
def test_metadata_migration_requires_approval_before_connecting(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    migration=importlib.import_module('migrations.versions.0041_runtime_readiness')
    get_bind=Mock()
    monkeypatch.setattr(migration.op,'get_context',lambda:Mock(config=Mock(attributes={})))
    monkeypatch.setattr(migration.op,'get_bind',get_bind)
    with pytest.raises(RuntimeError,match='explicit approval'):
        getattr(migration,action)()
    get_bind.assert_not_called()
