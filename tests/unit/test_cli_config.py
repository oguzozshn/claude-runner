"""Translation of CLI options into agent configuration."""

from __future__ import annotations

from claude_runner.cli.commands import build_claude_config
from claude_runner.infrastructure.claude.claude_client import DEFAULT_EXECUTABLE


def test_defaults_to_the_claude_executable_without_extra_args() -> None:
    config = build_claude_config(executable=None, claude_args=None)

    assert config.executable == DEFAULT_EXECUTABLE
    assert config.extra_args == ()


def test_repeated_claude_args_are_kept_in_order() -> None:
    config = build_claude_config(executable=None, claude_args=["--permission-mode", "acceptEdits"])

    assert config.extra_args == ("--permission-mode", "acceptEdits")


def test_a_custom_executable_replaces_claude() -> None:
    config = build_claude_config(executable="my-agent", claude_args=None)

    assert config.executable == "my-agent"
