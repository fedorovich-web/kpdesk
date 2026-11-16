"""Fail-closed audit of KPDesk's separate production and test-only Alembic lines.

This is read-only. It never upgrades, stamps, merges, or modifies PostgreSQL.
The 0006-0012 branch is a deliberate historical test-only fork from 0005.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND = Path(__file__).resolve().parents[1]
_FORK_BASE = "0005_tenant_expand"
_LEGACY_HEAD = "0012_session_bound_rls"
# 0048 and 0049 are opt-in migrations; fresh installation remains at 0047.
_RELEASE_HEAD = "0049_yandex_id"
_FROZEN_INSTALL_HEAD = "0047_auth_reset_serialization"
_OPT_IN_CHAIN = ("0049_yandex_id", "0048_lava_billing")
_RELEASE_BRANCH = "live_release"
_CLONE_HEAD = "0031_reviewed_catalog_clone"


@dataclass(frozen=True, slots=True)
class MigrationLineage:
    release_head: str
    historical_test_head: str
    release_revisions: tuple[str, ...]
    historical_test_revisions: tuple[str, ...]

    @property
    def target(self) -> str:
        return "live_release@head"


def audit_migration_lineage(
    backend: Path = _BACKEND,
) -> MigrationLineage:
    """Validate the default production Alembic script directory without DDL."""
    config = Config(str(backend / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = set(script.get_heads())
    if heads != {_LEGACY_HEAD, _RELEASE_HEAD}:
        raise RuntimeError(f"Unexpected Alembic heads: {sorted(heads)}")
    live = script.get_revision(f"{_RELEASE_BRANCH}@head")
    if (live is None or live.revision != _RELEASE_HEAD
            or live.down_revision != "0048_lava_billing"):
        raise RuntimeError("Active live_release lineage is not at reviewed revision.")
    if script.get_revision(_LEGACY_HEAD) is None:
        raise RuntimeError("Historical test-only migration lineage is missing.")
    release_steps = tuple(
        rev.revision for rev in script.iterate_revisions(_RELEASE_HEAD, _FORK_BASE)
    )
    test_steps = tuple(
        rev.revision for rev in script.iterate_revisions(_LEGACY_HEAD, _FORK_BASE)
    )
    if (
        not release_steps or not test_steps
        or release_steps[:3] != (*_OPT_IN_CHAIN, _FROZEN_INSTALL_HEAD)
        or release_steps[-1] != "0013_live_expand"
        or test_steps[-1] != "0006_legacy_quarantine"
        or set(release_steps) & set(test_steps)
        or _CLONE_HEAD in {rev.revision for rev in script.walk_revisions()}
    ):
        raise RuntimeError("Production and test-only Alembic branches are mixed.")
    return MigrationLineage(
        release_head=live.revision,
        historical_test_head=_LEGACY_HEAD,
        release_revisions=release_steps,
        historical_test_revisions=test_steps,
    )


def main() -> int:
    audited = audit_migration_lineage()
    print("KPDesk Alembic audit: PASS (read-only)")
    print(f"Production branch: {audited.target} -> {audited.release_head}")
    print(f"Historical test-only head: {audited.historical_test_head}")
    print(f"Release revisions since 0005: {len(audited.release_revisions)}")
    print(f"Isolated old revisions since 0005: {len(audited.historical_test_revisions)}")
    print("Do not use an ambiguous 'alembic upgrade head/heads'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
