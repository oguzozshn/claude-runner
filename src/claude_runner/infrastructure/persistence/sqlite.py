"""SQLite repositories.

State has to outlive the process: a usage limit can lift hours after the run
that hit it, quite possibly after the terminal is closed. Everything the runner
needs to pick a task back up is written here.

Timestamps are stored as ISO-8601 strings and enums as their string values, so
the database stays readable with any SQLite browser.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType
from uuid import UUID

from claude_runner.domain.enums import AttemptResult, SessionStatus, TaskStatus
from claude_runner.domain.models import ExecutionAttempt, Session, Task, UsageLimit

DEFAULT_DATABASE_PATH = Path.home() / ".claude-runner" / "runner.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    working_directory TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    claude_session_id TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_activity_at TEXT,
    completed_at TEXT,
    reset_at TEXT
);

CREATE INDEX IF NOT EXISTS sessions_by_claude_id
    ON sessions (claude_session_id);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    exit_code INTEGER,
    result TEXT
);

CREATE INDEX IF NOT EXISTS attempts_by_session ON attempts (session_id);

CREATE TABLE IF NOT EXISTS usage_limits (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    reset_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS usage_limits_by_session ON usage_limits (session_id);
"""


class Database:
    """Owns the connection and the schema.

    Pass ``":memory:"`` for tests.
    """

    def __init__(self, path: Path | str = DEFAULT_DATABASE_PATH) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._migrate()
        self._connection.commit()

    def _migrate(self) -> None:
        """Add columns that later versions introduced.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so a
        database written by an earlier build would silently lack the newer
        columns and lose the state that recovery depends on.
        """
        for table, column, declaration in ADDED_COLUMNS:
            if not self._has_column(table, column):
                self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _has_column(self, table: str, column: str) -> bool:
        rows = self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class SqliteTaskRepository:
    """Stores tasks."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def save(self, task: Task) -> None:
        with self._db.connection as connection:
            connection.execute(
                """
                INSERT INTO tasks (id, name, prompt, working_directory, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    prompt = excluded.prompt,
                    working_directory = excluded.working_directory,
                    status = excluded.status
                """,
                (
                    str(task.id),
                    task.name,
                    task.prompt,
                    str(task.working_directory),
                    task.status.value,
                    task.created_at.isoformat(),
                ),
            )

    def get(self, task_id: UUID) -> Task | None:
        row = self._db.connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (str(task_id),)
        ).fetchone()
        return _to_task(row) if row else None

    def list(self) -> Sequence[Task]:
        rows = self._db.connection.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC"
        ).fetchall()
        return tuple(_to_task(row) for row in rows)


class SqliteSessionRepository:
    """Stores sessions."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def save(self, session: Session) -> None:
        with self._db.connection as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, task_id, claude_session_id, status,
                    started_at, last_activity_at, completed_at, reset_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    claude_session_id = excluded.claude_session_id,
                    status = excluded.status,
                    last_activity_at = excluded.last_activity_at,
                    completed_at = excluded.completed_at,
                    reset_at = excluded.reset_at
                """,
                (
                    str(session.id),
                    str(session.task_id),
                    session.claude_session_id,
                    session.status.value,
                    session.started_at.isoformat(),
                    _iso_or_none(session.last_activity_at),
                    _iso_or_none(session.completed_at),
                    _iso_or_none(session.reset_at),
                ),
            )

    def get(self, session_id: UUID) -> Session | None:
        row = self._db.connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (str(session_id),)
        ).fetchone()
        return _to_session(row) if row else None

    def get_active(self) -> Sequence[Session]:
        placeholders = ", ".join("?" for _ in TERMINAL_STATUS_VALUES)
        rows = self._db.connection.execute(
            f"SELECT * FROM sessions WHERE status NOT IN ({placeholders}) ORDER BY started_at DESC",
            TERMINAL_STATUS_VALUES,
        ).fetchall()
        return tuple(_to_session(row) for row in rows)

    def list(self) -> Sequence[Session]:
        rows = self._db.connection.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC"
        ).fetchall()
        return tuple(_to_session(row) for row in rows)

    def find_by_claude_session_id(self, claude_session_id: str) -> Session | None:
        row = self._db.connection.execute(
            "SELECT * FROM sessions WHERE claude_session_id = ? ORDER BY started_at DESC LIMIT 1",
            (claude_session_id,),
        ).fetchone()
        return _to_session(row) if row else None

    def update(self, session: Session) -> None:
        self.save(session)


class SqliteAttemptRepository:
    """Stores execution attempts."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def save(self, attempt: ExecutionAttempt) -> None:
        with self._db.connection as connection:
            connection.execute(
                """
                INSERT INTO attempts (id, session_id, started_at, ended_at, exit_code, result)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    ended_at = excluded.ended_at,
                    exit_code = excluded.exit_code,
                    result = excluded.result
                """,
                (
                    str(attempt.id),
                    str(attempt.session_id),
                    attempt.started_at.isoformat(),
                    _iso_or_none(attempt.ended_at),
                    attempt.exit_code,
                    attempt.result.value if attempt.result else None,
                ),
            )

    def get(self, attempt_id: UUID) -> ExecutionAttempt | None:
        row = self._db.connection.execute(
            "SELECT * FROM attempts WHERE id = ?", (str(attempt_id),)
        ).fetchone()
        return _to_attempt(row) if row else None

    def list_for_session(self, session_id: UUID) -> Sequence[ExecutionAttempt]:
        rows = self._db.connection.execute(
            "SELECT * FROM attempts WHERE session_id = ? ORDER BY started_at",
            (str(session_id),),
        ).fetchall()
        return tuple(_to_attempt(row) for row in rows)


class SqliteUsageLimitRepository:
    """Stores observed usage limits."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def save(self, usage_limit: UsageLimit) -> None:
        with self._db.connection as connection:
            connection.execute(
                """
                INSERT INTO usage_limits (id, session_id, detected_at, reset_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET reset_at = excluded.reset_at
                """,
                (
                    str(usage_limit.id),
                    str(usage_limit.session_id),
                    usage_limit.detected_at.isoformat(),
                    usage_limit.reset_at.isoformat(),
                ),
            )

    def list_for_session(self, session_id: UUID) -> Sequence[UsageLimit]:
        rows = self._db.connection.execute(
            "SELECT * FROM usage_limits WHERE session_id = ? ORDER BY detected_at",
            (str(session_id),),
        ).fetchall()
        return tuple(_to_usage_limit(row) for row in rows)


ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (("sessions", "reset_at", "TEXT"),)
"""Columns introduced after the first release, applied to existing databases."""

TERMINAL_STATUS_VALUES = (
    SessionStatus.COMPLETED.value,
    SessionStatus.FAILED.value,
    SessionStatus.STOPPED.value,
)


def _to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=UUID(row["id"]),
        name=row["name"],
        prompt=row["prompt"],
        working_directory=Path(row["working_directory"]),
        status=TaskStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=UUID(row["id"]),
        task_id=UUID(row["task_id"]),
        claude_session_id=row["claude_session_id"],
        status=SessionStatus(row["status"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        last_activity_at=_parse_or_none(row["last_activity_at"]),
        completed_at=_parse_or_none(row["completed_at"]),
        reset_at=_parse_or_none(row["reset_at"]),
    )


def _to_attempt(row: sqlite3.Row) -> ExecutionAttempt:
    return ExecutionAttempt(
        id=UUID(row["id"]),
        session_id=UUID(row["session_id"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        ended_at=_parse_or_none(row["ended_at"]),
        exit_code=row["exit_code"],
        result=AttemptResult(row["result"]) if row["result"] else None,
    )


def _to_usage_limit(row: sqlite3.Row) -> UsageLimit:
    return UsageLimit(
        id=UUID(row["id"]),
        session_id=UUID(row["session_id"]),
        detected_at=datetime.fromisoformat(row["detected_at"]),
        reset_at=datetime.fromisoformat(row["reset_at"]),
    )


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_or_none(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
