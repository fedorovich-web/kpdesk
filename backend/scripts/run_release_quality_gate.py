"""Explicit full backend release gate: isolated PostgreSQL E2E; zero skipped.

Run from backend: python -m scripts.run_release_quality_gate --disposable-clones
A preflight refuses copying a nonempty production database to disposable clones.
This is NOT the quick developer pytest run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from scripts.audit_migration_lineage import audit_migration_lineage

_ROOT = Path(__file__).resolve().parents[2]
_OPT_IN_FLAGS = (
    "KP_DESK_RUN_A45_LIVE_ROLE_BROWSER",
    "KP_DESK_BROWSER_E2E",
    "KP_DESK_RUN_ISOLATED_MAPPING_PG",
    "KP_DESK_RUN_ISOLATED_PRECISION_PG",
    "KP_DESK_RUN_ISOLATED_UNIT_PG",
    "KP_DESK_RUN_ISOLATED_CANDIDATE_PG",
    "KP_DESK_RUN_ISOLATED_0033_PG",
)


class RejectSkipped:
    """Release acceptance must fail if any test is unexecuted."""

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is None:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            return
        skipped = len(reporter.stats.get("skipped", []))
        if skipped:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            print(f"RELEASE GATE FAILED: {skipped} skipped tests.", file=sys.stderr)


def _assert_safe_clone_source() -> None:
    # Abort before database connections if migration branches were merged or
    # an experimental revision entered the default production script graph.
    audit_migration_lineage()
    settings = dotenv_values(_ROOT / ".env")
    password = settings.get("KP_DESK_DB_PASSWORD")
    if not isinstance(password, str) or len(password) < 32:
        raise RuntimeError("Local PostgreSQL owner credentials not configured.")
    engine = create_engine(
        URL.create(
            "postgresql+psycopg", username="kpdesk", password=password,
            host="127.0.0.1", port=int(settings.get("KP_DESK_DB_PORT") or 55432),
            database="kpdesk",
        ),
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    try:
        with engine.connect() as connection:
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            if version != "0031_catalog_taxonomy_v4":
                raise RuntimeError("Release gate requires reviewed live schema 0031.")
            counts = connection.execute(text("""
                SELECT (SELECT count(*) FROM public.organizations),
                       (SELECT count(*) FROM public.users),
                       (SELECT count(*) FROM public.quote_documents),
                       (SELECT count(*) FROM public.tenant_llm_attempts)
            """)).one()
            if counts != (0, 0, 0, 0):
                raise RuntimeError(
                    "Live database has business records: refusing isolated clone tests."
                )
    finally:
        engine.dispose()


def main() -> int:
    if sys.argv[1:] != ["--disposable-clones"]:
        print(
            "Explicit opt-in required: --disposable-clones "
            "(may create/drop provenance-tagged temporary PostgreSQL databases)",
            file=sys.stderr,
        )
        return 2
    try:
        _assert_safe_clone_source()
    except (RuntimeError, ValueError) as error:
        print(f"Release preflight refused: {error}", file=sys.stderr)
        return 2
    for name in _OPT_IN_FLAGS:
        os.environ[name] = "1"
    os.chdir(_ROOT / "backend")
    return int(pytest.main(["-q", "-rs", "--tb=short"], plugins=[RejectSkipped()]))


if __name__ == "__main__":
    sys.exit(main())
