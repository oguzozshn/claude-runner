"""The session state machine.

Illegal transitions are rejected rather than quietly accepted, so a bug in the
orchestration shows up where it happens instead of as a nonsensical status in
the database days later.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from claude_runner.domain.enums import SessionStatus
from claude_runner.domain.exceptions import IllegalSessionTransitionError
from claude_runner.domain.models import Session

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=2)

LEGAL = [
    (SessionStatus.STARTING, SessionStatus.RUNNING),
    (SessionStatus.STARTING, SessionStatus.RESUMING),
    (SessionStatus.RUNNING, SessionStatus.WAITING_FOR_RESET),
    (SessionStatus.RUNNING, SessionStatus.COMPLETED),
    (SessionStatus.RUNNING, SessionStatus.FAILED),
    (SessionStatus.RUNNING, SessionStatus.STOPPED),
    (SessionStatus.WAITING_FOR_RESET, SessionStatus.RESUMING),
    (SessionStatus.WAITING_FOR_RESET, SessionStatus.STOPPED),
    (SessionStatus.RESUMING, SessionStatus.RUNNING),
    (SessionStatus.RESUMING, SessionStatus.FAILED),
]

ILLEGAL = [
    (SessionStatus.STARTING, SessionStatus.COMPLETED),
    (SessionStatus.STARTING, SessionStatus.WAITING_FOR_RESET),
    (SessionStatus.RUNNING, SessionStatus.RESUMING),
    (SessionStatus.WAITING_FOR_RESET, SessionStatus.RUNNING),
    (SessionStatus.WAITING_FOR_RESET, SessionStatus.COMPLETED),
    (SessionStatus.RESUMING, SessionStatus.WAITING_FOR_RESET),
    (SessionStatus.COMPLETED, SessionStatus.RUNNING),
    (SessionStatus.COMPLETED, SessionStatus.RESUMING),
    (SessionStatus.FAILED, SessionStatus.RUNNING),
    (SessionStatus.STOPPED, SessionStatus.RESUMING),
]


def _session_in(status: SessionStatus) -> Session:
    """Build a session directly in a status, bypassing the transition rules."""
    return Session(
        id=uuid4(),
        task_id=uuid4(),
        status=status,
        started_at=NOW,
        claude_session_id="agent-1",
    )


@pytest.mark.parametrize(("start", "target"), LEGAL)
def test_expected_transitions_are_allowed(start: SessionStatus, target: SessionStatus) -> None:
    session = _session_in(start)

    assert session.can_become(target)
    assert session.with_status(target).status is target


@pytest.mark.parametrize(("start", "target"), ILLEGAL)
def test_impossible_transitions_are_rejected(start: SessionStatus, target: SessionStatus) -> None:
    session = _session_in(start)

    assert not session.can_become(target)
    with pytest.raises(IllegalSessionTransitionError):
        session.with_status(target)


def test_a_finished_session_can_go_nowhere() -> None:
    for status in (SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.STOPPED):
        session = _session_in(status)
        assert session.is_finished
        assert not any(session.can_become(target) for target in SessionStatus)


def test_a_usage_limit_parks_the_session_and_records_the_reset_together() -> None:
    session = _session_in(SessionStatus.RUNNING)

    parked = session.with_usage_limit(LATER, at=NOW)

    assert parked.status is SessionStatus.WAITING_FOR_RESET
    assert parked.reset_at == LATER
    assert parked.completed_at is None


def test_a_usage_limit_cannot_park_a_session_that_is_not_running() -> None:
    with pytest.raises(IllegalSessionTransitionError):
        _session_in(SessionStatus.COMPLETED).with_usage_limit(LATER)


def test_a_future_reset_means_the_limit_is_still_active() -> None:
    parked = _session_in(SessionStatus.RUNNING).with_usage_limit(LATER, at=NOW)

    assert parked.limit_is_active(now=NOW)


def test_a_past_reset_means_the_limit_has_lifted() -> None:
    parked = _session_in(SessionStatus.RUNNING).with_usage_limit(LATER, at=NOW)

    assert not parked.limit_is_active(now=LATER + timedelta(seconds=1))


def test_a_session_that_never_hit_a_limit_has_none_active() -> None:
    assert not _session_in(SessionStatus.RUNNING).limit_is_active(now=NOW)
