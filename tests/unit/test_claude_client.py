"""The parts of the Claude adapter that do not need a process."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from claude_runner.domain.enums import AttemptResult
from claude_runner.domain.exceptions import ClaudeNotFoundError, SessionNotResumableError
from claude_runner.domain.models import Session, Task
from claude_runner.infrastructure.claude.claude_client import (
    RESUME_PROMPT,
    ClaudeCliConfig,
    ClaudeProcess,
    _resolve_executable,
    _StreamState,
    _to_outcome,
)

BASE_FLAGS = ["--print", "--output-format", "stream-json", "--verbose"]


class NullSink:
    """Discards agent output."""

    def write(self, line: str) -> None:
        del line


def _state(*lines: str) -> _StreamState:
    state = _StreamState()
    for line in lines:
        state.observe(line, NullSink())
    return state


def _result_line(**overrides: object) -> str:
    payload = {"type": "result", "subtype": "success", "is_error": False}
    payload.update(overrides)
    return json.dumps(payload)


def _rate_limit_line(status: str, resets_at: int = 1787673600) -> str:
    return json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": status,
                "resetsAt": resets_at,
                "rateLimitType": "five_hour",
                "utilization": 1.0,
            },
        }
    )


def test_start_command_asks_for_structured_streaming() -> None:
    command = ClaudeProcess().build_command("do it", session_id="sess-1")

    assert command == ["claude", *BASE_FLAGS, "--session-id", "sess-1", "do it"]


def test_resume_command_carries_the_agent_session_id() -> None:
    command = ClaudeProcess().build_command(RESUME_PROMPT, resume_id="sess-1")

    assert command == ["claude", *BASE_FLAGS, "--resume", "sess-1", RESUME_PROMPT]


def test_resume_wins_over_a_new_session_id() -> None:
    command = ClaudeProcess().build_command("x", session_id="new", resume_id="old")

    assert "--session-id" not in command
    assert "--resume" in command


def test_extra_args_are_placed_before_the_prompt() -> None:
    client = ClaudeProcess(ClaudeCliConfig(executable="agent", extra_args=("--model", "opus")))

    assert client.build_command("do it") == ["agent", *BASE_FLAGS, "--model", "opus", "do it"]


def test_a_session_without_an_agent_id_cannot_be_resumed() -> None:
    client = ClaudeProcess()
    task = Task.create(prompt="work", working_directory=Path())
    orphan = replace(Session.start(task.id), claude_session_id=None)

    with pytest.raises(SessionNotResumableError):
        client.resume(orphan, task, NullSink())


def test_stop_is_a_no_op_when_nothing_is_running() -> None:
    ClaudeProcess().stop(Session.start(uuid4()))


def test_exit_code_zero_is_a_success() -> None:
    outcome = _to_outcome(0, _state(_result_line()), cancelled=False)

    assert outcome.result is AttemptResult.SUCCESS


def test_non_zero_exit_code_is_a_failure() -> None:
    outcome = _to_outcome(2, _state(_result_line()), cancelled=False)

    assert outcome.result is AttemptResult.FAILED
    assert outcome.exit_code == 2


def test_an_error_result_fails_even_on_a_zero_exit_code() -> None:
    outcome = _to_outcome(0, _state(_result_line(is_error=True)), cancelled=False)

    assert outcome.result is AttemptResult.FAILED


def test_a_stopped_process_is_cancelled_not_failed() -> None:
    outcome = _to_outcome(1, _state(_result_line()), cancelled=True)

    assert outcome.result is AttemptResult.CANCELLED


def test_a_rejected_rate_limit_outranks_the_exit_code() -> None:
    state = _state(_rate_limit_line("rejected"), _result_line(is_error=True))

    outcome = _to_outcome(1, state, cancelled=False)

    assert outcome.result is AttemptResult.USAGE_LIMIT
    assert outcome.reset_at is not None
    assert int(outcome.reset_at.timestamp()) == 1787673600


def test_a_warning_rate_limit_does_not_stop_the_run() -> None:
    state = _state(_rate_limit_line("allowed_warning"), _result_line())

    assert _to_outcome(0, state, cancelled=False).result is AttemptResult.SUCCESS


def test_the_agent_session_id_is_read_from_the_result_event() -> None:
    state = _state(_result_line(session_id="sess-9"))

    assert _to_outcome(0, state, cancelled=False).claude_session_id == "sess-9"


def test_a_missing_executable_is_reported_as_a_domain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")

    with pytest.raises(ClaudeNotFoundError):
        _resolve_executable("definitely-not-installed-agent")
