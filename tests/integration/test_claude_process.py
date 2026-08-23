"""Real subprocess behaviour, verified against the stub agent."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_runner.domain.enums import AttemptResult
from claude_runner.domain.models import Session, Task
from claude_runner.infrastructure.claude.claude_client import ClaudeCliConfig, ClaudeProcess


class RecordingSink:
    """Collects streamed lines."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)


def _task(prompt: str, working_directory: Path) -> Task:
    return Task.create(prompt=prompt, working_directory=working_directory)


@pytest.mark.usefixtures("stub_agent_on_path")
def test_output_is_streamed_and_success_is_detected(tmp_path: Path) -> None:
    task = _task("Create a hello world program", tmp_path)
    session = Session.start(task.id)
    sink = RecordingSink()

    outcome = ClaudeProcess().start(session, task, sink)

    assert outcome.result is AttemptResult.SUCCESS
    assert outcome.exit_code == 0
    assert "prompt:Create a hello world program" in sink.lines


@pytest.mark.usefixtures("stub_agent_on_path")
def test_the_runner_chooses_the_session_id(tmp_path: Path) -> None:
    task = _task("who am i", tmp_path)
    session = Session.start(task.id)
    sink = RecordingSink()

    outcome = ClaudeProcess().start(session, task, sink)

    assert session.claude_session_id == str(session.id)
    assert outcome.claude_session_id == str(session.id)


@pytest.mark.usefixtures("stub_agent_on_path")
def test_the_agent_runs_in_the_task_working_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _task("where am i", workspace)
    sink = RecordingSink()

    ClaudeProcess().start(Session.start(task.id), task, sink)

    assert f"cwd:{workspace}" in sink.lines


@pytest.mark.usefixtures("stub_agent_on_path")
def test_stderr_is_merged_into_the_stream(tmp_path: Path) -> None:
    task = _task("talk to me", tmp_path)
    sink = RecordingSink()

    ClaudeProcess().start(Session.start(task.id), task, sink)

    assert "stderr line" in sink.lines


@pytest.mark.usefixtures("stub_agent_on_path")
def test_a_non_zero_exit_code_is_reported_as_a_failure(tmp_path: Path) -> None:
    task = _task("please fail", tmp_path)

    outcome = ClaudeProcess().start(Session.start(task.id), task, RecordingSink())

    assert outcome.result is AttemptResult.FAILED
    assert outcome.exit_code == 3


@pytest.mark.usefixtures("stub_agent_on_path")
def test_extra_args_reach_the_real_process(tmp_path: Path) -> None:
    config = ClaudeCliConfig(extra_args=("--permission-mode", "acceptEdits"))
    task = _task("do it", tmp_path)
    sink = RecordingSink()

    ClaudeProcess(config).start(Session.start(task.id), task, sink)

    argv = next(line for line in sink.lines if line.startswith("argv:"))
    assert "--permission-mode acceptEdits" in argv


@pytest.mark.usefixtures("stub_agent_on_path")
def test_a_rejected_rate_limit_becomes_a_usage_limit_outcome(
    tmp_path: Path, stub_limit_control: Callable[..., None]
) -> None:
    reset_epoch = int(time.time()) + 3600
    stub_limit_control(reset_epoch, False)
    task = _task("this will hit the limit", tmp_path)

    outcome = ClaudeProcess().start(Session.start(task.id), task, RecordingSink())

    assert outcome.result is AttemptResult.USAGE_LIMIT
    assert outcome.reset_at is not None
    assert int(outcome.reset_at.timestamp()) == reset_epoch


@pytest.mark.usefixtures("stub_agent_on_path")
def test_resume_targets_the_same_conversation(tmp_path: Path) -> None:
    task = _task("carry on", tmp_path)
    session = Session.start(task.id)
    sink = RecordingSink()

    ClaudeProcess().resume(session, task, sink)

    assert "resumed:true" in sink.lines
    argv = next(line for line in sink.lines if line.startswith("argv:"))
    assert f"--resume {session.claude_session_id}" in argv
