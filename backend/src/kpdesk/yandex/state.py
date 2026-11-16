"""Purpose-bound PKCE encryption and opaque-token validation, without cookies/logging."""
import hashlib
import re
from base64 import urlsafe_b64encode

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from kpdesk.yandex.client import YandexError
from kpdesk.yandex.settings import YandexSettings

_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')
_VERIFIER = re.compile(r'[A-Za-z0-9._~-]{43,128}\Z')
_PURPOSE = b'kpdesk:yandex-pkce:v1:'


def digest_token(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise YandexError('yandex_flow_expired')
    return hashlib.sha256(value.encode('ascii')).hexdigest()


def pkce_challenge(verifier: str) -> str:
    if not isinstance(verifier, str) or not _VERIFIER.fullmatch(verifier):
        raise YandexError('yandex_request_invalid')
    return urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode()


def safe_next_path(value: str | None) -> str:
    return value if value in ('/app', '/admin') else '/app'


class VerifierCipher:
    def __init__(self, key: bytes) -> None:
        try:
            self._cipher = Fernet(key)
        except (ValueError, TypeError):
            raise YandexError('yandex_configuration_invalid') from None

    @classmethod
    def from_runtime(cls) -> VerifierCipher:
        # Production containers have no local credentials/ directory. Derive an
        # independent 32-byte encryption key from the existing provider secret.
        # Rotation invalidates at most 10-minute in-flight authorization flows.
        try:
            settings = YandexSettings.from_env()
            if not settings.enabled:
                raise ValueError('provider_disabled')
            key = HKDF(algorithm=hashes.SHA256(), length=32,
                salt=b'kpdesk:yandex:pkce-envelope:salt:v1',
                info=b'kpdesk:yandex:pkce-envelope:fernet:v1',
            ).derive(settings.client_secret.encode('ascii'))
            return cls(urlsafe_b64encode(key))
        except (ValueError, TypeError, UnicodeError):
            raise YandexError('yandex_configuration_invalid') from None

    def seal(self, verifier: str) -> str:
        pkce_challenge(verifier)
        return self._cipher.encrypt(_PURPOSE + verifier.encode('ascii')).decode('ascii')

    def open(self, sealed: str) -> str:
        if not isinstance(sealed, str) or not 80 <= len(sealed) <= 4096:
            raise YandexError('yandex_flow_expired')
        try:
            raw = self._cipher.decrypt(sealed.encode('ascii'))
            if not raw.startswith(_PURPOSE):
                raise ValueError('wrong_purpose')
            verifier = raw[len(_PURPOSE):].decode('ascii')
            pkce_challenge(verifier)
            return verifier
        except (InvalidToken, ValueError, UnicodeError, YandexError):
            raise YandexError('yandex_flow_expired') from None
