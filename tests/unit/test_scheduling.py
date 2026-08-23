"""Waiting for a reset, without actually waiting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from claude_runner.domain.exceptions import WaitInterruptedError
from claude_runner.infrastructure.scheduling import SleepingResetScheduler

START = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def _scheduler(clock: FakeClock, poll: timedelta = timedelta(seconds=30)) -> SleepingResetScheduler:
    return SleepingResetScheduler(poll_interval=poll, clock=clock, sleep=clock.sleep)


def test_a_past_reset_time_does_not_sleep() -> None:
    clock = FakeClock()

    _scheduler(clock).wait_until(START - timedelta(minutes=1))

    assert clock.slept == []


def test_waiting_advances_to_the_reset_time() -> None:
    clock = FakeClock()

    _scheduler(clock).wait_until(START + timedelta(minutes=2))

    assert clock.now >= START + timedelta(minutes=2)


def test_the_wait_is_split_into_short_slices() -> None:
    """Long single sleeps oversleep when a laptop suspends mid-wait."""
    clock = FakeClock()

    _scheduler(clock, poll=timedelta(seconds=30)).wait_until(START + timedelta(minutes=2))

    assert clock.slept == [30, 30, 30, 30]


def test_the_last_slice_does_not_overshoot() -> None:
    clock = FakeClock()

    _scheduler(clock, poll=timedelta(seconds=30)).wait_until(START + timedelta(seconds=40))

    assert clock.slept == [30, 10]
    assert clock.now == START + timedelta(seconds=40)


def test_a_cancelled_wait_raises_rather_than_looking_like_it_elapsed() -> None:
    """Returning quietly would make the caller resume before the limit lifts."""
    clock = FakeClock()
    scheduler = _scheduler(clock)
    scheduler.cancel()

    with pytest.raises(WaitInterruptedError):
        scheduler.wait_until(START + timedelta(hours=1))

    assert clock.slept == []


def test_cancelling_stops_a_wait_that_is_already_under_way() -> None:
    """Stands in for another thread calling cancel() mid-sleep."""
    clock = FakeClock()
    started: list[SleepingResetScheduler] = []

    def sleep_then_cancel(seconds: float) -> None:
        clock.sleep(seconds)
        started[0].cancel()

    scheduler = SleepingResetScheduler(
        poll_interval=timedelta(seconds=30), clock=clock, sleep=sleep_then_cancel
    )
    started.append(scheduler)

    with pytest.raises(WaitInterruptedError):
        scheduler.wait_until(START + timedelta(hours=1))

    assert clock.slept == [30]
