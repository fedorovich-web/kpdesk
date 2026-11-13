"""Explicit local0031→0041 operator; default entrypoint is read-only."""
import argparse
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from scripts.audit_migration_lineage import audit_migration_lineage

SOURCE_REVISION = '0031_catalog_taxonomy_v4'
TARGET_REVISION = '0041_runtime_readiness'
APPLY_APPROVAL = 'kpdesk:0031-to-0041'
ROOT = Path(__file__).resolve().parents[2]
CLONE_NAME = re.compile(r'kpdesk_accounts_http_[0-9a-f]{12}\Z')
APPROVALS = (
    'kpdesk_review_schema_0033_approved', 'kpdesk_review_bridge_0034_approved',
    'kpdesk_supplier_discovery_0035_approved', 'kpdesk_supplier_registry_0036_approved',
    'kpdesk_company_profile_0037_approved', 'kpdesk_issuer_requisites_0038_approved',
    'kpdesk_company_logo_0039_approved', 'kpdesk_training_feedback_0040_approved',
    'kpdesk_runtime_readiness_0041_approved',
)
SOURCE_TABLES = frozenset({
    'account_action_limits', 'account_email_challenges', 'account_email_outbox',
    'alembic_version', 'auth_login_limits', 'auth_sessions', 'catalog_active',
    'catalog_items', 'catalog_versions', 'idempotency_records',
    'organization_email_invitations', 'organization_invitations',
    'organization_memberships', 'organizations', 'quote_documents', 'quote_revisions',
    'quote_write_requests', 'tenant_catalog_active', 'tenant_catalog_idempotency',
    'tenant_llm_attempts', 'tenant_llm_extraction_cache', 'tenant_quote_manager_decisions',
    'tenant_quote_reviews', 'tenant_quote_write_requests', 'tenant_request_candidate_adjudications',
    'tenant_request_candidate_reviews', 'tenant_request_source_restrictions',
    'tenant_supplier_mapping_profiles', 'tenant_supplier_precision_reviews',
    'tenant_supplier_unit_reviews', 'users',
})


@dataclass(frozen=True)
class SourceState:
    database: str
    role: str
    revision: str
    server_major: int
    counts: dict[str, int]
    inert_catalog_pointer: bool
    schema_sha256: str
    data_sha256: str
    restored_contract_sha256: str
    component_sha256: dict[str, str] = field(default_factory=dict)
    unexpected_schemas: tuple[str, ...] = ()
    object_owners: tuple[str, ...] = ('kpdesk',)
    database_acl: tuple[tuple[str, str, str, bool], ...] = ()


def validate_source(state: SourceState) -> None:
    if (state.database != 'kpdesk' or state.role != 'kpdesk'
            or state.revision != SOURCE_REVISION or state.server_major != 17
            or not state.inert_catalog_pointer or state.unexpected_schemas):
        raise RuntimeError('Unexpected local source identity/schema/pointer')
    if state.object_owners != ('kpdesk',):
        raise RuntimeError('Unsupported source object owner for no-owner restore')
    required = {'users', 'organizations', 'catalog_items', 'quote_documents',
                'alembic_version', 'catalog_active'}
    if not required.issubset(state.counts):
        raise RuntimeError('Incomplete local source table inventory')
    if (state.counts['alembic_version'] != 1 or state.counts['catalog_active'] != 1
            or any(count != 0 for name, count in state.counts.items()
                   if name not in {'alembic_version', 'catalog_active'})):
        raise RuntimeError('Local source application tables must be empty before backup')
    if set(state.counts) != SOURCE_TABLES:
        raise RuntimeError('Unreviewed local source table inventory')
    if any(grantor != 'kpdesk' or privilege not in {'CREATE', 'CONNECT', 'TEMPORARY'}
           for grantor, grantee, privilege, grantable in state.database_acl):
        raise RuntimeError('Unsupported source database grant owner/privilege')


def owner_engine(database: str, *, admin: bool = False) -> Engine:
    if database not in {'kpdesk', 'postgres'} and not CLONE_NAME.fullmatch(database):
        raise RuntimeError('Unrecognized local operator database')
    settings = dotenv_values(ROOT / '.env')
    password = settings.get('KP_DESK_DB_PASSWORD')
    if not isinstance(password, str) or len(password) < 32:
        raise RuntimeError('Local owner credential unavailable')
    return create_engine(
        URL.create('postgresql+psycopg', username='kpdesk', password=password,
                   host='127.0.0.1', port=int(settings.get('KP_DESK_DB_PORT') or 55432),
                   database=database),
        isolation_level='AUTOCOMMIT' if admin else 'READ COMMITTED',
        poolclass=NullPool, hide_parameters=True, connect_args={'connect_timeout': 4},
    )


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                    separators=(',', ':')).encode()).hexdigest()


_SQL_STRING = r"'(?:[^']|'')*'"
_ORIGINAL_ITEM = _SQL_STRING + r'::character varying'
_RESTORED_ITEM = r'\(' + _ORIGINAL_ITEM + r'\)::text'
_STRING_ARRAY = re.compile(
    r'\(ARRAY\[(?P<original>' + _ORIGINAL_ITEM + r'(?:, ' + _ORIGINAL_ITEM + r')*)\]\)::text\[\]'
    + r'|ARRAY\[(?P<restored>' + _RESTORED_ITEM + r'(?:, ' + _RESTORED_ITEM + r')*)\]'
)


def canonical_sql(definition: str) -> str:
    """Only PostgreSQL17's recorded varchar-array→text-array spelling change."""
    def replace_array(match: re.Match[str]) -> str:
        values = re.findall(_SQL_STRING, match.group('original') or match.group('restored'))
        return 'ARRAY[' + ', '.join(value + '::text' for value in values) + ']'

    return _STRING_ARRAY.sub(replace_array, definition)


# PostgreSQL17 flattens one nested AND after restoring these exact historical
# checks. This is a spelling allowlist, not a general rewrite of SQL logic.
_CONSTRAINT_SPELLINGS = {
    'tenant_supplier_precision_reviews.ck_precision_review_bounds': (
        'CHECK ((((length(TRIM(BOTH FROM supplier_id)) >= 1) AND (length(TRIM(BOTH FROM supplier_id)) <= 120)) AND ((length(TRIM(BOTH FROM rationale)) >= 32) AND (length(TRIM(BOTH FROM rationale)) <= 500))))',
        'CHECK (((length(TRIM(BOTH FROM supplier_id)) >= 1) AND (length(TRIM(BOTH FROM supplier_id)) <= 120) AND ((length(TRIM(BOTH FROM rationale)) >= 32) AND (length(TRIM(BOTH FROM rationale)) <= 500))))',
    ),
    'tenant_supplier_precision_reviews.ck_precision_review_source_am': (
        "CHECK ((((length((source_price_raw)::text) >= 1) AND (length((source_price_raw)::text) <= 128)) AND (source_price_exact > (0)::numeric) AND (source_price_exact < ('1000000000000'::bigint)::numeric) AND (kpdesk_precision_source_numeric((source_price_raw)::text) IS NOT NULL) AND (source_price_exact = kpdesk_precision_source_numeric((source_price_raw)::text))))",
        "CHECK (((length((source_price_raw)::text) >= 1) AND (length((source_price_raw)::text) <= 128) AND (source_price_exact > (0)::numeric) AND (source_price_exact < ('1000000000000'::bigint)::numeric) AND (kpdesk_precision_source_numeric((source_price_raw)::text) IS NOT NULL) AND (source_price_exact = kpdesk_precision_source_numeric((source_price_raw)::text))))",
    ),
}


def canonical_definition(kind: str, name: str, definition: str) -> str:
    spellings = _CONSTRAINT_SPELLINGS.get(name) if kind == 'constraint' else None
    if spellings is not None and definition in spellings:
        return spellings[1]
    return canonical_sql(definition)


def snapshot(connection: Connection, *, database: str) -> SourceState:
    """Read one consistent source snapshot; only fingerprints leave the connection."""
    connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    actual, role, raw_version = connection.execute(text(
        'SELECT current_database(),current_user,current_setting(\'server_version_num\')::int'
    )).one()
    version = int(str(raw_version))
    if actual != database or role != 'kpdesk':
        raise RuntimeError('Unexpected local source connection identity')
    if database != 'kpdesk':
        from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

        if not is_live_accounts_http_clone(connection):
            raise RuntimeError('Restore source requires exact clone provenance')
    namespace_values: list[object] = list(connection.execute(text(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT IN ('public','information_schema') "
        "AND left(nspname,3)<>'pg_' ORDER BY nspname"
    )).scalars())
    unexpected_schemas = tuple(str(name) for name in namespace_values)
    owner_values: list[object] = list(connection.execute(text('''
        SELECT owner FROM (
          SELECT pg_get_userbyid(relowner) owner FROM pg_class WHERE relnamespace='public'::regnamespace
          UNION SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE pronamespace='public'::regnamespace
          UNION SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()
        ) owners ORDER BY owner
    ''')).scalars())
    object_owners = tuple(str(owner) for owner in owner_values)
    if connection.scalar(text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='public'")) not in {'pg_database_owner', 'kpdesk'}:
        raise RuntimeError('Unsupported source public schema owner')
    database_acl = tuple((str(grantor), str(grantee), str(privilege), bool(grantable))
                         for grantor, grantee, privilege, grantable in connection.execute(text('''
        SELECT pg_get_userbyid(a.grantor), CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type,a.is_grantable
        FROM pg_database d CROSS JOIN LATERAL aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a
        WHERE d.datname=current_database() ORDER BY 1,2,3,4
    ''')))
    revision = str(connection.execute(text('SELECT version_num FROM public.alembic_version')).scalar_one())
    table_values: list[object] = list(connection.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    )).scalars().all())
    tables = [str(name) for name in table_values]
    counts = {name: int(connection.exec_driver_sql(
        'SELECT count(*) FROM public.' + connection.dialect.identifier_preparer.quote(name)
    ).scalar_one()) for name in tables}
    pointer = connection.scalar(text(
        'SELECT count(*)=1 AND bool_and(id=1 AND catalog_version_id IS NULL) FROM public.catalog_active'
    )) is True
    # Validate before reading/persisting any table data; unknown populated tables also stop.
    state = SourceState(database='kpdesk', role=role, revision=revision, server_major=version // 10000,
                        counts=counts, inert_catalog_pointer=pointer,
                        schema_sha256='', data_sha256='', restored_contract_sha256='',
                        unexpected_schemas=unexpected_schemas, object_owners=object_owners,
                        database_acl=database_acl)
    validate_source(state)
    rows: dict[str, object] = {name: connection.exec_driver_sql(
        'SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),\'[]\'::jsonb) FROM public.'
        + connection.dialect.identifier_preparer.quote(name) + ' t'
    ).scalar_one() for name in tables}
    sequence_values: list[object] = list(connection.execute(text(
        "SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='S' ORDER BY relname"
    )).scalars())
    for sequence in sequence_values:
        quoted = connection.dialect.identifier_preparer.quote(str(sequence))
        rows['sequence:' + str(sequence)] = tuple(connection.exec_driver_sql(
            'SELECT last_value,is_called FROM public.' + quoted
        ).one())
    metadata = [(str(kind), str(name), canonical_definition(str(kind), str(name), str(value)))
                for kind, name, value in connection.execute(text(_SCHEMA))]
    contract = metadata
    return SourceState(database=actual, role=role, revision=revision, server_major=version // 10000,
                       counts=counts, inert_catalog_pointer=pointer,
                       schema_sha256=_digest(metadata), data_sha256=_digest(rows),
                       restored_contract_sha256=_digest(contract),
                       component_sha256={f'{kind}:{name}': _digest(value)
                                         for kind, name, value in metadata},
                       unexpected_schemas=unexpected_schemas, object_owners=object_owners,
                       database_acl=database_acl)


_SCHEMA = '''
SELECT kind,name,definition FROM (
 SELECT 'table' AS kind,c.relname AS name,
   jsonb_build_array(pg_get_userbyid(c.relowner),c.relrowsecurity,c.relforcerowsecurity,
     (SELECT jsonb_agg(a::text ORDER BY a::text)
      FROM unnest(coalesce(c.relacl,acldefault('r',c.relowner))) a))::text AS definition
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relkind='r'
 UNION ALL
 SELECT 'column',table_name||'.'||column_name,
   jsonb_build_array(ordinal_position,udt_name,is_nullable,column_default)::text
 FROM information_schema.columns WHERE table_schema='public'
 UNION ALL
 SELECT 'function',p.oid::regprocedure::text,pg_get_functiondef(p.oid)||pg_get_userbyid(p.proowner)||coalesce(
   (SELECT jsonb_agg(a::text ORDER BY a::text)::text FROM unnest(coalesce(p.proacl,acldefault('f',p.proowner))) a),'')
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND p.prokind='f'
 UNION ALL
 SELECT 'constraint',conrelid::regclass::text||'.'||conname,pg_get_constraintdef(oid)
 FROM pg_constraint WHERE connamespace='public'::regnamespace
 UNION ALL
 SELECT 'index',indexname,indexdef FROM pg_indexes WHERE schemaname='public'
 UNION ALL
 SELECT 'policy',tablename||'.'||policyname,
   jsonb_build_array(permissive,roles,cmd,qual,with_check)::text
 FROM pg_policies WHERE schemaname='public'
 UNION ALL
 SELECT 'trigger',tgrelid::regclass::text||'.'||tgname,pg_get_triggerdef(t.oid)
 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
 WHERE c.relnamespace='public'::regnamespace AND NOT t.tgisinternal
 UNION ALL
 SELECT 'schema',nspname,jsonb_build_array(pg_get_userbyid(nspowner),
   (SELECT jsonb_agg(a::text ORDER BY a::text) FROM unnest(coalesce(nspacl,acldefault('n',nspowner))) a))::text
 FROM pg_namespace WHERE nspname='public'
 UNION ALL
 SELECT 'sequence',relname,jsonb_build_array(pg_get_userbyid(relowner),
   (SELECT jsonb_agg(a::text ORDER BY a::text) FROM unnest(coalesce(relacl,acldefault('s',relowner))) a))::text
 FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='S'
 UNION ALL
 SELECT 'database','local_database',jsonb_build_array(pg_get_userbyid(datdba),
   (SELECT jsonb_agg(a::text ORDER BY a::text) FROM unnest(coalesce(datacl,acldefault('d',datdba))) a))::text
 FROM pg_database WHERE datname=current_database()
 UNION ALL
 SELECT 'default_acl',pg_get_userbyid(defaclrole)||'.'||defaclnamespace::regnamespace::text||'.'||defaclobjtype::text,
   (SELECT jsonb_agg(a::text ORDER BY a::text)::text FROM unnest(defaclacl) a)
 FROM pg_default_acl
) metadata ORDER BY kind,name,definition
'''


def read_source() -> SourceState:
    audit_migration_lineage()
    engine = owner_engine('kpdesk')
    try:
        with engine.connect() as connection:
            state = snapshot(connection, database='kpdesk')
        validate_source(state)
        return state
    finally:
        engine.dispose()


def migration_config() -> Config:
    config = Config(str(ROOT / 'backend/alembic.ini'))
    config.attributes.update(dict.fromkeys(APPROVALS, True))
    return config


def rehearse(state: SourceState) -> dict[str, object]:
    from scripts.local_release_backup import rehearse_fresh_backup

    validate_source(state)
    return rehearse_fresh_backup(state)


def upgrade_working(state: SourceState) -> None:
    validate_source(state)
    if read_source() != state:
        raise RuntimeError('Local source changed before working upgrade')
    # Exact target is selected here; never carry clone injection into this path.
    config = migration_config()
    previous_name = os.environ.get('KP_DESK_DB_NAME')
    previous_approval = os.environ.get('KP_DESK_APPROVE_LOCAL_0032')
    try:
        os.environ['KP_DESK_DB_NAME'] = 'kpdesk'
        os.environ['KP_DESK_APPROVE_LOCAL_0032'] = '1'
        engine = owner_engine('kpdesk')
        try:
            with engine.begin() as connection:
                # DBA-managed prerequisite; may remain installed if later migrations fail.
                connection.exec_driver_sql("SET LOCAL lock_timeout='5s'")
                connection.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public')
        finally:
            engine.dispose()
        command.upgrade(config, TARGET_REVISION)
    finally:
        for name, previous in (('KP_DESK_DB_NAME', previous_name),
                               ('KP_DESK_APPROVE_LOCAL_0032', previous_approval)):
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous


def apply_local(state: SourceState, *, maintenance_confirmed: bool, approval: str) -> None:
    if not maintenance_confirmed or approval != APPLY_APPROVAL:
        raise RuntimeError('Explicit local target approval and maintenance confirmation required')
    validate_source(state)
    receipt = rehearse(state)
    if receipt.get('status') != 'clone_rehearsal_passed':
        raise RuntimeError('Successful fresh backup rehearsal required')
    if read_source() != state:
        raise RuntimeError('Local source changed after rehearsal')
    result: dict[str, object] = {'status': 'starting', 'phase': 'working_upgrade',
                                 'source': asdict(state), 'target_schema': TARGET_REVISION,
                                 'automatic_rollback': False, 'public_deployment': False}
    write_apply_evidence(receipt, result, create=True)
    try:
        upgrade_working(state)
        from scripts.local_release_backup import verify_restricted_release

        result['phase'] = 'restricted_verification'
        write_apply_evidence(receipt, result)
        verify_restricted_release('kpdesk')
        result.update(status='local_working_schema_upgraded', phase='finished')
    except BaseException as error:
        result.update(status='failed_requires_operator_review', error_type=type(error).__name__)
        raise
    finally:
        write_apply_evidence(receipt, result)
    print(json.dumps({'status': result['status'], 'schema': TARGET_REVISION,
                      'rehearsal_evidence': receipt['evidence_relative_path'], 'public_deployment': False}))


def write_apply_evidence(receipt: dict[str, object], result: dict[str, object], *, create: bool = False) -> None:
    evidence = (ROOT / str(receipt['evidence_relative_path'])).resolve()
    allowed = (ROOT / 'artifacts/local-operator-20261102').resolve()
    if (not evidence.is_relative_to(allowed) or evidence.name != 'rehearsal.json'
            or re.fullmatch('[0-9a-f]{12}', evidence.parent.name) is None):
        raise RuntimeError('Apply evidence requires a fresh operator rehearsal path')
    with (evidence.parent / 'working-apply.json').open('x' if create else 'w', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--rehearse', action='store_true')
    mode.add_argument('--apply-local', choices=[APPLY_APPROVAL])
    parser.add_argument('--maintenance-confirmed', action='store_true')
    args = parser.parse_args(argv)
    if bool(args.apply_local) != args.maintenance_confirmed:
        parser.error('--apply-local requires --maintenance-confirmed, which is apply-only')
    state = read_source()
    if args.apply_local:
        apply_local(state, maintenance_confirmed=True, approval=args.apply_local)
    elif args.rehearse:
        receipt = rehearse(state)
        print(json.dumps({key: receipt[key] for key in (
            'status', 'backup_relative_path', 'backup_sha256', 'backup_bytes',
            'restore_with_acl_verified', 'restricted_readiness_verified',
            'source_unchanged', 'clone_removed', 'evidence_relative_path',
        )}, indent=2))
    else:
        source_summary = {key: value for key, value in asdict(state).items() if key != 'component_sha256'}
        print(json.dumps({'mode': 'read_only_preflight', 'source': source_summary,
                          'target': TARGET_REVISION, 'working_database_modified': False}, indent=2))
    return 0


if __name__ == '__main__':
    # Import by its stable package name so helper/dataclass identity is shared.
    from scripts.local_release_operator import main as operator_main

    try:
        raise SystemExit(operator_main())
    except Exception as error:  # noqa: BLE001 -- redact all uncaught SQL/data errors at CLI boundary
        # SQL exception strings can contain data; only expose the exception class.
        print(json.dumps({'status': 'operator_failed_no_automatic_rollback',
                          'error_type': type(error).__name__}))
        raise SystemExit(1) from None
