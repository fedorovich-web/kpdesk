"""Explicit fresh-instance installer. Never called by application startup."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

TARGET = '0047_auth_reset_serialization'
VERIFIED_REVISIONS = {TARGET: 48, '0048_lava_billing': 56, '0049_yandex_id': 58, '0050_billing_grace': 59}
ACCOUNTS = {'kpdesk_auth_service': 'kpdesk_auth_runtime',
            'kpdesk_tenant_service': 'kpdesk_tenant_runtime'}
OPERATIONS = Path('/app/operations')


def admission(database: str, owner: str, revision: str | None, marker: str | None,
              tables: int, roles: int, nonce: str) -> str:
    if not re.fullmatch(r'[a-f0-9]{24}', nonce) or (database, owner) != ('kpdesk', 'kpdesk'):
        raise ValueError('production_target_identity_mismatch')
    if (revision in VERIFIED_REVISIONS and marker == 'kpdesk:production:' + nonce
            and tables == VERIFIED_REVISIONS[revision] and roles == 5):
        return 'verify'
    if revision is None and marker is None and tables == roles == 0:
        return 'install'
    raise ValueError('production_install_refuses_existing_or_unknown_database')


def check_secrets(owner: str, services: dict[str, str]) -> None:
    if set(services) != set(ACCOUNTS):
        raise ValueError('production_service_secret_keys_invalid')
    values = [owner, *services.values()]
    if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_-]{48,128}', v) for v in values):
        raise ValueError('production_secret_format_invalid')
    if len(set(values)) != 3:
        raise ValueError('production_secrets_must_be_distinct')


def migration_plan(migrations: Path, manifest_file: Path) -> tuple[list[str], list[str]]:
    manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
    # ScriptDirectory imports every version module, including the historical
    # branch. Verify the complete source inventory before importing any of it.
    sources = list((migrations/'versions').rglob('*.py'))
    if any(path.is_symlink() for path in sources):
        raise ValueError('migration_bytes_mismatch')
    actual = {path.relative_to(migrations/'versions').as_posix():
              hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    if actual != manifest['source_files']:
        raise ValueError('migration_bytes_mismatch')
    cfg = Config()
    cfg.set_main_option('script_location', str(migrations))
    directory = ScriptDirectory.from_config(cfg)
    steps = list(reversed(list(directory.iterate_revisions(TARGET, 'base'))))
    names = [item.revision for item in steps]
    if len(steps) != 40 or names != manifest['revisions']:
        raise ValueError('production_migration_lineage_mismatch')
    hashes = {Path(item.path).name: hashlib.sha256(Path(item.path).read_bytes()).hexdigest() for item in steps}
    if hashes != manifest['files']:
        raise ValueError('migration_bytes_mismatch')
    return names, manifest['approvals']


def proxy_addresses(hostname: str, *, resolver: Any = socket.getaddrinfo) -> str:
    if hostname != 'dokploy-traefik':
        raise ValueError('production_proxy_hostname_not_allowed')
    addresses = {item[4][0] for item in resolver(hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM)}
    if not addresses:
        raise ValueError('production_proxy_has_no_addresses')
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
            raise ValueError('production_proxy_address_not_private_peer')
    return ','.join(sorted(addresses))


def verify_database(connection: Connection) -> str:
    revision = connection.scalar(text('SELECT public.kpdesk_runtime_schema_revision()'))
    if revision not in VERIFIED_REVISIONS:
        raise ValueError('production_runtime_revision_mismatch')
    definition = connection.scalar(text("SELECT pg_get_functiondef('public.kpdesk_auth_credential(text,text)'::regprocedure)"))
    if not isinstance(definition, str) or 'FOR UPDATE OF u' not in definition:
        raise ValueError('production_credential_serialization_missing')
    roles = connection.execute(text("""SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,
        rolcreaterole,rolbypassrls,rolreplication,rolinherit FROM pg_roles
        WHERE rolname IN ('kpdesk_app','kpdesk_auth_runtime','kpdesk_tenant_runtime',
                         'kpdesk_auth_service','kpdesk_tenant_service') ORDER BY rolname""")).all()
    if len(roles) != 5:
        raise ValueError('production_role_inventory_mismatch')
    for row in roles:
        if tuple(row[1:]) != (row[0] in ACCOUNTS, False, False, False, False, False, False):
            raise ValueError('production_role_privileges_unsafe')
    if revision in ('0048_lava_billing', '0049_yandex_id', '0050_billing_grace'):
        from kpdesk.billing.schema import billing_available

        if not billing_available(connection):
            raise ValueError('production_billing_schema_incomplete')
    if revision in ('0049_yandex_id', '0050_billing_grace'):
        from kpdesk.yandex.schema import yandex_available

        if not yandex_available(connection):
            raise ValueError('production_yandex_schema_incomplete')
    if revision == '0050_billing_grace':
        from kpdesk.billing.grace import grace_available
        if not grace_available(connection):
            raise ValueError('production_billing_grace_schema_incomplete')
    return str(revision)


def install() -> dict[str, object]:
    owner_password = Path('/run/owner-secret/db_owner_password').read_text(encoding='utf-8').strip()
    services = json.loads(Path('/run/service-secret/service_credentials').read_text(encoding='utf-8'))
    check_secrets(owner_password, services)
    nonce = os.environ['KP_DESK_INSTALL_NONCE']
    host = os.environ.get('KP_DESK_DB_HOST', 'db')
    if host != 'db':
        raise ValueError('production_installer_requires_project_db_service')
    steps, approvals = migration_plan(OPERATIONS / 'migrations', OPERATIONS / 'migrations.sha256.json')
    engine = create_engine(URL.create('postgresql+psycopg', username='kpdesk', password=owner_password,
        host=host, port=5432, database='kpdesk'), poolclass=NullPool, hide_parameters=True,
        connect_args={'connect_timeout': 5})
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL lock_timeout='10s'")
            connection.exec_driver_sql("SET LOCAL statement_timeout='120s'")
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('kpdesk:production-install'))")
            if connection.scalar(text('SHOW server_version_num'))[:2] != '17':
                raise ValueError('production_requires_postgresql17')
            database, owner = connection.execute(text('SELECT current_database(),current_user')).one()
            if not isinstance(database, str) or not isinstance(owner, str):
                raise TypeError('production_target_identity_mismatch')
            marker = connection.scalar(text("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"))
            tables = connection.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'"))
            roles = connection.scalar(text("SELECT count(*) FROM pg_roles WHERE rolname IN ('kpdesk_app','kpdesk_auth_runtime','kpdesk_tenant_runtime','kpdesk_auth_service','kpdesk_tenant_service')"))
            revision = None
            if connection.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")):
                rows: list[str] = list(connection.execute(text('SELECT version_num FROM public.alembic_version')).scalars())
                if len(rows) != 1:
                    raise ValueError('production_revision_inventory_invalid')
                revision = rows[0]
            mode = admission(database, owner, revision, marker, tables, roles, nonce)
            if mode == 'install':
                if connection.scalar(text("SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace")) or connection.scalar(text("SELECT count(*) FROM pg_proc WHERE pronamespace='public'::regnamespace")):
                    raise ValueError('production_public_schema_not_empty')
                connection.exec_driver_sql("COMMENT ON DATABASE kpdesk IS 'kpdesk:production:" + nonce + "'")
                connection.exec_driver_sql('CREATE ROLE kpdesk_app NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS')
                connection.exec_driver_sql('GRANT CONNECT ON DATABASE kpdesk TO kpdesk_app')
                connection.exec_driver_sql('GRANT USAGE ON SCHEMA public TO kpdesk_app')
                connection.exec_driver_sql('CREATE EXTENSION pgcrypto WITH SCHEMA public')
                cfg = Config()
                cfg.set_main_option('script_location', str(OPERATIONS / 'alembic'))
                cfg.set_main_option('version_locations', str(OPERATIONS / 'migrations/versions'))
                cfg.attributes.update({name: True for name in approvals})
                cfg.attributes['production_connection'] = connection
                cfg.attributes['production_nonce'] = nonce
                os.environ['KP_DESK_APPROVE_LOCAL_0031'] = '1'
                os.environ['KP_DESK_APPROVE_LOCAL_0032'] = '1'
                command.upgrade(cfg, '0005_tenant_expand')
                grants = {'catalog_versions': 'SELECT,INSERT', 'catalog_items': 'SELECT,INSERT',
                    'catalog_active': 'SELECT,UPDATE', 'idempotency_records': 'SELECT,INSERT',
                    'quote_documents': 'SELECT,INSERT,UPDATE', 'quote_revisions': 'SELECT,INSERT',
                    'quote_write_requests': 'SELECT,INSERT'}
                for table, access in grants.items():
                    connection.exec_driver_sql('GRANT ' + access + ' ON TABLE public.' + table + ' TO kpdesk_app')
                command.upgrade(cfg, TARGET)
                for login, role in ACCOUNTS.items():
                    # Identifiers fixed, secret ASCII-alnum format admitted above; never log SQL/errors.
                    connection.exec_driver_sql('CREATE ROLE ' + login +
                        " LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '" + services[login] + "'")
                    connection.exec_driver_sql('GRANT ' + role + ' TO ' + login)
            verified_revision = verify_database(connection)
        from kpdesk.db.live_service_engines import _restricted_engine
        from kpdesk.release_runtime import verify_release_schema

        for login, role in ACCOUNTS.items():
            restricted = _restricted_engine(login, role, services[login], 5432, host=host)
            try:
                with restricted.begin() as connection:
                    connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                    if tuple(connection.execute(text('SELECT session_user,current_user,current_database()')).one()) != (login, role, 'kpdesk'):
                        raise ValueError('production_restricted_identity_mismatch')
                    if connection.scalar(text("SELECT has_table_privilege(current_user,'public.users','SELECT')")) is not False:
                        raise ValueError('production_direct_users_access_forbidden')
                if role == 'kpdesk_tenant_runtime':
                    verify_release_schema(restricted, check_billing_activation=False,
                                          check_yandex_activation=False)
            finally:
                restricted.dispose()
        return {'status': 'passed', 'mode': mode, 'revision': verified_revision,
                'migration_count': len(steps) if mode == 'install' else 0, 'restricted_logins_verified': True}
    finally:
        engine.dispose()


if __name__ == '__main__':
    import sys

    if sys.argv[1:] != ['--new-instance-or-verify']:
        raise SystemExit('Explicit production provisioning mode required.')
    try:
        print(json.dumps(install()))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, SQLAlchemyError, CommandError) as error:
        # SQL exceptions can include passwords. Only the class / fixed admission codes leave the process.
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__,
                          'code': str(error) if type(error) is ValueError and re.fullmatch(r'(production_[a-z0-9_]+|migration_bytes_mismatch)', str(error)) else 'provisioning_failed'}))
        raise SystemExit(1) from None
