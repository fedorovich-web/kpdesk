"""Regression boundaries for issuing a capability over fixed approved PDF bytes."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import Connection
from test_commercial_document import content
from test_quote_pdf import draft

from kpdesk.db.tenant_access import AuthenticatedUser
from kpdesk.quotes.document import DocumentApproval
from kpdesk.quotes.storage import QuoteConflict, StoredQuote
from kpdesk.quotes.tenant_storage import TenantQuoteRepository


def approved_quote() -> StoredQuote:
    original = draft()
    return replace(original, approved=True, document=content(), approval=DocumentApproval(
        user_id=uuid.uuid4(), approved_at=datetime.now(UTC), source_revision=1,
        source_snapshot_sha256=original.snapshot_sha256,
    ))


@pytest.mark.parametrize('condition', ['unapproved', 'revision', 'checksum'])
def test_share_refuses_unapproved_or_stale_quote(condition: str) -> None:
    from kpdesk.quotes.sharing import issue_share

    quote = approved_quote() if condition != 'unapproved' else draft()
    conn = Mock()
    store = TenantQuoteRepository(cast(Connection, conn), AuthenticatedUser(uuid.uuid4()), uuid.uuid4(), 's', 'c')
    with (patch.object(TenantQuoteRepository, '_authorize'),
          patch.object(TenantQuoteRepository, 'get', return_value=quote), pytest.raises(QuoteConflict)):
        issue_share(store, quote.quote_id, quote.revision + (condition == 'revision'),
                    'b' * 64 if condition == 'checksum' else quote.snapshot_sha256, 7)


def test_issue_stores_digest_and_pdf_bytes_never_raw_bearer() -> None:
    from kpdesk.quotes.sharing import issue_share

    quote = approved_quote()
    conn = Mock()
    conn.scalar.return_value = {'id': str(uuid.uuid4()), 'revision': 2,
        'created_at': '2026-11-04T12:00:00Z', 'expires_at': '2026-11-11T12:00:00Z', 'revoked_at': None}
    store = TenantQuoteRepository(cast(Connection, conn), AuthenticatedUser(uuid.uuid4()), uuid.uuid4(), 's', 'c')
    with patch.object(TenantQuoteRepository, '_authorize'), patch.object(TenantQuoteRepository, 'get', return_value=quote):
        result = issue_share(store, quote.quote_id, 2, quote.snapshot_sha256, 7)
    assert len(result.token) == 43
    params = conn.scalar.call_args.args[1]
    assert params['digest'] == hashlib.sha256(result.token.encode('ascii')).hexdigest()
    assert params['pdf'].startswith(b'%PDF-')
    assert result.token not in repr(params)
    assert params['pdf_sha'] == hashlib.sha256(params['pdf']).hexdigest()


@pytest.mark.parametrize('token', ['', 'bad', 'a' * 44, '../secret', 'я' * 43])
def test_public_invalid_token_is_generic_and_does_not_reach_sql(token: str) -> None:
    from kpdesk.quotes.sharing import ShareUnavailable, public_pdf

    conn = Mock()
    with pytest.raises(ShareUnavailable):
        public_pdf(cast(Connection, conn), token)
    conn.scalar.assert_not_called()


def test_public_pdf_validates_stored_bytes_digest() -> None:
    from kpdesk.quotes.sharing import ShareUnavailable, public_pdf

    conn = Mock()
    raw = b'%PDF-fixed'
    conn.execute.return_value.one_or_none.return_value = (raw, hashlib.sha256(raw).hexdigest())
    assert public_pdf(cast(Connection, conn), 'a' * 43) == raw
    conn.execute.return_value.one_or_none.return_value = (raw, '0' * 64)
    with pytest.raises(ShareUnavailable):
        public_pdf(cast(Connection, conn), 'a' * 43)
