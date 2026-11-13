"""Own blocking work until it settles, without logging private worker exceptions."""
import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Failure:
    error: Exception


async def settle_thread[T](call: Callable[[], T]) -> tuple[T, asyncio.CancelledError | None]:
    """Return a result and deferred cancellation so callers can adopt committed work.

    Callers must bound their blocking I/O and propagate the returned cancellation
    after adopting its result. Failure after cancellation preserves cancellation.
    """
    async def captured() -> T | _Failure:
        try:
            return await asyncio.to_thread(call)
        except Exception as error:  # noqa: BLE001 - re-raised below, kept out of shield exception logs
            return _Failure(error)

    task = asyncio.create_task(captured())
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancellation = cancellation or error
    if isinstance(result, _Failure):
        if cancellation is not None:
            _LOG.warning('worker_failed_during_cancellation')
            raise cancellation from None
        raise result.error
    return result, cancellation


async def run_thread[T](call: Callable[[], T]) -> T:
    """Settle a blocking operation and propagate cancellation after it finishes."""
    result, cancellation = await settle_thread(call)
    if cancellation is not None:
        raise cancellation
    return result
