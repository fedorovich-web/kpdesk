"""Full release migration on a SECOND marked clone; source stays read-only."""
import json
import re
import secrets
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from postgres_clone_connection import drop_legacy_test_clone as drop_test_clone
from sqlalchemy import Engine, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.live_service_engines import _restricted_engine
from kpdesk.production import create_production_app
from kpdesk.release_runtime import verify_release_schema

ROOT=Path(__file__).resolve().parents[2]


def verify_release_runtime(admin: Engine, owner_factory: Callable[[str],Engine]) -> None:
    nonce=secrets.token_hex(6)
    name='kpdesk_accounts_http_'+nonce
    assert re.fullmatch(r'kpdesk_accounts_http_[0-9a-f]{12}',name)
    created=False
    engines: list[Engine]=[]
    source=owner_factory('kpdesk')
    try:
        with source.connect() as connection:
            assert connection.scalar(text('SELECT version_num FROM public.alembic_version'))=='0031_catalog_taxonomy_v4'
            assert connection.execute(text('''SELECT
                (SELECT count(*) FROM public.users),(SELECT count(*) FROM public.organizations),
                (SELECT count(*) FROM public.quote_documents),(SELECT count(*) FROM public.catalog_items)
            ''')).one()==(0,0,0,0)
    finally:
        source.dispose()
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 OWNER kpdesk')
            created=True
            connection.exec_driver_sql(f"COMMENT ON DATABASE \"{name}\" IS 'kpdesk:accounts-http:{nonce}'")
        result=subprocess.run(['docker','compose','--env-file','.env','ps','-q','db'],cwd=ROOT,
                              capture_output=True,text=True,check=True,timeout=15)
        container=result.stdout.strip()
        assert re.fullmatch(r'[a-f0-9]{12,64}',container)
        with subprocess.Popen(['docker','exec',container,'pg_dump','-U','kpdesk','-d','kpdesk','-Fc','--no-owner'],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE) as backup:
            assert backup.stdout is not None
            restored=subprocess.run(['docker','exec','-i',container,'pg_restore','-U','kpdesk','-d',name,
                                     '--exit-on-error','--no-owner'],stdin=backup.stdout,
                                    capture_output=True,timeout=120,check=False)
            backup.stdout.close()
            assert backup.wait(timeout=120)==0 and restored.returncode==0
        owner=owner_factory(name)
        engines.append(owner)
        config=Config(str(ROOT/'backend/alembic.ini'))
        config.attributes.update({
            'kpdesk_review_schema_0033_approved':True,'kpdesk_review_bridge_0034_approved':True,
            'kpdesk_supplier_discovery_0035_approved':True,'kpdesk_supplier_registry_0036_approved':True,
            'kpdesk_company_profile_0037_approved':True,'kpdesk_issuer_requisites_0038_approved':True,
            'kpdesk_company_logo_0039_approved':True,'kpdesk_training_feedback_0040_approved':True,
            'kpdesk_runtime_readiness_0041_approved':True,
        })
        with owner.begin() as connection:
            assert is_live_accounts_http_clone(connection)
            config.attributes['connection']=connection
            connection.execute(text('CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public'))
            command.upgrade(config,'0041_runtime_readiness')
        values=dotenv_values(ROOT/'.env')
        credentials=json.loads((ROOT/'credentials/kpdesk-runtime-db.json').read_text(encoding='utf-8'))
        auth=_restricted_engine('kpdesk_auth_service','kpdesk_auth_runtime',credentials['kpdesk_auth_service'],
                                int(values.get('KP_DESK_DB_PORT') or 55432),database=name)
        tenant=_restricted_engine('kpdesk_tenant_service','kpdesk_tenant_runtime',credentials['kpdesk_tenant_service'],
                                  int(values.get('KP_DESK_DB_PORT') or 55432),database=name)
        engines.extend((auth,tenant))
        with tenant.begin() as connection:
            assert connection.scalar(text("SELECT has_table_privilege(current_user,'public.alembic_version','SELECT')")) is False
            assert connection.scalar(text('SELECT public.kpdesk_runtime_schema_revision()'))=='0041_runtime_readiness'
        verify_release_schema(tenant)
        environment={'KP_DESK_PUBLIC_ORIGIN':'https://kpdesk.ru','KP_DESK_DB_HOST':'127.0.0.1',
                     'KP_DESK_DB_PORT':str(values.get('KP_DESK_DB_PORT') or 55432),
                     'KP_DESK_SERVICE_CREDENTIALS_FILE':str(ROOT/'credentials/kpdesk-runtime-db.json'),
                     'KP_DESK_FRONTEND_DIR':str(ROOT/'frontend/dist')}
        with patch.dict('os.environ',environment),patch('kpdesk.db.live_service_engines.live_service_engines',return_value=(auth,tenant)):
            app=create_production_app()
            with TestClient(app,base_url='https://kpdesk.ru') as client:
                assert client.get('/api/health/live').status_code==200
                assert client.get('/api/health/ready').status_code==200
                assert client.get('/').status_code==200
                assert client.get('/app').headers['cache-control']=='no-store'
                assert client.get('/api/catalog/current').status_code==404
                assert client.get('/api/session/me').status_code==401
                with owner.begin() as connection:
                    assert is_live_accounts_http_clone(connection)
                    config.attributes['connection']=connection
                    command.downgrade(config,'0040_training_feedback')
                assert client.get('/api/health/ready').status_code==503
                with owner.begin() as connection:
                    config.attributes['connection']=connection
                    command.upgrade(config,'0041_runtime_readiness')
                assert client.get('/api/health/ready').status_code==200
            from canonical_release_provider_journey import verify_provider_journey

            journey=verify_provider_journey(app,owner,auth,tenant,ROOT/'artifacts/local-release-20261102')
        output=ROOT/'artifacts/local-release-20261102'
        output.mkdir(exist_ok=True)
        report=output/f'clone-release-runtime-{nonce}.json'
        with report.open('x',encoding='utf-8') as evidence:
            json.dump({
            'source_schema':'0031_catalog_taxonomy_v4','release_schema':'0041_runtime_readiness',
            'actual_full_migrations':True,'runtime_direct_alembic_access':False,
            'production_factory_real_restricted_clone':True,'frontend_served':True,
            'readiness_detected_metadata_downgrade':True,'provider_calls':journey['provider_calls'],
            'journey':journey,
            'working_database_changed':False,
        },evidence,ensure_ascii=False,indent=2)
    finally:
        for engine in engines:
            engine.dispose()
        if created:
            drop_test_clone(admin, name)
