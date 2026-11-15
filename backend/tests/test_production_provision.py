"""Offline admission checks for the explicit first production install operator."""
from pathlib import Path

import pytest

from scripts.provision_production import admission, check_secrets, migration_plan, proxy_addresses

NONCE = 'a' * 24


def test_unknown_or_existing_unmarked_database_is_refused() -> None:
    assert admission('kpdesk', 'kpdesk', None, None, 0, 0, NONCE) == 'install'
    for database, owner, version, marker, tables, roles in (
        ('other', 'kpdesk', None, None, 0, 0),
        ('kpdesk', 'postgres', None, None, 0, 0),
        ('kpdesk', 'kpdesk', None, None, 1, 0),
        ('kpdesk', 'kpdesk', None, None, 0, 1),
        ('kpdesk', 'kpdesk', '0046_provider_authority', None, 48, 0),
        ('kpdesk', 'kpdesk', '0047_auth_reset_serialization', None, 48, 0),
        ('kpdesk', 'kpdesk', '0047_auth_reset_serialization', 'foreign', 48, 0),
    ):
        with pytest.raises(ValueError):
            admission(database, owner, version, marker, tables, roles, NONCE)


def test_matching_completed_install_only_enters_read_only_verification() -> None:
    assert admission('kpdesk', 'kpdesk', '0047_auth_reset_serialization',
        'kpdesk:production:' + NONCE, 48, 5, NONCE) == 'verify'
    with pytest.raises(ValueError):
        admission('kpdesk', 'kpdesk', None, None, 0, 0, "';DROP DATABASE kpdesk")


def test_reviewed_billing_upgrade_is_verified_without_reinstalling() -> None:
    assert admission('kpdesk', 'kpdesk', '0048_lava_billing',
        'kpdesk:production:' + NONCE, 56, 5, NONCE) == 'verify'
    for tables, roles, marker in ((48, 5, 'kpdesk:production:' + NONCE),
                                  (56, 4, 'kpdesk:production:' + NONCE),
                                  (56, 5, 'foreign')):
        with pytest.raises(ValueError):
            admission('kpdesk', 'kpdesk', '0048_lava_billing', marker, tables, roles, NONCE)


def test_separated_owner_auth_tenant_secrets_are_required() -> None:
    credentials = {'kpdesk_auth_service': 'b' * 64, 'kpdesk_tenant_service': 'c' * 64}
    check_secrets('a' * 64, credentials)
    for owner, services in (
        ('short', credentials), ('b' * 64, credentials),
        ('a' * 64, {**credentials, 'kpdesk_tenant_service': 'b' * 64}),
        ('a' * 64, {**credentials, 'kpdesk_auth_service': 'x\n' * 32}),
        ('a' * 64, {**credentials, 'extra': 'd' * 64}),
    ):
        with pytest.raises(ValueError):
            check_secrets(owner, services)


def test_only_reviewed_release_lineage_and_bytes_are_accepted(tmp_path: Path) -> None:
    import shutil

    root = Path(__file__).resolve().parents[2]
    migrations = tmp_path / 'migrations'
    shutil.copytree(root / 'backend/migrations/versions', migrations / 'versions')
    manifest = root / 'infra/production/migrations.sha256.json'
    steps, approvals = migration_plan(migrations, manifest)
    assert len(steps) == 40 and steps[-1] == '0047_auth_reset_serialization'
    assert not any(x.startswith(tuple(f'{n:04d}_' for n in range(6, 13))) for x in steps)
    assert 'kpdesk_auth_reset_serialization_0047_approved' in approvals
    file = migrations / 'versions/0047_auth_reset_serialization.py'
    file.write_text(file.read_text(encoding='utf-8') + '\n# changed bytes\n', encoding='utf-8')
    with pytest.raises(ValueError, match='migration_bytes_mismatch'):
        migration_plan(migrations, manifest)


def test_proxy_resolution_keeps_exact_private_addresses_and_refuses_other_hosts() -> None:
    resolver = lambda *args, **kwargs: [(2, 1, 6, '', ('172.18.0.2', 0))]
    assert proxy_addresses('dokploy-traefik', resolver=resolver) == '172.18.0.2'
    for hostname in ('*', 'example.com', '127.0.0.1', 'dokploy-traefik.evil'):
        with pytest.raises(ValueError):
            proxy_addresses(hostname, resolver=resolver)
    for address in ('8.8.8.8', '127.0.0.1', '0.0.0.0', '169.254.169.254'):
        with pytest.raises(ValueError):
            proxy_addresses('dokploy-traefik', resolver=lambda *args, value=address, **kwargs: [(2, 1, 6, '', (value, 0))])


def test_altered_migration_cannot_execute_even_top_level_code(tmp_path: Path) -> None:
    import shutil

    root = Path(__file__).resolve().parents[2]
    migrations = tmp_path/'migrations'
    shutil.copytree(root/'backend/migrations/versions', migrations/'versions')
    sentinel = tmp_path/'code-executed'
    file = migrations/'versions/0006_legacy_quarantine.py'
    file.write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').touch()\n' +
        file.read_text(encoding='utf-8').replace('from __future__ import annotations', ''), encoding='utf-8')
    with pytest.raises(ValueError, match='migration_bytes_mismatch'):
        migration_plan(migrations, root/'infra/production/migrations.sha256.json')
    assert not sentinel.exists(), 'Changed migration executed before admission'
