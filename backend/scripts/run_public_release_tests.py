"""Reviewed runtime CI without private datasets/models; full offline profile unchanged."""
from __future__ import annotations

import json
import os
from pathlib import Path

from scripts import run_safe_tests

# This one historical case requires the immutable external supplier original.
EXTERNAL_ORIGINAL = 'tests/test_supplier_precision_batch.py::test_real_ecoplast_precision_packet_matches_immutable_original'


def selection(backend: Path, manifest: Path) -> list[str]:
    files = json.loads(manifest.read_text(encoding='utf-8'))['selected']
    if not isinstance(files, list) or not files or any(not isinstance(x, str) for x in files) or len(set(files)) != len(files):
        raise ValueError('Public release requires an explicit unique test list.')
    run_safe_tests.select_tests(backend, 'offline', files)
    return files


if __name__ == '__main__':
    import sys

    backend = Path(__file__).resolve().parents[1]
    manifest = backend/'test_profiles/public-release.json'
    files = selection(backend, manifest)
    previous = os.environ.get('PYTEST_ADDOPTS', '')
    os.environ['PYTEST_ADDOPTS'] = previous + ' --deselect=' + EXTERNAL_ORIGINAL
    print('Public runtime release profile; protected ML/data quality is a separate gate.', flush=True)
    raise SystemExit(run_safe_tests.main(['--profile', 'offline', *sys.argv[1:], *files]))
