"""The CLI wired end to end against the stub agent."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest
from typer.testing import CliRunner, Result

from claude_runner.cli import commands
from claude_runner.cli.commands import (
    NOT_RESUMABLE_EXIT_CODE,
    USAGE_LIMIT_EXIT_CODE,
    app,
)
from claude_runner.infrastructure.claude.session_store import ClaudeTranscriptStore

runner = CliRunner()

LimitControl = Callable[..., None]


def _invoke(args: list[str], workspace: Path, database: Path) -> Result:
    """Run a command that needs a working directory."""
    return runner.invoke(app, [*args, "-C", str(workspace), "--db", str(database)])


def _invoke_without_directory(args: list[str], database: Path) -> Result:
    """Run a command with no `-C`, as the user would from anywhere."""
    return runner.invoke(app, [*args, "--db", str(database)])


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "runner.db"


@pytest.mark.usefixtures("stub_agent_on_path")
def test_run_streams_agent_output_and_reports_success(workspace: Path, database: Path) -> None:
    result = _invoke(["run", "Create a hello world program"], workspace, database)

    assert result.exit_code == 0
    assert "prompt:Create a hello world program" in result.stdout
    assert "completed" in result.output


@pytest.mark.usefixtures("stub_agent_on_path")
def test_claude_arg_is_passed_through_to_the_agent(workspace: Path, database: Path) -> None:
    result = _invoke(
        ["run", "hello", "--claude-arg", "--permission-mode", "--claude-arg", "acceptEdits"],
        workspace,
        database,
    )

    assert result.exit_code == 0
    assert "--permission-mode acceptEdits" in result.stdout


@pytest.mark.usefixtures("stub_agent_on_path")
def test_a_failing_agent_makes_the_cli_exit_non_zero(workspace: Path, database: Path) -> None:
    result = _invoke(["run", "please fail"], workspace, database)

    assert result.exit_code == 1


@pytest.mark.usefixtures("stub_agent_on_path")
def test_the_lifecycle_is_narrated(workspace: Path, database: Path) -> None:
    result = _invoke(["run", "hello"], workspace, database)

    assert "Starting session" in result.output
    assert str(workspace) in result.output


# --------------------------------------------------------------- usage limit


@pytest.mark.usefixtures("stub_agent_on_path")
def test_a_usage_limit_saves_the_session_and_tells_you_how_to_continue(
    workspace: Path, database: Path, stub_limit_control: LimitControl
) -> None:
    stub_limit_control(int(time.time()) + 3600, False)

    result = _invoke(["run", "work until the limit"], workspace, database)

    assert result.exit_code == USAGE_LIMIT_EXIT_CODE
    assert "Usage limit reached" in result.output
    assert "Reset at:" in result.output
    assert "claude-runner resume" in result.output


@pytest.mark.usefixtures("stub_agent_on_path")
def test_an_interrupted_session_is_listed_and_marked(
    workspace: Path, database: Path, stub_limit_control: LimitControl
) -> None:
    stub_limit_control(int(time.time()) + 3600, False)
    _invoke(["run", "work until the limit"], workspace, database)

    listing = runner.invoke(app, ["sessions", "--db", str(database)])

    assert listing.exit_code == 0
    assert "waiting_for_reset" in listing.stdout
    assert "* " in listing.stdout


@pytest.mark.usefixtures("stub_agent_on_path")
def test_wait_sleeps_through_the_limit_and_finishes_the_task(
    workspace: Path, database: Path, stub_limit_control: LimitControl
) -> None:
    """A reset time already in the past means the wait returns immediately."""
    stub_limit_control(int(time.time()) - 1)

    result = _invoke(["run", "work until the limit", "--wait"], workspace, database)

    assert result.exit_code == 0
    assert "resumed:true" in result.stdout
    assert "completed" in result.output


# -------------------------------------------------------------------- resume


@pytest.mark.usefixtures("stub_agent_on_path")
def test_resume_continues_the_interrupted_session_after_the_reset(
    workspace: Path, database: Path, stub_limit_control: LimitControl
) -> None:
    """The whole point: a limit stops the run, and a later command carries on."""
    stub_limit_control(int(time.time()) - 1)
    interrupted = _invoke(["run", "work until the limit"], workspace, database)
    assert interrupted.exit_code == USAGE_LIMIT_EXIT_CODE

    resumed = _invoke(["resume"], workspace, database)

    assert resumed.exit_code == 0
    assert "resumed:true" in resumed.stdout
    assert "completed" in resumed.output


@pytest.mark.usefixtures("stub_agent_on_path")
def test_resume_survives_the_process_that_started_the_work(
    workspace: Path, database: Path, stub_limit_control: LimitControl
) -> None:
    """Nothing is carried in memory: the second command reads it all back."""
    stub_limit_control(int(time.time()) - 1)
    first = _invoke(["run", "work until the limit"], workspace, database)
    session_id = _session_id_from(first.output)

    resumed = _invoke(["resume", session_id], workspace, database)

    assert resumed.exit_code == 0
    assert f"--resume {session_id}" in resumed.stdout


@pytest.mark.usefixtures("stub_agent_on_path")
def test_resume_uses_the_sessions_own_directory_not_the_current_one(
    workspace: Path, database: Path, stub_limit_control: LimitControl
) -> None:
    """`resume` is run from elsewhere, with no `-C`; the agent must still land
    in the directory the session was created in."""
    stub_limit_control(int(time.time()) - 1)
    started = _invoke(["run", "work until the limit"], workspace, database)
    session_id = _session_id_from(started.output)

    resumed = _invoke_without_directory(["resume", session_id], database)

    assert resumed.exit_code == 0
    assert f"cwd:{workspace}" in resumed.stdout


@pytest.mark.usefixtures("stub_agent_on_path")
def test_a_directory_flag_cannot_move_a_known_session(
    workspace: Path, database: Path, tmp_path: Path, stub_limit_control: LimitControl
) -> None:
    stub_limit_control(int(time.time()) - 1)
    started = _invoke(["run", "work until the limit"], workspace, database)
    session_id = _session_id_from(started.output)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    resumed = _invoke(["resume", session_id], elsewhere, database)

    assert resumed.exit_code == 0
    assert f"cwd:{workspace}" in resumed.stdout


@pytest.mark.usefixtures("stub_agent_on_path")
def test_resuming_while_the_limit_is_active_refuses_with_the_reset_time(
    workspace: Path, database: Path, stub_limit_control: LimitControl
) -> None:
    stub_limit_control(int(time.time()) + 3600, False)
    started = _invoke(["run", "work until the limit"], workspace, database)
    session_id = _session_id_from(started.output)

    refused = _invoke(["resume", session_id], workspace, database)

    assert refused.exit_code == USAGE_LIMIT_EXIT_CODE
    assert "Usage limit is still active." in refused.output
    assert "Reset:" in refused.output
    assert "resumed:true" not in refused.stdout


@pytest.mark.usefixtures("stub_agent_on_path")
def test_wait_makes_resume_sit_out_an_active_limit(
    workspace: Path, database: Path, stub_limit_control: LimitControl
) -> None:
    """The stored reset is in the past, so the wait returns at once."""
    stub_limit_control(int(time.time()) - 1)
    started = _invoke(["run", "work until the limit"], workspace, database)
    session_id = _session_id_from(started.output)

    resumed = _invoke(["resume", session_id, "--wait"], workspace, database)

    assert resumed.exit_code == 0
    assert "completed" in resumed.output


@pytest.mark.usefixtures("stub_agent_on_path")
def test_resuming_a_completed_session_is_refused(workspace: Path, database: Path) -> None:
    finished = _invoke(["run", "hello"], workspace, database)
    assert finished.exit_code == 0
    session_id = _session_id_from_listing(database)

    refused = _invoke(["resume", session_id], workspace, database)

    assert refused.exit_code == NOT_RESUMABLE_EXIT_CODE
    assert "already completed" in refused.output


def test_resuming_an_unknown_session_is_refused_without_a_traceback(
    workspace: Path, database: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        commands, "ClaudeTranscriptStore", lambda: ClaudeTranscriptStore(tmp_path / "empty")
    )
    unknown = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    refused = _invoke(["resume", unknown], workspace, database)

    assert refused.exit_code == NOT_RESUMABLE_EXIT_CODE
    assert f"Session '{unknown}' was not found." in refused.output
    assert refused.exception is None or isinstance(refused.exception, SystemExit)


@pytest.mark.usefixtures("stub_agent_on_path")
def test_a_session_the_agent_knows_is_adopted(
    workspace: Path, database: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A session started by hand in Claude Code, not by this runner."""
    external = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    transcripts = tmp_path / "projects"
    _write_transcript(transcripts, external, "Started by hand")
    monkeypatch.setattr(
        commands, "ClaudeTranscriptStore", lambda: ClaudeTranscriptStore(transcripts)
    )

    result = _invoke(["resume", external], workspace, database)

    assert result.exit_code == 0
    assert f"--resume {external}" in result.stdout


@pytest.mark.usefixtures("stub_agent_on_path")
def test_resume_without_anything_to_resume_explains_itself(workspace: Path, database: Path) -> None:
    result = _invoke(["resume"], workspace, database)

    assert result.exit_code == 1
    assert "No interrupted session" in result.output


# ------------------------------------------------------------------ sessions


def test_sessions_is_empty_before_anything_runs(database: Path) -> None:
    result = runner.invoke(app, ["sessions", "--db", str(database)])

    assert result.exit_code == 0
    assert "No runner sessions recorded yet." in result.stdout


def test_a_missing_agent_is_reported_without_a_traceback(
    workspace: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")

    result = _invoke(["run", "anything"], workspace, database)

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.usefixtures("stub_agent_on_path")
def test_all_lists_claude_sessions_the_runner_never_started(
    database: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transcripts = tmp_path / "projects"
    _write_transcript(transcripts, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "Fix the parser")
    monkeypatch.setattr(
        commands, "ClaudeTranscriptStore", lambda: ClaudeTranscriptStore(transcripts)
    )

    listing = runner.invoke(app, ["sessions", "--all", "--db", str(database)])

    assert listing.exit_code == 0
    assert "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee" in listing.stdout
    assert "Fix the parser" in listing.stdout


@pytest.mark.usefixtures("stub_agent_on_path")
def test_all_does_not_repeat_sessions_the_runner_already_tracks(
    workspace: Path, database: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = _invoke(["run", "hello"], workspace, database)
    assert first.exit_code == 0
    tracked = _session_id_from_listing(database)

    transcripts = tmp_path / "projects"
    _write_transcript(transcripts, tracked, "Already tracked")
    monkeypatch.setattr(
        commands, "ClaudeTranscriptStore", lambda: ClaudeTranscriptStore(transcripts)
    )

    listing = runner.invoke(app, ["sessions", "--all", "--db", str(database)])

    assert listing.stdout.count(tracked) == 1
    assert "No other Claude sessions found." in listing.stdout


# ------------------------------------------------------------------- helpers


def _write_transcript(root: Path, session_id: str, title: str) -> None:
    directory = root / "some-project"
    directory.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "ai-title", "aiTitle": title, "sessionId": session_id},
        {"type": "user", "cwd": r"C:\workspace", "sessionId": session_id},
    ]
    lines = [json.dumps(record) for record in records]
    (directory / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _session_id_from_listing(database: Path) -> str:
    """Pull the first session id out of a listing, ignoring the `*` marker column."""
    listing = runner.invoke(app, ["sessions", "--db", str(database)])
    line = listing.stdout.splitlines()[1]
    return next(token for token in line.split() if _is_uuid(token))


def _is_uuid(token: str) -> bool:
    try:
        UUID(token)
    except ValueError:
        return False
    return True


def _session_id_from(output: str) -> str:
    line = next(line for line in output.splitlines() if "Session " in line)
    return line.split("Session ")[1].split(" ")[0].rstrip(".")


@pytest.mark.usefixtures("stub_agent_on_path")
def test_a_queued_message_is_delivered_to_the_conversation(
    workspace: Path, database: Path, stub_limit_control: LimitControl
) -> None:
    """Leave it running overnight: the message lands once the limit lifts."""
    stub_limit_control(int(time.time()) - 1)
    started = _invoke(["run", "work until the limit"], workspace, database)
    session_id = _session_id_from(started.output)

    resumed = _invoke(
        ["resume", session_id, "--prompt", "Idea I had at midnight", "--wait"],
        workspace,
        database,
    )

    assert resumed.exit_code == 0
    assert "prompt:Idea I had at midnight" in resumed.stdout
