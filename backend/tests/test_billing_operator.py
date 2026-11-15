"""Operator command admission uses mocks only; no provider or database traffic."""
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from scripts import reconcile_lava_reversal as operator


def test_operator_requires_deliberate_invoice_correlation(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver=AsyncMock();monkeypatch.setattr(operator,'resolve',resolver)
    with pytest.raises(SystemExit):
        operator.main(['--event-id',str(uuid4()),'--invoice-id',str(uuid4())])
    resolver.assert_not_awaited()


def test_operator_passes_exact_ids_and_sanitizes_failures(monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    event,invoice=uuid4(),uuid4();resolver=AsyncMock()
    monkeypatch.setattr(operator,'resolve',resolver)
    args=['--event-id',str(event),'--invoice-id',str(invoice),'--confirm-invoice-correlation']
    assert operator.main(args)==0
    resolver.assert_awaited_once_with(event,invoice)
    resolver.side_effect=RuntimeError('private-provider-secret')
    assert operator.main(args)==1
    assert 'private-provider-secret' not in capsys.readouterr().out
