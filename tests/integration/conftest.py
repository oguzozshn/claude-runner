"""Shared fixtures for tests that spawn real processes.

The stub agent speaks the same ``stream-json`` protocol as Claude Code, which
lets the usage-limit path be exercised end to end without waiting hours for a
real limit — or spending any API usage at all.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

STUB_AGENT = """
import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
prompt = argv[-1] if argv else ""
flags = argv[:-1]


def emit(payload):
    print(json.dumps(payload), flush=True)


def text(value):
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": value}]}})


def flag_value(name):
    return flags[flags.index(name) + 1] if name in flags else None


session_id = flag_value("--session-id") or flag_value("--resume") or "unknown"
resumed = "--resume" in flags

text("argv:" + " ".join(flags))
text("cwd:" + str(Path.cwd()))
text("prompt:" + prompt)
text("resumed:" + str(resumed).lower())
sys.stderr.write("stderr line\\n")

limit_once = os.environ.get("STUB_LIMIT_ONCE")
already_limited = bool(limit_once) and Path(limit_once).exists()

if "limit" in prompt and not already_limited:
    if limit_once:
        Path(limit_once).write_text("1", encoding="utf-8")
    emit({
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "rejected",
            "resetsAt": int(os.environ.get("STUB_RESET_EPOCH", "0")),
            "rateLimitType": "five_hour",
            "utilization": 1.0,
        },
    })
    emit({
        "type": "result", "subtype": "error_usage_limit", "is_error": True,
        "session_id": session_id, "result": "usage limit",
    })
    sys.exit(1)

emit({
    "type": "result", "subtype": "success", "is_error": False,
    "session_id": session_id, "result": "done",
})
sys.exit(3 if "fail" in prompt else 0)
"""


@pytest.fixture
def stub_agent_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a fake ``claude`` executable on PATH and return its directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "stub_agent.py"
    script.write_text(STUB_AGENT, encoding="utf-8")

    if os.name == "nt":
        launcher = bin_dir / "claude.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = bin_dir / "claude"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)

    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return bin_dir


@pytest.fixture
def stub_limit_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[int, bool], None]:
    """Configure when the stub reports a usage limit and when it resets."""

    def configure(reset_epoch: int, once: bool = True) -> None:
        monkeypatch.setenv("STUB_RESET_EPOCH", str(reset_epoch))
        if once:
            monkeypatch.setenv("STUB_LIMIT_ONCE", str(tmp_path / "limit-hit.marker"))

    return configure
