"""Generic named-task supervisor.

Runs a set of long-lived coroutines as `asyncio.Task`s, restarts any that
exit unexpectedly with backoff up to a bounded attempt count, and tracks a
per-task heartbeat so a caller can see a task that is alive but silently
stuck (no exception, just no progress) - something `task.done()` alone can
never reveal.

Deliberately has no idea what any task actually does: a task's own loop body
calls `heartbeat(name)` once per iteration, and that is the entire coupling
between this module and the scheduler loops in `orchestration/runtime.py`.
The Binance/user-data WebSocket streams are *not* registered here - they
already run their own internal reconnect-with-backoff supervisor
(`exchange.websocket_manager.ReconnectingStream`) and, by design, never
exit on their own short of `.stop()`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from config.settings import WatchdogConfig

logger = logging.getLogger(__name__)

TaskFactory = Callable[[], Coroutine[Any, Any, None]]
GaveUpCallback = Callable[[str, "BaseException | None"], Awaitable[None]]


@dataclass
class _TaskState:
    factory: TaskFactory
    task: asyncio.Task[None] | None = None
    restart_count: int = 0
    last_heartbeat: float = field(default_factory=time.monotonic)
    gave_up: bool = False


class Watchdog:
    def __init__(self, config: WatchdogConfig, *, on_task_gave_up: GaveUpCallback | None = None) -> None:
        self._config = config
        self._on_task_gave_up = on_task_gave_up
        self._tasks: dict[str, _TaskState] = {}
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stopping = False

    def register(self, name: str, factory: TaskFactory) -> None:
        self._tasks[name] = _TaskState(factory=factory)

    def heartbeat(self, name: str) -> None:
        state = self._tasks.get(name)
        if state is not None:
            state.last_heartbeat = time.monotonic()

    def _start_task(self, state: _TaskState, *, name: str) -> None:
        state.task = asyncio.create_task(state.factory(), name=name)
        state.last_heartbeat = time.monotonic()

    async def start(self) -> None:
        self._stopping = False
        for name, state in self._tasks.items():
            self._start_task(state, name=name)
        self._supervisor_task = asyncio.create_task(self._supervise(), name="watchdog")

    async def _supervise(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._config.check_interval_seconds)
            for name, state in self._tasks.items():
                if state.gave_up or state.task is None or not state.task.done():
                    continue
                exc = state.task.exception() if not state.task.cancelled() else None
                await self._handle_exit(name, state, exc)

    async def _handle_exit(self, name: str, state: _TaskState, exc: BaseException | None) -> None:
        if state.restart_count >= self._config.max_restart_attempts:
            state.gave_up = True
            logger.critical(
                "Task %s exceeded max restart attempts (%s); giving up: %r",
                name, self._config.max_restart_attempts, exc,
            )
            if self._on_task_gave_up is not None:
                await self._on_task_gave_up(name, exc)
            return

        backoff = self._config.task_restart_backoff_seconds
        delay = backoff[min(state.restart_count, len(backoff) - 1)] if backoff else 0
        logger.warning(
            "Task %s exited (%r); restarting in %ss (attempt %s/%s)",
            name, exc, delay, state.restart_count + 1, self._config.max_restart_attempts,
        )
        state.restart_count += 1
        await asyncio.sleep(delay)
        self._start_task(state, name=name)

    def snapshot(self) -> dict[str, dict[str, object]]:
        now = time.monotonic()
        return {
            name: {
                "running": state.task is not None and not state.task.done(),
                "restart_count": state.restart_count,
                "gave_up": state.gave_up,
                "seconds_since_heartbeat": round(now - state.last_heartbeat, 1),
            }
            for name, state in self._tasks.items()
        }

    async def stop(self) -> None:
        self._stopping = True
        tasks = [s.task for s in self._tasks.values() if s.task is not None]
        if self._supervisor_task is not None:
            tasks.append(self._supervisor_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
