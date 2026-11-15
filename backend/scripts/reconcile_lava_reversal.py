"""Explicit operator resolution for Lava reversal events without invoice IDs.

Requires protected service credentials and an invoice ID confirmed in Lava's
operator console. Never looks up a purchase by email or creates an invoice.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

from kpdesk.billing.lava import LavaClient
from kpdesk.billing.service import BillingService, DatabaseBillingRepository
from kpdesk.billing.settings import BillingSettings


async def resolve(event_id: UUID, invoice_id: UUID) -> None:
    settings=BillingSettings.from_env()
    if not settings.enabled:
        raise ValueError('Billing must be enabled for operator reconciliation.')
    from kpdesk.db.live_service_engines import live_service_engines

    auth,tenant=live_service_engines()
    try:
        service=BillingService(settings,DatabaseBillingRepository(tenant,auth),LavaClient(settings))
        await service.resolve_reversal(event_id,invoice_id)
    finally:
        auth.dispose()
        tenant.dispose()


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event-id',required=True,type=UUID)
    parser.add_argument('--invoice-id',required=True,type=UUID)
    parser.add_argument('--confirm-invoice-correlation',required=True,action='store_true',
        help='The invoice was explicitly correlated to this event in the provider console.')
    options=parser.parse_args(argv)
    try:
        asyncio.run(resolve(options.event_id,options.invoice_id))
    except Exception:  # noqa: BLE001 - provider/DB exceptions may contain private data
        print(json.dumps({'status':'failed','code':'billing_reversal_not_resolved'}))
        return 1
    print(json.dumps({'status':'resolved'}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
