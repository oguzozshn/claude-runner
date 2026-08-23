"""Running a task, and picking it back up after a usage limit."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from claude_runner.domain.enums import AttemptResult, SessionStatus, TaskStatus
from claude_runner.domain.exceptions import (
    ClaudeClientError,
    SessionAlreadyCompletedError,
    SessionNotFoundError,
    UsageLimitActiveError,
)
from claude_runner.domain.interfaces import (
    AgentSessionStore,
    AttemptRepository,
    ClaudeClient,
    OutputSink,
    ResetScheduler,
    RunObserver,
    SessionRepository,
    TaskRepository,
    UsageLimitRepository,
)
from claude_runner.domain.models import (
    ExecutionAttempt,
    ExecutionOutcome,
    Session,
    Task,
    UsageLimit,
    utc_now,
)

logger = logging.getLogger(__name__)

ADOPTED_TASK_NAME = "Adopted session"

_SESSION_STATUS_BY_RESULT: dict[AttemptResult, SessionStatus] = {
    AttemptResult.SUCCESS: SessionStatus.COMPLETED,
    AttemptResult.FAILED: SessionStatus.FAILED,
    AttemptResult.CANCELLED: SessionStatus.STOPPED,
    # A usage limit does not end the session: it is waiting to be resumed.
    AttemptResult.USAGE_LIMIT: SessionStatus.WAITING_FOR_RESET,
}

_TASK_STATUS_BY_RESULT: dict[AttemptResult, TaskStatus] = {
    AttemptResult.SUCCESS: TaskStatus.COMPLETED,
    AttemptResult.FAILED: TaskStatus.FAILED,
    AttemptResult.CANCELLED: TaskStatus.CANCELLED,
    AttemptResult.USAGE_LIMIT: TaskStatus.RUNNING,
}


class NullRunObserver:
    """Ignores lifecycle events. The default when nobody is watching."""

    def session_starting(self, session: Session, task: Task) -> None: ...

    def session_resuming(self, session: Session, task: Task) -> None: ...

    def usage_limit_reached(self, session: Session, reset_at: datetime) -> None: ...

    def waiting_for_reset(self, reset_at: datetime) -> None: ...

    def reset_reached(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Everything the caller needs to provide to start a task."""

    prompt: str
    working_directory: Path
    name: str | None = None
    wait_for_reset: bool = False


@dataclass(frozen=True, slots=True)
class ResumeRequest:
    """Which conversation to continue, and whether to wait for it.

    ``claude_session_id`` may name a conversation this runner never started —
    one begun by hand in Claude Code — which is adopted into a new task.
    ``working_directory`` is only a fallback for that case: a session the
    runner already knows brings its own directory with it.
    """

    session_id: UUID | None = None
    claude_session_id: str | None = None
    working_directory: Path | None = None
    wait_for_reset: bool = False
    prompt: str | None = None
    """A new instruction for the conversation. Without one the agent is asked
    to carry on where it stopped."""


@dataclass(frozen=True, slots=True)
class RunResult:
    """The persisted state of a finished run."""

    task: Task
    session: Session
    attempt: ExecutionAttempt

    @property
    def succeeded(self) -> bool:
        """Whether the agent finished the work without error."""
        return self.attempt.result is AttemptResult.SUCCESS

    @property
    def hit_usage_limit(self) -> bool:
        """Whether the run stopped because the usage limit was reached."""
        return self.attempt.result is AttemptResult.USAGE_LIMIT


class RunnerService:
    """Runs tasks through an agent client and records what happened."""

    def __init__(
        self,
        claude_client: ClaudeClient,
        task_repository: TaskRepository,
        session_repository: SessionRepository,
        attempt_repository: AttemptRepository,
        usage_limit_repository: UsageLimitRepository,
        reset_scheduler: ResetScheduler,
        agent_session_store: AgentSessionStore | None = None,
        observer: RunObserver | None = None,
    ) -> None:
        self._claude = claude_client
        self._tasks = task_repository
        self._sessions = session_repository
        self._attempts = attempt_repository
        self._usage_limits = usage_limit_repository
        self._scheduler = reset_scheduler
        self._agent_sessions = agent_session_store
        self._observer: RunObserver = observer or NullRunObserver()

    def run(self, request: RunRequest, output: OutputSink) -> RunResult:
        """Execute a new task, optionally waiting out any usage limit."""
        task = self._create_task(request)
        session = self._start_session(task)
        self._observer.session_starting(session, task)

        result = self._attempt(task, session, output, resuming=False)
        if request.wait_for_reset:
            result = self._wait_and_continue(result, output, prompt=None)
        return result

    def resume(self, request: ResumeRequest, output: OutputSink) -> RunResult:
        """Continue an interrupted conversation.

        Refuses up front — without spending a call on the agent — when the
        session is finished or its usage limit has not lifted yet.
        """
        session, task = self._locate(request)
        self._ensure_resumable(session)

        reset_at = session.reset_at
        if reset_at is not None and session.limit_is_active():
            session = self._handle_active_limit(session, reset_at, wait=request.wait_for_reset)

        return self._resume_from(
            session, task, output, wait=request.wait_for_reset, prompt=request.prompt
        )

    def latest_resumable(self) -> Session | None:
        """The most recent session that stopped at a usage limit."""
        waiting = [s for s in self._sessions.get_active() if s.is_waiting_for_reset]
        return max(waiting, key=lambda s: s.started_at) if waiting else None

    # ------------------------------------------------------------------ resume

    def _ensure_resumable(self, session: Session) -> None:
        if session.status is SessionStatus.COMPLETED:
            raise SessionAlreadyCompletedError(f"Session '{session.id}' is already completed.")
        if session.is_finished:
            raise SessionAlreadyCompletedError(
                f"Session '{session.id}' has already finished as {session.status}."
            )

    def _handle_active_limit(self, session: Session, reset_at: datetime, *, wait: bool) -> Session:
        """Either wait the limit out, or refuse with the time it lifts."""
        if not wait:
            raise UsageLimitActiveError(
                f"Usage limit is still active. Reset: {reset_at.isoformat()}", reset_at
            )
        self._wait_for(reset_at)
        return session

    def _resume_from(
        self,
        session: Session,
        task: Task,
        output: OutputSink,
        *,
        wait: bool,
        prompt: str | None = None,
    ) -> RunResult:
        session = self._advance(session, SessionStatus.RESUMING)
        self._observer.session_resuming(session, task)
        result = self._attempt(task, session, output, resuming=True, prompt=prompt)
        if wait:
            result = self._wait_and_continue(result, output, prompt=prompt)
        return result

    def _wait_and_continue(
        self, result: RunResult, output: OutputSink, prompt: str | None
    ) -> RunResult:
        """Keep resuming for as long as usage limits keep interrupting.

        ``prompt`` is re-sent on every retry: a message that was refused never
        reached the conversation, so dropping it would silently lose the very
        thing the user queued up.
        """
        while result.hit_usage_limit:
            reset_at = result.session.reset_at
            if reset_at is None:
                logger.warning("No reset time recorded; not waiting.")
                return result

            self._wait_for(reset_at)
            result = self._resume_from(
                result.session, result.task, output, wait=False, prompt=prompt
            )
        return result

    def _wait_for(self, reset_at: datetime) -> None:
        if reset_at <= utc_now():
            return
        self._observer.waiting_for_reset(reset_at)
        self._scheduler.wait_until(reset_at)
        self._observer.reset_reached()

    # ----------------------------------------------------------------- lookups

    def _locate(self, request: ResumeRequest) -> tuple[Session, Task]:
        """Find the session and task to resume, adopting a known agent session."""
        own = self._sessions.get(request.session_id) if request.session_id else None
        if own is not None:
            return self._known_session(own.id)

        identifier = request.claude_session_id
        if identifier is None:
            raise SessionNotFoundError("No session was given to resume.")

        existing = self._sessions.find_by_claude_session_id(identifier)
        if existing is not None:
            return self._known_session(existing.id)

        if not self._agent_knows(identifier):
            raise SessionNotFoundError(f"Session '{identifier}' was not found.")
        return self._adopt(request, identifier)

    def _agent_knows(self, claude_session_id: str) -> bool:
        """Whether the agent has a stored conversation with this id.

        Without this check a typo would be adopted as a brand new session and
        only fail later, inside the agent.
        """
        if self._agent_sessions is None:
            return False
        return any(
            candidate.claude_session_id == claude_session_id
            for candidate in self._agent_sessions.discover()
        )

    def _known_session(self, session_id: UUID) -> tuple[Session, Task]:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"Session '{session_id}' was not found.")
        task = self._tasks.get(session.task_id)
        if task is None:
            raise SessionNotFoundError(f"Session '{session_id}' has no stored task.")
        return session, task

    def _adopt(self, request: ResumeRequest, claude_session_id: str) -> tuple[Session, Task]:
        """Take over a conversation started outside this runner."""
        working_directory = request.working_directory or Path.cwd()
        task = Task.create(
            prompt=ADOPTED_TASK_NAME,
            working_directory=working_directory,
            name=ADOPTED_TASK_NAME,
        ).with_status(TaskStatus.RUNNING)
        self._tasks.save(task)

        session = Session.start(task.id, claude_session_id=claude_session_id)
        self._sessions.save(session)
        logger.info("Adopted agent session %s", claude_session_id)
        return session, task

    # --------------------------------------------------------------- execution

    def _create_task(self, request: RunRequest) -> Task:
        task = Task.create(
            prompt=request.prompt,
            working_directory=request.working_directory,
            name=request.name,
        )
        self._tasks.save(task)
        running = task.with_status(TaskStatus.RUNNING)
        self._tasks.save(running)
        logger.info("Task %s (%s) created", running.id, running.name)
        return running

    def _start_session(self, task: Task) -> Session:
        session = Session.start(task.id)
        self._sessions.save(session)
        return session

    def _attempt(
        self,
        task: Task,
        session: Session,
        output: OutputSink,
        *,
        resuming: bool,
        prompt: str | None = None,
    ) -> RunResult:
        attempt = ExecutionAttempt.begin(session.id)
        self._attempts.save(attempt)
        session = self._advance(session, SessionStatus.RUNNING)

        try:
            outcome = self._invoke(session, task, output, resuming=resuming, prompt=prompt)
        except ClaudeClientError:
            logger.warning("Agent execution failed for task %s", task.id)
            self._finalize(task, session, attempt.finish(AttemptResult.FAILED))
            raise

        logger.info("Attempt for task %s ended as %s", task.id, outcome.result)
        return self._finalize(
            task, session, attempt.finish(outcome.result, outcome.exit_code), outcome
        )

    def _invoke(
        self,
        session: Session,
        task: Task,
        output: OutputSink,
        *,
        resuming: bool,
        prompt: str | None,
    ) -> ExecutionOutcome:
        """Hand off to the agent. The task carries the working directory."""
        if resuming:
            return self._claude.resume(session, task, output, prompt)
        return self._claude.start(session, task, output)

    def _finalize(
        self,
        task: Task,
        session: Session,
        attempt: ExecutionAttempt,
        outcome: ExecutionOutcome | None = None,
    ) -> RunResult:
        self._attempts.save(attempt)
        result = attempt.result or AttemptResult.FAILED

        if outcome is not None and outcome.claude_session_id is not None:
            session = session.with_claude_session_id(outcome.claude_session_id)

        session = self._settle(session, result, outcome)
        task = task.with_status(_TASK_STATUS_BY_RESULT[result])
        self._tasks.save(task)
        return RunResult(task=task, session=session, attempt=attempt)

    def _settle(
        self, session: Session, result: AttemptResult, outcome: ExecutionOutcome | None
    ) -> Session:
        """Move the session to its post-attempt status and persist it."""
        reset_at = outcome.reset_at if outcome else None
        if result is AttemptResult.USAGE_LIMIT and reset_at is not None:
            updated = session.with_usage_limit(reset_at)
            self._sessions.update(updated)
            self._usage_limits.save(UsageLimit.detected(session.id, reset_at=reset_at))
            self._observer.usage_limit_reached(updated, reset_at)
            logger.info("Usage limit recorded, resets at %s", reset_at)
            return updated

        return self._advance(session, _SESSION_STATUS_BY_RESULT[result])

    def _advance(self, session: Session, status: SessionStatus) -> Session:
        updated = session.with_status(status)
        self._sessions.update(updated)
        return updated
