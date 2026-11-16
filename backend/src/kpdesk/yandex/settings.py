"""Opt-in Yandex settings; secrets never enter public capabilities or repr."""
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

CALLBACK = 'https://kpdesk.ru/api/session/yandex/callback'


@dataclass(frozen=True, slots=True)
class YandexSettings:
    enabled: bool = False
    client_id: str = ''
    client_secret: str = field(default='', repr=False)
    redirect_uri: str = CALLBACK

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError('yandex_enabled_invalid')
        if self.redirect_uri != CALLBACK:
            raise ValueError('yandex_callback_invalid')
        if not self.enabled:
            return
        if not isinstance(self.client_id, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', self.client_id):
            raise ValueError('yandex_client_id_invalid')
        object.__setattr__(self, 'client_id', self.client_id.lower())
        if (not isinstance(self.client_secret, str) or not 16 <= len(self.client_secret) <= 512
                or any(not 33 <= ord(c) <= 126 for c in self.client_secret)):
            raise ValueError('yandex_client_secret_invalid')

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> YandexSettings:
        env = os.environ if environ is None else environ
        flag = env.get('KP_DESK_YANDEX_ENABLED')
        if flag not in (None, 'true', 'false'):
            raise ValueError('yandex_enabled_invalid')
        if flag != 'true':
            return cls()
        return cls(enabled=True, client_id=env.get('KP_DESK_YANDEX_CLIENT_ID', ''),
                   client_secret=env.get('KP_DESK_YANDEX_CLIENT_SECRET', ''),
                   redirect_uri=env.get('KP_DESK_YANDEX_REDIRECT_URI', CALLBACK))
