"""Container PID1 and internal readiness probe; never migrates or sends data."""
import ipaddress
import os
import sys

import httpx
import uvicorn

from kpdesk.public_origin import public_hostname


def trusted_proxies(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError('Explicit KP_DESK_TRUSTED_PROXY_IPS addresses are required.')
    try:
        addresses = {str(ipaddress.ip_address(item)) for item in value.split(',')}
    except ValueError as error:
        raise ValueError('Proxy trust accepts exact IP addresses only.') from error
    # Only the container-local health probe uses this extra trusted loopback peer.
    addresses.add('127.0.0.1')
    return ','.join(sorted(addresses))


def healthcheck() -> int:
    try:
        host = public_hostname(os.environ['KP_DESK_PUBLIC_ORIGIN'])
        response = httpx.get('http://127.0.0.1:8000/api/health/ready',
                             headers={'Host': host, 'X-Forwarded-Proto': 'https'},
                             timeout=3, trust_env=False)
        return 0 if response.status_code == 200 and response.json() == {'status': 'ready'} else 1
    except (httpx.HTTPError, ValueError, KeyError):
        return 1


def main() -> None:
    if sys.argv[1:] == ['--healthcheck']:
        raise SystemExit(healthcheck())
    if sys.argv[1:]:
        raise SystemExit('Unexpected container arguments.')
    from kpdesk.production import public_tenant_settings

    proxies = trusted_proxies(os.environ.get('KP_DESK_TRUSTED_PROXY_IPS', ''))
    public_tenant_settings()
    uvicorn.run('kpdesk.production:create_production_app', factory=True,
                host='0.0.0.0', port=8000, proxy_headers=True, forwarded_allow_ips=proxies,
                server_header=False, access_log=False, workers=1)


if __name__ == '__main__':
    main()
