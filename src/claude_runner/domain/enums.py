"""State machines expressed as string-backed enums.

String values are used so that persistence layers (SQLite, JSON) can store
them as-is without a translation table.
"""

from enum import StrEnum


class TaskStatus(StrEnum):
    """Lifecycle of a unit of work requested by the user."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionStatus(StrEnum):
    """Lifecycle of a single Claude conversation attached to a task."""

    STARTING = "starting"
    RUNNING = "running"
    WAITING_FOR_RESET = "waiting_for_reset"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class AttemptResult(StrEnum):
    """How one execution of the agent ended."""

    SUCCESS = "success"
    USAGE_LIMIT = "usage_limit"
    FAILED = "failed"
    CANCELLED = "cancelled"
