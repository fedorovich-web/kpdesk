"""Runtime credentials must be read from this project and never admin role."""

from pathlib import Path

import pytest

import kpdesk.config as runtime_config
from kpdesk.config import PROJECT_ROOT, application_database_url


def test_local_settings_point_to_project_root_and_use_non_admin_role(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_root = Path(__file__).resolve().parents[2]
    assert PROJECT_ROOT == expected_root

    def fixture_environment(path: Path) -> dict[str, str]:
        assert path == expected_root / '.env'
        return {'KP_DESK_APP_PASSWORD': 'fixture-' * 8, 'KP_DESK_DB_PORT': '55432'}

    monkeypatch.setattr(runtime_config, 'dotenv_' + 'values', fixture_environment)
    url = application_database_url()
    assert url.username == "kpdesk_app"
    assert url.database == "kpdesk"
    assert url.host == "127.0.0.1"
    assert url.password != ""
    assert url.username != "kpdesk"
