"""Explicit 0047->0048 operator. Default is read-only; never enables billing."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from kpdesk.billing.schema import billing_available, billing_enabled
from kpdesk.db.live_service_engines import _restricted_engine
from kpdesk.release_runtime import verify_release_schema
from scripts.provision_production import (
    OPERATIONS,
    admission,
    check_secrets,
    migration_plan,
    verify_database,
)
from scripts.provision_production import (
    TARGET as SOURCE,
)

TARGET = '0048_lava_billing'


def verify_backup(path: Path, expected_sha256: str) -> str:
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or not re.fullmatch(r'[a-f0-9]{64}', expected_sha256)):
        raise ValueError('billing_upgrade_backup_invalid')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        prefix = stream.read(5)
        if prefix != b'PGDMP':
            raise ValueError('billing_upgrade_backup_invalid')
        digest.update(prefix)
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError('billing_upgrade_backup_invalid')
    return expected_sha256


def migration_config(config: Config) -> Config:
    migration_plan(OPERATIONS / 'migrations', OPERATIONS / 'migrations.sha256.json')
    config.set_main_option('script_location', str(OPERATIONS / 'alembic'))
    config.set_main_option('version_locations', str(OPERATIONS / 'migrations/versions'))
    revision = ScriptDirectory.from_config(config).get_revision(TARGET)
    if revision is None or revision.down_revision != SOURCE:
        raise ValueError('billing_upgrade_lineage_invalid')
    return config


def verify_target(connection: Connection, nonce: str) -> str:
    if not re.fullmatch(r'[a-f0-9]{24}', nonce):
        raise ValueError('billing_upgrade_identity_invalid')
    database, owner, version, database_owner = connection.execute(text('''
        SELECT current_database(),current_user,current_setting('server_version_num')::int,
          (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database())
    ''')).one()
    if (not isinstance(database, str) or not isinstance(owner, str)
            or not isinstance(version, int) or not 170000 <= version < 180000
            or database_owner != 'kpdesk'):
        raise ValueError('billing_upgrade_identity_invalid')
    marker = connection.scalar(text("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"))
    revisions: list[str] = list(connection.execute(text('SELECT version_num FROM public.alembic_version')).scalars())
    if len(revisions) != 1:
        raise ValueError('billing_upgrade_identity_invalid')
    tables = connection.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'"))
    roles = connection.scalar(text("SELECT count(*) FROM pg_roles WHERE rolname IN ('kpdesk_app','kpdesk_auth_runtime','kpdesk_tenant_runtime','kpdesk_auth_service','kpdesk_tenant_service')"))
    admission(database, owner, revisions[0], marker, tables, roles, nonce)
    if verify_database(connection) != revisions[0]:
        raise ValueError('billing_upgrade_identity_invalid')
    return str(revisions[0])


def apply_schema(connection: Connection, nonce: str, config: Config) -> None:
    if not connection.in_transaction():
        raise ValueError('billing_upgrade_transaction_required')
    connection.exec_driver_sql("SET LOCAL lock_timeout='5s'")
    connection.exec_driver_sql("SET LOCAL statement_timeout='30s'")
    connection.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('kpdesk:production-install'))")
    # Serialize with other operators and pin the revision until transaction end.
    connection.exec_driver_sql('LOCK TABLE public.alembic_version IN EXCLUSIVE MODE')
    if verify_target(connection, nonce) != SOURCE:
        raise ValueError('billing_upgrade_requires_0047')
    config.attributes.update(production_connection=connection, production_nonce=nonce,
        kpdesk_lava_billing_0048_approved=True,
        kpdesk_lava_billing_0048_working_database_approved=True)
    try:
        command.upgrade(config, TARGET)
        if (verify_target(connection, nonce) != TARGET or not billing_available(connection)
                or billing_enabled(connection)):
            raise ValueError('billing_upgrade_postcondition_failed')
    finally:
        for key in ('production_connection', 'production_nonce',
                    'kpdesk_lava_billing_0048_approved',
                    'kpdesk_lava_billing_0048_working_database_approved'):
            config.attributes.pop(key, None)


def run(*, apply: bool = False, maintenance: bool = False, backup_restored: bool = False,
        backup: Path | None = None, backup_sha256: str | None = None) -> dict[str, object]:
    if apply and not (maintenance and backup_restored and backup is not None and backup_sha256):
        raise ValueError('billing_upgrade_confirmation_required')
    verified_backup = verify_backup(backup, backup_sha256) if apply and backup is not None and backup_sha256 else None
    nonce = os.environ['KP_DESK_INSTALL_NONCE']
    if not re.fullmatch(r'[a-f0-9]{24}', nonce) or os.environ.get('KP_DESK_DB_HOST', 'db') != 'db':
        raise ValueError('billing_upgrade_identity_invalid')
    config = migration_config(Config())
    owner_password = Path('/run/owner-secret/db_owner_password').read_text(encoding='utf-8').strip()
    services = json.loads(Path('/run/service-secret/service_credentials').read_text(encoding='utf-8'))
    check_secrets(owner_password, services)
    engine = create_engine(URL.create('postgresql+psycopg', username='kpdesk', password=owner_password,
        host='db', port=5432, database='kpdesk'), poolclass=NullPool, hide_parameters=True,
        connect_args={'connect_timeout': 5})
    restricted = _restricted_engine('kpdesk_tenant_service', 'kpdesk_tenant_runtime',
                                    services['kpdesk_tenant_service'], 5432, host='db')
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql('SET TRANSACTION READ ONLY')
            connection.exec_driver_sql("SET LOCAL statement_timeout='30s'")
            revision = verify_target(connection, nonce)
        verify_release_schema(restricted, check_billing_activation=False,
                              check_yandex_activation=False)
        if not apply:
            return {'status': 'passed', 'mode': 'preflight', 'revision': revision, 'database_writes': 0}
        with engine.begin() as connection:
            apply_schema(connection, nonce, config)
        # A failed final physical-role check cannot roll back the committed schema.
        try:
            verify_release_schema(restricted, check_billing_activation=False,
                              check_yandex_activation=False)
        except (RuntimeError, SQLAlchemyError):
            return {'status': 'failed', 'code': 'billing_upgrade_postcommit_verification_failed',
                    'revision': TARGET, 'schema_committed': True, 'billing_enabled': False}
        return {'status': 'passed', 'mode': 'schema_upgrade', 'revision': TARGET,
                'schema_committed': True, 'billing_enabled': False, 'backup_sha256': verified_backup}
    finally:
        restricted.dispose()
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply-0048', action='store_true')
    parser.add_argument('--confirm-maintenance', action='store_true')
    parser.add_argument('--confirm-backup-restored', action='store_true')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--backup-sha256')
    args = parser.parse_args(argv)
    if args.apply_0048 and not (args.confirm_maintenance and args.confirm_backup_restored
                                and args.backup is not None and args.backup_sha256):
        parser.error('Apply requires maintenance and restored-backup confirmations, archive and SHA256.')
    if not args.apply_0048 and (args.confirm_maintenance or args.confirm_backup_restored
                               or args.backup is not None or args.backup_sha256 is not None):
        parser.error('Mutation options require --apply-0048.')
    try:
        result = run(apply=True, maintenance=True, backup_restored=True, backup=args.backup,
                     backup_sha256=args.backup_sha256) if args.apply_0048 else run(apply=False)
        print(json.dumps(result))
        return 0 if result['status'] == 'passed' else 1
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, SQLAlchemyError, CommandError):
        # Database/provider/OS exception details may contain secrets or private paths.
        print(json.dumps({'status': 'failed', 'code': 'billing_upgrade_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
