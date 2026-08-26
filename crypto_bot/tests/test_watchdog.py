from __future__ import annotations

import asyncio

from config.settings import WatchdogConfig
from orchestration.watchdog import Watchdog


async def _wait_until(predicate, *, timeout_iterations: int = 200, step: float = 0.005) -> None:
    for _ in range(timeout_iterations):
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition never became true")


def test_crashing_task_is_restarted_then_gives_up_after_max_attempts():
    config = WatchdogConfig(check_interval_seconds=0, task_restart_backoff_seconds=[0, 0], max_restart_attempts=2)
    attempts: list[int] = []

    async def flaky() -> None:
        attempts.append(1)
        raise RuntimeError("boom")

    async def scenario() -> Watchdog:
        watchdog = Watchdog(config)
        watchdog.register("flaky", flaky)
        await watchdog.start()
        await _wait_until(lambda: watchdog.snapshot()["flaky"]["gave_up"])
        await watchdog.stop()
        return watchdog

    watchdog = asyncio.run(scenario())
    snapshot = watchdog.snapshot()
    assert snapshot["flaky"]["gave_up"] is True
    assert snapshot["flaky"]["restart_count"] == 2
    assert len(attempts) == 3  # 1 initial run + 2 restarts, then give up


def test_gave_up_callback_receives_task_name_and_exception():
    config = WatchdogConfig(check_interval_seconds=0, task_restart_backoff_seconds=[0], max_restart_attempts=1)
    called: list[tuple[str, BaseException | None]] = []

    async def on_gave_up(name: str, exc: BaseException | None) -> None:
        called.append((name, exc))

    async def always_fails() -> None:
        raise ValueError("nope")

    async def scenario() -> None:
        watchdog = Watchdog(config, on_task_gave_up=on_gave_up)
        watchdog.register("bad", always_fails)
        await watchdog.start()
        await _wait_until(lambda: bool(called))
        await watchdog.stop()

    asyncio.run(scenario())
    assert len(called) == 1
    name, exc = called[0]
    assert name == "bad"
    assert isinstance(exc, ValueError)


def test_healthy_long_running_task_is_never_restarted():
    config = WatchdogConfig(check_interval_seconds=0, task_restart_backoff_seconds=[0], max_restart_attempts=1)

    async def long_runner() -> None:
        await asyncio.sleep(10)

    async def scenario() -> dict:
        watchdog = Watchdog(config)
        watchdog.register("steady", long_runner)
        await watchdog.start()
        watchdog.heartbeat("steady")
        await asyncio.sleep(0.05)
        snapshot = watchdog.snapshot()
        await watchdog.stop()
        return snapshot

    snapshot = asyncio.run(scenario())
    assert snapshot["steady"]["running"] is True
    assert snapshot["steady"]["restart_count"] == 0
    assert snapshot["steady"]["seconds_since_heartbeat"] < 0.5  # generous margin for scheduler jitter
