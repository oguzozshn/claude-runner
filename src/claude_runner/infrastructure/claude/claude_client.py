"""Runs Claude Code as a child process.

This module is the only place in the codebase that knows the agent is a
subprocess. Everything above it talks to ``ClaudeClient`` and receives an
``ExecutionOutcome``.

Two transport decisions drive the design:

* Output is **piped, not inherited**, so the runner can watch it. The readable
  parts are forwarded to an ``OutputSink``; the CLI decides how to render them.
* The agent is asked for ``--output-format stream-json``, which streams
  structured events instead of prose. That is what makes usage-limit detection
  reliable: the limit arrives as a ``rate_limit_event`` with an exact reset
  timestamp rather than as a sentence that varies by plan and version.

The session id is supplied by the runner via ``--session-id`` rather than read
back from the agent, so a conversation is resumable from the moment it starts
even if the very first run is cut off.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from claude_runner.domain.enums import AttemptResult
from claude_runner.domain.exceptions import (
    ClaudeClientError,
    ClaudeNotFoundError,
    SessionNotResumableError,
)
from claude_runner.domain.interfaces import OutputSink
from claude_runner.domain.models import ExecutionOutcome, Session, Task
from claude_runner.infrastructure.claude.stream_events import (
    RateLimitSnapshot,
    ResultSummary,
    parse_line,
)

logger = logging.getLogger(__name__)

DEFAULT_EXECUTABLE = "claude"
RESUME_PROMPT = "Continue the previous task from where it stopped."
SUCCESS_EXIT_CODE = 0

STREAM_FLAGS = (
    "--print",
    "--output-format",
    "stream-json",
    "--verbose",
)
SESSION_ID_FLAG = "--session-id"
RESUME_FLAG = "--resume"


class ClaudeCliConfig(BaseModel):
    """How to invoke the agent CLI.

    Validated with Pydantic because these values come from outside the process
    (CLI flags today, a config file later).
    """

    model_config = ConfigDict(frozen=True)

    executable: str = Field(default=DEFAULT_EXECUTABLE, min_length=1)
    extra_args: tuple[str, ...] = ()


class _ProcessRegistry:
    """Tracks running children so ``stop`` can reach them from another thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[UUID, subprocess.Popen[str]] = {}
        self._cancelled: set[UUID] = set()

    def register(self, key: UUID, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes[key] = process
            self._cancelled.discard(key)

    def release(self, key: UUID) -> bool:
        """Forget the process and report whether it was cancelled."""
        with self._lock:
            self._processes.pop(key, None)
            was_cancelled = key in self._cancelled
            self._cancelled.discard(key)
            return was_cancelled

    def cancel(self, key: UUID) -> bool:
        """Terminate the tracked process. Return False if there is none."""
        with self._lock:
            process = self._processes.get(key)
            if process is None:
                return False
            self._cancelled.add(key)
        process.terminate()
        return True


class _StreamState:
    """Accumulates what the event stream revealed during one execution."""

    def __init__(self) -> None:
        self.rate_limit: RateLimitSnapshot | None = None
        self.result: ResultSummary | None = None

    def observe(self, line: str, output: OutputSink) -> None:
        event = parse_line(line)
        if event.text is not None:
            output.write(event.text)
        if event.rate_limit is not None:
            self._observe_rate_limit(event.rate_limit)
        if event.result is not None:
            self.result = event.result

    def _observe_rate_limit(self, snapshot: RateLimitSnapshot) -> None:
        self.rate_limit = snapshot
        if snapshot.is_rejected:
            logger.warning("Usage limit reached, resets at %s", snapshot.reset_at)
        elif snapshot.is_warning:
            logger.info(
                "Approaching the %s usage limit (%.0f%% used)",
                snapshot.limit_type,
                (snapshot.utilization or 0) * 100,
            )


class ClaudeProcess:
    """``ClaudeClient`` implementation backed by the ``claude`` CLI."""

    def __init__(self, config: ClaudeCliConfig | None = None) -> None:
        self._config = config or ClaudeCliConfig()
        self._registry = _ProcessRegistry()

    def start(self, session: Session, task: Task, output: OutputSink) -> ExecutionOutcome:
        """Run the task prompt in a new agent session with a known id."""
        command = self.build_command(task.prompt, session_id=session.claude_session_id)
        return self._execute(command, task.working_directory, task.id, output)

    def resume(
        self,
        session: Session,
        task: Task,
        output: OutputSink,
        prompt: str | None = None,
    ) -> ExecutionOutcome:
        """Continue a previous agent session in the task working directory."""
        if session.claude_session_id is None:
            raise SessionNotResumableError(
                f"Session {session.id} has no agent session id to resume."
            )
        command = self.build_command(prompt or RESUME_PROMPT, resume_id=session.claude_session_id)
        return self._execute(command, task.working_directory, task.id, output)

    def stop(self, session: Session) -> None:
        """Terminate the process running this session, if there is one."""
        if not self._registry.cancel(session.task_id):
            logger.debug("No running process for session %s", session.id)

    def build_command(
        self,
        prompt: str,
        session_id: str | None = None,
        resume_id: str | None = None,
    ) -> list[str]:
        """Build the argv for a run. Pure, so it can be asserted on in tests."""
        command = [self._config.executable, *STREAM_FLAGS]
        if resume_id is not None:
            command += [RESUME_FLAG, resume_id]
        elif session_id is not None:
            command += [SESSION_ID_FLAG, session_id]
        command += [*self._config.extra_args, prompt]
        return command

    def _execute(
        self,
        command: list[str],
        working_directory: Path,
        key: UUID,
        output: OutputSink,
    ) -> ExecutionOutcome:
        argv = [_resolve_executable(command[0]), *command[1:]]
        logger.debug("Starting %s in %s", argv, working_directory)
        state = _StreamState()
        cancelled = False

        try:
            # argv is assembled here and never passed through a shell.
            with subprocess.Popen(
                argv,
                cwd=str(working_directory),
                # Without this the agent waits several seconds for stdin it
                # will never receive.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            ) as process:
                self._registry.register(key, process)
                try:
                    exit_code = _stream(process, state, output)
                finally:
                    cancelled = self._registry.release(key)
        except OSError as error:
            raise ClaudeClientError(f"Could not run the agent CLI: {error}") from error

        return _to_outcome(exit_code, state, cancelled=cancelled)


def _resolve_executable(name: str) -> str:
    """Resolve an executable on PATH, honouring Windows extensions."""
    resolved = shutil.which(name)
    if resolved is None:
        raise ClaudeNotFoundError(
            f"'{name}' was not found on PATH. Install Claude Code or set a custom executable."
        )
    return resolved


def _stream(process: subprocess.Popen[str], state: _StreamState, output: OutputSink) -> int:
    """Forward the merged stream through the parser, then wait for the exit code."""
    if process.stdout is not None:
        for line in process.stdout:
            state.observe(line, output)
    return process.wait()


def _to_outcome(exit_code: int, state: _StreamState, *, cancelled: bool) -> ExecutionOutcome:
    """Translate a finished process into a domain outcome.

    A usage limit outranks the exit code: the agent may exit non-zero, but the
    run did not fail in a way that retrying differently would fix. It has to
    wait, which is a different decision for the caller.
    """
    if cancelled:
        return ExecutionOutcome(result=AttemptResult.CANCELLED, exit_code=exit_code)

    rate_limit = state.rate_limit
    if rate_limit is not None and rate_limit.is_rejected:
        return ExecutionOutcome(
            result=AttemptResult.USAGE_LIMIT,
            exit_code=exit_code,
            claude_session_id=_session_id_of(state),
            reset_at=rate_limit.reset_at,
            detail=f"Usage limit reached ({rate_limit.limit_type}).",
        )

    return ExecutionOutcome(
        result=_result_for(exit_code, state),
        exit_code=exit_code,
        claude_session_id=_session_id_of(state),
        detail=_detail_of(state),
    )


def _result_for(exit_code: int, state: _StreamState) -> AttemptResult:
    if exit_code != SUCCESS_EXIT_CODE:
        return AttemptResult.FAILED
    if state.result is not None and state.result.is_error:
        return AttemptResult.FAILED
    return AttemptResult.SUCCESS


def _session_id_of(state: _StreamState) -> str | None:
    return state.result.session_id if state.result is not None else None


def _detail_of(state: _StreamState) -> str | None:
    return state.result.subtype if state.result is not None else None
