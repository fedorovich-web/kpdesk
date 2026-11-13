from pathlib import Path

import pytest
from pydantic import ValidationError

from kpdesk.llm.settings import LLMSettings


def test_settings_locked_and_secret_hidden() -> None:
    s = LLMSettings(api_key='secret')
    assert 'secret' not in repr(s)
    with pytest.raises(ValidationError): LLMSettings.model_validate({'api_key': 'x', 'model': 'paid'})
    with pytest.raises(ValidationError): LLMSettings.model_validate({'api_key': 'x', 'data_mode': 'production'})

def test_explicit_empty_environment_disables_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / '.env'
    path.write_text('OPENROUTER_API_KEY=local\n', encoding='utf8')
    monkeypatch.setenv('OPENROUTER_API_KEY', '')
    with pytest.raises(ValueError): LLMSettings.from_local_env(path)
