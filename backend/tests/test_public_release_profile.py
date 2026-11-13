"""The public CI profile may only select reviewed offline tests, never discovery."""
import json
from pathlib import Path

import pytest

from scripts.run_public_release_tests import selection


def test_public_profile_is_explicit_and_cannot_admit_unsafe_files(tmp_path: Path) -> None:
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tests/'test_safe.py').write_text('def test_safe(): pass', encoding='utf-8')
    (tests/'test_unsafe.py').write_text('create_' + 'engine(', encoding='utf-8')
    manifest = tmp_path/'profile.json'
    for files in ([], ['test_unknown.py'], ['test_unsafe.py'], ['test_safe.py', 'test_unsafe.py']):
        manifest.write_text(json.dumps({'selected': files}), encoding='utf-8')
        with pytest.raises(ValueError):
            selection(tmp_path, manifest)
    manifest.write_text(json.dumps({'selected': ['test_safe.py']}), encoding='utf-8')
    assert selection(tmp_path, manifest) == ['test_safe.py']
