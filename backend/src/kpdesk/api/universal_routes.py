"""Separate universal workflow; server sessions, RLS and existing provider budgets."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Cookie, Depends, File, Header, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError, llm_error_message
from kpdesk.api.provider_scope import BudgetFactory, get_provider_budget_factory, operation_budget
from kpdesk.api.routes import get_groq_client, get_groq_settings
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    _required_session,
    get_unreleased_catalog_engine,
)
from kpdesk.catalog.schema import ImportLimits
from kpdesk.company.requisites import PartyDetails
from kpdesk.db.async_inference import InferenceReservations
from kpdesk.db.auth_sessions import AuthSessionRepository, SessionDenied
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.llm_budget import InferenceQuotaExceeded, finish_inference, reserve_inference
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.db.universal_schema import universal_available
from kpdesk.llm.client import LLMError, llm_operation
from kpdesk.llm.settings import LLMSettings
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.quotes.narrative import generate_proposal_narrative
from kpdesk.universal.document import narrative_facts, render_pdf
from kpdesk.universal.extraction import extract_request
from kpdesk.universal.feedback import FeedbackBody, FeedbackDecision
from kpdesk.universal.importer import parse_universal_price
from kpdesk.universal.matching import MatchPreview, build_quote, match_request
from kpdesk.universal.schema import (
    CatalogSnapshot,
    QuoteContext,
    QuoteSnapshot,
    Selection,
    UniversalRequest,
)
from kpdesk.universal.storage import (
    UniversalConflict,
    UniversalNotFound,
    UniversalStore,
    UniversalUnavailable,
    digest,
)

__all__ = ['FeedbackBody', 'FeedbackDecision', 'router']

router=APIRouter(prefix='/api/session/organizations/{organization_id}/universal')
EngineDep=Annotated[Engine,Depends(get_unreleased_catalog_engine)]
CookieDep=Annotated[str | None,Cookie(alias=SESSION_COOKIE)]
CsrfDep=Annotated[str | None,Header(alias='X-CSRF-Token')]
KeyDep=Annotated[str | None,Header(alias='Idempotency-Key')]


@contextmanager
def _errors() -> Iterator[None]:
    try:yield
    except ApiError:raise
    except SessionDenied as error:raise ApiError(401,'invalid_session','Требуется вход в систему.') from error
    except TenantAccessDenied as error:raise ApiError(403,'organization_forbidden','Доступ запрещён.') from error
    except UniversalUnavailable as error:raise ApiError(503,'universal_schema_required','Раздел подготовки КП пока недоступен.') from error
    except UniversalNotFound as error:raise ApiError(404,'universal_not_found','Прайс или предложение не найдено.') from error
    except UniversalConflict as error:raise ApiError(409,str(error),'Данные изменились либо эту операцию уже выполняли. Проверьте результат перед повторной попыткой.') from error
    except InferenceQuotaExceeded as error:raise ApiError(403 if error.reason=='forbidden' else 429,'inference_quota_exceeded','Проверьте права и лимит обращений.') from error
    except LLMError as error:
        status=(403 if error.code=='organization_forbidden' else
                503 if error.code=='quota_ledger_unavailable' else
                422 if error.code in {'unverified_narrative','review_context_too_large','correction_context_too_large'}
                else 429 if error.code in {'rate_limited','inference_quota_exceeded','duplicate_inference_in_progress'}
                else 504 if error.code=='provider_timeout' else 502)
        raise ApiError(status,error.code,llm_error_message(error.code)) from error
    except ValueError as error:raise ApiError(422,'universal_invalid_data','Проверьте заявку, единицы измерения и выбранные товары. Данные не прошли проверку.') from error
    except (RuntimeError,SQLAlchemyError) as error:raise ApiError(503,'tenant_store_unavailable','Не удалось получить или сохранить данные KPDesk. Проверьте результат операции перед повторной попыткой.') from error


@contextmanager
def _store(engine: Engine, org: UUID, cookie: str | None, csrf: str | None) -> Iterator[UniversalStore]:
    with _errors(),engine.begin() as connection:
        _required_session(AuthSessionRepository(connection),cookie)
        yield UniversalStore(connection,org,cookie or '',csrf)


def _key(key: str | None) -> UUID:
    if key is None:raise ApiError(422,'invalid_idempotency_key','Не удалось определить запрос. Проверьте результат операции перед повторной попыткой.')
    try:return UUID(key)
    except ValueError as error:raise ApiError(422,'invalid_idempotency_key','Не удалось определить запрос. Проверьте результат операции перед повторной попыткой.') from error


class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')


class MatchBody(Body):
    catalog_version_id: UUID
    request: UniversalRequest


class ExtractBody(Body):
    catalog_version_id: UUID
    text: str=Field(min_length=1,max_length=12000)
    consent_to_external_processing: StrictBool


class QuoteBody(MatchBody):
    selections: list[Selection]=Field(min_length=1,max_length=20)
    issuer_name: str=Field(min_length=1,max_length=200)
    recipient_name: str | None=Field(default=None,max_length=2000)
    manager_brief: str | None=Field(default=None,max_length=2000)
    commercial_terms: str | None=Field(default=None,max_length=2000)
    issuer_details: PartyDetails | None=Field(default=None,exclude_if=lambda value:value is None)
    recipient_details: PartyDetails | None=Field(default=None,exclude_if=lambda value:value is None)
    issuer_contacts: str | None=Field(default=None,max_length=600,exclude_if=lambda value:value is None)

    def context(self, narrative: ProposalNarrative | None=None) -> QuoteContext:
        return QuoteContext(issuer_name=self.issuer_name,recipient_name=self.recipient_name or None,
            manager_brief=self.manager_brief or None,commercial_terms=self.commercial_terms or None,narrative=narrative,
            issuer_details=self.issuer_details,recipient_details=self.recipient_details,
            issuer_contacts=self.issuer_contacts or None)


class Binding(Body):
    expected_revision: int=Field(ge=1,le=2147483646,strict=True)
    source_snapshot_sha256: str=Field(pattern=r'^[a-f0-9]{64}$')


class RevisionBody(QuoteBody,Binding):
    narrative: ProposalNarrative | None=None
    reviewed: StrictBool=False


class PresentationBody(Binding):
    consent_to_external_processing: StrictBool


def _bind(quote: QuoteSnapshot, body: Binding | FeedbackBody) -> None:
    if quote.revision!=body.expected_revision or quote.snapshot_sha256!=body.source_snapshot_sha256:
        raise UniversalConflict('stale_revision')


@router.get('/capabilities')
def capabilities(organization_id: UUID,engine: EngineDep,cookie: CookieDep=None) -> dict[str,Any]:
    with _errors(),engine.begin() as connection:
        _required_session(AuthSessionRepository(connection),cookie)
        bind_authenticated_tenant(connection,session_token=cookie or '',organization_id=organization_id,operation='read')
        available=universal_available(connection)
    return {'available':available,'reason':None if available else 'universal_schema_required','strategy':'lexical_universal_v1'}


@router.post('/catalog/import',response_model=CatalogSnapshot,status_code=201)
async def import_catalog(organization_id: UUID,engine: EngineDep,file: Annotated[UploadFile,File()],
                         cookie: CookieDep=None,csrf: CsrfDep=None,key: KeyDep=None) -> CatalogSnapshot:
    operation=_key(key)
    with _store(engine,organization_id,cookie,csrf) as store:store.authorize(write=True)
    limits=ImportLimits()
    raw=await file.read(limits.max_upload_bytes+1)
    with _errors():catalog=parse_universal_price(file.filename or '',raw,limits)
    with _store(engine,organization_id,cookie,csrf) as store:return store.publish(catalog,operation)


@router.get('/catalog/current',response_model=CatalogSnapshot)
def current_catalog(organization_id: UUID,engine: EngineDep,cookie: CookieDep=None) -> CatalogSnapshot:
    with _store(engine,organization_id,cookie,None) as store:return store.catalog()


@router.get('/catalog/{version_id}',response_model=CatalogSnapshot)
def get_catalog(organization_id: UUID,version_id: UUID,engine: EngineDep,cookie: CookieDep=None) -> CatalogSnapshot:
    with _store(engine,organization_id,cookie,None) as store:return store.catalog(version_id)


@router.post('/matches/preview')
def matches(organization_id: UUID,body: MatchBody,engine: EngineDep,cookie: CookieDep=None,csrf: CsrfDep=None) -> MatchPreview:
    with _store(engine,organization_id,cookie,csrf) as store:
        store.authorize(write=True)
        return match_request(body.request,store.catalog(body.catalog_version_id))


@router.post('/quotes',response_model=QuoteSnapshot,status_code=201)
def create_quote(organization_id: UUID,body: QuoteBody,engine: EngineDep,cookie: CookieDep=None,
                 csrf: CsrfDep=None,key: KeyDep=None) -> QuoteSnapshot:
    operation=_key(key); fingerprint=digest({'create':body.model_dump(mode='json')})
    with _store(engine,organization_id,cookie,csrf) as store:
        store.lock()
        replay=store.quote_replay(operation,fingerprint)
        if replay is not None:return replay
        quote=build_quote(store.catalog(body.catalog_version_id),body.request,body.selections,body.context(),uuid4(),1)
        return store.save_quote(quote,operation,fingerprint)


@router.get('/quotes')
def list_quotes(organization_id: UUID,engine: EngineDep,cookie: CookieDep=None) -> dict[str,list[QuoteSnapshot]]:
    with _store(engine,organization_id,cookie,None) as store:return {'items':store.list_quotes()}


@router.get('/quotes/{quote_id}',response_model=QuoteSnapshot)
def get_quote(organization_id: UUID,quote_id: UUID,engine: EngineDep,cookie: CookieDep=None,
              revision: Annotated[int | None,Query(ge=1)]=None) -> QuoteSnapshot:
    with _store(engine,organization_id,cookie,None) as store:return store.quote(quote_id,revision)


@router.post('/quotes/{quote_id}/revisions',response_model=QuoteSnapshot,status_code=201)
def revise_quote(organization_id: UUID,quote_id: UUID,body: RevisionBody,engine: EngineDep,
                 cookie: CookieDep=None,csrf: CsrfDep=None,key: KeyDep=None) -> QuoteSnapshot:
    operation=_key(key)
    if body.narrative is not None and not body.reviewed:
        raise ApiError(422,'document_review_required','Подтвердите проверку текста предложения.')
    fingerprint=digest({'quote':str(quote_id),'revision':body.model_dump(mode='json')})
    with _store(engine,organization_id,cookie,csrf) as store:
        store.lock()
        replay=store.quote_replay(operation,fingerprint)
        if replay is not None:return replay
        previous=store.quote(quote_id); _bind(previous,body)
        quote=build_quote(store.catalog(body.catalog_version_id),body.request,body.selections,
            body.context(body.narrative),quote_id,previous.revision+1)
        return store.save_quote(quote,operation,fingerprint,body.expected_revision,body.source_snapshot_sha256)


@router.get('/quotes/{quote_id}/pdf')
def quote_pdf(organization_id: UUID,quote_id: UUID,engine: EngineDep,cookie: CookieDep=None,
              revision: Annotated[int | None,Query(ge=1)]=None) -> Response:
    with _store(engine,organization_id,cookie,None) as store:quote=store.quote(quote_id,revision)
    with _errors():raw=render_pdf(quote)
    return Response(raw,media_type='application/pdf',headers={'Cache-Control':'no-store',
        'Content-Disposition':'inline; filename="proposal-draft.pdf"','X-Content-Type-Options':'nosniff'})


async def _provider_operation[T](engine: Engine,org: UUID,cookie: str | None,csrf: str | None,key: str | None,
    settings: LLMSettings | None,factory: BudgetFactory,purpose: str,payload: str,
    run: Callable[[Callable[[str,bool],Awaitable[None]]],Awaitable[T]]) -> T:
    if settings is None:raise ApiError(503,'provider_not_configured','OpenRouter не настроен.')
    def reserve(input_chars: int) -> UUID:
        return reserve_inference(engine,session_cookie=cookie,csrf=csrf,organization_id=org,
            model=settings.model,input_chars=input_chars)
    def complete(current: UUID,succeeded: bool) -> None:
        finish_inference(engine,request_id=current,session_cookie=cookie or '',succeeded=succeeded)
    reservations=InferenceReservations(reserve,complete)
    with _errors():
        async with reservations.scope(len(payload)):
            budget=operation_budget(factory,engine,organization_id=org,cookie=cookie,csrf=csrf,key=key,
                purpose=purpose,payload=payload,settings=settings)
            async with llm_operation(budget):result=await run(reservations.before_followup)
            return result


ProviderDep=Annotated[LLMSettings | None,Depends(get_groq_settings)]
ClientDep=Annotated[httpx.AsyncClient,Depends(get_groq_client)]
BudgetDep=Annotated[BudgetFactory,Depends(get_provider_budget_factory)]


@router.post('/requests/extract')
async def extract(organization_id: UUID,body: ExtractBody,engine: EngineDep,provider: ProviderDep,
                  client: ClientDep,budget_factory: BudgetDep,cookie: CookieDep=None,
                  csrf: CsrfDep=None,key: KeyDep=None) -> dict[str,Any]:
    if not body.consent_to_external_processing:
        raise ApiError(422,'external_processing_consent_required','Подтвердите передачу заявки в OpenRouter.')
    with _store(engine,organization_id,cookie,csrf) as store:
        store.authorize(write=True); catalog=store.catalog(body.catalog_version_id)
    if provider is None:raise ApiError(503,'provider_not_configured','OpenRouter не настроен.')
    async def run(_: Callable[[str,bool],Awaitable[None]]) -> UniversalRequest:
        return await extract_request(body.text,catalog,provider,client)
    request=await _provider_operation(engine,organization_id,cookie,csrf,key,provider,budget_factory,
        'universal-extraction:'+str(catalog.version_id),body.text,run)
    return {'request':request,'requires_human_review':True}


@router.post('/quotes/{quote_id}/presentation')
async def presentation(organization_id: UUID,quote_id: UUID,body: PresentationBody,engine: EngineDep,
    provider: ProviderDep,client: ClientDep,budget_factory: BudgetDep,cookie: CookieDep=None,
    csrf: CsrfDep=None,key: KeyDep=None) -> dict[str,Any]:
    if not body.consent_to_external_processing:
        raise ApiError(422,'external_processing_consent_required','Подтвердите передачу контекста в OpenRouter.')
    with _store(engine,organization_id,cookie,csrf) as store:
        store.authorize(write=True); quote=store.quote(quote_id); _bind(quote,body)
        facts=narrative_facts(quote)
    if provider is None:raise ApiError(503,'provider_not_configured','OpenRouter не настроен.')
    async def run(before: Callable[[str,bool],Awaitable[None]]) -> ProposalNarrative:
        return await generate_proposal_narrative(facts,provider,client,before_followup=before)
    narrative=await _provider_operation(engine,organization_id,cookie,csrf,key,provider,budget_factory,
        'universal-presentation:'+str(quote_id),facts,run)
    return {'quote_id':quote_id,'source_revision':quote.revision,'source_snapshot_sha256':quote.snapshot_sha256,
        'narrative':narrative,'requires_human_review':True}


@router.post('/quotes/{quote_id}/feedback',status_code=201)
def feedback(organization_id: UUID,quote_id: UUID,body: FeedbackBody,engine: EngineDep,
              cookie: CookieDep=None,csrf: CsrfDep=None,key: KeyDep=None) -> dict[str,Any]:
    operation=_key(key)
    if not body.reviewed or not body.training_consent:
        raise ApiError(422,'feedback_review_required','Подтвердите оценки и использование разметки для обучения.')
    fingerprint=digest({'quote':str(quote_id),'feedback':body.model_dump(mode='json')})
    with _store(engine,organization_id,cookie,csrf) as store:
        store.lock()
        replay=store.feedback_replay(operation,fingerprint)
        if replay is not None:return replay
        quote=store.quote(quote_id); _bind(quote,body)
        catalog=store.catalog(quote.catalog_version_id); skus={product.sku for product in catalog.products}
        if {decision.line_number for decision in body.decisions}!=set(range(1,len(quote.lines)+1)) or len(body.decisions)!=len(quote.lines):
            raise ValueError('feedback_line_mismatch')
        for decision in body.decisions:
            negatives=set(decision.negative_skus)
            if len(negatives)!=len(decision.negative_skus) or not negatives.issubset(skus) or decision.relevant_sku in negatives:
                raise ValueError('feedback_invalid_sku')
            if decision.relevant_sku is not None:
                if decision.relevant_sku not in skus:raise ValueError('feedback_invalid_sku')
                reviewed=build_quote(catalog,quote.source_request,[Selection(line_number=decision.line_number,sku=decision.relevant_sku)],
                    QuoteContext(issuer_name=quote.issuer_name),uuid4(),1)
                if reviewed.lines[decision.line_number-1].status!='manager_selected':
                    raise ValueError('feedback_positive_incompatible')
        payload=body.model_dump(mode='json')
        for decision,data in zip(body.decisions,payload['decisions'],strict=True):
            data['no_match_asserted']=(decision.relevant_sku is None and set(decision.negative_skus)==skus)
        return store.save_feedback(quote,payload,operation,fingerprint)


@router.get('/quotes/{quote_id}/feedback')
def list_quote_feedback(organization_id: UUID,quote_id: UUID,engine: EngineDep,cookie: CookieDep=None,
                         offset: Annotated[int,Query(ge=0,le=10000)]=0) -> dict[str,Any]:
    with _store(engine,organization_id,cookie,None) as store:items=store.list_feedback(quote_id,offset)
    return {'items':items[:100],'next_offset':offset+100 if len(items)>100 else None}


@router.get('/feedback/export')
def export_feedback(organization_id: UUID,engine: EngineDep,cookie: CookieDep=None,
                    offset: Annotated[int,Query(ge=0,le=10000)]=0) -> dict[str,Any]:
    with _store(engine,organization_id,cookie,None) as store:
        items=store.export_feedback(offset)
    return {'format':'kpdesk-universal-feedback-v1','training_activated':False,'items':items[:100],
        'limit':100,'next_offset':offset+100 if len(items)>100 else None,
        'note':'Разметка для отдельной проверки и обучения; действующая модель не изменена.'}


@router.delete('/feedback/{feedback_id}',status_code=204)
def withdraw_feedback(organization_id: UUID,feedback_id: UUID,engine: EngineDep,
                       cookie: CookieDep=None,csrf: CsrfDep=None) -> Response:
    with _store(engine,organization_id,cookie,csrf) as store:store.withdraw_feedback(feedback_id)
    return Response(status_code=204)
