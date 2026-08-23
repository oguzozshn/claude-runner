"""Typer commands.

This is both the presentation layer and the composition root: it is the only
module allowed to name concrete infrastructure classes.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from claude_runner.application.runner_service import (
    ResumeRequest,
    RunnerService,
    RunRequest,
    RunResult,
)
from claude_runner.cli.presentation import ConsoleOutputSink, ConsoleRunObserver, local_time
from claude_runner.domain.exceptions import (
    ClaudeRunnerError,
    SessionAlreadyCompletedError,
    SessionNotFoundError,
    UsageLimitActiveError,
)
from claude_runner.domain.models import AgentSession, Session
from claude_runner.infrastructure.claude.claude_client import (
    DEFAULT_EXECUTABLE,
    ClaudeCliConfig,
    ClaudeProcess,
)
from claude_runner.infrastructure.claude.session_store import ClaudeTranscriptStore
from claude_runner.infrastructure.persistence.sqlite import (
    DEFAULT_DATABASE_PATH,
    Database,
    SqliteAttemptRepository,
    SqliteSessionRepository,
    SqliteTaskRepository,
    SqliteUsageLimitRepository,
)
from claude_runner.infrastructure.scheduling import SleepingResetScheduler

LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"
DISCOVERY_LIMIT = 20

FAILURE_EXIT_CODE = 1
"""The task ran and did not succeed."""

USAGE_LIMIT_EXIT_CODE = 2
"""Nothing is wrong; the work is parked until the limit lifts."""

NOT_RESUMABLE_EXIT_CODE = 3
"""The session cannot be continued: unknown, or already finished."""

INTERRUPTED_EXIT_CODE = 130
"""Ctrl+C. State is already persisted, so this is a clean stop."""

app = typer.Typer(
    name="claude-runner",
    help="Run Claude Code tasks and resume them after usage limits.",
    no_args_is_help=True,
    add_completion=False,
)

DirectoryOption = Annotated[
    Path | None, typer.Option("--directory", "-C", help="Working directory (default: current).")
]
ExecutableOption = Annotated[
    str | None, typer.Option("--executable", help="Agent CLI to run instead of 'claude'.")
]
ClaudeArgOption = Annotated[
    list[str] | None,
    typer.Option(
        "--claude-arg",
        help="Extra argument passed straight to the agent CLI. Repeat for each one, "
        "e.g. --claude-arg --permission-mode --claude-arg acceptEdits.",
    ),
]
DatabaseOption = Annotated[
    Path | None, typer.Option("--db", help=f"State database (default: {DEFAULT_DATABASE_PATH}).")
]
WaitOption = Annotated[
    bool,
    typer.Option(
        "--wait",
        help="On a usage limit, sleep until it resets and continue automatically "
        "instead of exiting.",
    ),
]
VerboseOption = Annotated[
    bool, typer.Option("--verbose", "-v", help="Log internal steps to stderr.")
]


@app.callback()
def cli() -> None:
    """Keep sub-commands explicit.

    Without a callback Typer would flatten a single-command app into
    ``claude-runner <prompt>``.
    """


@app.command()
def run(
    prompt: Annotated[str, typer.Argument(help="What Claude should do.")],
    directory: DirectoryOption = None,
    name: Annotated[
        str | None, typer.Option("--name", help="Task name (default: from the prompt).")
    ] = None,
    executable: ExecutableOption = None,
    claude_arg: ClaudeArgOption = None,
    database: DatabaseOption = None,
    wait: WaitOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Run a single task with Claude Code."""
    _configure_logging(verbose=verbose)
    with Database(database or DEFAULT_DATABASE_PATH) as db:
        service = _build_runner_service(db, executable, claude_arg)
        request = RunRequest(
            prompt=prompt,
            working_directory=(directory or Path.cwd()).resolve(),
            name=name,
            wait_for_reset=wait,
        )
        _report(_guard(lambda: service.run(request, ConsoleOutputSink())))


@app.command()
def resume(
    session: Annotated[
        str | None,
        typer.Argument(
            help="Session id to continue. Accepts a runner session id or a Claude "
            "session id. Omit to continue the most recent interrupted session."
        ),
    ] = None,
    message: Annotated[
        str | None,
        typer.Option(
            "--prompt",
            "-m",
            help="Send this to the conversation instead of just asking it to carry on. "
            "Combine with --wait to queue a message until the limit lifts.",
        ),
    ] = None,
    directory: DirectoryOption = None,
    executable: ExecutableOption = None,
    claude_arg: ClaudeArgOption = None,
    database: DatabaseOption = None,
    wait: WaitOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Continue a conversation that was cut short."""
    _configure_logging(verbose=verbose)
    with Database(database or DEFAULT_DATABASE_PATH) as db:
        service = _build_runner_service(db, executable, claude_arg)
        request = _resume_request(service, session, directory, wait=wait, prompt=message)
        _report(_guard(lambda: service.resume(request, ConsoleOutputSink())))


@app.command()
def sessions(
    show_all: Annotated[
        bool,
        typer.Option(
            "--all",
            "-a",
            help="Also list Claude's own conversations, including ones this runner "
            "never started. Any of them can be passed to 'resume'.",
        ),
    ] = False,
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="How many discovered sessions to show.")
    ] = DISCOVERY_LIMIT,
    database: DatabaseOption = None,
    verbose: VerboseOption = False,
) -> None:
    """List sessions, newest first. `*` marks one waiting for a usage limit."""
    _configure_logging(verbose=verbose)
    with Database(database or DEFAULT_DATABASE_PATH) as db:
        known = _print_known_sessions(db)

    if show_all:
        _print_discovered_sessions(known, limit)


def _print_known_sessions(db: Database) -> set[str]:
    """Print what the runner itself recorded, and report which ids those were."""
    stored = SqliteSessionRepository(db).list()
    tasks = SqliteTaskRepository(db)
    if not stored:
        typer.echo("No runner sessions recorded yet.")
        return set()

    typer.secho("Runner sessions", bold=True)
    for session in stored:
        task = tasks.get(session.task_id)
        typer.echo(_format_session(session, task.name if task else "?"))
    return {s.claude_session_id for s in stored if s.claude_session_id}


def _print_discovered_sessions(known: set[str], limit: int) -> None:
    """Print Claude's own conversations that the runner does not already track."""
    discovered = [
        session
        for session in ClaudeTranscriptStore().discover(limit=limit)
        if session.claude_session_id not in known
    ]

    typer.echo()
    if not discovered:
        typer.echo("No other Claude sessions found.")
        return

    typer.secho("Claude sessions (not started by the runner)", bold=True)
    for session in discovered:
        typer.echo(_format_agent_session(session))


def _format_session(session: Session, task_name: str) -> str:
    marker = "*" if session.is_waiting_for_reset else " "
    started = session.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
    return f"{marker} {session.id}  {session.status:<18} {started}  {task_name}"


def _format_agent_session(session: AgentSession) -> str:
    when = (
        session.last_activity_at.astimezone().strftime("%Y-%m-%d %H:%M")
        if session.last_activity_at
        else "?"
    )
    label = session.title or "(untitled)"
    directory = session.working_directory.name if session.working_directory else "?"
    return f"  {session.claude_session_id}  {when}  [{directory}] {label}"


def _resume_request(
    service: RunnerService,
    session: str | None,
    directory: Path | None,
    *,
    wait: bool,
    prompt: str | None = None,
) -> ResumeRequest:
    """Work out which conversation the user meant."""
    working_directory = directory.resolve() if directory else None
    if session is None:
        latest = service.latest_resumable()
        if latest is None:
            typer.secho(
                "No interrupted session to resume. Pass a session id explicitly.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=FAILURE_EXIT_CODE)
        return ResumeRequest(
            session_id=latest.id,
            working_directory=working_directory,
            wait_for_reset=wait,
            prompt=prompt,
        )

    # A Claude session id is a UUID as well, so both readings are offered and
    # the service resolves whichever one actually exists.
    return ResumeRequest(
        session_id=_parse_uuid(session),
        claude_session_id=session,
        working_directory=working_directory,
        wait_for_reset=wait,
        prompt=prompt,
    )


def _parse_uuid(value: str) -> UUID | None:
    """Parse a UUID, or return None if the text is not one."""
    try:
        return UUID(value)
    except ValueError:
        return None


def _build_runner_service(
    database: Database, executable: str | None, claude_args: list[str] | None
) -> RunnerService:
    """Compose the object graph."""
    return RunnerService(
        claude_client=ClaudeProcess(build_claude_config(executable, claude_args)),
        task_repository=SqliteTaskRepository(database),
        session_repository=SqliteSessionRepository(database),
        attempt_repository=SqliteAttemptRepository(database),
        usage_limit_repository=SqliteUsageLimitRepository(database),
        reset_scheduler=SleepingResetScheduler(),
        agent_session_store=ClaudeTranscriptStore(),
        observer=ConsoleRunObserver(),
    )


def build_claude_config(executable: str | None, claude_args: list[str] | None) -> ClaudeCliConfig:
    """Turn raw CLI options into agent configuration.

    Agent-specific flags are passed through verbatim rather than modelled here,
    so that supporting another coding agent later does not mean growing this
    CLI a flag per agent.
    """
    return ClaudeCliConfig(
        executable=executable or DEFAULT_EXECUTABLE,
        extra_args=tuple(claude_args or ()),
    )


def _guard(action: Callable[[], RunResult]) -> RunResult:
    """Turn domain errors and Ctrl+C into a clean message and exit code.

    Every path here has already persisted whatever it learned, so exiting is
    safe: the session can always be picked up again with ``resume``.
    """
    try:
        return action()
    except UsageLimitActiveError as error:
        typer.secho("Usage limit is still active.", fg=typer.colors.YELLOW, err=True)
        typer.secho(f"Reset: {local_time(error.reset_at)}", fg=typer.colors.YELLOW, err=True)
        typer.secho("Re-run with --wait to sit it out.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=USAGE_LIMIT_EXIT_CODE) from error
    except (SessionNotFoundError, SessionAlreadyCompletedError) as error:
        typer.secho(f"{error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=NOT_RESUMABLE_EXIT_CODE) from error
    except ClaudeRunnerError as error:
        typer.secho(f"Error: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=FAILURE_EXIT_CODE) from error
    except KeyboardInterrupt as error:
        typer.secho("Interrupted. Session state is saved.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=INTERRUPTED_EXIT_CODE) from error


def _report(result: RunResult) -> None:
    """Render the final state and set the process exit code."""
    if result.succeeded:
        typer.secho(f"Task '{result.task.name}' completed.", fg=typer.colors.GREEN)
        return

    if result.hit_usage_limit:
        _report_usage_limit(result)
        raise typer.Exit(code=USAGE_LIMIT_EXIT_CODE)

    typer.secho(
        f"Task '{result.task.name}' ended as {result.task.status} "
        f"(exit code {result.attempt.exit_code}).",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=FAILURE_EXIT_CODE)


def _report_usage_limit(result: RunResult) -> None:
    """The limit was already announced by the observer; say what to do next."""
    typer.secho(f"Session {result.session.id} saved.", fg=typer.colors.YELLOW, err=True)
    typer.secho(
        f"Continue later with:  claude-runner resume {result.session.id}",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _configure_logging(*, verbose: bool) -> None:
    """Send internal logs to stderr so they never mix with agent output."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format=LOG_FORMAT,
    )
