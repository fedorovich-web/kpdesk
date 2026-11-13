"""Immutable universal snapshots; caller owns transaction and verified session."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import RowMapping

from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied
from kpdesk.db.universal_schema import universal_available
from kpdesk.universal.feedback import FeedbackSnapshot
from kpdesk.universal.schema import CatalogSnapshot, QuoteSnapshot


class UniversalUnavailable(RuntimeError):
    pass


class UniversalNotFound(LookupError):
    pass


class UniversalConflict(ValueError):
    pass


def digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def _parse_catalog(payload: object, source_sha256: str, version_id: UUID) -> CatalogSnapshot:
    try:
        result=CatalogSnapshot.model_validate(payload)
    except (TypeError,ValueError) as error:
        raise RuntimeError('universal_catalog_integrity') from error
    if result.source_sha256!=source_sha256 or result.version_id!=version_id:
        raise RuntimeError('universal_catalog_integrity')
    return result


def _parse_quote(payload: object, snapshot_sha256: str, quote_id: UUID,
                 revision: int, catalog_version_id: UUID) -> QuoteSnapshot:
    try:
        result=QuoteSnapshot.model_validate(payload)
    except (TypeError,ValueError) as error:
        raise RuntimeError('universal_quote_integrity') from error
    if (result.snapshot_sha256!=snapshot_sha256 or result.quote_id!=quote_id
            or result.revision!=revision or result.catalog_version_id!=catalog_version_id
            or digest(result.model_dump(mode='json',exclude={'snapshot_sha256'}))!=result.snapshot_sha256):
        raise RuntimeError('universal_quote_integrity')
    return result


def _parse_feedback(payload: object, feedback_id: UUID, actor_id: UUID,
                    fingerprint: str, quote: QuoteSnapshot, catalog: CatalogSnapshot) -> dict[str,Any]:
    try:
        result=FeedbackSnapshot.model_validate(payload)
    except (TypeError,ValueError) as error:
        raise RuntimeError('universal_feedback_integrity') from error
    if (result.id!=feedback_id or result.reviewer_id!=actor_id
            or result.quote_id!=quote.quote_id or result.revision!=quote.revision
            or result.catalog_version_id!=quote.catalog_version_id
            or result.source_sha256!=quote.source_sha256 or result.source_request!=quote.source_request
            or result.source_snapshot_sha256!=quote.snapshot_sha256
            or digest({'quote':str(result.quote_id),'feedback':result.input_payload()})!=fingerprint):
        raise RuntimeError('universal_feedback_integrity')
    skus={product.sku for product in catalog.products}
    for decision in result.decisions:
        negatives=set(decision.negative_skus)
        if (not negatives.issubset(skus)
                or (decision.relevant_sku is not None and decision.relevant_sku not in skus)
                or decision.no_match_asserted!=(decision.relevant_sku is None and negatives==skus)):
            raise RuntimeError('universal_feedback_integrity')
    return result.model_dump(mode='json')


@dataclass(frozen=True)
class UniversalStore:
    connection: Connection
    organization_id: UUID
    session_token: str
    csrf_token: str | None

    def authorize(self, *, write: bool=False, require_write_role: bool=False) -> AuthenticatedUser:
        scope=bind_authenticated_tenant(self.connection,session_token=self.session_token,
            organization_id=self.organization_id,operation='write' if write else 'read',csrf=self.csrf_token)
        if require_write_role and not write and self.connection.scalar(
            text("SELECT public.kpdesk_session_tenant_allowed(:org, 'write')"),
            {'org':self.organization_id},
        ) is not True:
            raise TenantAccessDenied('Organization membership denied.')
        if not universal_available(self.connection):
            raise UniversalUnavailable('universal_schema_required')
        return scope.identity

    def lock(self) -> AuthenticatedUser:
        self.authorize(write=True)
        self.connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
            {'scope':'kpdesk:universal:'+str(self.organization_id)})
        return self.authorize(write=True)

    def catalog(self, version_id: UUID | None=None) -> CatalogSnapshot:
        self.authorize()
        condition='AND id=:id' if version_id is not None else ''
        row=self.connection.execute(text(f"""SELECT id,payload,source_sha256 FROM public.universal_catalogs
            WHERE organization_id=:org {condition} ORDER BY created_at DESC,id DESC LIMIT 1"""),
            {'org':self.organization_id,'id':version_id}).mappings().one_or_none()
        if row is None:raise UniversalNotFound('catalog_not_found')
        return _parse_catalog(row['payload'],row['source_sha256'],row['id'])

    def publish(self, catalog: CatalogSnapshot, key: UUID) -> CatalogSnapshot:
        actor=self.lock()
        fingerprint=digest({'source_sha256':catalog.source_sha256})
        row=self.connection.execute(text("""SELECT id,request_sha256,source_sha256,payload,created_by FROM public.universal_catalogs
            WHERE organization_id=:org AND idempotency_key=:key"""),
            {'org':self.organization_id,'key':key}).mappings().one_or_none()
        if row is not None:
            if row['created_by']!=actor.user_id or row['request_sha256']!=fingerprint:
                raise UniversalConflict('idempotency_conflict')
            return _parse_catalog(row['payload'],row['source_sha256'],row['id'])
        self.connection.execute(text("""INSERT INTO public.universal_catalogs(
            organization_id,id,source_sha256,payload,idempotency_key,request_sha256,created_by)
            VALUES(:org,:id,:source,:payload,:key,:fingerprint,:actor)""").bindparams(bindparam('payload',type_=JSONB)),
            {'org':self.organization_id,'id':catalog.version_id,'source':catalog.source_sha256,
             'payload':catalog.model_dump(mode='json'),'key':key,'fingerprint':fingerprint,'actor':actor.user_id})
        return catalog

    def quote(self, quote_id: UUID, revision: int | None=None, *,
              _catalogs: dict[UUID,CatalogSnapshot] | None=None) -> QuoteSnapshot:
        self.authorize()
        condition='AND revision=:revision' if revision is not None else ''
        row=self.connection.execute(text(f"""SELECT quote_id,revision,catalog_version_id,payload,snapshot_sha256 FROM public.universal_quote_revisions
            WHERE organization_id=:org AND quote_id=:id {condition} ORDER BY revision DESC LIMIT 1"""),
            {'org':self.organization_id,'id':quote_id,'revision':revision}).mappings().one_or_none()
        if row is None:raise UniversalNotFound('quote_not_found')
        quote=_parse_quote(row['payload'],row['snapshot_sha256'],row['quote_id'],
                            row['revision'],row['catalog_version_id'])
        catalogs={} if _catalogs is None else _catalogs
        try:
            if quote.catalog_version_id not in catalogs:
                catalogs[quote.catalog_version_id]=self.catalog(quote.catalog_version_id)
            quote.verify_catalog(catalogs[quote.catalog_version_id])
        except (ValueError,UniversalNotFound) as error:
            raise RuntimeError('universal_quote_integrity') from error
        return quote

    def list_quotes(self, limit: int=50) -> list[QuoteSnapshot]:
        self.authorize()
        rows: list[UUID]=list(self.connection.execute(text("""SELECT quote_id FROM (
            SELECT DISTINCT ON(quote_id) quote_id,created_at FROM public.universal_quote_revisions
            WHERE organization_id=:org ORDER BY quote_id,revision DESC) heads
            ORDER BY created_at DESC,quote_id DESC LIMIT :limit"""),{'org':self.organization_id,'limit':limit}).scalars())
        catalogs: dict[UUID,CatalogSnapshot]={}
        return [self.quote(quote_id,_catalogs=catalogs) for quote_id in rows]

    def quote_replay(self, key: UUID, fingerprint: str) -> QuoteSnapshot | None:
        actor=self.authorize(write=True)
        row=self.connection.execute(text("""SELECT quote_id,revision,request_sha256,created_by FROM public.universal_quote_revisions
            WHERE organization_id=:org AND idempotency_key=:key"""),
            {'org':self.organization_id,'key':key}).mappings().one_or_none()
        if row is None:return None
        if row['created_by']!=actor.user_id or row['request_sha256']!=fingerprint:
            raise UniversalConflict('idempotency_conflict')
        return self.quote(row['quote_id'],row['revision'])

    def save_quote(self, quote: QuoteSnapshot, key: UUID, fingerprint: str,
                   expected_revision: int | None=None, source_snapshot: str | None=None) -> QuoteSnapshot:
        actor=self.lock()
        replay=self.quote_replay(key,fingerprint)
        if replay is not None:return replay
        if expected_revision is not None:
            previous=self.quote(quote.quote_id)
            if previous.revision!=expected_revision or previous.snapshot_sha256!=source_snapshot:
                raise UniversalConflict('stale_revision')
        if not quote.ready_for_draft or quote.total_rub is None or quote.approved:
            raise ValueError('universal_quote_requires_verified_selection')
        quote.verify_catalog(self.catalog(quote.catalog_version_id))
        if digest(quote.model_dump(mode='json',exclude={'snapshot_sha256'}))!=quote.snapshot_sha256:
            raise ValueError('universal_quote_digest_mismatch')
        self.connection.execute(text("""INSERT INTO public.universal_quote_revisions(
            organization_id,quote_id,revision,catalog_version_id,payload,snapshot_sha256,
            idempotency_key,request_sha256,created_by)
            VALUES(:org,:quote,:revision,:catalog,:payload,:snapshot,:key,:fingerprint,:actor)""").bindparams(bindparam('payload',type_=JSONB)),
            {'org':self.organization_id,'quote':quote.quote_id,'revision':quote.revision,
             'catalog':quote.catalog_version_id,'payload':quote.model_dump(mode='json'),
             'snapshot':quote.snapshot_sha256,'key':key,'fingerprint':fingerprint,'actor':actor.user_id})
        return quote

    def feedback_replay(self, key: UUID, fingerprint: str) -> dict[str,Any] | None:
        actor=self.authorize(write=True)
        previous=self.connection.execute(text("""SELECT id,quote_id,revision,payload,request_sha256,withdrawn_at,created_by FROM public.universal_feedback
            WHERE organization_id=:org AND idempotency_key=:key"""),
            {'org':self.organization_id,'key':key}).mappings().one_or_none()
        if previous is not None:
            if previous['created_by']!=actor.user_id or previous['request_sha256']!=fingerprint:
                raise UniversalConflict('idempotency_conflict')
            if previous['withdrawn_at'] is not None:raise UniversalConflict('feedback_withdrawn')
            quote=self.quote(previous['quote_id'],previous['revision'])
            return _parse_feedback(previous['payload'],previous['id'],previous['created_by'],
                previous['request_sha256'],quote,self.catalog(quote.catalog_version_id))
        return None

    def save_feedback(self, quote: QuoteSnapshot, payload: dict[str,Any], key: UUID,
                      fingerprint: str) -> dict[str,Any]:
        actor=self.lock()
        previous=self.feedback_replay(key,fingerprint)
        if previous is not None:return previous
        current=self.quote(quote.quote_id)
        if current.snapshot_sha256!=quote.snapshot_sha256:raise UniversalConflict('stale_revision')
        result={**payload,'id':str(uuid4()),'quote_id':str(quote.quote_id),'revision':quote.revision,
            'catalog_version_id':str(quote.catalog_version_id),'source_sha256':quote.source_sha256,
            'source_request':quote.source_request.model_dump(mode='json'),'reviewer_id':str(actor.user_id),
            'curation_status':'pending_review','training_activated':False}
        result=_parse_feedback(result,UUID(result['id']),actor.user_id,fingerprint,current,
            self.catalog(current.catalog_version_id))
        self.connection.execute(text("""INSERT INTO public.universal_feedback(
            organization_id,id,quote_id,revision,payload,idempotency_key,request_sha256,created_by)
            VALUES(:org,:id,:quote,:revision,:payload,:key,:fingerprint,:actor)""").bindparams(bindparam('payload',type_=JSONB)),
            {'org':self.organization_id,'id':UUID(result['id']),'quote':quote.quote_id,'revision':quote.revision,
             'payload':result,'key':key,'fingerprint':fingerprint,'actor':actor.user_id})
        return result

    def list_feedback(self, quote_id: UUID, offset: int=0) -> list[dict[str,Any]]:
        self.authorize(require_write_role=True)
        self.quote(quote_id)
        rows=self.connection.execute(text("""SELECT id,quote_id,revision,payload,request_sha256,created_by FROM public.universal_feedback
            WHERE organization_id=:org AND quote_id=:quote AND withdrawn_at IS NULL
            ORDER BY created_at,id LIMIT 101 OFFSET :offset"""),
            {'org':self.organization_id,'quote':quote_id,'offset':offset}).mappings().all()
        return self._feedback_items(rows)

    def export_feedback(self, offset: int=0) -> list[dict[str,Any]]:
        self.authorize(require_write_role=True)
        rows=self.connection.execute(text("""SELECT id,quote_id,revision,payload,request_sha256,created_by FROM public.universal_feedback
            WHERE organization_id=:org AND withdrawn_at IS NULL ORDER BY created_at,id LIMIT 101 OFFSET :offset"""),
            {'org':self.organization_id,'offset':offset}).mappings().all()
        return self._feedback_items(rows)

    def _feedback_items(self, rows: Sequence[RowMapping]) -> list[dict[str,Any]]:
        quotes: dict[tuple[UUID,int],QuoteSnapshot]={}
        catalogs: dict[UUID,CatalogSnapshot]={}
        items: list[dict[str,Any]]=[]
        for row in rows:
            identity=(row['quote_id'],row['revision'])
            if identity not in quotes:
                quotes[identity]=self.quote(*identity,_catalogs=catalogs)
            quote=quotes[identity]
            if quote.catalog_version_id not in catalogs:
                catalogs[quote.catalog_version_id]=self.catalog(quote.catalog_version_id)
            items.append(_parse_feedback(row['payload'],row['id'],row['created_by'],
                row['request_sha256'],quote,catalogs[quote.catalog_version_id]))
        return items

    def withdraw_feedback(self, feedback_id: UUID) -> None:
        self.lock()
        exists=self.connection.scalar(text('SELECT id FROM public.universal_feedback WHERE organization_id=:org AND id=:id'),
            {'org':self.organization_id,'id':feedback_id})
        if exists is None:raise UniversalNotFound('feedback_not_found')
        self.connection.execute(text("""UPDATE public.universal_feedback SET withdrawn_at=clock_timestamp()
            WHERE organization_id=:org AND id=:id AND withdrawn_at IS NULL"""),
            {'org':self.organization_id,'id':feedback_id})
