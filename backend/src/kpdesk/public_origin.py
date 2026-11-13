"""Lightweight shared canonical origin validation, without importing the ASGI app."""
import re
from urllib.parse import urlsplit

_PUBLIC_DNS_NAME = re.compile(
    r'(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+'
    r'[a-z]{2,63}\Z'
)


def public_hostname(raw: str) -> str:
    if not raw or raw != raw.strip():
        raise ValueError('KP_DESK_PUBLIC_ORIGIN must be an explicit HTTPS origin.')
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError('Invalid production origin.') from error
    if (parsed.scheme != 'https' or parsed.netloc != hostname
        or not hostname or not _PUBLIC_DNS_NAME.fullmatch(hostname)
        or port is not None or parsed.username is not None
        or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
        raise ValueError(
            'Public tenant origin must be a canonical https:// DNS host with no port or path.'
        )
    if hostname.rsplit('.', maxsplit=1)[-1] in {
        'localhost', 'local', 'test', 'invalid', 'example', 'internal',
    }:
        raise ValueError('Production origin must use a public DNS suffix.')
    return hostname
