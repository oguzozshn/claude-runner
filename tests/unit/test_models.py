"""Domain model behaviour."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from claude_runner.domain.enums import AttemptResult, SessionStatus, TaskStatus
from claude_runner.domain.exceptions import DomainValidationError
from claude_runner.domain.models import (
    MAX_GENERATED_NAME_LENGTH,
    ExecutionAttempt,
    Session,
    Task,
    UsageLimit,
)


def test_task_is_created_pending_with_a_name_derived_from_the_prompt() -> None:
    task = Task.create(prompt="Create a hello world\nthen stop", working_directory=Path())

    assert task.status is TaskStatus.PENDING
    assert task.name == "Create a hello world"


def test_task_name_is_truncated() -> None:
    task = Task.create(prompt="x" * 200, working_directory=Path())

    assert len(task.name) == MAX_GENERATED_NAME_LENGTH


def test_explicit_task_name_wins() -> None:
    task = Task.create(prompt="anything", working_directory=Path(), name="nightly")

    assert task.name == "nightly"


def test_blank_prompt_is_rejected() -> None:
    with pytest.raises(DomainValidationError):
        Task.create(prompt="   \n ", working_directory=Path())


def test_task_status_change_does_not_mutate_the_original() -> None:
    task = Task.create(prompt="work", working_directory=Path())

    running = task.with_status(TaskStatus.RUNNING)

    assert running.status is TaskStatus.RUNNING
    assert task.status is TaskStatus.PENDING
    assert running.id == task.id


def test_session_starts_in_starting_status() -> None:
    session = Session.start(uuid4())

    assert session.status is SessionStatus.STARTING
    assert session.completed_at is None
    assert session.last_activity_at == session.started_at


def test_terminal_session_status_stamps_completed_at() -> None:
    session = Session.start(uuid4())

    running = session.with_status(SessionStatus.RUNNING)
    completed = running.with_status(SessionStatus.COMPLETED)

    assert running.completed_at is None
    assert completed.completed_at is not None


def test_waiting_for_reset_is_not_terminal() -> None:
    running = Session.start(uuid4()).with_status(SessionStatus.RUNNING)

    session = running.with_status(SessionStatus.WAITING_FOR_RESET)

    assert session.completed_at is None


def test_claude_session_id_can_be_attached_later() -> None:
    session = Session.start(uuid4()).with_claude_session_id("abc-123")

    assert session.claude_session_id == "abc-123"


def test_attempt_records_how_it_ended() -> None:
    attempt = ExecutionAttempt.begin(uuid4())

    finished = attempt.finish(AttemptResult.USAGE_LIMIT, exit_code=1)

    assert attempt.result is None
    assert finished.result is AttemptResult.USAGE_LIMIT
    assert finished.exit_code == 1
    assert finished.ended_at is not None


def test_usage_limit_records_detection_and_reset_times() -> None:
    reset_at = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    session_id = uuid4()

    usage_limit = UsageLimit.detected(session_id, reset_at=reset_at)

    assert usage_limit.session_id == session_id
    assert usage_limit.reset_at == reset_at
    assert usage_limit.detected_at.tzinfo is not None
