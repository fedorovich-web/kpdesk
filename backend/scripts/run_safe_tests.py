"""Explicit offline and reviewed PostgreSQL test profiles; never generic discovery."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
SQL_OPT_IN = "KP_DESK_RUN_ISOLATED_LEGACY_PG"
REVIEWED_SQL_FILES = (
    "test_catalog_db.py", "test_catalog_http_postgres.py", "test_quote_persistence.py",
    "test_tenant_models.py", "test_tenant_foundation_schema.py", "test_tenant_access.py",
)
# Keep the historical safe selection boundaries. Files migrate into the
# offline selection only after their dangerous setup has actually been removed.
HAZARDS = (
    "dotenv_values", "create_engine(", "subprocess.", "postgres_test_engine(",
    "psycopg.connect", "KP_DESK_RUN_", "KP_DESK_BROWSER_E2E",
)
DB_FILES = frozenset({
    "test_live_account_security.py", "test_tenant_supplier_precision_http.py", "test_live_tenant_api.py",
    "test_supplier_canonical_http.py", "test_tenant_candidate_governance.py", "test_tenant_supplier_unit_http.py",
    "test_catalog_preflight.py", "test_checkpoint_route_guard.py", "test_live_member_routes.py",
    "test_supplier_historical_http.py", "test_tenant_supplier_comparison_http.py",
})


class SelectionError(ValueError):
    """Unknown, unsafe, empty or incomplete test selection."""


def select_tests(backend: Path, profile: str, requested: list[str]) -> tuple[list[Path], list[str]]:
    files = {p.name: p for p in sorted((backend / "tests").glob("test_*.py"))}
    if profile == "offline":
        excluded = sorted(name for name, p in files.items()
                          if name in DB_FILES or any(word in p.read_text(encoding="utf-8") for word in HAZARDS))
        available = {name: p for name, p in files.items() if name not in excluded}
    elif profile == "postgres-legacy":
        missing = set(REVIEWED_SQL_FILES) - files.keys()
        if missing:
            raise SelectionError("Reviewed SQL profile is incomplete: " + ", ".join(sorted(missing)))
        available = {name: files[name] for name in REVIEWED_SQL_FILES}
        excluded = sorted(files.keys() - available.keys())
    else:
        raise SelectionError("Unknown safe test profile.")
    refused = set(requested) - available.keys()
    if refused:
        raise SelectionError("Test selection refused: " + ", ".join(sorted(refused)))
    selected = [p for name, p in sorted(available.items()) if not requested or name in requested]
    if not selected:
        raise SelectionError("No reviewed tests selected; pytest was not invoked.")
    return selected, excluded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("offline", "postgres-legacy"), default="offline")
    parser.add_argument("--junitxml", type=Path)
    parser.add_argument("files", nargs="*", help="Exact filenames within the selected profile")
    options = parser.parse_args(argv)
    try:
        selected, excluded = select_tests(BACKEND, options.profile, options.files)
    except SelectionError as error:
        print(str(error), file=sys.stderr)
        return 2
    args = ["-q", "--tb=short", "--rootdir=" + str(BACKEND)]
    if options.junitxml is not None:
        options.junitxml.parent.mkdir(parents=True, exist_ok=True)
        args.append("--junitxml=" + str(options.junitxml))
        options.junitxml.with_suffix(".selection.json").write_text(json.dumps({
            "profile": options.profile, "selected": [str(p) for p in selected],
            "excluded": excluded,
        }, indent=2), encoding="utf-8")
    args.extend(str(p) for p in selected)
    print(f"Safe profile {options.profile}: {len(selected)} files selected, {len(excluded)} excluded", flush=True)
    previous = os.environ.get(SQL_OPT_IN)
    os.environ[SQL_OPT_IN] = "1" if options.profile == "postgres-legacy" else ""
    try:
        return int(pytest.main(args))
    finally:
        if previous is None:
            os.environ.pop(SQL_OPT_IN, None)
        else:
            os.environ[SQL_OPT_IN] = previous


if __name__ == "__main__":
    raise SystemExit(main())
