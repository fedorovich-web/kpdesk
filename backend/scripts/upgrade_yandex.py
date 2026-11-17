"""Reviewed, operator-only 0048->0049 Yandex ID upgrade.

Read-only preflight by default. No app startup may apply a migration, and
no existing instance may be reset or recreated. Requires an independently
tested/restored database backup and explicit maintenance opt-ins to mutate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from kpdesk.db.live_service_engines import _restricted_engine
from kpdesk.release_runtime import verify_release_schema
from scripts.provision_production import OPERATIONS, check_secrets, migration_plan
from scripts.upgrade_billing import verify_backup, verify_target


@dataclass(frozen=True, slots=True)
class Upgrade:
    previous: str
    approval: str
    working_approval: str


UPGRADES = {
    "0049_yandex_id": Upgrade(
        "0048_lava_billing", "kpdesk_yandex_id_0049_approved",
        "kpdesk_yandex_id_0049_working_database_approved",
    ),
    "0050_billing_grace": Upgrade(
        "0049_yandex_id", "kpdesk_billing_grace_0050_approved",
        "kpdesk_billing_grace_0050_working_database_approved",
    ),
}


def checked_plan(target: str) -> Config:
    if target not in UPGRADES:
        raise ValueError("unknown_reviewed_upgrade")
    # Detect unexpected or tampered files before importing Alembic versions.
    migration_plan(OPERATIONS / "migrations", OPERATIONS / "migrations.sha256.json")
    config = Config()
    config.set_main_option("script_location", str(OPERATIONS / "alembic"))
    config.set_main_option("version_locations", str(OPERATIONS / "migrations/versions"))
    version = ScriptDirectory.from_config(config).get_revision(target)
    if version is None or version.down_revision != UPGRADES[target].previous:
        raise ValueError("reviewed_upgrade_lineage_invalid")
    return config


def apply_in_transaction(connection: Connection, target: str, nonce: str,
                         config: Config) -> None:
    if target not in UPGRADES or not connection.in_transaction():
        raise ValueError("reviewed_upgrade_transaction_invalid")
    version = UPGRADES[target]
    connection.exec_driver_sql("SET LOCAL lock_timeout='5s'")
    connection.exec_driver_sql("SET LOCAL statement_timeout='30s'")
    connection.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('kpdesk:production-install'))")
    connection.exec_driver_sql("LOCK TABLE public.alembic_version IN EXCLUSIVE MODE")
    if verify_target(connection, nonce) != version.previous:
        raise ValueError("reviewed_upgrade_previous_revision_required")
    config.attributes.update({
        "production_connection": connection,
        "production_nonce": nonce,
        version.approval: True,
        version.working_approval: True,
    })
    try:
        command.upgrade(config, target)
        if verify_target(connection, nonce) != target:
            raise ValueError("reviewed_upgrade_postcondition_failed")
    finally:
        for key in ("production_connection", "production_nonce",
                    version.approval, version.working_approval):
            config.attributes.pop(key, None)


def run(target: str, *, apply: bool = False, maintenance: bool = False,
        backup_restored: bool = False, backup: Path | None = None,
        backup_sha256: str | None = None) -> dict[str, object]:
    if target not in UPGRADES:
        raise ValueError("unknown_reviewed_upgrade")
    if apply and not (maintenance and backup_restored and backup and backup_sha256):
        raise ValueError("reviewed_upgrade_confirmation_required")
    backup_hash = verify_backup(backup, backup_sha256) if apply and backup and backup_sha256 else None
    nonce = os.environ["KP_DESK_INSTALL_NONCE"]
    if not re.fullmatch(r"[a-f0-9]{24}", nonce) or os.environ.get("KP_DESK_DB_HOST", "db") != "db":
        raise ValueError("reviewed_upgrade_identity_invalid")
    config = checked_plan(target)
    password = Path("/run/owner-secret/db_owner_password").read_text(encoding="utf-8").strip()
    services = json.loads(Path("/run/service-secret/service_credentials").read_text(encoding="utf-8"))
    check_secrets(password, services)
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password, host="db",
        port=5432, database="kpdesk",
    ), poolclass=NullPool, hide_parameters=True, connect_args={"connect_timeout": 5})
    restricted = _restricted_engine(
        "kpdesk_tenant_service", "kpdesk_tenant_runtime",
        services["kpdesk_tenant_service"], 5432, host="db",
    )
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET LOCAL statement_timeout='30s'")
            current = verify_target(connection, nonce)
        verify_release_schema(
            restricted, check_billing_activation=False, check_yandex_activation=False,
        )
        if current not in (UPGRADES[target].previous, target):
            raise ValueError("reviewed_upgrade_wrong_revision")
        if not apply:
            return {"status": "passed", "mode": "preflight",
                    "target": target, "revision": current, "database_writes": 0}
        if current == target:
            raise ValueError("reviewed_upgrade_already_applied")
        with engine.begin() as connection:
            apply_in_transaction(connection, target, nonce, config)
        try:
            verify_release_schema(
                restricted, check_billing_activation=False, check_yandex_activation=False,
            )
        except (RuntimeError, SQLAlchemyError):
            return {"status": "failed", "code": "reviewed_upgrade_postcommit_verification_failed",
                    "target": target, "schema_committed": True}
        return {"status": "passed", "mode": "schema_upgrade", "target": target,
                "revision": target, "schema_committed": True, "backup_sha256": backup_hash}
    finally:
        restricted.dispose()
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=UPGRADES, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-maintenance", action="store_true")
    parser.add_argument("--confirm-backup-restored", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--backup-sha256")
    args = parser.parse_args(argv)
    if args.apply and not (args.confirm_maintenance and args.confirm_backup_restored
                           and args.backup and args.backup_sha256):
        parser.error("Apply requires maintenance, tested-backup restoration and dump/SHA256.")
    if not args.apply and (args.confirm_maintenance or args.confirm_backup_restored
                           or args.backup or args.backup_sha256):
        parser.error("Mutation options require --apply.")
    try:
        result = run(args.target, apply=args.apply,
                     maintenance=args.confirm_maintenance,
                     backup_restored=args.confirm_backup_restored,
                     backup=args.backup, backup_sha256=args.backup_sha256)
        print(json.dumps(result))
        return 0 if result["status"] == "passed" else 1
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, SQLAlchemyError, CommandError):
        # Never log DSN, provider tokens, provider body or database exception text.
        print(json.dumps({"status": "failed", "code": "reviewed_upgrade_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
