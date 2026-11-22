"""Offline release boundaries: no DB connection, provider calls or filesystem secrets."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from kpdesk.extraction.groq import GroqSettings
from kpdesk.production import create_production_app


def test_groq_runtime_environment_precedes_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path=tmp_path/'settings.env'
    path.write_text('OPENROUTER_API_KEY=file-key\nOPENROUTER_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free\n')
    monkeypatch.setenv('OPENROUTER_API_KEY','runtime-key')
    monkeypatch.setenv('OPENROUTER_MODEL','nvidia/nemotron-3-ultra-550b-a55b:free')
    settings=GroqSettings.from_local_env(path)
    assert settings.api_key=='runtime-key' and settings.model=='nvidia/nemotron-3-ultra-550b-a55b:free'


def test_explicitly_empty_runtime_groq_key_does_not_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path=tmp_path/'settings.env';path.write_text('OPENROUTER_API_KEY=file-key\n')
    monkeypatch.setenv('OPENROUTER_API_KEY','')
    with pytest.raises(ValueError,match='not configured'):
        GroqSettings.from_local_env(path)


def test_runtime_provider_model_allowlist_remains_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('OPENROUTER_API_KEY','runtime-key')
    monkeypatch.setenv('OPENROUTER_MODEL','untrusted-model')
    with pytest.raises(ValueError):
        GroqSettings.from_local_env(tmp_path/'absent.env')


def variables(frontend: Path | None = None) -> dict[str,str]:
    result={'KP_DESK_PUBLIC_ORIGIN':'https://kpdesk.ru','KP_DESK_DB_HOST':'db',
            'KP_DESK_DB_PORT':'5432','KP_DESK_SERVICE_CREDENTIALS_FILE':'/run/secrets/db.json'}
    if frontend:
        result['KP_DESK_FRONTEND_DIR']=str(frontend)
    return result


def engines(revision: str='0042_openrouter_budget') -> tuple[MagicMock,MagicMock]:
    auth,tenant=MagicMock(),MagicMock()
    tenant.begin.return_value.__enter__.return_value.execute.return_value.one.return_value=(
        'kpdesk_tenant_runtime',revision,True,True,True)
    tenant.begin.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value=True
    return auth,tenant


@pytest.mark.parametrize('revision',['0031_catalog_taxonomy_v4','0039_company_logo','unknown'])
def test_production_does_not_start_with_incomplete_schema(revision: str) -> None:
    with (patch.dict('os.environ',variables(),clear=True),patch(
        'kpdesk.db.live_service_engines.live_service_engines',return_value=engines(revision)),
        pytest.raises(RuntimeError,match='schema')):
        create_production_app()


def test_production_serves_built_frontend_without_spa_fallback_for_api(tmp_path: Path) -> None:
    for relative in ('index.html','app/index.html','auth/login/index.html','faq/index.html','404.html','assets/app.js'):
        target=tmp_path/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(relative)
    with (patch.dict('os.environ',variables(tmp_path),clear=True),patch(
        'kpdesk.db.live_service_engines.live_service_engines',return_value=engines()),
        TestClient(create_production_app(),base_url='https://kpdesk.ru') as client):
            for path,text in [('/','index.html'),('/faq','faq/index.html'),('/faq/','faq/index.html'),
                              ('/app/quotes/review','app/index.html'),('/auth/login','auth/login/index.html'),
                              ('/assets/app.js','assets/app.js')]:
                response=client.get(path);assert response.status_code==200 and response.text==text
            assert client.get('/missing').status_code==404
            assert client.get('/api/unrecognized').status_code==404
            assert client.get('/api/catalog/current').status_code==404
            assert client.get('/app/private').headers['cache-control']=='no-store'
            assert 'noindex' in client.get('/auth/login').headers['x-robots-tag']
            assert client.post('/faq',headers={'Origin':'https://kpdesk.ru'}).status_code==405


def test_frontend_cache_lifetimes_keep_private_data_uncached(tmp_path: Path) -> None:
    files = (
        'index.html', '404.html', 'app/index.html', 'auth/login/index.html',
        'proposal/index.html', 'features/index.html', 'admin/index.html',
        'assets/index-AbCd1234.js', 'assets/index-AbCd1234.css',
        'assets/hero/01_hero_main.avif', 'fonts/inter/inter-cyrillic.woff2',
        'robots.txt',
    )
    for name in files:
        file_path = tmp_path / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(name)
    with (patch.dict('os.environ', variables(tmp_path), clear=True),
          patch('kpdesk.db.live_service_engines.live_service_engines', return_value=engines()),
          TestClient(create_production_app(), base_url='https://kpdesk.ru') as client):
        immutable = 'public, max-age=31536000, immutable'
        static = 'public, max-age=2592000, stale-while-revalidate=86400'
        html = 'no-cache, max-age=0, must-revalidate'
        for path in ('/assets/index-AbCd1234.js', '/assets/index-AbCd1234.css'):
            assert client.get(path).headers['cache-control'] == immutable
        for path in ('/assets/hero/01_hero_main.avif', '/fonts/inter/inter-cyrillic.woff2'):
            assert client.get(path).headers['cache-control'] == static
        for path in ('/', '/features', '/features/'):
            assert client.get(path).headers['cache-control'] == html
        assert client.get('/robots.txt').headers['cache-control'] == 'public, max-age=3600'
        for path in ('/app', '/auth/login', '/proposal', '/proposal/index.html', '/admin', '/admin/'):
            response = client.get(path)
            assert response.headers['cache-control'] == 'no-store'
            assert 'noindex' in response.headers['x-robots-tag']
        assert client.get('/admin').text == 'admin/index.html'
        assert client.get('/admin/unrecognized').status_code == 404
        for path in ('/', '/features', '/assets/index-AbCd1234.js',
                     '/fonts/inter/inter-cyrillic.woff2', '/app'):
            head = client.head(path)
            get = client.get(path)
            assert head.status_code == 200
            assert head.headers['cache-control'] == get.headers['cache-control']
            assert head.content == b''
        unknown = client.get('/unknown-page')
        assert unknown.status_code == 404
        assert unknown.headers['cache-control'] == html


def test_precompressed_static_files_respect_accept_encoding_and_privacy(tmp_path: Path) -> None:
    import gzip

    for name in ('index.html', '404.html', 'app/index.html', 'assets/index-AbCd1234.js'):
        file_path = tmp_path / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text('body ' + name)
    source = tmp_path / 'assets/index-AbCd1234.js'
    encoded = gzip.compress(source.read_bytes())
    (tmp_path / 'assets/index-AbCd1234.js.gz').write_bytes(encoded)
    with (patch.dict('os.environ', variables(tmp_path), clear=True),
          patch('kpdesk.db.live_service_engines.live_service_engines', return_value=engines()),
          TestClient(create_production_app(), base_url='https://kpdesk.ru') as client):
        zipped = client.get('/assets/index-AbCd1234.js',
                            headers={'Accept-Encoding': 'gzip'})
        assert zipped.status_code == 200
        assert zipped.text == source.read_text()
        assert zipped.headers['content-encoding'] == 'gzip'
        assert 'Accept-Encoding' in zipped.headers['vary'].split(', ')
        assert zipped.headers['cache-control'].endswith('immutable')
        plain = client.get('/assets/index-AbCd1234.js',
                           headers={'Accept-Encoding': 'gzip;q=0, br;q=0'})
        assert plain.headers.get('content-encoding') is None
        assert 'Accept-Encoding' in plain.headers['vary'].split(', ')
        assert plain.text == zipped.text
        head = client.head('/assets/index-AbCd1234.js',
                           headers={'Accept-Encoding': 'gzip'})
        assert head.status_code == 200
        assert head.headers['content-encoding'] == 'gzip'
        assert int(head.headers['content-length']) == len(encoded)
        assert head.content == b''
        assert client.get('/assets/index-AbCd1234.js.gz').status_code == 404


def test_static_escape_or_symlink_cannot_read_outside_build(tmp_path: Path) -> None:
    root=tmp_path/'dist';root.mkdir();(root/'index.html').write_text('home');(root/'404.html').write_text('missing')
    (tmp_path/'secret.txt').write_text('private-sentinel')
    try:
        (root/'link.txt').symlink_to(tmp_path/'secret.txt')
    except OSError:
        pass  # Windows may require elevation for symlinks; traversal is still tested.
    with (patch.dict('os.environ',variables(root),clear=True),patch(
        'kpdesk.db.live_service_engines.live_service_engines',return_value=engines()),
        TestClient(create_production_app(),base_url='https://kpdesk.ru') as client):
            for path in ('/..%2fsecret.txt','/%2e%2e%2fsecret.txt','/link.txt','/.env'):
                response=client.get(path);assert response.status_code==404 and 'private-sentinel' not in response.text


def test_readiness_rechecks_schema_after_startup() -> None:
    pair=engines()
    connection=pair[1].begin.return_value.__enter__.return_value
    connection.scalar.return_value='kpdesk_tenant_runtime'
    with patch.dict('os.environ',variables(),clear=True),patch(
        'kpdesk.db.live_service_engines.live_service_engines',return_value=pair):
        app=create_production_app()
        with TestClient(app,base_url='https://kpdesk.ru') as client:
            assert client.get('/api/health/ready').status_code==200
            connection.execute.return_value.one.return_value=('kpdesk_tenant_runtime','0031_catalog_taxonomy_v4',False,False,False)
            response=client.get('/api/health/ready')
            assert response.status_code==503 and response.json()['code']=='tenant_schema_unavailable'


@pytest.mark.parametrize('ready', [True, False])
def test_0043_requires_share_gateway_and_force_rls(ready: bool) -> None:
    from kpdesk.release_runtime import verify_release_schema

    pair = engines('0043_proposal_shares')
    pair[1].begin.return_value.__enter__.return_value.scalar.return_value = ready
    if ready:
        verify_release_schema(pair[1])
    else:
        with pytest.raises(RuntimeError, match='schema'):
            verify_release_schema(pair[1])


@pytest.mark.parametrize('gate', ['sharing', 'universal', 'registry'])
def test_0047_preserves_all_previous_release_guards(gate: str) -> None:
    from kpdesk.release_runtime import verify_release_schema

    pair = engines('0047_auth_reset_serialization')
    conn = pair[1].begin.return_value.__enter__.return_value
    conn.scalar.return_value = True
    with (patch('kpdesk.quotes.sharing.sharing_available', return_value=gate != 'sharing'),
          patch('kpdesk.db.universal_schema.universal_available', return_value=gate != 'universal'),
          patch('kpdesk.db.supplier_registry_schema.registry_retired', return_value=gate != 'registry'),
          pytest.raises(RuntimeError, match='schema')):
        verify_release_schema(pair[1])


def test_0047_ready_with_prior_release_guards_intact() -> None:
    from kpdesk.release_runtime import verify_release_schema

    pair = engines('0047_auth_reset_serialization')
    pair[1].begin.return_value.__enter__.return_value.scalar.return_value = True
    with (patch('kpdesk.quotes.sharing.sharing_available', return_value=True),
          patch('kpdesk.db.universal_schema.universal_available', return_value=True),
          patch('kpdesk.db.supplier_registry_schema.registry_retired', return_value=True)):
        verify_release_schema(pair[1])


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('gate', ['sharing', 'universal', 'registry', 'billing'])
def test_0048_preserves_all_release_guards_even_when_billing_off(enabled: bool, gate: str) -> None:
    from kpdesk.release_runtime import verify_release_schema

    pair = engines('0048_lava_billing')
    pair[1].begin.return_value.__enter__.return_value.scalar.return_value = True
    with (patch('kpdesk.billing.settings.BillingSettings.from_env', return_value=SimpleNamespace(enabled=enabled)),
          patch('kpdesk.quotes.sharing.sharing_available', return_value=gate != 'sharing'),
          patch('kpdesk.db.universal_schema.universal_available', return_value=gate != 'universal'),
          patch('kpdesk.db.supplier_registry_schema.registry_retired', return_value=gate != 'registry'),
          patch('kpdesk.billing.schema.billing_available', return_value=gate != 'billing'),
          patch('kpdesk.billing.schema.billing_enabled', return_value=enabled),
          pytest.raises(RuntimeError, match='schema')):
        verify_release_schema(pair[1])


@pytest.mark.parametrize('enabled', [False, True])
def test_0048_ready_only_when_application_and_database_switches_match(enabled: bool) -> None:
    from kpdesk.release_runtime import verify_release_schema

    pair = engines('0048_lava_billing')
    connection = pair[1].begin.return_value.__enter__.return_value
    connection.scalar.return_value = True
    with (patch('kpdesk.billing.settings.BillingSettings.from_env', return_value=SimpleNamespace(enabled=enabled)),
          patch('kpdesk.quotes.sharing.sharing_available', return_value=True),
          patch('kpdesk.db.universal_schema.universal_available', return_value=True),
          patch('kpdesk.db.supplier_registry_schema.registry_retired', return_value=True),
          patch('kpdesk.billing.schema.billing_available', return_value=True),
          patch('kpdesk.billing.schema.billing_enabled', return_value=enabled)):
        verify_release_schema(pair[1])
    statements = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert statements[0] == 'SET TRANSACTION READ ONLY'
    assert not any(statement.lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ', 'CREATE ', 'ALTER ')) for statement in statements)


@pytest.mark.parametrize('enabled', [False, True])
def test_0048_application_database_switch_mismatch_fails_closed(enabled: bool) -> None:
    from kpdesk.release_runtime import verify_release_schema

    pair = engines('0048_lava_billing')
    pair[1].begin.return_value.__enter__.return_value.scalar.return_value = True
    with (patch('kpdesk.billing.settings.BillingSettings.from_env', return_value=SimpleNamespace(enabled=enabled)),
          patch('kpdesk.quotes.sharing.sharing_available', return_value=True),
          patch('kpdesk.db.universal_schema.universal_available', return_value=True),
          patch('kpdesk.db.supplier_registry_schema.registry_retired', return_value=True),
          patch('kpdesk.billing.schema.billing_available', return_value=True),
          patch('kpdesk.billing.schema.billing_enabled', return_value=not enabled),
          pytest.raises(RuntimeError, match='billing')):
        verify_release_schema(pair[1])


@pytest.mark.parametrize('revision', ['0042_openrouter_budget', '0043_proposal_shares', '0044_universal_products', '0045_registry_retirement', '0046_provider_authority', '0047_auth_reset_serialization'])
def test_enabled_billing_requires_0048_even_if_older_schema_is_complete(revision: str) -> None:
    from kpdesk.release_runtime import verify_release_schema

    pair = engines(revision)
    pair[1].begin.return_value.__enter__.return_value.scalar.return_value = True
    with (patch('kpdesk.billing.settings.BillingSettings.from_env', return_value=SimpleNamespace(enabled=True)),
          patch('kpdesk.quotes.sharing.sharing_available', return_value=True),
          patch('kpdesk.db.universal_schema.universal_available', return_value=True),
          patch('kpdesk.db.supplier_registry_schema.registry_retired', return_value=True),
          pytest.raises(RuntimeError, match='billing')):
        verify_release_schema(pair[1])


@pytest.mark.parametrize('revision', ['0042_openrouter_budget', '0043_proposal_shares', '0044_universal_products', '0045_registry_retirement', '0046_provider_authority', '0047_auth_reset_serialization'])
def test_disabled_billing_preserves_every_supported_previous_release(revision: str) -> None:
    from kpdesk.release_runtime import verify_release_schema

    pair = engines(revision)
    pair[1].begin.return_value.__enter__.return_value.scalar.return_value = True
    with (patch.dict('os.environ', {'KP_DESK_LAVA_ENABLED': 'false'}, clear=True),
          patch('kpdesk.quotes.sharing.sharing_available', return_value=True),
          patch('kpdesk.db.universal_schema.universal_available', return_value=True),
          patch('kpdesk.db.supplier_registry_schema.registry_retired', return_value=True),
          patch('kpdesk.billing.schema.billing_available') as available):
        verify_release_schema(pair[1])
    available.assert_not_called()


@pytest.mark.parametrize('flag', ['TRUE', '1', '', 'true'])
def test_invalid_billing_configuration_fails_before_database_work(flag: str) -> None:
    from kpdesk.release_runtime import verify_release_schema

    pair = engines()
    with (patch.dict('os.environ', {'KP_DESK_LAVA_ENABLED': flag}, clear=True),
          pytest.raises(RuntimeError, match='billing')):
        verify_release_schema(pair[1])
    pair[1].begin.assert_not_called()


def test_production_environment_passes_disabled_billing_and_server_only_settings() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / 'compose.production.yaml').read_text(encoding='utf-8')
    example = (root / '.env.example').read_text(encoding='utf-8')
    assert 'KP_DESK_LAVA_ENABLED: ${KP_DESK_LAVA_ENABLED:-false}' in compose
    assert 'KP_DESK_LAVA_ENABLED=false' in example
    names = ['KP_DESK_LAVA_API_KEY', 'KP_DESK_LAVA_WEBHOOK_SECRET']
    names += [f'KP_DESK_LAVA_{plan}_{field}_ID' for plan in ('START', 'TEAM', 'BUSINESS') for field in ('PRODUCT', 'OFFER')]
    for name in names:
        assert f'{name}: ${{{name}:-}}' in compose
        assert f'{name}=\n' in example


@pytest.mark.parametrize('slot',[0,2,3,4])
def test_startup_rejects_wrong_role_or_missing_release_objects(slot: int) -> None:
    pair=engines()
    result=['kpdesk_tenant_runtime','0042_openrouter_budget',True,True,True]
    result[slot]='kpdesk' if slot==0 else False
    pair[1].begin.return_value.__enter__.return_value.execute.return_value.one.return_value=tuple(result)
    with (patch.dict('os.environ',variables(),clear=True),patch(
        'kpdesk.db.live_service_engines.live_service_engines',return_value=pair),
        pytest.raises(RuntimeError,match='schema')):
        create_production_app()


def test_required_error_page_cannot_resolve_outside_frontend_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root=tmp_path/'dist';root.mkdir();(root/'index.html').write_text('home');(root/'404.html').write_text('missing')
    outside=tmp_path/'outside.html';outside.write_text('private')
    original=Path.resolve

    def resolve(path: Path, strict: bool=False) -> Path:
        return outside if path==root/'404.html' else original(path,strict=strict)

    monkeypatch.setattr(Path,'resolve',resolve)
    with (patch.dict('os.environ',variables(root),clear=True),patch(
        'kpdesk.db.live_service_engines.live_service_engines',return_value=engines()),
        pytest.raises(RuntimeError,match='Frontend')):
        create_production_app()


def test_operator_structural_check_does_not_require_application_provider_secrets() -> None:
    from kpdesk.release_runtime import verify_release_schema

    pair=engines('0048_lava_billing')
    pair[1].begin.return_value.__enter__.return_value.scalar.return_value=True
    with (patch.dict('os.environ',{},clear=True),
          patch('kpdesk.quotes.sharing.sharing_available',return_value=True),
          patch('kpdesk.db.universal_schema.universal_available',return_value=True),
          patch('kpdesk.db.supplier_registry_schema.registry_retired',return_value=True),
          patch('kpdesk.billing.schema.billing_available',return_value=True),
          patch('kpdesk.billing.schema.billing_enabled',return_value=True)):
        verify_release_schema(pair[1],check_billing_activation=False)
        with pytest.raises(RuntimeError,match='activation'):
            verify_release_schema(pair[1])
