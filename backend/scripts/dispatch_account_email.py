"""Dispatch pending verification/reset emails from durable PostgreSQL outbox.

One-shot mode is suitable for Task Scheduler; --loop runs a foreground worker.
Does not print customer addresses, verification tokens or provider credentials.
"""
from __future__ import annotations

import argparse
import asyncio

from kpdesk.db.live_service_engines import live_service_engines
from kpdesk.notifications.account_email_outbox import dispatch_one
from kpdesk.notifications.team_invite_outbox import dispatch_team_one


async def run(*, loop: bool, interval_seconds: int) -> None:
    auth_engine, _ = live_service_engines()
    while True:
        account_result = await dispatch_one(auth_engine)
        team_result = await dispatch_team_one(auth_engine)
        if "unconfigured" in (account_result, team_result):
            raise RuntimeError("Resend credentials unavailable; outbox remains queued.")
        print("ACCOUNT_EMAIL_OUTBOX status=" + account_result
              + " team=" + team_result)
        if not loop:
            return
        await asyncio.sleep(
            1 if "sent" in (account_result, team_result) else interval_seconds
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="KPDesk account mail outbox worker")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args()
    if not 10 <= args.interval_seconds <= 3600:
        parser.error("--interval-seconds must be between 10 and 3600")
    try:
        asyncio.run(run(loop=args.loop, interval_seconds=args.interval_seconds))
    except KeyboardInterrupt:
        print("ACCOUNT_EMAIL_OUTBOX stopped=true")


if __name__ == "__main__":
    main()
