"""Application logic, exercised without ever touching the real agent."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from claude_runner.application.runner_service import (
    ResumeRequest,
    RunnerService,
    RunRequest,
    RunResult,
)
from claude_runner.domain.enums import AttemptResult, SessionStatus, TaskStatus
from claude_runner.domain.exceptions import (
    ClaudeClientError,
    SessionAlreadyCompletedError,
    SessionNotFoundError,
    UsageLimitActiveError,
)
from claude_runner.domain.interfaces import OutputSink
from claude_runner.domain.models import AgentSession, ExecutionOutcome, Session, Task, utc_now
from claude_runner.infrastructure.persistence.in_memory import (
    InMemoryAttemptRepository,
    InMemorySessionRepository,
    InMemoryTaskRepository,
    InMemoryUsageLimitRepository,
)

PROJECT_DIRECTORY = Path("/projects/MyProject")
ELSEWHERE = Path("/tmp/elsewhere")


def future(hours: int = 5) -> datetime:
    """A reset time that has not arrived yet."""
    return utc_now() + timedelta(hours=hours)


def past(hours: int = 1) -> datetime:
    """A reset time that has already elapsed."""
    return utc_now() - timedelta(hours=hours)


class RecordingSink:
    """Collects the lines an agent would have printed."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)


class RecordingScheduler:
    """A ``ResetScheduler`` that records waits instead of sleeping."""

    def __init__(self) -> None:
        self.waited_until: list[datetime] = []

    def wait_until(self, reset_at: datetime) -> None:
        self.waited_until.append(reset_at)


class RecordingObserver:
    """A ``RunObserver`` that remembers the lifecycle it was told about."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def session_starting(self, session: Session, task: Task) -> None:
        del session, task
        self.events.append("starting")

    def session_resuming(self, session: Session, task: Task) -> None:
        del session, task
        self.events.append("resuming")

    def usage_limit_reached(self, session: Session, reset_at: datetime) -> None:
        del session, reset_at
        self.events.append("limit")

    def waiting_for_reset(self, reset_at: datetime) -> None:
        del reset_at
        self.events.append("waiting")

    def reset_reached(self) -> None:
        self.events.append("reset")


class FakeAgentSessionStore:
    """Pretends the agent has these conversations stored."""

    def __init__(self, *known: str) -> None:
        self._known = known

    def discover(self, limit: int | None = None) -> tuple[AgentSession, ...]:
        del limit
        return tuple(AgentSession(claude_session_id=identifier) for identifier in self._known)


class FakeClaudeClient:
    """A ``ClaudeClient`` that replays a scripted list of outcomes."""

    def __init__(
        self,
        outcomes: list[ExecutionOutcome] | None = None,
        error: Exception | None = None,
        lines: tuple[str, ...] = (),
    ) -> None:
        self._outcomes = outcomes or [ExecutionOutcome(AttemptResult.SUCCESS, exit_code=0)]
        self._error = error
        self._lines = lines
        self.started: list[tuple[Session, Task]] = []
        self.resumed: list[tuple[Session, Task]] = []
        self.prompts: list[str | None] = []

    @property
    def calls(self) -> int:
        return len(self.started) + len(self.resumed)

    @property
    def directories(self) -> list[Path]:
        """Where the agent was told to work, in call order."""
        return [task.working_directory for _, task in self.started + self.resumed]

    def start(self, session: Session, task: Task, output: OutputSink) -> ExecutionOutcome:
        self.started.append((session, task))
        return self._respond(output)

    def resume(
        self,
        session: Session,
        task: Task,
        output: OutputSink,
        prompt: str | None = None,
    ) -> ExecutionOutcome:
        self.resumed.append((session, task))
        self.prompts.append(prompt)
        return self._respond(output)

    def stop(self, session: Session) -> None:
        del session

    def _respond(self, output: OutputSink) -> ExecutionOutcome:
        for line in self._lines:
            output.write(line)
        if self._error is not None:
            raise self._error
        return self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]


class Harness:
    """A wired ``RunnerService`` plus the repositories it writes to."""

    def __init__(
        self, client: FakeClaudeClient, store: FakeAgentSessionStore | None = None
    ) -> None:
        self.client = client
        self.tasks = InMemoryTaskRepository()
        self.sessions = InMemorySessionRepository()
        self.attempts = InMemoryAttemptRepository()
        self.usage_limits = InMemoryUsageLimitRepository()
        self.scheduler = RecordingScheduler()
        self.observer = RecordingObserver()
        self.sink = RecordingSink()
        self.service = RunnerService(
            claude_client=client,
            task_repository=self.tasks,
            session_repository=self.sessions,
            attempt_repository=self.attempts,
            usage_limit_repository=self.usage_limits,
            reset_scheduler=self.scheduler,
            agent_session_store=store,
            observer=self.observer,
        )

    def run(
        self,
        prompt: str = "Create a hello world program",
        wait: bool = False,
        working_directory: Path = PROJECT_DIRECTORY,
    ) -> RunResult:
        request = RunRequest(
            prompt=prompt, working_directory=working_directory, wait_for_reset=wait
        )
        return self.service.run(request, self.sink)

    def resume(self, **kwargs: object) -> RunResult:
        return self.service.resume(ResumeRequest(**kwargs), self.sink)  # type: ignore[arg-type]


def _limit(reset_at: datetime | None = None) -> ExecutionOutcome:
    return ExecutionOutcome(
        AttemptResult.USAGE_LIMIT, exit_code=1, reset_at=reset_at if reset_at else future()
    )


def _success() -> ExecutionOutcome:
    return ExecutionOutcome(AttemptResult.SUCCESS, exit_code=0)


# --------------------------------------------------------------------- run


def test_successful_run_completes_task_and_session() -> None:
    harness = Harness(FakeClaudeClient())

    result = harness.run()

    assert result.succeeded
    assert result.task.status is TaskStatus.COMPLETED
    assert result.session.status is SessionStatus.COMPLETED


def test_agent_output_is_forwarded_to_the_sink_not_printed() -> None:
    harness = Harness(FakeClaudeClient(lines=("hello", "world")))

    harness.run()

    assert harness.sink.lines == ["hello", "world"]


def test_the_task_reaches_the_client_with_prompt_and_directory() -> None:
    harness = Harness(FakeClaudeClient())

    harness.run(prompt="Write tests")

    _, task = harness.client.started[0]
    assert task.prompt == "Write tests"
    assert task.working_directory == PROJECT_DIRECTORY
    assert task.status is TaskStatus.RUNNING


def test_a_new_session_carries_a_resumable_agent_id_from_the_start() -> None:
    harness = Harness(FakeClaudeClient())

    harness.run()

    session, _ = harness.client.started[0]
    assert session.claude_session_id == str(session.id)


def test_failed_run_marks_task_and_session_failed() -> None:
    harness = Harness(FakeClaudeClient([ExecutionOutcome(AttemptResult.FAILED, exit_code=2)]))

    result = harness.run()

    assert not result.succeeded
    assert result.task.status is TaskStatus.FAILED
    assert result.session.status is SessionStatus.FAILED
    assert result.attempt.exit_code == 2


def test_client_errors_are_recorded_before_they_propagate() -> None:
    harness = Harness(FakeClaudeClient(error=ClaudeClientError("boom")))

    with pytest.raises(ClaudeClientError):
        harness.run()

    assert [task.status for task in harness.tasks.list()] == [TaskStatus.FAILED]
    assert harness.sessions.get_active() == ()


def test_every_run_persists_exactly_one_attempt() -> None:
    harness = Harness(FakeClaudeClient())

    result = harness.run()

    attempts = harness.attempts.list_for_session(result.session.id)
    assert len(attempts) == 1
    assert attempts[0].ended_at is not None


# ------------------------------------------------------------- usage limits


def test_usage_limit_leaves_the_task_running_and_the_session_waiting() -> None:
    harness = Harness(FakeClaudeClient([_limit()]))

    result = harness.run()

    assert result.hit_usage_limit
    assert result.task.status is TaskStatus.RUNNING
    assert result.session.status is SessionStatus.WAITING_FOR_RESET
    assert result.session.completed_at is None


def test_usage_limit_records_the_reset_time_on_the_session_and_the_log() -> None:
    reset_at = future()
    harness = Harness(FakeClaudeClient([_limit(reset_at)]))

    result = harness.run()

    assert result.session.reset_at == reset_at
    stored = harness.sessions.get(result.session.id)
    assert stored is not None
    assert stored.reset_at == reset_at
    assert [limit.reset_at for limit in harness.usage_limits.list_for_session(stored.id)] == [
        reset_at
    ]


def test_without_wait_the_runner_does_not_sleep() -> None:
    harness = Harness(FakeClaudeClient([_limit()]))

    harness.run()

    assert harness.scheduler.waited_until == []
    assert harness.client.resumed == []


def test_with_wait_the_runner_sleeps_then_resumes_the_same_session() -> None:
    reset_at = future()
    harness = Harness(FakeClaudeClient([_limit(reset_at), _success()]))

    result = harness.run(wait=True)

    assert harness.scheduler.waited_until == [reset_at]
    assert len(harness.client.resumed) == 1
    resumed_session, _ = harness.client.resumed[0]
    started_session, _ = harness.client.started[0]
    assert resumed_session.id == started_session.id
    assert result.succeeded
    assert result.task.status is TaskStatus.COMPLETED


def test_the_full_lifecycle_is_narrated_in_order() -> None:
    harness = Harness(FakeClaudeClient([_limit(), _success()]))

    harness.run(wait=True)

    assert harness.observer.events == ["starting", "limit", "waiting", "reset", "resuming"]


def test_with_wait_the_runner_survives_repeated_limits() -> None:
    first, second = future(hours=5), future(hours=10)
    harness = Harness(FakeClaudeClient([_limit(first), _limit(second), _success()]))

    result = harness.run(wait=True)

    assert harness.scheduler.waited_until == [first, second]
    assert result.succeeded


def test_a_reset_that_already_passed_is_not_waited_for() -> None:
    harness = Harness(FakeClaudeClient([_limit(past()), _success()]))

    result = harness.run(wait=True)

    assert harness.scheduler.waited_until == []
    assert len(harness.client.resumed) == 1
    assert result.succeeded


def test_with_wait_but_no_reset_time_the_runner_gives_up_instead_of_hanging() -> None:
    outcome = ExecutionOutcome(AttemptResult.USAGE_LIMIT, exit_code=1, reset_at=None)
    harness = Harness(FakeClaudeClient([outcome]))

    result = harness.run(wait=True)

    assert harness.scheduler.waited_until == []
    assert result.hit_usage_limit


def test_a_resume_adds_a_second_attempt_to_the_same_session() -> None:
    harness = Harness(FakeClaudeClient([_limit(), _success()]))

    result = harness.run(wait=True)

    attempts = harness.attempts.list_for_session(result.session.id)
    assert [attempt.result for attempt in attempts] == [
        AttemptResult.USAGE_LIMIT,
        AttemptResult.SUCCESS,
    ]


# ------------------------------------------------------------------ resume


def test_a_stored_session_can_be_resumed_by_its_runner_id() -> None:
    harness = Harness(FakeClaudeClient([_limit(past()), _success()]))
    interrupted = harness.run()

    result = harness.resume(session_id=interrupted.session.id)

    assert result.succeeded
    assert result.session.id == interrupted.session.id
    assert len(harness.client.resumed) == 1


def test_a_session_can_be_resumed_by_its_agent_id() -> None:
    harness = Harness(FakeClaudeClient([_limit(past()), _success()]))
    interrupted = harness.run()
    agent_id = interrupted.session.claude_session_id
    assert agent_id is not None

    result = harness.resume(claude_session_id=agent_id)

    assert result.session.id == interrupted.session.id


def test_an_unknown_session_is_reported_not_silently_adopted() -> None:
    harness = Harness(FakeClaudeClient(), store=FakeAgentSessionStore())

    with pytest.raises(SessionNotFoundError, match="was not found"):
        harness.resume(claude_session_id="typo-not-a-real-session")

    assert harness.client.calls == 0


def test_resuming_with_no_identifier_at_all_is_reported() -> None:
    harness = Harness(FakeClaudeClient())

    with pytest.raises(SessionNotFoundError):
        harness.resume()


def test_a_session_the_agent_knows_is_adopted_into_a_new_task() -> None:
    harness = Harness(FakeClaudeClient([_success()]), store=FakeAgentSessionStore("external-1"))

    result = harness.resume(claude_session_id="external-1", working_directory=ELSEWHERE)

    assert result.session.claude_session_id == "external-1"
    assert result.task.working_directory == ELSEWHERE
    assert harness.client.resumed
    assert result.succeeded


def test_a_completed_session_cannot_be_resumed() -> None:
    harness = Harness(FakeClaudeClient([_success()]))
    finished = harness.run()

    with pytest.raises(SessionAlreadyCompletedError, match="already completed"):
        harness.resume(session_id=finished.session.id)

    assert harness.client.calls == 1


def test_a_failed_session_cannot_be_resumed() -> None:
    harness = Harness(FakeClaudeClient([ExecutionOutcome(AttemptResult.FAILED, exit_code=1)]))
    failed = harness.run()

    with pytest.raises(SessionAlreadyCompletedError):
        harness.resume(session_id=failed.session.id)


def test_resuming_while_the_limit_is_active_refuses_without_calling_the_agent() -> None:
    reset_at = future()
    harness = Harness(FakeClaudeClient([_limit(reset_at)]))
    interrupted = harness.run()

    with pytest.raises(UsageLimitActiveError) as caught:
        harness.resume(session_id=interrupted.session.id)

    assert caught.value.reset_at == reset_at
    assert harness.client.calls == 1  # only the original run


def test_resuming_with_wait_sits_out_an_active_limit_first() -> None:
    reset_at = future()
    harness = Harness(FakeClaudeClient([_limit(reset_at), _success()]))
    interrupted = harness.run()

    result = harness.resume(session_id=interrupted.session.id, wait_for_reset=True)

    assert harness.scheduler.waited_until == [reset_at]
    assert result.succeeded


def test_resuming_after_the_reset_needs_no_wait() -> None:
    harness = Harness(FakeClaudeClient([_limit(past()), _success()]))
    interrupted = harness.run()

    result = harness.resume(session_id=interrupted.session.id)

    assert harness.scheduler.waited_until == []
    assert result.succeeded


def test_latest_resumable_finds_the_interrupted_session() -> None:
    harness = Harness(FakeClaudeClient([_limit()]))
    interrupted = harness.run()

    latest = harness.service.latest_resumable()

    assert latest is not None
    assert latest.id == interrupted.session.id


def test_latest_resumable_is_none_when_everything_finished() -> None:
    harness = Harness(FakeClaudeClient())
    harness.run()

    assert harness.service.latest_resumable() is None


# ------------------------------------------------------- working directory


def test_resume_uses_the_stored_working_directory_not_the_current_one() -> None:
    """The session was created in one project; resume may be run from anywhere."""
    harness = Harness(FakeClaudeClient([_limit(past()), _success()]))
    interrupted = harness.run(working_directory=PROJECT_DIRECTORY)

    harness.resume(session_id=interrupted.session.id, working_directory=ELSEWHERE)

    assert harness.client.directories == [PROJECT_DIRECTORY, PROJECT_DIRECTORY]


def test_a_directory_hint_cannot_override_a_known_session() -> None:
    harness = Harness(FakeClaudeClient([_limit(past()), _success()]))
    interrupted = harness.run(working_directory=PROJECT_DIRECTORY)

    result = harness.resume(session_id=interrupted.session.id, working_directory=ELSEWHERE)

    assert result.task.working_directory == PROJECT_DIRECTORY


def test_the_automatic_resume_stays_in_the_original_directory() -> None:
    harness = Harness(FakeClaudeClient([_limit(), _success()]))

    harness.run(wait=True, working_directory=PROJECT_DIRECTORY)

    assert harness.client.directories == [PROJECT_DIRECTORY, PROJECT_DIRECTORY]


# ---------------------------------------------------- queueing a message


def test_a_follow_up_message_reaches_the_conversation() -> None:
    harness = Harness(FakeClaudeClient([_limit(past()), _success()]))
    interrupted = harness.run()

    harness.resume(session_id=interrupted.session.id, prompt="Also add type hints")

    assert harness.client.prompts == ["Also add type hints"]


def test_without_a_message_the_agent_is_asked_to_carry_on() -> None:
    harness = Harness(FakeClaudeClient([_limit(past()), _success()]))
    interrupted = harness.run()

    harness.resume(session_id=interrupted.session.id)

    assert harness.client.prompts == [None]


def test_a_queued_message_is_resent_after_the_limit_lifts() -> None:
    """The refused attempt never reached the conversation, so it must go again."""
    harness = Harness(FakeClaudeClient([_limit(past()), _limit(past()), _success()]))
    interrupted = harness.run()

    result = harness.resume(
        session_id=interrupted.session.id, prompt="Idea I had at midnight", wait_for_reset=True
    )

    assert harness.client.prompts == ["Idea I had at midnight", "Idea I had at midnight"]
    assert result.succeeded


def test_a_message_queued_while_the_limit_is_active_waits_then_lands() -> None:
    """Scenario: the limit is exhausted now and resets at 03:00."""
    reset_at = future()
    harness = Harness(FakeClaudeClient([_limit(reset_at), _success()]))
    interrupted = harness.run()

    result = harness.resume(
        session_id=interrupted.session.id, prompt="Start the refactor", wait_for_reset=True
    )

    assert harness.scheduler.waited_until == [reset_at]
    assert harness.client.prompts == ["Start the refactor"]
    assert result.succeeded


def test_the_automatic_resume_after_run_does_not_invent_a_message() -> None:
    """`run --wait` already sent its prompt; the retry only says carry on."""
    harness = Harness(FakeClaudeClient([_limit(), _success()]))

    harness.run(wait=True)

    assert harness.client.prompts == [None]
