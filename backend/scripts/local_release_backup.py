"""Fresh database-only backup, marked-clone restore and exact release rehearsal."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
from dataclasses import asdict

from alembic import command
from dotenv import dotenv_values
from sqlalchemy import Connection, Engine, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.live_service_engines import _restricted_engine
from kpdesk.release_runtime import verify_release_schema
from scripts.local_release_operator import (
    _SCHEMA,
    CLONE_NAME,
    ROOT,
    TARGET_REVISION,
    SourceState,
    migration_config,
    owner_engine,
    read_source,
    snapshot,
    validate_source,
)


def _require_clone(name: str) -> None:
    if CLONE_NAME.fullmatch(name) is None:
        raise RuntimeError('Refusing unknown operator clone name')


def drop_owned_clone(admin: Engine, name: str) -> None:
    _require_clone(name)
    with admin.connect() as connection:
        marker = connection.scalar(text(
            "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"
        ), {'name': name})
        if marker != 'kpdesk:accounts-http:' + name.rsplit('_', 1)[-1]:
            raise RuntimeError('Clone cleanup refuses a mismatching marker')
        connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')


def _container() -> str:
    result = subprocess.run(
        ['docker', 'compose', '--env-file', '.env', 'ps', '-q', 'db'],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=15,
    ).stdout.strip()
    if re.fullmatch(r'[a-f0-9]{12,64}', result) is None:
        raise RuntimeError('Local KPDesk database container is unavailable')
    data = json.loads(subprocess.run(
        ['docker', 'inspect', result], capture_output=True, text=True, check=True, timeout=15,
    ).stdout)[0]
    labels = data['Config']['Labels']
    port = str(int(dotenv_values(ROOT / '.env').get('KP_DESK_DB_PORT') or 55432))
    bindings = data['NetworkSettings']['Ports'].get('5432/tcp') or []
    if (labels.get('com.docker.compose.project') != 'kpdesk'
            or labels.get('com.docker.compose.service') != 'db'
            or not any(binding == {'HostIp': '127.0.0.1', 'HostPort': port}
                       for binding in bindings)):
        raise RuntimeError('Unexpected local database container identity/binding')
    return result


def verify_restricted_release(database: str) -> None:
    if database != 'kpdesk':
        _require_clone(database)
    settings = dotenv_values(ROOT / '.env')
    credentials = json.loads((ROOT / 'credentials/kpdesk-runtime-db.json').read_text(encoding='utf-8'))
    for login, role in (('kpdesk_auth_service', 'kpdesk_auth_runtime'),
                        ('kpdesk_tenant_service', 'kpdesk_tenant_runtime')):
        engine = _restricted_engine(login, role, credentials[login],
                                    int(settings.get('KP_DESK_DB_PORT') or 55432), database=database)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                actual = connection.execute(text(
                    'SELECT session_user,current_user,current_database()'
                )).one()
                if actual != (login, role, database):
                    raise RuntimeError('Restricted physical role identity mismatch')
                if role == 'kpdesk_auth_runtime':
                    verify_auth_contract(connection)
                if role == 'kpdesk_tenant_runtime' and connection.scalar(text(
                    "SELECT has_table_privilege(current_user,'public.alembic_version','SELECT')"
                )) is not False:
                    raise RuntimeError('Runtime direct metadata SELECT must remain forbidden')
            if role == 'kpdesk_tenant_runtime':
                verify_release_schema(engine)
        finally:
            engine.dispose()


def verify_auth_contract(connection: Connection) -> None:
    gateway, direct_table = connection.execute(text('''
        SELECT has_function_privilege(current_user,to_regprocedure('public.kpdesk_auth_credential(text,text)'),'EXECUTE'),
               has_table_privilege(current_user,'public.auth_sessions','SELECT')
    ''')).one()
    if gateway is not True or direct_table is not False:
        raise RuntimeError('Auth runtime gateway/table privilege contract is invalid')


def restore_clone_database_acl(owner: Engine, name: str, state: SourceState) -> None:
    """pg_dump without --create omits database-level ACL; replay only onto our clone."""
    _require_clone(name)
    with owner.begin() as connection:
        if not is_live_accounts_http_clone(connection):
            raise RuntimeError('Database ACL restore requires marked clone')
        quote = connection.dialect.identifier_preparer.quote
        target = quote(name)
        connection.exec_driver_sql(f'REVOKE ALL ON DATABASE {target} FROM PUBLIC')
        connection.exec_driver_sql(f'REVOKE ALL ON DATABASE {target} FROM kpdesk')
        for grantor, grantee, privilege, grantable in state.database_acl:
            if grantor != 'kpdesk' or privilege not in {'CREATE', 'CONNECT', 'TEMPORARY'}:
                raise RuntimeError('Unreviewed database ACL grant')
            receiver = 'PUBLIC' if grantee == 'PUBLIC' else quote(grantee)
            suffix = ' WITH GRANT OPTION' if grantable else ''
            connection.exec_driver_sql(f'GRANT {privilege} ON DATABASE {target} TO {receiver}{suffix}')


def rehearse_fresh_backup(state: SourceState) -> dict[str, object]:
    validate_source(state)
    if read_source() != state:
        raise RuntimeError('Local source changed before fresh backup')
    nonce = secrets.token_hex(6)
    name = 'kpdesk_accounts_http_' + nonce
    _require_clone(name)
    output = ROOT / 'artifacts/local-operator-20261102' / nonce
    output.mkdir(parents=True, exist_ok=False)
    backup_dir = ROOT / 'credentials/local-backups'
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f'kpdesk-operator-before-0041-{nonce}.dump'
    progress: dict[str, object] = {
        'status': 'started', 'phase': 'backup', 'source': asdict(state),
        'backup_relative_path': str(backup.relative_to(ROOT)), 'clone': name,
        'working_database_modified': False, 'provider_calls': 0,
    }

    def save() -> None:
        (output / 'rehearsal.json').write_text(json.dumps(progress, indent=2), encoding='utf-8')

    save()
    admin: Engine | None = None
    created = False
    try:
        container = _container()
        with backup.open('xb') as backup_output:
            subprocess.run(
                ['docker', 'exec', container, 'pg_dump', '-U', 'kpdesk', '-d', 'kpdesk',
                 '-Fc', '--no-owner', '--serializable-deferrable'],
                stdout=backup_output, stderr=subprocess.PIPE, check=True, timeout=120,
            )
        with backup.open('rb') as stream:
            progress['backup_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
        progress['backup_bytes'] = backup.stat().st_size
        progress['phase'] = 'restore'
        save()
        admin = owner_engine('postgres', admin=True)
        with admin.connect() as connection:
            if connection.execute(text('SELECT current_database(),current_user')).one() != ('postgres', 'kpdesk'):
                raise RuntimeError('Clone creation requires exact local admin identity')
            connection.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 OWNER kpdesk')
            created = True
            connection.exec_driver_sql(f"COMMENT ON DATABASE \"{name}\" IS 'kpdesk:accounts-http:{nonce}'")
        owner = owner_engine(name)
        try:
            with owner.connect() as connection:
                if not is_live_accounts_http_clone(connection):
                    raise RuntimeError('Restore target clone marker is missing')
            with backup.open('rb') as stream:
                subprocess.run(
                    ['docker', 'exec', '-i', container, 'pg_restore', '-U', 'kpdesk', '-d', name,
                     '--exit-on-error', '--no-owner'],
                    stdin=stream, capture_output=True, check=True, timeout=120,
                )
            restore_clone_database_acl(owner, name, state)
            with owner.connect() as connection:
                restored = snapshot(connection, database=name)
            progress['restore_comparison'] = {
                'counts_equal': restored.counts == state.counts,
                'data_equal': restored.data_sha256 == state.data_sha256,
                'contract_equal': restored.restored_contract_sha256 == state.restored_contract_sha256,
                'different_components': sorted(key for key in
                    state.component_sha256.keys() | restored.component_sha256.keys()
                    if state.component_sha256.get(key) != restored.component_sha256.get(key)),
            }
            if restored.schema_sha256 != state.schema_sha256:
                source_engine = owner_engine('kpdesk')
                try:
                    with source_engine.connect() as source_connection, owner.connect() as restored_connection:
                        original_constraints = {row[1]: row[2] for row in source_connection.execute(text(_SCHEMA))
                                                if row[0] == 'constraint'}
                        restored_constraints = {row[1]: row[2] for row in restored_connection.execute(text(_SCHEMA))
                                                if row[0] == 'constraint'}
                        progress['constraint_spelling_differences'] = [
                            {'object': key, 'source_definition': original_constraints.get(key),
                             'restored_definition': restored_constraints.get(key)}
                            for key in original_constraints.keys() | restored_constraints.keys()
                            if 'constraint:' + key in progress['restore_comparison']['different_components']  # type: ignore[index]
                        ]
                finally:
                    source_engine.dispose()
            if (restored.counts != state.counts or restored.data_sha256 != state.data_sha256
                    or restored.restored_contract_sha256 != state.restored_contract_sha256):
                source_engine = owner_engine('kpdesk')
                try:
                    with source_engine.connect() as source_connection, owner.connect() as restored_connection:
                        original_metadata = {(row[0], row[1]): row[2]
                                             for row in source_connection.execute(text(_SCHEMA))}
                        restored_metadata = {(row[0], row[1]): row[2]
                                             for row in restored_connection.execute(text(_SCHEMA))}
                        progress['contract_differences'] = [
                            {'object': key, 'source_definition': original_metadata.get(key),
                             'restored_definition': restored_metadata.get(key)}
                            for key in original_metadata.keys() | restored_metadata.keys()
                            if key[0] != 'constraint' and original_metadata.get(key) != restored_metadata.get(key)
                        ]
                finally:
                    source_engine.dispose()
                raise RuntimeError('Restored data/schema/ACL contract differs from source')
            progress['restore_with_acl_verified'] = True
            progress['restored_source'] = asdict(restored)
            progress['phase'] = 'clone_migrations'
            save()
            config = migration_config()
            with owner.begin() as connection:
                if not is_live_accounts_http_clone(connection):
                    raise RuntimeError('Migration target clone provenance mismatch')
                connection.exec_driver_sql("SET LOCAL lock_timeout='5s'")
                connection.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public')
                config.attributes['connection'] = connection
                command.upgrade(config, TARGET_REVISION)
        finally:
            owner.dispose()
        progress['phase'] = 'restricted_runtime_check'
        save()
        verify_restricted_release(name)
        progress['restricted_readiness_verified'] = True
        if read_source() != state:
            raise RuntimeError('Working source changed during clone rehearsal')
        progress['source_unchanged'] = True
        progress['status'] = 'clone_rehearsal_passed'
    except Exception as error:
        progress.update(status='failed', error_type=type(error).__name__)
        raise
    finally:
        try:
            if created and admin is not None:
                drop_owned_clone(admin, name)
                progress['clone_removed'] = True
        except Exception as error:
            progress.update(status='failed_cleanup', cleanup_error_type=type(error).__name__)
            raise
        finally:
            if admin is not None:
                admin.dispose()
            save()
    progress['evidence_relative_path'] = str((output / 'rehearsal.json').relative_to(ROOT))
    save()
    return progress
