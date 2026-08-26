"""Async retry with exponential backoff + jitter.

Used for every network call to Binance / Telegram / news sources so a
transient timeout, 429, or connection drop doesn't take down the bot or spam
the exchange. `asyncio.CancelledError` is always re-raised immediately -
retrying it would break task cancellation / shutdown semantics.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
P = ParamSpec("P")


class RetryExhaustedError(RuntimeError):
    def __init__(self, attempts: int, last_exception: BaseException) -> None:
        super().__init__(f"Retry exhausted after {attempts} attempt(s): {last_exception!r}")
        self.attempts = attempts
        self.last_exception = last_exception


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: float = 0.25,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[int, BaseException], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call `fn()` (no-arg async callable) until it succeeds or attempts run out.

    Delay grows as `base_delay * 2**(attempt-1)`, capped at `max_delay`, plus
    up to `jitter` fraction of extra random delay to avoid thundering-herd
    retries against the exchange.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except retry_on as exc:
            if attempt >= max_attempts:
                raise RetryExhaustedError(attempt, exc) from exc
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            delay += random.uniform(0, jitter * delay)
            if on_retry is not None:
                on_retry(attempt, exc)
            else:
                logger.warning(
                    "Retry %s/%s after error: %r (sleeping %.1fs)", attempt, max_attempts, exc, delay
                )
            await sleep(delay)


def async_retry(
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorator form of :func:`retry_async`."""

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return await retry_async(
                lambda: func(*args, **kwargs),
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                retry_on=retry_on,
            )

        return wrapper

    return decorator
