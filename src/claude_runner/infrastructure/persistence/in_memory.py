"""In-memory repositories.

State lives for the duration of one process, which is exactly what the current
single-run CLI needs. They exist so the wiring is real today; the SQLite
implementations will replace them without touching the layers above.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from claude_runner.domain.models import (
    TERMINAL_SESSION_STATUSES,
    ExecutionAttempt,
    Session,
    Task,
    UsageLimit,
)


class InMemoryTaskRepository:
    """Stores tasks in a dictionary keyed by task id."""

    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}

    def save(self, task: Task) -> None:
        self._tasks[task.id] = task

    def get(self, task_id: UUID) -> Task | None:
        return self._tasks.get(task_id)

    def list(self) -> Sequence[Task]:
        return tuple(self._tasks.values())


class InMemorySessionRepository:
    """Stores sessions in a dictionary keyed by session id."""

    def __init__(self) -> None:
        self._sessions: dict[UUID, Session] = {}

    def save(self, session: Session) -> None:
        self._sessions[session.id] = session

    def get(self, session_id: UUID) -> Session | None:
        return self._sessions.get(session_id)

    def get_active(self) -> Sequence[Session]:
        return tuple(
            session
            for session in self._sessions.values()
            if session.status not in TERMINAL_SESSION_STATUSES
        )

    def list(self) -> Sequence[Session]:
        return tuple(
            sorted(self._sessions.values(), key=lambda session: session.started_at, reverse=True)
        )

    def find_by_claude_session_id(self, claude_session_id: str) -> Session | None:
        for session in self.list():
            if session.claude_session_id == claude_session_id:
                return session
        return None

    def update(self, session: Session) -> None:
        self._sessions[session.id] = session


class InMemoryAttemptRepository:
    """Stores execution attempts in a dictionary keyed by attempt id."""

    def __init__(self) -> None:
        self._attempts: dict[UUID, ExecutionAttempt] = {}

    def save(self, attempt: ExecutionAttempt) -> None:
        self._attempts[attempt.id] = attempt

    def get(self, attempt_id: UUID) -> ExecutionAttempt | None:
        return self._attempts.get(attempt_id)

    def list_for_session(self, session_id: UUID) -> Sequence[ExecutionAttempt]:
        return tuple(
            attempt for attempt in self._attempts.values() if attempt.session_id == session_id
        )


class InMemoryUsageLimitRepository:
    """Stores observed usage limits in insertion order."""

    def __init__(self) -> None:
        self._usage_limits: list[UsageLimit] = []

    def save(self, usage_limit: UsageLimit) -> None:
        self._usage_limits.append(usage_limit)

    def list_for_session(self, session_id: UUID) -> Sequence[UsageLimit]:
        return tuple(limit for limit in self._usage_limits if limit.session_id == session_id)


__all__ = [
    "InMemoryAttemptRepository",
    "InMemorySessionRepository",
    "InMemoryTaskRepository",
    "InMemoryUsageLimitRepository",
]
