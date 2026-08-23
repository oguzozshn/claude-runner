"""Ports (Protocols) the outer layers must implement.

Application services depend on these names only; concrete adapters live in
``claude_runner.infrastructure`` and are wired together in the CLI layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from claude_runner.domain.models import (
    AgentSession,
    ExecutionAttempt,
    ExecutionOutcome,
    Session,
    Task,
    UsageLimit,
)


class OutputSink(Protocol):
    """Receives agent output one line at a time, as it is produced.

    The CLI renders these lines; a future usage-limit detector will observe the
    same stream. Neither concern belongs to the application layer.
    """

    def write(self, line: str) -> None:
        """Handle a single line of agent output, without its trailing newline."""
        ...


class RunObserver(Protocol):
    """Told when the run moves through its lifecycle.

    Separate from ``OutputSink``, which carries the agent's words: this carries
    the runner's own. Both exist so the application layer can narrate progress
    without deciding how — or whether — it is displayed.
    """

    def session_starting(self, session: Session, task: Task) -> None: ...

    def session_resuming(self, session: Session, task: Task) -> None: ...

    def usage_limit_reached(self, session: Session, reset_at: datetime) -> None: ...

    def waiting_for_reset(self, reset_at: datetime) -> None: ...

    def reset_reached(self) -> None: ...


class ClaudeClient(Protocol):
    """Drives a coding agent. Blocks until the agent stops."""

    def start(self, session: Session, task: Task, output: OutputSink) -> ExecutionOutcome:
        """Run ``task`` in a fresh agent session.

        The session id is chosen by the runner, not by the agent, so that the
        conversation can be resumed later even if the first run is cut short.
        """
        ...

    def resume(
        self,
        session: Session,
        task: Task,
        output: OutputSink,
        prompt: str | None = None,
    ) -> ExecutionOutcome:
        """Continue an existing agent session.

        ``task`` supplies the working directory that a session alone does not
        carry. ``prompt`` is a new instruction for the conversation; without
        one the agent is simply asked to carry on where it stopped.
        """
        ...

    def stop(self, session: Session) -> None:
        """Ask a running session to terminate. A no-op if it is not running."""
        ...


class TaskRepository(Protocol):
    """Persists tasks. ``save`` is an upsert keyed by the task id."""

    def save(self, task: Task) -> None: ...

    def get(self, task_id: UUID) -> Task | None: ...

    def list(self) -> Sequence[Task]: ...


class SessionRepository(Protocol):
    """Persists sessions. ``save`` is an upsert keyed by the session id."""

    def save(self, session: Session) -> None: ...

    def get(self, session_id: UUID) -> Session | None: ...

    def get_active(self) -> Sequence[Session]:
        """Return sessions that have not reached a terminal status."""
        ...

    def list(self) -> Sequence[Session]:
        """Return every stored session, newest first."""
        ...

    def find_by_claude_session_id(self, claude_session_id: str) -> Session | None:
        """Look up a session by the id the agent knows it as."""
        ...

    def update(self, session: Session) -> None:
        """Persist a state change of an already saved session."""
        ...


class AttemptRepository(Protocol):
    """Persists execution attempts. ``save`` is an upsert."""

    def save(self, attempt: ExecutionAttempt) -> None: ...

    def get(self, attempt_id: UUID) -> ExecutionAttempt | None: ...

    def list_for_session(self, session_id: UUID) -> Sequence[ExecutionAttempt]: ...


class UsageLimitRepository(Protocol):
    """Persists observed usage limits."""

    def save(self, usage_limit: UsageLimit) -> None: ...

    def list_for_session(self, session_id: UUID) -> Sequence[UsageLimit]: ...


class AgentSessionStore(Protocol):
    """Reads the conversations the agent itself keeps, wherever it keeps them."""

    def discover(self, limit: int | None = None) -> Sequence[AgentSession]:
        """Return stored conversations, most recently active first."""
        ...


class ResetScheduler(Protocol):
    """Waits until a usage limit resets."""

    def wait_until(self, reset_at: datetime) -> None:
        """Block until ``reset_at`` has passed. Return immediately if it has."""
        ...
