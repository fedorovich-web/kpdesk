"""A97: real Alembic upgrade/downgrade rehearsal on a marked A45 clone.

Neither the production Alembic heads nor live database are changed. For this
isolated rehearsal only, a custom script directory includes proposals/0031.
"""
from __future__ import annotations

import importlib
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES

_PROPOSAL = importlib.import_module("migrations.versions.0031_catalog_taxonomy_v4")
_OLD = frozenset(_PROPOSAL.V3_CATEGORIES)
_NEW = frozenset(_PROPOSAL.V4_CATEGORIES)
_V3 = "0030_tenant_llm_cache"
_V4 = "0031_catalog_taxonomy_v4"


def _verified_clone_owner(connection: Connection, *, version: str) -> None:
    if (
        not connection.in_transaction()
        or not is_live_accounts_http_clone(connection)
        or connection.scalar(text("SELECT current_user")) != "kpdesk"
        or MigrationContext.configure(connection).get_current_revision() != version
    ):
        raise RuntimeError("A97 requires a provenance-marked A45 clone schema owner.")


def _categories(connection: Connection) -> frozenset[str]:
    definition: object = _PROPOSAL._existing_categories(connection)
    if not isinstance(definition, frozenset) or any(
        type(value) is not str for value in definition
    ):
        raise TypeError("A97 catalog category introspection returned invalid values.")
    return frozenset(str(value) for value in definition)


def _privileges(connection: Connection) -> tuple[bool, ...]:
    result = connection.execute(text("""
        SELECT has_table_privilege('kpdesk_tenant_runtime',
                   'public.catalog_items','SELECT'),
               has_table_privilege('kpdesk_tenant_runtime',
                   'public.catalog_items','INSERT'),
               has_table_privilege('kpdesk_auth_runtime',
                   'public.catalog_items','SELECT'),
               has_table_privilege('kpdesk_app',
                   'public.catalog_items','INSERT'),
               (SELECT relrowsecurity FROM pg_class
                WHERE oid='public.catalog_items'::regclass),
               (SELECT relforcerowsecurity FROM pg_class
                WHERE oid='public.catalog_items'::regclass)
    """)).one()
    if any(type(value) is not bool for value in result):
        raise RuntimeError("A97 PostgreSQL ACL/RLS introspection was incomplete.")
    return tuple(bool(value) for value in result)


def _isolated_alembic_config(connection: Connection) -> Config:
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    # Use the actual promoted release migration, not the proposal copy.
    # env.py explicitly rejects unmarked injected connections.
    config.attributes["connection"] = connection
    return config


def rehearse_catalog_category_migration(owner: Engine) -> tuple[str, ...]:
    """Real Alembic commands: rollback, upgrade/downgrade, upgrade."""
    if (
        _NEW != set(CATEGORY_ATTRIBUTES)
        or len(_OLD) != 34 or len(_NEW) != 40
        or not _OLD < _NEW
    ):
        raise RuntimeError("A97 Python taxonomy no longer matches frozen migration.")
    additions = tuple(sorted(_NEW - _OLD))
    if additions != _PROPOSAL.NEW_CATEGORIES:
        raise RuntimeError("A97 extra categories do not match frozen migration.")
    with owner.begin() as connection:
        _verified_clone_owner(connection, version=_V3)
        assert _categories(connection) == _OLD
        old_acl = _privileges(connection)
        config = _isolated_alembic_config(connection)

        # SQL migration AND Alembic version stamp rollback together.
        with connection.begin_nested() as rollback:
            command.upgrade(config, _V4)
            _verified_clone_owner(connection, version=_V4)
            assert _categories(connection) == _NEW
            assert _privileges(connection) == old_acl
            rollback.rollback()
        _verified_clone_owner(connection, version=_V3)
        assert _categories(connection) == _OLD
        assert _privileges(connection) == old_acl

        command.upgrade(config, _V4)
        _verified_clone_owner(connection, version=_V4)
        assert _categories(connection) == _NEW
        command.downgrade(config, _V3)
        _verified_clone_owner(connection, version=_V3)
        assert _categories(connection) == _OLD
        command.upgrade(config, _V4)
        _verified_clone_owner(connection, version=_V4)
        assert _categories(connection) == _NEW
        assert _privileges(connection) == old_acl
    with owner.connect() as connection:
        if not is_live_accounts_http_clone(connection):
            raise RuntimeError("A97 lost clone provenance after commit.")
        assert MigrationContext.configure(connection).get_current_revision() == _V4
        assert _categories(connection) == _NEW
    return additions


def assert_downgrade_denied_with_history(owner: Engine) -> None:
    """Historical new-category SKUs prevent a real Alembic downgrade."""
    with owner.begin() as connection:
        _verified_clone_owner(connection, version=_V4)
        assert _categories(connection) == _NEW
        count = connection.scalar(text("""
            SELECT count(*) FROM public.catalog_items
            WHERE category = ANY(CAST(:new_categories AS text[]))
        """), {"new_categories": list(_PROPOSAL.NEW_CATEGORIES)})
        assert isinstance(count, int) and count > 0
        config = _isolated_alembic_config(connection)
        with connection.begin_nested() as guard:
            try:
                command.downgrade(config, _V3)
            except RuntimeError as error:
                assert "contains post-v3 categories" in str(error)
            else:
                raise AssertionError("A97 downgraded a populated V4 catalog.")
            _verified_clone_owner(connection, version=_V4)
            assert _categories(connection) == _NEW
            guard.rollback()
        assert connection.scalar(text("""
            SELECT count(*) FROM public.catalog_items
            WHERE category = ANY(CAST(:new_categories AS text[]))
        """), {"new_categories": list(_PROPOSAL.NEW_CATEGORIES)}) == count
