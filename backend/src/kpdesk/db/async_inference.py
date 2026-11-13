"""Cancellation-safe ownership of a request's logical inference reservations."""
import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from uuid import UUID

from kpdesk.async_work import settle_thread

_LOG = logging.getLogger(__name__)


class InferenceReservations:
    def __init__(self, reserve: Callable[[int], UUID], finish: Callable[[UUID, bool], None]):
        self._reserve, self._finish = reserve, finish
        self.current: UUID | None = None

    async def reserve(self, input_chars: int) -> UUID:
        if self.current is not None:
            raise RuntimeError('Inference reservation is already active')
        reservation, cancellation = await settle_thread(partial(self._reserve, input_chars))
        self.current = reservation
        if cancellation is not None:
            raise cancellation
        return reservation

    async def complete(self, succeeded: bool) -> None:
        current, self.current = self.current, None
        if current is not None:
            _, cancellation = await settle_thread(partial(self._finish, current, succeeded))
            if cancellation is not None:
                raise cancellation

    async def before_followup(self, payload: str, previous_succeeded: bool) -> None:
        await self.complete(previous_succeeded)
        await self.reserve(len(payload))

    @asynccontextmanager
    async def scope(self, input_chars: int) -> AsyncIterator[UUID]:
        succeeded = cancelled = False
        try:
            reservation = await self.reserve(input_chars)
            yield reservation
            succeeded = True
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            try:
                await self.complete(succeeded)
            except Exception:
                if not cancelled:
                    raise
                _LOG.warning('logical_inference_finish_failed_during_cancellation')
