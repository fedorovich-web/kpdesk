"""Offline, one-shot immutable secret files, each in its own project volume."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

from scripts.provision_production import check_secrets


def seed_files(root: Path, owner: str, services: str, email: str) -> int:
    credentials = json.loads(services)
    check_secrets(owner, credentials)
    Fernet(email.encode('ascii'))
    outputs = {
        root/'owner/db_owner_password': owner.encode('ascii'),
        root/'service/service_credentials': json.dumps(credentials, sort_keys=True).encode('ascii'),
        root/'email/kpdesk-email-envelope.key': email.encode('ascii'),
    }
    # Validate every existing entry before creating anything. Rotation is a
    # separate operator action; restarts must never silently replace keys.
    for path, value in outputs.items():
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('secret_path_not_regular')
        if path.exists():
            if not path.is_file():
                raise ValueError('secret_path_not_regular')
            if not hmac.compare_digest(path.read_bytes(), value):
                raise ValueError('existing_secret_differs')
    created = 0
    for path, value in outputs.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags, 0o444)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        created += 1
    return created


if __name__ == '__main__':
    try:
        count = seed_files(Path('/secrets'), os.environ['KP_DESK_PRODUCTION_DB_PASSWORD'],
            os.environ['KP_DESK_PRODUCTION_SERVICE_CREDENTIALS'], os.environ['KP_DESK_PRODUCTION_EMAIL_KEY'])
        print(json.dumps({'status': 'passed', 'new_secret_files': count, 'rotation': False}))
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
