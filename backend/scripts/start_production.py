"""Resolve the fixed Dokploy proxy peer, then exec the standard app entrypoint."""
import os
import sys

from scripts.provision_production import proxy_addresses

if os.environ.get('KP_DESK_PROXY_HOST'):
    if os.environ.get('KP_DESK_TRUSTED_PROXY_IPS'):
        raise SystemExit('Specify either proxy hostname or exact addresses, not both.')
    os.environ['KP_DESK_TRUSTED_PROXY_IPS'] = proxy_addresses(os.environ['KP_DESK_PROXY_HOST'])
os.execv(sys.executable, [sys.executable, '-m', 'kpdesk.container_entrypoint'])
