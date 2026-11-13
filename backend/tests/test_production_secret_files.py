"""Read-only app containers receive isolated immutable secret files."""
import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from scripts.seed_production_secrets import seed_files


def material() -> tuple[str, str, str]:
    return 'owner-' * 10, json.dumps({'kpdesk_auth_service': 'auth-' * 12,
        'kpdesk_tenant_service': 'tenant-' * 9}), Fernet.generate_key().decode()


def test_files_are_created_once_and_rerun_cannot_rotate_them(tmp_path: Path) -> None:
    owner, services, email = material()
    assert seed_files(tmp_path, owner, services, email) == 3
    assert (tmp_path/'owner/db_owner_password').read_text() == owner
    assert json.loads((tmp_path/'service/service_credentials').read_text()) == json.loads(services)
    assert (tmp_path/'email/kpdesk-email-envelope.key').read_text() == email
    before = {x: x.read_bytes() for x in tmp_path.rglob('*') if x.is_file()}
    assert seed_files(tmp_path, owner, services, email) == 0
    assert all(x.read_bytes() == value for x, value in before.items())
    with pytest.raises(ValueError, match='existing_secret_differs'):
        seed_files(tmp_path, 'different-' * 7, services, email)
    assert all(x.read_bytes() == value for x, value in before.items())


def test_invalid_material_fails_before_any_file_is_written(tmp_path: Path) -> None:
    owner, services, _ = material()
    with pytest.raises(ValueError):
        seed_files(tmp_path, owner, services, 'invalid-key')
    assert not list(tmp_path.rglob('*'))


def test_foreign_existing_entry_is_never_replaced(tmp_path: Path) -> None:
    owner, services, email = material()
    path = tmp_path/'owner/db_owner_password'
    path.mkdir(parents=True)
    with pytest.raises(ValueError, match='secret_path_not_regular'):
        seed_files(tmp_path, owner, services, email)
    assert path.is_dir() and not (tmp_path/'service/service_credentials').exists()
