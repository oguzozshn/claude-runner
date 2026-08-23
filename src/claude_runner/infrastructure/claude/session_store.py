"""Discovering conversations the agent stored on disk.

Claude Code writes every conversation to
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``, whether it was started
interactively or in print mode. The file name is the session id, which is
exactly what ``--resume`` needs — so a session this runner never started can
still be picked up, as long as the user can find its id.

Only the head of each transcript is read: the title and working directory
appear in the first few records, and the files grow to megabytes.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from claude_runner.domain.models import AgentSession

logger = logging.getLogger(__name__)

DEFAULT_TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"
TRANSCRIPT_SUFFIX = ".jsonl"
HEAD_LINES = 40
"""How far into a transcript to look for its title and working directory."""


class ClaudeTranscriptStore:
    """``AgentSessionStore`` backed by Claude Code's transcript directory."""

    def __init__(self, root: Path = DEFAULT_TRANSCRIPT_ROOT) -> None:
        self._root = root

    def discover(self, limit: int | None = None) -> tuple[AgentSession, ...]:
        """Return stored conversations, most recently active first."""
        if not self._root.is_dir():
            logger.debug("No transcript directory at %s", self._root)
            return ()

        transcripts = sorted(
            self._root.glob(f"*/*{TRANSCRIPT_SUFFIX}"),
            key=_modified_at,
            reverse=True,
        )
        if limit is not None:
            transcripts = transcripts[:limit]
        return tuple(self._read(path) for path in transcripts)

    def _read(self, path: Path) -> AgentSession:
        title, working_directory = _read_head(path)
        return AgentSession(
            claude_session_id=path.stem,
            working_directory=working_directory,
            title=title,
            last_activity_at=datetime.fromtimestamp(_modified_at(path), tz=UTC),
        )


def _modified_at(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read_head(path: Path) -> tuple[str | None, Path | None]:
    """Pull the title and working directory out of a transcript."""
    title: str | None = None
    working_directory: Path | None = None

    try:
        with path.open(encoding="utf-8", errors="replace") as transcript:
            for index, line in enumerate(transcript):
                if index >= HEAD_LINES or (title and working_directory):
                    break
                title, working_directory = _merge(line, title, working_directory)
    except OSError as error:
        logger.debug("Could not read %s: %s", path, error)

    return title, working_directory


def _merge(
    line: str, title: str | None, working_directory: Path | None
) -> tuple[str | None, Path | None]:
    record = _load(line)
    if record is None:
        return title, working_directory

    found_title = record.get("aiTitle")
    if title is None and isinstance(found_title, str) and found_title:
        title = found_title

    found_cwd = record.get("cwd")
    if working_directory is None and isinstance(found_cwd, str) and found_cwd:
        working_directory = Path(found_cwd)

    return title, working_directory


def _load(line: str) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None
