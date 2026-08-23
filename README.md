# claude-runner

A CLI that runs Claude Code and survives usage limits. When the limit hits, the
run does not die: the session is saved with the exact time the limit lifts, and
you continue the **same conversation** later — either by hand with
`claude-runner resume`, or automatically with `--wait`.

```
claude-runner run "Refactor the parser" --wait
        │
        ▼
  RunnerService ──► ClaudeClient ──► claude --output-format stream-json
        │                                   │
        │                            rate_limit_event: rejected, resetsAt=…
        │                                   │
        ├── session → WAITING_FOR_RESET, reset time stored in SQLite
        ├── ResetScheduler sleeps until the reset
        └── claude --resume <session-id> → the conversation carries on
```

## How the recovery works

Three facts about Claude Code make this reliable rather than guesswork:

**The runner picks the session id.** `claude --session-id <uuid>` accepts an id
we generate, so a conversation is resumable from its first moment. Nothing has
to be scraped out of the output, and nothing is lost if the very first run is
cut off.

**Limits arrive as structured events, not prose.** With
`--output-format stream-json` the agent emits:

```json
{"type": "rate_limit_event", "rate_limit_info": {
   "status": "rejected", "resetsAt": 1787673600,
   "rateLimitType": "five_hour", "utilization": 1.0}}
```

`status` is one of `allowed`, `allowed_warning`, `rejected`, and `resetsAt` is a
unix timestamp. Matching on sentences was rejected on purpose: the wording
differs per plan ("You've hit your…", "You've reached your…", daily vs 5-hour vs
7-day) and changes between releases. The status field does not.

**`--resume <id>` really continues the conversation.** Verified end to end: a
codeword given in one process was recalled by a separate `resume` process.

### The session lifecycle

```
STARTING → RUNNING → WAITING_FOR_RESET → RESUMING → RUNNING → COMPLETED
```

Transitions are checked, not assumed: `Session.with_status` raises
`IllegalSessionTransitionError` for anything the table in `domain/models.py`
does not allow, so an orchestration bug surfaces where it happens rather than
as a nonsensical status in the database days later.

Everything needed to pick the work up again is written **before** any waiting
begins — working directory, agent session id, status and `reset_at`. Killing
the process mid-wait therefore loses nothing:

```
10:00  limit detected   → WAITING_FOR_RESET + reset_at persisted
10:01  process killed
14:05  claude-runner resume <id>   → reads it all back, continues
```

### The working directory rule

A session records the directory it was created in, and **resume always uses
that**, never the current one. Running `resume` from `C:\Temp` for a session
created in `C:\Projects\MyProject` still starts the agent in
`C:\Projects\MyProject`. `-C` only supplies a directory for a session the
runner has never seen before; it cannot move one it already knows.

## Architecture overview

Four layers, with dependencies pointing inwards only:

| Layer | Package | Knows about |
| --- | --- | --- |
| CLI | `cli/` | application + infrastructure (composition root) |
| Application | `application/` | domain only |
| Domain | `domain/` | nothing — standard library only |
| Infrastructure | `infrastructure/` | domain ports |

`RunnerService` never names `ClaudeProcess`, `subprocess` or `sqlite3`. It
depends on `Protocol`s in `domain/interfaces.py`, and `cli/commands.py` is the
only place where concrete classes are chosen.

Decisions worth knowing:

- **Output is piped, not inherited.** Letting the child write straight to the
  terminal would be simpler, but limit detection has to read those same lines.
  Output flows through the `OutputSink` port: the CLI renders it, the parser
  inspects it. Internal logs go to stderr via `logging`, never mixed in.
- **`ClaudeClient` returns an `ExecutionOutcome`, not an exit code.** Deciding
  *what happened* — `SUCCESS`, `FAILED`, `USAGE_LIMIT`, `CANCELLED`, and the
  reset time — belongs to the adapter that can see the stream.
- **A usage limit is not a failure.** It leaves the session in
  `WAITING_FOR_RESET` and the task in `RUNNING`, because nothing is wrong and
  retrying differently would not help. It just has to wait.
- **Agent flags are passed through, not modelled.** `--claude-arg` keeps
  Claude-specific concepts out of the runner's own vocabulary, so a second
  coding agent does not mean a flag per agent.

## Folder structure

```
src/claude_runner/
├── main.py                          entry point
├── cli/commands.py                  Typer commands and wiring
├── cli/presentation.py              lifecycle messages, local-time formatting
├── application/runner_service.py    run + resume use cases
├── domain/
│   ├── models.py                    Task, Session, ExecutionAttempt, UsageLimit
│   ├── enums.py                     TaskStatus, SessionStatus, AttemptResult
│   ├── interfaces.py                ports: ClaudeClient, repositories, scheduler
│   └── exceptions.py                error hierarchy
└── infrastructure/
    ├── claude/claude_client.py      ClaudeProcess: runs the claude CLI
    ├── claude/stream_events.py      stream-json parsing (pure functions)
    ├── claude/session_store.py      discovering Claude's stored conversations
    ├── persistence/sqlite.py        SQLite repositories, with migrations
    ├── persistence/in_memory.py     in-memory repositories (tests)
    └── scheduling.py                waiting for a reset

tests/
├── unit/                            no processes, no filesystem
└── integration/                     real subprocess against a stub agent
```

## Development setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Install it as a global command:

```bash
uv tool install --editable .
```

## Usage

Run a task in the current directory:

```bash
claude-runner run "Create a hello world program"
```

`--wait` decides only what happens *after* a limit is hit. Without it the run
saves its state, prints when the limit lifts, and exits with code `2`:

```bash
claude-runner run "Refactor the parser and update the tests"
```

With it, the same run sleeps until the reset and continues by itself, as many
times as the limit interrupts it:

```bash
claude-runner run "Refactor the parser and update the tests" --wait
```

```
Starting session 7ceb91a4-…
Working directory: C:\Projects\MyProject
Usage limit reached.
Reset at: 2026-08-23 14:00 Türkiye Standart Saati
Waiting for reset (2h 27m to go)...
Reset reached.
Resuming session 7ceb91a4-…
Task 'Refactor the parser' completed.
```

Lifecycle notes go to stderr, so piping stdout still yields only what the agent
said. Timestamps are stored timezone-aware in UTC and shown in local time.

Continue the most recent interrupted session:

```bash
claude-runner resume
```

Continue a specific one — a runner session id, or a Claude session id from a
conversation this runner never started:

```bash
claude-runner resume 1ec36dd2-4693-4fab-8d68-97276f29e8be
```

`resume` refuses, without spending a call on the agent, when continuing makes
no sense:

| Situation | Response | Exit |
| --- | --- | --- |
| Ready to continue | Resumes in the session's own directory | `0` |
| Usage limit has not lifted | `Usage limit is still active. Reset: …` | `2` |
| No such session | `Session '<id>' was not found.` | `3` |
| Already finished | `Session '<id>' is already completed.` | `3` |
| Ctrl+C while waiting | `Interrupted. Session state is saved.` | `130` |

Add `--wait` to any of these and an active limit is slept through instead of
refused.

### Queueing work while the limit is exhausted

The limit is gone, it resets at 03:00, and you have an idea now. Leave the
machine on and let the runner deliver it.

Into an **existing** conversation:

```bash
claude-runner resume <session-id> -m "Start the refactor we discussed" --wait
```

As a **new** conversation, for a different project:

```bash
claude-runner run "Build the importer" -C C:\Projects\NewThing --wait
```

Both make one call that gets refused — the only way to learn the reset time,
which arrives inside the refusal — then sleep and go again when it lifts. A
message passed with `-m` is re-sent on every retry: a refused attempt never
reached the conversation, so dropping it would lose the very thing you queued.

Find the session id with `claude-runner sessions --all`, which lists Claude's
own conversations too, interactive ones included.

See what the runner recorded (`*` marks sessions waiting for a reset):

```bash
claude-runner sessions
```

Also list Claude's own conversations — including ones started by hand,
interactively, that this runner never saw. Any id here can be passed to
`resume`:

```bash
claude-runner sessions --all
```

```
Runner sessions
* 1ec36dd2-…  waiting_for_reset  2026-08-23 10:55  Refactor the parser

Claude sessions (not started by the runner)
  80f85a34-…  2026-08-23 11:13  [my-project] Fix the failing tests
```

These are read from `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`,
where Claude Code stores every conversation. The file name is the session id.

### Options

| Option | Meaning |
| --- | --- |
| `-C`, `--directory` | Working directory for the agent (default: current) |
| `--wait` | On a usage limit, sleep until it resets and continue |
| `--name` | Task name (default: derived from the prompt) |
| `--executable` | Agent CLI to run instead of `claude` |
| `-m`, `--prompt` | (`resume`) send this message instead of "carry on" |
| `--claude-arg` | Extra argument forwarded to the agent CLI (repeatable) |
| `--db` | State database (default: `~/.claude-runner/runner.db`) |
| `-a`, `--all` | (`sessions`) also list Claude's own conversations |
| `-n`, `--limit` | (`sessions`) how many discovered sessions to show |
| `-v`, `--verbose` | Print internal logs to stderr |

Exit codes: `0` completed, `1` failed, `2` stopped at a usage limit, `3` the
session cannot be resumed, `130` interrupted.

### Letting the agent actually edit files

Claude Code runs in `--print` mode, where there is nobody to answer a permission
prompt, so file writes are denied by default and Claude will only describe what
it would do. Grant permission by forwarding a flag:

```bash
claude-runner run "Create a hello world program" --claude-arg --permission-mode --claude-arg acceptEdits
```

## Testing notes

The integration suite runs a **stub agent** that speaks the same `stream-json`
protocol as Claude Code, including `rate_limit_event`. That is what makes the
limit → save → resume path testable in milliseconds instead of hours, without
spending API usage.

Confirmed against a real limit on 2026-08-23: three consecutive runs were
detected as `usage_limit`, and all three recorded the same reset time, which
matched when the limit actually lifted.

```
detected 11:32  ->  resets 14:00
detected 11:34  ->  resets 14:00
detected 11:36  ->  resets 14:00
```

## Roadmap

1. **Crash recovery** — `claude-runner recover` picks up sessions left `RUNNING`
   by a killed process.
2. **`status` command** — current utilization and time until reset, which the
   agent already reports on every run via `rate_limit_event`.
3. **`stop` command** — the process registry and cancellation path already exist.
4. **Task queue and daemon** — run queued tasks unattended in the background.
5. **Dashboard**.
6. **Other coding-agent CLIs** behind the same `ClaudeClient` port.
