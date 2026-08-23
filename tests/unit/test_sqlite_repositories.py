"""Round-tripping domain models through SQLite."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from claude_runner.domain.enums import AttemptResult, SessionStatus, TaskStatus
from claude_runner.domain.models import ExecutionAttempt, Session, Task, UsageLimit
from claude_runner.infrastructure.persistence.sqlite import (
    Database,
    SqliteAttemptRepository,
    SqliteSessionRepository,
    SqliteTaskRepository,
    SqliteUsageLimitRepository,
)

RESET_AT = datetime(2026, 8, 23, 18, 0, tzinfo=UTC)


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database(":memory:")
    yield db
    db.close()


def _running(task_id: UUID | None = None) -> Session:
    """A session past STARTING, so limit and completion transitions are legal."""
    return Session.start(task_id or uuid4()).with_status(SessionStatus.RUNNING)


def _task() -> Task:
    return Task.create(prompt="Do the thing", working_directory=Path("/workspace"))


def test_a_task_survives_a_round_trip(database: Database) -> None:
    tasks = SqliteTaskRepository(database)
    task = _task()

    tasks.save(task)
    loaded = tasks.get(task.id)

    assert loaded == task


def test_saving_a_task_twice_updates_it(database: Database) -> None:
    tasks = SqliteTaskRepository(database)
    task = _task()
    tasks.save(task)

    tasks.save(task.with_status(TaskStatus.COMPLETED))

    assert len(tasks.list()) == 1
    loaded = tasks.get(task.id)
    assert loaded is not None
    assert loaded.status is TaskStatus.COMPLETED


def test_a_session_survives_a_round_trip(database: Database) -> None:
    sessions = SqliteSessionRepository(database)
    session = Session.start(uuid4())

    sessions.save(session)

    assert sessions.get(session.id) == session


def test_a_completed_session_is_not_active(database: Database) -> None:
    sessions = SqliteSessionRepository(database)
    waiting = _running().with_status(SessionStatus.WAITING_FOR_RESET)
    done = _running().with_status(SessionStatus.COMPLETED)
    sessions.save(waiting)
    sessions.save(done)

    active = sessions.get_active()

    assert [session.id for session in active] == [waiting.id]


def test_a_session_can_be_found_by_its_agent_id(database: Database) -> None:
    sessions = SqliteSessionRepository(database)
    session = Session.start(uuid4(), claude_session_id="external-1")
    sessions.save(session)

    found = sessions.find_by_claude_session_id("external-1")

    assert found is not None
    assert found.id == session.id


def test_an_unknown_agent_id_returns_nothing(database: Database) -> None:
    assert SqliteSessionRepository(database).find_by_claude_session_id("nope") is None


def test_attempts_are_listed_for_their_session(database: Database) -> None:
    attempts = SqliteAttemptRepository(database)
    session_id = uuid4()
    first = ExecutionAttempt.begin(session_id).finish(AttemptResult.USAGE_LIMIT, exit_code=1)
    second = ExecutionAttempt.begin(session_id).finish(AttemptResult.SUCCESS, exit_code=0)
    attempts.save(first)
    attempts.save(second)
    attempts.save(ExecutionAttempt.begin(uuid4()))

    stored = attempts.list_for_session(session_id)

    assert {attempt.result for attempt in stored} == {
        AttemptResult.USAGE_LIMIT,
        AttemptResult.SUCCESS,
    }


def test_an_unfinished_attempt_round_trips_with_empty_fields(database: Database) -> None:
    attempts = SqliteAttemptRepository(database)
    attempt = ExecutionAttempt.begin(uuid4())

    attempts.save(attempt)
    loaded = attempts.get(attempt.id)

    assert loaded is not None
    assert loaded.result is None
    assert loaded.ended_at is None
    assert loaded.exit_code is None


def test_usage_limits_are_listed_for_their_session(database: Database) -> None:
    limits = SqliteUsageLimitRepository(database)
    session_id = uuid4()
    limits.save(UsageLimit.detected(session_id, reset_at=RESET_AT))

    stored = limits.list_for_session(session_id)

    assert [limit.reset_at for limit in stored] == [RESET_AT]


def test_state_outlives_the_connection(tmp_path: Path) -> None:
    """The whole point of persistence: a later process can pick the work up."""
    path = tmp_path / "runner.db"
    task = _task()
    session = Session.start(task.id).with_status(SessionStatus.RUNNING).with_usage_limit(RESET_AT)

    with Database(path) as first:
        SqliteTaskRepository(first).save(task)
        SqliteSessionRepository(first).save(session)

    with Database(path) as second:
        reopened = SqliteSessionRepository(second).get_active()
        assert [s.id for s in reopened] == [session.id]
        assert SqliteTaskRepository(second).get(task.id) == task


def test_the_reset_time_survives_a_round_trip(database: Database) -> None:
    """Recovery in a later process depends on this one column."""
    sessions = SqliteSessionRepository(database)
    parked = _running().with_usage_limit(RESET_AT)

    sessions.save(parked)
    loaded = sessions.get(parked.id)

    assert loaded is not None
    assert loaded.reset_at == RESET_AT
    assert loaded.limit_is_active(now=RESET_AT - timedelta(minutes=1))
    assert not loaded.limit_is_active(now=RESET_AT + timedelta(minutes=1))


def test_a_database_from_before_the_reset_column_is_migrated(tmp_path: Path) -> None:
    """An older build wrote sessions without reset_at; opening must not lose them."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(str(path)) as legacy:
        legacy.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, claude_session_id TEXT,
                status TEXT NOT NULL, started_at TEXT NOT NULL,
                last_activity_at TEXT, completed_at TEXT
            )
            """
        )
        legacy.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid4()), str(uuid4()), "agent-1", "running", RESET_AT.isoformat(), None, None),
        )

    with Database(path) as migrated:
        stored = SqliteSessionRepository(migrated).list()

    assert len(stored) == 1
    assert stored[0].reset_at is None
