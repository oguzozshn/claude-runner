"""Discovering the agent's own stored conversations."""

from __future__ import annotations

import json
import os
from pathlib import Path

from claude_runner.infrastructure.claude.session_store import ClaudeTranscriptStore

SESSION_A = "8644c562-e342-4b8b-b639-ca8ba10584e8"
SESSION_B = "1ec36dd2-4693-4fab-8d68-97276f29e8be"


def _write_transcript(
    root: Path,
    project: str,
    session_id: str,
    title: str | None = "Create Python hello world program",
    cwd: str | None = r"C:\workspace",
    modified: float | None = None,
) -> Path:
    """Write a transcript shaped like the real Claude Code files."""
    directory = root / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"

    records: list[dict[str, object]] = []
    if title is not None:
        records.append({"type": "ai-title", "aiTitle": title, "sessionId": session_id})
    records.append({"type": "queue-operation", "sessionId": session_id})
    if cwd is not None:
        records.append({"type": "user", "cwd": cwd, "sessionId": session_id})

    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    if modified is not None:
        os.utime(path, (modified, modified))
    return path


def test_the_file_name_is_the_session_id(tmp_path: Path) -> None:
    _write_transcript(tmp_path, "project-a", SESSION_A)

    discovered = ClaudeTranscriptStore(tmp_path).discover()

    assert [session.claude_session_id for session in discovered] == [SESSION_A]


def test_the_title_and_directory_are_read_from_the_transcript(tmp_path: Path) -> None:
    _write_transcript(tmp_path, "project-a", SESSION_A)

    session = ClaudeTranscriptStore(tmp_path).discover()[0]

    assert session.title == "Create Python hello world program"
    assert session.working_directory == Path(r"C:\workspace")


def test_sessions_are_listed_most_recently_active_first(tmp_path: Path) -> None:
    _write_transcript(tmp_path, "project-a", SESSION_A, modified=1_000_000)
    _write_transcript(tmp_path, "project-b", SESSION_B, modified=2_000_000)

    discovered = ClaudeTranscriptStore(tmp_path).discover()

    assert [session.claude_session_id for session in discovered] == [SESSION_B, SESSION_A]


def test_the_limit_keeps_the_newest(tmp_path: Path) -> None:
    _write_transcript(tmp_path, "project-a", SESSION_A, modified=1_000_000)
    _write_transcript(tmp_path, "project-b", SESSION_B, modified=2_000_000)

    discovered = ClaudeTranscriptStore(tmp_path).discover(limit=1)

    assert [session.claude_session_id for session in discovered] == [SESSION_B]


def test_a_transcript_without_a_title_is_still_listed(tmp_path: Path) -> None:
    _write_transcript(tmp_path, "project-a", SESSION_A, title=None)

    session = ClaudeTranscriptStore(tmp_path).discover()[0]

    assert session.claude_session_id == SESSION_A
    assert session.title is None


def test_malformed_lines_do_not_break_discovery(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path, "project-a", SESSION_A)
    path.write_text("not json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    session = ClaudeTranscriptStore(tmp_path).discover()[0]

    assert session.title == "Create Python hello world program"


def test_a_missing_transcript_directory_is_not_an_error(tmp_path: Path) -> None:
    assert ClaudeTranscriptStore(tmp_path / "nope").discover() == ()


def test_only_transcripts_are_considered(tmp_path: Path) -> None:
    _write_transcript(tmp_path, "project-a", SESSION_A)
    (tmp_path / "project-a" / "memory").mkdir()
    (tmp_path / "project-a" / "notes.txt").write_text("x", encoding="utf-8")

    discovered = ClaudeTranscriptStore(tmp_path).discover()

    assert len(discovered) == 1
