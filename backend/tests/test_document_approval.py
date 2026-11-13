"""Document approval appends a revision and cannot reprice or approve stale data."""
from __future__ import annotations

import uuid
from dataclasses import replace
from typing import cast
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import Connection
from test_commercial_document import content
from test_quote_pdf import draft

from kpdesk.db.tenant_access import AuthenticatedUser
from kpdesk.quotes.storage import QuoteConflict
from kpdesk.quotes.tenant_storage import TenantQuoteRepository


@pytest.mark.parametrize("condition", ["ready", "stale_revision", "stale_snapshot", "incomplete"])
def test_approval_is_bound_to_exact_saved_revision_and_actor(condition: str) -> None:
    current = draft()
    if condition == "incomplete":
        current = replace(current, ready_for_draft=False)
    conn = Mock()
    conn.scalar.return_value = current.revision + (condition == "stale_revision")
    conn.execute.return_value.rowcount = 1
    actor = uuid.uuid4()
    store = TenantQuoteRepository(cast(Connection, conn), AuthenticatedUser(actor), uuid.uuid4(), "session", "csrf")
    with (
        patch.object(TenantQuoteRepository, "_authorize") as authorize,
        patch.object(TenantQuoteRepository, "_lock_tenant"),
        patch.object(TenantQuoteRepository, "_existing", return_value=None),
        patch.object(TenantQuoteRepository, "_read_revision", return_value=current),
        patch.object(TenantQuoteRepository, "_append", return_value=current) as append,
    ):
        if condition != "ready":
            with pytest.raises(QuoteConflict):
                store.approve_document(
                    current.quote_id, current.revision,
                    "b" * 64 if condition == "stale_snapshot" else current.snapshot_sha256,
                    content(), "approve-key",
                )
            append.assert_not_called()
        else:
            store.approve_document(current.quote_id, current.revision, current.snapshot_sha256, content(), "approve-key")
            snapshot = append.call_args.kwargs["snapshot"]
            assert snapshot.approved and snapshot.approval.user_id == actor
            assert snapshot.total_rub == current.total_rub and snapshot.lines == current.lines
            assert snapshot.approval.source_snapshot_sha256 == current.snapshot_sha256
            assert append.call_args.kwargs["number"] == current.revision + 1
            assert not any("catalog_items" in str(call.args[0]) for call in conn.execute.call_args_list)
        authorize.assert_called_once_with(write=True)


def test_approval_rejects_client_tampering_with_alternative_money() -> None:
    from decimal import Decimal

    from test_document_alternatives import saved

    from kpdesk.quotes.alternatives import available_alternatives

    current, products = saved()
    alternatives = available_alternatives(current, products)
    document = content().model_copy(update={'alternatives': (
        alternatives[0].model_copy(update={'total_rub': Decimal('0.01')}),
    )})
    conn = Mock()
    conn.scalar.return_value = current.revision
    store = TenantQuoteRepository(cast(Connection, conn), AuthenticatedUser(uuid.uuid4()), uuid.uuid4(), 'session', 'csrf')
    with (
        patch.object(TenantQuoteRepository, '_authorize'),
        patch.object(TenantQuoteRepository, '_lock_tenant'),
        patch.object(TenantQuoteRepository, '_existing', return_value=None),
        patch.object(TenantQuoteRepository, '_read_revision', return_value=current),
        patch.object(TenantQuoteRepository, 'resolve_document_alternatives', return_value=alternatives),
        patch.object(TenantQuoteRepository, '_append') as append,
    ):
        with pytest.raises(ValueError, match='alternative_snapshot_mismatch'):
            store.approve_document(current.quote_id, current.revision, current.snapshot_sha256, document, 'tampered')
        append.assert_not_called()
