"""Turning runner events into terminal output.

Everything the user reads is decided here, so the application layer can
narrate progress without knowing whether anyone is listening. Timestamps are
kept timezone-aware internally and only converted to local time on the way
out.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import typer

from claude_runner.domain.models import Session, Task

TIMESTAMP_FORMAT = "%H:%M"
DATED_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M"


def local_time(moment: datetime) -> str:
    """Render an instant in the user's timezone, with its name."""
    local = moment.astimezone()
    label = local.strftime(DATED_TIMESTAMP_FORMAT)
    zone = local.tzname()
    return f"{label} {zone}" if zone else label


def humanise(duration: timedelta) -> str:
    """Render a wait as hours and minutes, rounded down to the minute."""
    total_minutes = max(int(duration.total_seconds()) // 60, 0)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


class ConsoleOutputSink:
    """Streams agent output to stdout as it arrives."""

    def write(self, line: str) -> None:
        typer.echo(line)


class ConsoleRunObserver:
    """Narrates the session lifecycle on stderr.

    Lifecycle notes go to stderr so that piping stdout still yields only what
    the agent said.
    """

    def session_starting(self, session: Session, task: Task) -> None:
        _note(f"Starting session {session.id}...")
        _note(f"Working directory: {task.working_directory}")

    def session_resuming(self, session: Session, task: Task) -> None:
        _note(f"Resuming session {session.id}...")
        _note(f"Working directory: {task.working_directory}")

    def usage_limit_reached(self, session: Session, reset_at: datetime) -> None:
        del session
        typer.secho("Usage limit reached.", fg=typer.colors.YELLOW, err=True)
        typer.secho(f"Reset at: {local_time(reset_at)}", fg=typer.colors.YELLOW, err=True)

    def waiting_for_reset(self, reset_at: datetime) -> None:
        remaining = humanise(reset_at - datetime.now(reset_at.tzinfo))
        typer.secho(f"Waiting for reset ({remaining} to go)...", fg=typer.colors.YELLOW, err=True)

    def reset_reached(self) -> None:
        typer.secho("Reset reached.", fg=typer.colors.GREEN, err=True)


def _note(message: str) -> None:
    typer.secho(message, fg=typer.colors.BRIGHT_BLACK, err=True)
