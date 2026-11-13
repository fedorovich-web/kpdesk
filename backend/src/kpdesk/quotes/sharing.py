"""Explicit, revocable capabilities over frozen client-facing PDF bytes."""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from kpdesk.quotes.pdf import render_draft_pdf
from kpdesk.quotes.storage import QuoteConflict
from kpdesk.quotes.tenant_storage import TenantQuoteRepository


class ShareUnavailable(LookupError):
    """Unknown, revoked, expired and damaged capabilities share one response."""


class ShareMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: uuid.UUID
    revision: int
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class IssuedShare(BaseModel):
    share: ShareMetadata
    token: str


def sharing_available(connection: Connection) -> bool:
    return connection.scalar(text("""SELECT
        to_regprocedure('public.kpdesk_proposal_share_issue(uuid,uuid,integer,text,text,bytea,text,integer)') IS NOT NULL
        AND to_regprocedure('public.kpdesk_proposal_share_list(uuid,uuid)') IS NOT NULL
        AND to_regprocedure('public.kpdesk_proposal_share_revoke(uuid,uuid,uuid)') IS NOT NULL
        AND to_regprocedure('public.kpdesk_proposal_share_pdf(text)') IS NOT NULL
    """)) is True


def issue_share(store: TenantQuoteRepository, quote_id: uuid.UUID, revision: int,
                snapshot_sha256: str, days: int) -> IssuedShare:
    store._authorize(write=True)
    quote = store.get(quote_id)
    if quote.revision != revision or quote.snapshot_sha256 != snapshot_sha256:
        raise QuoteConflict('stale_revision')
    if not quote.approved or quote.document is None or quote.approval is None:
        raise QuoteConflict('approved_document_required')
    if days not in (1, 7, 30):
        raise ValueError('invalid_share_expiry')
    raw = render_draft_pdf(quote)
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError('share_pdf_too_large')
    token = secrets.token_urlsafe(32)
    result = store.connection.scalar(text("""SELECT public.kpdesk_proposal_share_issue(
        :org,:quote,:revision,:snapshot,:digest,:pdf,:pdf_sha,:days)
    """), {'org': store.organization_id, 'quote': quote_id, 'revision': revision,
        'snapshot': snapshot_sha256, 'digest': hashlib.sha256(token.encode('ascii')).hexdigest(),
        'pdf': raw, 'pdf_sha': hashlib.sha256(raw).hexdigest(), 'days': days})
    if result is None:
        raise QuoteConflict('share_issue_conflict')
    return IssuedShare(share=ShareMetadata.model_validate(result), token=token)


def list_shares(store: TenantQuoteRepository, quote_id: uuid.UUID) -> list[ShareMetadata]:
    store.get(quote_id)  # Session, organization and quote membership before gateway.
    rows = store.connection.scalar(text('SELECT public.kpdesk_proposal_share_list(:org,:quote)'),
                                   {'org': store.organization_id, 'quote': quote_id})
    if rows is None:
        raise RuntimeError('Share metadata unavailable')
    return [ShareMetadata.model_validate(row) for row in rows]


def revoke_share(store: TenantQuoteRepository, quote_id: uuid.UUID, share_id: uuid.UUID) -> None:
    store._authorize(write=True)
    store.get(quote_id)
    if store.connection.scalar(text('SELECT public.kpdesk_proposal_share_revoke(:org,:quote,:id)'),
                              {'org': store.organization_id, 'quote': quote_id, 'id': share_id}) is not True:
        raise ShareUnavailable()


def public_pdf(connection: Connection, token: str | None) -> bytes:
    if not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9_-]{43}', token) is None:
        raise ShareUnavailable()
    row = connection.execute(text('SELECT pdf,pdf_sha256 FROM public.kpdesk_proposal_share_pdf(:digest)'),
        {'digest': hashlib.sha256(token.encode('ascii')).hexdigest()}).one_or_none()
    if row is None:
        raise ShareUnavailable()
    raw = bytes(row[0])
    if not raw.startswith(b'%PDF-') or hashlib.sha256(raw).hexdigest() != row[1]:
        raise ShareUnavailable()
    return raw
