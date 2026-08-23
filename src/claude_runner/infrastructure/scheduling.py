"""Waiting for a usage limit to reset."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from claude_runner.domain.exceptions import WaitInterruptedError

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = timedelta(seconds=30)


class SleepingResetScheduler:
    """Blocks until the reset time, in short slices, and can be cancelled.

    Short slices rather than one long sleep because the wait can last hours: a
    laptop may suspend, and the process should notice the world has moved on
    instead of oversleeping by however long the machine was away. Slicing also
    keeps ``Ctrl+C`` responsive.

    Waiting is done on an :class:`threading.Event` so another thread can cut
    the wait short with :meth:`cancel`. Session state is always persisted
    before a wait begins, so being interrupted here loses nothing.
    """

    def __init__(
        self,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._poll_interval = poll_interval
        self._clock = clock
        self._cancelled = threading.Event()
        self._sleep = sleep or self._sleep_unless_cancelled

    def cancel(self) -> None:
        """Ask an in-progress wait to give up."""
        self._cancelled.set()

    def wait_until(self, reset_at: datetime) -> None:
        """Block until ``reset_at``. Return immediately if it has passed.

        Raises :class:`WaitInterruptedError` if the wait is cancelled, so the
        caller never mistakes an aborted wait for an elapsed one.
        """
        remaining = reset_at - self._clock()
        if remaining <= timedelta(0):
            return

        logger.info("Waiting %s until the usage limit resets at %s", remaining, reset_at)
        while True:
            if self._cancelled.is_set():
                raise WaitInterruptedError("Waiting for the usage limit reset was cancelled.")
            remaining = reset_at - self._clock()
            if remaining <= timedelta(0):
                return
            self._sleep(min(remaining, self._poll_interval).total_seconds())

    def _sleep_unless_cancelled(self, seconds: float) -> None:
        self._cancelled.wait(seconds)
