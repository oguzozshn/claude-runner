"""Parsing of the agent's ``--output-format stream-json`` event stream.

Everything here is a pure function over one line of text, so the whole
protocol can be tested without spawning a process.

The events we care about, as emitted by Claude Code 2.1:

``rate_limit_event``
    Carries ``rate_limit_info`` with a ``status`` of ``allowed``,
    ``allowed_warning`` or ``rejected``, plus ``resetsAt`` as a unix
    timestamp. This is the structured signal used to detect usage limits;
    matching on prose was rejected because the wording differs per plan and
    changes between releases.

``assistant``
    A message from the model. Its text blocks are what the user wants to read.

``result``
    The final summary: ``is_error``, ``subtype``, ``session_id``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

RATE_LIMIT_EVENT = "rate_limit_event"
ASSISTANT_EVENT = "assistant"
RESULT_EVENT = "result"

STATUS_REJECTED = "rejected"
STATUS_ALLOWED_WARNING = "allowed_warning"


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    """The agent's view of the current usage limits."""

    status: str
    reset_at: datetime | None = None
    limit_type: str | None = None
    utilization: float | None = None

    @property
    def is_rejected(self) -> bool:
        """Whether the agent was refused because the limit is exhausted."""
        return self.status == STATUS_REJECTED

    @property
    def is_warning(self) -> bool:
        """Whether the agent is close to the limit but still running."""
        return self.status == STATUS_ALLOWED_WARNING


@dataclass(frozen=True, slots=True)
class ResultSummary:
    """The terminating ``result`` event."""

    is_error: bool
    subtype: str | None = None
    session_id: str | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One parsed line of the agent stream.

    At most one of the fields is set; a line the runner does not care about
    yields an event with ``text`` only when it carries readable content.
    """

    text: str | None = None
    rate_limit: RateLimitSnapshot | None = None
    result: ResultSummary | None = None


def parse_line(line: str) -> StreamEvent:
    """Parse one stream line.

    Non-JSON lines are treated as plain text: the CLI prefixes warnings before
    the stream starts, and dropping them silently would hide real problems.
    """
    stripped = line.strip()
    if not stripped:
        return StreamEvent()
    if not stripped.startswith("{"):
        return StreamEvent(text=stripped)

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return StreamEvent(text=stripped)
    if not isinstance(payload, dict):
        return StreamEvent(text=stripped)

    return _parse_payload(payload)


def _parse_payload(payload: dict[str, Any]) -> StreamEvent:
    event_type = payload.get("type")
    if event_type == RATE_LIMIT_EVENT:
        return StreamEvent(rate_limit=_parse_rate_limit(payload.get("rate_limit_info")))
    if event_type == ASSISTANT_EVENT:
        return StreamEvent(text=_assistant_text(payload))
    if event_type == RESULT_EVENT:
        return StreamEvent(result=_parse_result(payload))
    return StreamEvent()


def _parse_rate_limit(info: object) -> RateLimitSnapshot | None:
    if not isinstance(info, dict):
        return None
    return RateLimitSnapshot(
        status=str(info.get("status", "")),
        reset_at=_epoch_to_datetime(info.get("resetsAt")),
        limit_type=_optional_str(info.get("rateLimitType")),
        utilization=_optional_float(info.get("utilization")),
    )


def _parse_result(payload: dict[str, Any]) -> ResultSummary:
    return ResultSummary(
        is_error=bool(payload.get("is_error", False)),
        subtype=_optional_str(payload.get("subtype")),
        session_id=_optional_str(payload.get("session_id")),
        text=_optional_str(payload.get("result")),
    )


def _assistant_text(payload: dict[str, Any]) -> str | None:
    """Join the text blocks of an assistant message."""
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None

    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    ]
    return "\n".join(parts) or None


def _epoch_to_datetime(value: object) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(float(value), tz=UTC)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
