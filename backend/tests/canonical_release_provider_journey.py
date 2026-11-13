"""One bounded synthetic full API journey on the fresh release clone only."""
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from canonical_groq_diagnostics import rate_limit_diagnostics
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Engine, text

from kpdesk.api.routes import get_groq_client, get_groq_settings
from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.db.auth_sessions import AuthSessionRepository, hash_password
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.extraction.groq import GROQ_URL, GroqSettings
from kpdesk.quotes.pdf_brand import money

SOURCE='Для стойки приёма нужны 20 пачек офисной бумаги A4 плотностью 80 г/м² для распечатки документов. Подготовьте предложение.'


def verify_provider_journey(app: FastAPI, owner: Engine, auth: Engine, tenant: Engine, root: Path) -> dict[str,Any]:
    series=os.environ.get('KP_DESK_RELEASE_LIVE_GROQ_SERIES','')
    live=series in ('local-release-paper-20261102-v1','local-release-paper-consumption-fix-20261102-v2')
    root.mkdir(parents=True,exist_ok=True)
    name=('live-release-journey-consumption-fix-v2' if series=='local-release-paper-consumption-fix-20261102-v2'
          else 'live-release-journey' if live else 'mock-release-journey-'+uuid4().hex[:8])
    folder=root/name
    folder.mkdir(exist_ok=False)
    plan={'synthetic_source':SOURCE,'live_groq':live,'max_http_attempts':5,'network_retries':0,
          'extraction_calls_max':1,'narrative_chain_max':4,'reasoning':'low','completion_cap':3072,
          'client_data':False,'dadata_requests':0,'real_client_send':False}
    (folder/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    calls: list[dict[str,Any]]=[]
    provider=GroqSettings.from_local_env() if live else GroqSettings(api_key='release-offline-only')
    org,actor=uuid4(),uuid4()
    password='Synthetic-Release-Only-2026!'
    email=f'release-{actor.hex}@example.invalid'
    with owner.begin() as connection:
        assert is_live_accounts_http_clone(connection)
        connection.execute(text("INSERT INTO public.organizations(id,name,status) VALUES(:org,'Учебная организация release','active')"),{'org':org})
        connection.execute(text("INSERT INTO public.users(id,email,password_hash,status,email_verified_at) VALUES(:actor,:email,:password,'active',clock_timestamp())"),
                           {'actor':actor,'email':email,'password':hash_password(password)})
        connection.execute(text("INSERT INTO public.organization_memberships(organization_id,user_id,role,status) VALUES(:org,:actor,'owner','active')"),{'org':org,'actor':actor})
    with auth.begin() as connection:
        session=AuthSessionRepository(connection).login(email,password,peer='127.0.0.5')

    async def started(request: httpx.Request) -> None:
        if str(request.url)!=GROQ_URL or len(calls)>=5:
            raise RuntimeError('Release provider boundary/cap reached')
        body=json.loads(request.content)
        schema=body['response_format']['json_schema']['name']
        if body.get('reasoning_effort')!='low' or body.get('max_completion_tokens')!=3072:
            raise RuntimeError('Release provider reasoning/output cap changed')
        calls.append({'attempt':len(calls)+1,'schema':schema,'model':body['model'],'started':True,
                      'started_at_utc':datetime.now(UTC).isoformat()})
        (folder/f'call-{len(calls):02d}-started.json').write_text(json.dumps(calls[-1],indent=2),encoding='utf-8')

    async def completed(response: httpx.Response) -> None:
        await response.aread()
        try:
            payload=response.json()
        except ValueError:
            payload={}
        calls[-1].update({'http_status':response.status_code,
                         'usage':payload.get('usage') if isinstance(payload,dict) else None,
                         'completed_at_utc':datetime.now(UTC).isoformat()})
        diagnostics=rate_limit_diagnostics(response,payload)
        if diagnostics:
            calls[-1]['rate_limit_diagnostics']=diagnostics
        (folder/f'call-{len(calls):02d}-completed.json').write_text(json.dumps(calls[-1],indent=2),encoding='utf-8')

    def mocked(request: httpx.Request) -> httpx.Response:
        schema=json.loads(request.content)['response_format']['json_schema']['name']
        payload: dict[str,Any]
        narrative={'headline':'Бумага для документов на стойке приёма',
                   'opening':'Предлагаем офисную бумагу по вашей заявке для распечатки документов.',
                   'solution':'Выбрана бумага формата A4 плотностью 80 г/м². Параметры соответствуют указанным требованиям; состав и расчёт приведены в таблице.',
                   'closing':'Подтвердите состав заказа; наличие, доставку и оплату согласуем отдельно.'}
        if schema=='kpdesk_proposal_fact_review_v2':
            payload={name:{'status':'supported','issues':[]} for name in narrative}
        elif schema in ('kpdesk_commercial_proposal_v2','kpdesk_commercial_proposal_revision_v2'):
            payload=narrative
        else:
            payload={'lines':[{'source_quote':SOURCE,'description':'Офисная бумага A4 80 г/м²',
                'category':'paper','quantity':20,'unit':'пачка','brand':None,'paper_format':'A4',
                'paper_gsm':80,'pen_ink_color':None,'pen_ink_type':None,'attributes':[]}],'instruction_quotes':[]}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(payload,ensure_ascii=False)}}]})

    async def client_dependency() -> AsyncIterator[httpx.AsyncClient]:
        transport=httpx.AsyncHTTPTransport(retries=0) if live else httpx.MockTransport(mocked)
        async with httpx.AsyncClient(transport=transport,timeout=60,trust_env=False,
                                    event_hooks={'request':[started],'response':[completed]}) as client:
            yield client

    app.dependency_overrides[get_groq_settings]=lambda:provider
    app.dependency_overrides[get_groq_client]=client_dependency
    prefix=f'/api/session/organizations/{org}'
    headers={'Origin':'https://kpdesk.ru','X-CSRF-Token':session.csrf_token}
    try:
        with TestClient(app,base_url='https://kpdesk.ru') as client:
            client.cookies.set(SESSION_COOKIE,session.session_token)
            catalogue=(root.parents[1]/'frontend/public/templates/kpdesk-portfolio-synthetic-v1.csv').read_bytes()
            preflight=client.post(prefix+'/catalog/preflight',headers=headers,
                                  files={'file':('release-catalog.csv',catalogue,'text/csv')})
            assert preflight.status_code==200 and preflight.json()['publishable'],preflight.text
            published=client.post(prefix+'/catalog/import',headers={**headers,'Idempotency-Key':uuid4().hex,
                'X-Source-SHA256':preflight.json()['source_sha256']},files={'file':('release-catalog.csv',catalogue,'text/csv')})
            assert published.status_code==201,published.text
            extracted=client.post(prefix+'/requests/extract',headers=headers,
                json={'text':SOURCE,'consent_to_external_processing':True})
            (folder/'extraction-response.json').write_text(json.dumps(extracted.json(),ensure_ascii=False,indent=2),encoding='utf-8')
            assert extracted.status_code==200,extracted.text
            preview=extracted.json();request=preview['extracted_request']
            assert not preview['cache_hit'] and len(request['lines'])==1
            line=request['lines'][0]
            assert (line['category'],line['quantity'],line['unit'],line['paper_format'],line['paper_gsm'])==('paper',20,'пачка','A4',80)
            created=client.post(prefix+'/quotes',headers={**headers,'Idempotency-Key':uuid4().hex},json={
                'catalog_version_id':published.json()['version_id'],'extracted_request':request,
                'manager_selections':[{'line_number':1,'sku':'PAPER-A4-80'}],
                'review':{'extraction_id':preview['extraction_id'],'source_text':SOURCE,
                          'original_extracted_request':request,'reviewed_lines':[True]}})
            assert created.status_code==201,created.text
            quote=created.json();assert quote['total_rub']=='7980.00' and quote['ready_for_draft']
            base=prefix+'/quotes/'+quote['quote_id']
            scope={'expected_revision':quote['revision'],'source_snapshot_sha256':quote['snapshot_sha256']}
            context={'issuer_name':'Учебный поставщик','recipient_name':'Учебная стойка приёма',
                'manager_brief':'Предложите выбранную бумагу для распечатки документов на стойке приёма. Объясните соответствие заявке и предложите следующий шаг.',
                'commercial_terms':'Наличие, доставку и оплату согласуем отдельно.'}
            presentation=client.post(base+'/presentation',headers=headers,
                json={**scope,**context,'consent_to_external_processing':True})
            (folder/'presentation-response.json').write_text(json.dumps(presentation.json(),ensure_ascii=False,indent=2),encoding='utf-8')
            assert presentation.status_code==200,presentation.text
            document={**context,'issued_on':date(2026,11,2).isoformat(),'narrative':presentation.json()['narrative'],
                'appearance':{'template':'presentation','accent_color':'#007F5F','show_company_section':True,
                              'layout_version':2,'default_logo':'kpdesk_v1'}}
            pdf_preview=client.post(base+'/document-preview',headers=headers,json={**scope,'document':document})
            assert pdf_preview.status_code==200
            (folder/'preview.pdf').write_bytes(pdf_preview.content)
            approved=client.post(base+'/approve',headers={**headers,'Idempotency-Key':uuid4().hex},
                                 json={**scope,'document':document,'reviewed':True})
            assert approved.status_code==201,approved.text
            pdf=client.get(base+'/pdf');assert pdf.status_code==200
            (folder/'approved-synthetic.pdf').write_bytes(pdf.content)
            text_pdf='\n'.join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
            assert 'PAPER-A4-80' in text_pdf and money(Decimal('7980.00')).replace('\u00a0',' ') in text_pdf.replace('\u00a0',' ') and 'Не утверждено' not in text_pdf
            reopened=client.get(base).json()
            assert reopened['approved'] and reopened['document']['narrative']==document['narrative']
            (folder/'approved-snapshot.json').write_text(json.dumps(reopened,ensure_ascii=False,indent=2),encoding='utf-8')
        with owner.connect() as connection:
            ledger=connection.execute(text('SELECT status,count(*) FROM public.tenant_llm_attempts WHERE organization_id=:org GROUP BY status'),{'org':org}).all()
        outcome={'technical_journey_passed':True,'live_groq':live,'evidence_folder':folder.name,
            'provider_calls':len(calls) if live else 0,'mock_http_attempts':0 if live else len(calls),
            'http_attempts':len(calls),'usage':[c.get('usage') for c in calls],'ledger':[list(row) for row in ledger],
            'synthetic_manager_approval':True,'independent_commercial_acceptance':False,'model_activated':False,
            'working_database_changed':False}
        (folder/'outcome.json').write_text(json.dumps(outcome,ensure_ascii=False,indent=2),encoding='utf-8')
        return outcome
    finally:
        (folder/'calls.json').write_text(json.dumps(calls,indent=2),encoding='utf-8')
        app.dependency_overrides.pop(get_groq_settings,None)
        app.dependency_overrides.pop(get_groq_client,None)
