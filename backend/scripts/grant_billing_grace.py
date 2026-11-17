"""Owner-only existing-organization grace admission; never creates fake invoices.

Preflight by default. An explicit, tested production backup and a sole existing
organization are required for grant mutation; no user-provided organization ID.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from kpdesk.billing.grace import grace_available
from scripts.upgrade_billing import verify_backup, verify_target

GRACE_REVISION = "0050_billing_grace"


def run(*, apply: bool = False, confirm_sole_organization: bool = False,
        backup: Path | None = None, backup_sha256: str | None = None,
        days: int = 30) -> dict[str, object]:
    if type(days) is not int or not 1 <= days <= 30:
        raise ValueError("grace_duration_invalid")
    if apply and not (confirm_sole_organization and backup and backup_sha256):
        raise ValueError("grace_operator_confirmation_required")
    backup_hash = verify_backup(backup, backup_sha256) if apply and backup and backup_sha256 else None
    nonce = os.environ["KP_DESK_INSTALL_NONCE"]
    if os.environ.get("KP_DESK_DB_HOST", "db") != "db":
        raise ValueError("grace_operator_identity_invalid")
    owner_password = Path("/run/owner-secret/db_owner_password").read_text(encoding="utf-8").strip()
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=owner_password, host="db",
        port=5432, database="kpdesk"), poolclass=NullPool, hide_parameters=True,
        connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("SET LOCAL statement_timeout='30s'")
            conn.exec_driver_sql("SET LOCAL lock_timeout='5s'")
            if apply:
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('kpdesk:production-install'))")
                conn.exec_driver_sql("LOCK TABLE public.alembic_version IN EXCLUSIVE MODE")
            else:
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            revision = verify_target(conn, nonce)
            if revision != GRACE_REVISION or not grace_available(conn):
                raise ValueError("grace_schema_revision_required")
            count = conn.scalar(text("SELECT count(*) FROM public.organizations"))
            active = conn.scalar(text("""SELECT count(*) FROM public.billing_grants
                WHERE revoked_at IS NULL AND starts_at<=clock_timestamp() AND ends_at>clock_timestamp()"""))
            grace_count = conn.scalar(text("SELECT count(*) FROM public.billing_grace"))
            offers = conn.scalar(text("SELECT count(*) FROM public.billing_offers"))
            billing = conn.scalar(text("SELECT enabled FROM public.billing_config WHERE singleton"))
            if (count != 1 or active != 0 or grace_count != 0 or offers != 3 or billing is not False):
                raise ValueError("grace_operator_preconditions_failed")
            if not apply:
                return {"status": "passed", "mode": "preflight",
                        "revision": revision, "existing_organizations": count,
                        "database_writes": 0}
            now = datetime.now(timezone.utc)
            issued = conn.execute(text("""INSERT INTO public.billing_grace
                    (organization_id,starts_at,ends_at,reason,issued_by)
                    SELECT id,:starts,:ends,'legacy_launch_transition','kpdesk_operator'
                    FROM public.organizations RETURNING organization_id"""),
                    {"starts": now, "ends": now + timedelta(days=days)}).all()
            if len(issued) != 1 or conn.scalar(text("SELECT count(*) FROM public.billing_grace")) != 1:
                raise ValueError("grace_operator_postcondition_failed")
        return {"status": "passed", "mode": "grant_legacy_grace",
                "duration_days": days, "revision": GRACE_REVISION,
                "backup_sha256": backup_hash}
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-single-existing-organization", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--backup-sha256")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)
    if args.apply and not (args.confirm_single_existing_organization and args.backup and args.backup_sha256):
        parser.error("Apply requires a single-organization confirmation and tested backup/SHA256.")
    if not args.apply and (args.confirm_single_existing_organization or args.backup or args.backup_sha256):
        parser.error("Mutation arguments require --apply.")
    try:
        result = run(apply=args.apply, confirm_sole_organization=args.confirm_single_existing_organization,
                     backup=args.backup, backup_sha256=args.backup_sha256, days=args.days)
        print(json.dumps(result))
        return 0 if result["status"] == "passed" else 1
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, SQLAlchemyError):
        print(json.dumps({"status": "failed", "code": "grace_operator_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
