"""Immutable domain models.

Every model is a frozen dataclass: state changes return a new instance instead
of mutating in place, which keeps persistence trivial (a model is just a value)
and makes concurrent access safe by construction.

This module intentionally imports from the standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from claude_runner.domain.enums import AttemptResult, SessionStatus, TaskStatus
from claude_runner.domain.exceptions import (
    DomainValidationError,
    IllegalSessionTransitionError,
)

MAX_GENERATED_NAME_LENGTH = 60

TERMINAL_SESSION_STATUSES = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.STOPPED}
)

ALLOWED_SESSION_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.STARTING: frozenset(
        {
            SessionStatus.RUNNING,
            # An adopted conversation is created in STARTING but its history
            # already lives on the agent side, so it resumes rather than starts.
            SessionStatus.RESUMING,
            SessionStatus.FAILED,
            SessionStatus.STOPPED,
        }
    ),
    SessionStatus.RUNNING: frozenset(
        {
            SessionStatus.WAITING_FOR_RESET,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.STOPPED,
        }
    ),
    SessionStatus.WAITING_FOR_RESET: frozenset(
        {SessionStatus.RESUMING, SessionStatus.STOPPED, SessionStatus.FAILED}
    ),
    SessionStatus.RESUMING: frozenset(
        {SessionStatus.RUNNING, SessionStatus.FAILED, SessionStatus.STOPPED}
    ),
    SessionStatus.COMPLETED: frozenset(),
    SessionStatus.FAILED: frozenset(),
    SessionStatus.STOPPED: frozenset(),
}
"""Which status may follow which. Anything absent is rejected, not ignored."""


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def derive_task_name(prompt: str) -> str:
    """Build a short, human-readable task name from a prompt."""
    first_line = prompt.strip().splitlines()[0].strip()
    return first_line[:MAX_GENERATED_NAME_LENGTH]


@dataclass(frozen=True, slots=True)
class Task:
    """A unit of work the user wants the coding agent to perform."""

    id: UUID
    name: str
    prompt: str
    working_directory: Path
    status: TaskStatus
    created_at: datetime

    @classmethod
    def create(
        cls,
        prompt: str,
        working_directory: Path,
        name: str | None = None,
        created_at: datetime | None = None,
    ) -> Task:
        """Create a pending task, deriving a name from the prompt when absent."""
        if not prompt.strip():
            raise DomainValidationError("A task prompt cannot be empty.")
        return cls(
            id=uuid4(),
            name=name or derive_task_name(prompt),
            prompt=prompt,
            working_directory=working_directory,
            status=TaskStatus.PENDING,
            created_at=created_at or utc_now(),
        )

    def with_status(self, status: TaskStatus) -> Task:
        """Return a copy of this task in a new status."""
        return replace(self, status=status)


@dataclass(frozen=True, slots=True)
class Session:
    """One agent conversation working on a task, possibly across several runs."""

    id: UUID
    task_id: UUID
    status: SessionStatus
    started_at: datetime
    claude_session_id: str | None = None
    last_activity_at: datetime | None = None
    completed_at: datetime | None = None
    reset_at: datetime | None = None
    """When the usage limit that interrupted this session lifts.

    Stored on the session itself so a later process can decide whether to
    resume immediately or wait, reading one row instead of replaying history.
    """

    @classmethod
    def start(
        cls,
        task_id: UUID,
        started_at: datetime | None = None,
        claude_session_id: str | None = None,
    ) -> Session:
        """Create a session that is about to be handed to the agent.

        The runner picks the agent-side id instead of discovering it after the
        fact, so the conversation is resumable from the very first moment. Pass
        ``claude_session_id`` to adopt a conversation that already exists.
        """
        moment = started_at or utc_now()
        session_id = uuid4()
        return cls(
            id=session_id,
            task_id=task_id,
            status=SessionStatus.STARTING,
            started_at=moment,
            last_activity_at=moment,
            claude_session_id=claude_session_id or str(session_id),
        )

    @property
    def is_waiting_for_reset(self) -> bool:
        """Whether this session stopped at a usage limit and can be resumed."""
        return self.status is SessionStatus.WAITING_FOR_RESET

    @property
    def is_finished(self) -> bool:
        """Whether this session has reached a status it cannot leave."""
        return self.status in TERMINAL_SESSION_STATUSES

    def limit_is_active(self, now: datetime | None = None) -> bool:
        """Whether a recorded usage limit has still not lifted."""
        if self.reset_at is None:
            return False
        return self.reset_at > (now or utc_now())

    def can_become(self, status: SessionStatus) -> bool:
        """Whether ``status`` is reachable from the current one."""
        return status in ALLOWED_SESSION_TRANSITIONS[self.status]

    def with_status(self, status: SessionStatus, at: datetime | None = None) -> Session:
        """Return a copy in a new status.

        Raises rather than quietly accepting a transition the lifecycle does
        not allow, so a bug in the orchestration surfaces where it happens.
        """
        if not self.can_become(status):
            raise IllegalSessionTransitionError(
                f"Session {self.id} cannot move from {self.status} to {status}."
            )
        moment = at or utc_now()
        completed_at = moment if status in TERMINAL_SESSION_STATUSES else self.completed_at
        return replace(self, status=status, last_activity_at=moment, completed_at=completed_at)

    def with_usage_limit(self, reset_at: datetime, at: datetime | None = None) -> Session:
        """Park this session until ``reset_at``, recording both facts together."""
        return replace(self.with_status(SessionStatus.WAITING_FOR_RESET, at=at), reset_at=reset_at)

    def with_claude_session_id(self, claude_session_id: str, at: datetime | None = None) -> Session:
        """Attach the agent-side session id used to resume this conversation."""
        return replace(
            self,
            claude_session_id=claude_session_id,
            last_activity_at=at or utc_now(),
        )


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    """A single execution of the agent inside a session."""

    id: UUID
    session_id: UUID
    started_at: datetime
    ended_at: datetime | None = None
    exit_code: int | None = None
    result: AttemptResult | None = None

    @classmethod
    def begin(cls, session_id: UUID, started_at: datetime | None = None) -> ExecutionAttempt:
        """Create an attempt that has started but not finished yet."""
        return cls(id=uuid4(), session_id=session_id, started_at=started_at or utc_now())

    def finish(
        self,
        result: AttemptResult,
        exit_code: int | None = None,
        ended_at: datetime | None = None,
    ) -> ExecutionAttempt:
        """Return a copy of this attempt marked as finished."""
        return replace(self, result=result, exit_code=exit_code, ended_at=ended_at or utc_now())


@dataclass(frozen=True, slots=True)
class UsageLimit:
    """A usage limit observed during a session, plus when it resets."""

    id: UUID
    session_id: UUID
    detected_at: datetime
    reset_at: datetime

    @classmethod
    def detected(
        cls, session_id: UUID, reset_at: datetime, detected_at: datetime | None = None
    ) -> UsageLimit:
        """Record a usage limit that was just observed."""
        return cls(
            id=uuid4(),
            session_id=session_id,
            detected_at=detected_at or utc_now(),
            reset_at=reset_at,
        )


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """What an agent client reports back after one execution.

    This is the abstraction that keeps process details out of the application
    layer: ``exit_code`` is optional context, ``result`` is the decision.
    """

    result: AttemptResult
    exit_code: int | None = None
    claude_session_id: str | None = None
    detail: str | None = None
    reset_at: datetime | None = None
    """When the usage limit lifts. Only set when ``result`` is USAGE_LIMIT."""


@dataclass(frozen=True, slots=True)
class AgentSession:
    """A conversation the agent stored, discovered rather than started here.

    This is how a session created outside the runner — by hand in Claude Code —
    becomes resumable: all ``--resume`` needs is the id.
    """

    claude_session_id: str
    working_directory: Path | None = None
    title: str | None = None
    last_activity_at: datetime | None = None
