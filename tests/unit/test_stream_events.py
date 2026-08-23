"""The agent stream protocol, parsed line by line.

The payloads here are copied from real Claude Code 2.1 output.
"""

from __future__ import annotations

import json

from claude_runner.infrastructure.claude.stream_events import parse_line

REAL_RATE_LIMIT_EVENT = json.dumps(
    {
        "type": "rate_limit_event",
        "session_id": "77777777-2222-4333-8444-555555555555",
        "rate_limit_info": {
            "status": "allowed_warning",
            "resetsAt": 1787673600,
            "rateLimitType": "seven_day",
            "utilization": 0.77,
            "isUsingOverage": False,
            "surpassedThreshold": 0.75,
        },
    }
)

REAL_ASSISTANT_EVENT = json.dumps(
    {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Created hello.py"}]},
        "session_id": "77777777-2222-4333-8444-555555555555",
    }
)


def test_a_rate_limit_event_yields_status_reset_time_and_type() -> None:
    event = parse_line(REAL_RATE_LIMIT_EVENT)

    assert event.rate_limit is not None
    assert event.rate_limit.status == "allowed_warning"
    assert event.rate_limit.limit_type == "seven_day"
    assert event.rate_limit.utilization == 0.77
    assert event.rate_limit.reset_at is not None
    assert int(event.rate_limit.reset_at.timestamp()) == 1787673600


def test_a_rejected_status_is_recognised() -> None:
    payload = json.loads(REAL_RATE_LIMIT_EVENT)
    payload["rate_limit_info"]["status"] = "rejected"

    event = parse_line(json.dumps(payload))

    assert event.rate_limit is not None
    assert event.rate_limit.is_rejected
    assert not event.rate_limit.is_warning


def test_a_warning_status_is_not_a_rejection() -> None:
    event = parse_line(REAL_RATE_LIMIT_EVENT)

    assert event.rate_limit is not None
    assert event.rate_limit.is_warning
    assert not event.rate_limit.is_rejected


def test_assistant_text_blocks_become_readable_output() -> None:
    event = parse_line(REAL_ASSISTANT_EVENT)

    assert event.text == "Created hello.py"


def test_multiple_text_blocks_are_joined() -> None:
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "first"},
                {"type": "tool_use", "name": "Write"},
                {"type": "text", "text": "second"},
            ]
        },
    }

    assert parse_line(json.dumps(payload)).text == "first\nsecond"


def test_a_tool_only_message_produces_no_output() -> None:
    payload = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Write"}]},
    }

    assert parse_line(json.dumps(payload)).text is None


def test_a_result_event_carries_the_session_id_and_error_flag() -> None:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "abc",
        "result": "done",
    }

    event = parse_line(json.dumps(payload))

    assert event.result is not None
    assert event.result.session_id == "abc"
    assert event.result.is_error is False
    assert event.result.text == "done"


def test_unknown_event_types_are_ignored_quietly() -> None:
    event = parse_line(json.dumps({"type": "system", "subtype": "init"}))

    assert event.text is None
    assert event.rate_limit is None
    assert event.result is None


def test_non_json_lines_are_surfaced_as_text() -> None:
    """The CLI can print warnings before the stream begins; hiding them helps nobody."""
    event = parse_line("Warning: no stdin data received in 3s")

    assert event.text == "Warning: no stdin data received in 3s"


def test_malformed_json_is_surfaced_rather_than_dropped() -> None:
    assert parse_line('{"type": "result"').text == '{"type": "result"'


def test_blank_lines_produce_nothing() -> None:
    event = parse_line("   \n")

    assert event.text is None
    assert event.result is None


def test_a_missing_reset_time_is_tolerated() -> None:
    payload = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}

    event = parse_line(json.dumps(payload))

    assert event.rate_limit is not None
    assert event.rate_limit.is_rejected
    assert event.rate_limit.reset_at is None
