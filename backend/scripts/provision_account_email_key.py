"""Create a private local Fernet key for durable encrypted account email outbox."""
from __future__ import annotations

from cryptography.fernet import Fernet

from kpdesk.config import PROJECT_ROOT
from scripts.provision_live_auth_logins import _lock_windows_secret_file


def provision() -> None:
    path = PROJECT_ROOT / "credentials" / "kpdesk-email-envelope.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        Fernet(path.read_bytes().strip())
        print("ACCOUNT_OUTBOX_ENCRYPTION_KEY_VERIFIED existing=true")
        return
    with path.open("xb") as file:
        file.write(Fernet.generate_key() + b"\n")
    try:
        _lock_windows_secret_file(path)
        Fernet(path.read_bytes().strip())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    print("ACCOUNT_OUTBOX_ENCRYPTION_KEY_PROVISIONED owner_acl=true")


if __name__ == "__main__":
    provision()
