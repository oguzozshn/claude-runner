"""Exception hierarchy shared by every layer.

Infrastructure failures are translated into these types at the boundary so
that the application layer never has to catch ``subprocess`` or driver errors,
and the CLI can turn any of them into a message instead of a traceback.
"""

from datetime import datetime


class ClaudeRunnerError(Exception):
    """Base class for every error raised by this application."""


class DomainValidationError(ClaudeRunnerError):
    """A domain model was asked to represent an impossible state."""


class IllegalSessionTransitionError(DomainValidationError):
    """A session was asked to move to a status it cannot reach from here."""


class SessionNotFoundError(ClaudeRunnerError):
    """No session with the given id is known, to this runner or to the agent."""


class SessionAlreadyCompletedError(ClaudeRunnerError):
    """A finished session cannot be continued."""


class UsageLimitActiveError(ClaudeRunnerError):
    """The usage limit has not lifted yet, so resuming now would just fail.

    Carries ``reset_at`` so the caller can tell the user when to come back, or
    wait for it.
    """

    def __init__(self, message: str, reset_at: datetime) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class WaitInterruptedError(ClaudeRunnerError):
    """A wait for a usage-limit reset was cancelled before it elapsed."""


class ClaudeClientError(ClaudeRunnerError):
    """The coding agent could not be executed or failed unexpectedly."""


class ClaudeNotFoundError(ClaudeClientError):
    """The agent executable is not installed or not on PATH."""


class SessionNotResumableError(ClaudeClientError):
    """A resume was requested for a session that has no agent session id."""
