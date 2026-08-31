# Reviewer Supervisor Design

## Purpose

Replace the fragile background-shell launch pattern for independent code review with one observable, fail-closed Reviewer Supervisor. The goal is not to create a second review policy or project state machine. It is to make the existing required Reviewer step machine-verifiable from process launch through final verdict.

## Scope

The supervisor owns only review execution infrastructure: process start, identity binding, liveness evidence, terminal capture, and infrastructure retry. It does not decide when review is required, does not change reviewer independence rules, does not write project task state, and does not replace Integration Gate semantics.

The existing technical Skill ID and runtime roots remain unchanged. Reviewer state is ephemeral machine evidence tied to one repository and candidate revision; it is not a new project ledger.

## Architecture

Add `scripts/reviewer_supervisor.py` as the single local entry point for supervised Codex review execution.

The supervisor launches `codex exec review` directly with `subprocess.Popen([...], start_new_session=True)` and no `nohup`, `sh -c`, or PID obtained from a wrapper shell. Each run receives a generated `run_id`, canonical repository root, exact candidate HEAD, base branch, start time, direct child PID, event-log path, final-message path, and state path.

The Codex process runs with `--json` so the supervisor can observe real execution events. A run is not `RUNNING` merely because `Popen` returned. Startup is confirmed only after the direct child is alive and at least one valid Codex event is received. If the event stream exposes a session/thread identifier, it is persisted as additional identity evidence; absence of a session identifier in a CLI version that does not expose one is not by itself fatal as long as the direct process and valid Codex events are proven.

## State Model

The supervisor has exactly these infrastructure states:

- `STARTING`: process requested but execution not yet proven.
- `RUNNING`: direct Codex process plus valid event evidence observed.
- `PASS`: process exited successfully and a valid PASS verdict is bound to the requested candidate revision.
- `FINDINGS`: process exited successfully and returned review findings or a non-PASS review verdict.
- `REVIEW_INFRA_FAILED`: launch, event-stream, timeout, output, schema, revision-binding, or abnormal-exit failure prevents a trustworthy review verdict.

These are supervisor execution states, not project task states. They must not be projected into the five project lifecycle states as a parallel state machine.

## Verdict Contract

The final Reviewer output is validated against a small structured contract containing at minimum:

- `reviewed_head`
- `verdict` (`PASS` or `FINDINGS`)
- `critical`
- `important`
- `minor`

`reviewed_head` must equal the candidate HEAD captured before launch. Missing output, malformed output, stale revision, empty output after exit 0, or exit 0 without a valid verdict is `REVIEW_INFRA_FAILED`, never PASS.

For backward compatibility, the supervisor may normalize the current Reviewer prose format into this contract only when the reviewed revision and severity sections can be parsed unambiguously. Ambiguous prose fails closed.

## Retry and Recovery

Infrastructure failure may be retried automatically once using the same repository, base branch, candidate HEAD, and review instructions. A review that returned findings is not an infrastructure failure and must never be auto-retried to hunt for a different verdict.

The second infrastructure failure terminates as `REVIEW_INFRA_FAILED` with bounded diagnostics. The controller can then use the existing governance fallback for reviewer-channel failure. The supervisor itself does not appoint a replacement reviewer or modify project state.

## Persistence and Observability

Each run writes one bounded state document under Git common-dir machine state so linked worktrees see the same evidence, for example `.git/adaptive-delivery/reviewer-runs/<run_id>.json`. This directory is execution evidence only and is not another canonical ledger.

State writes are atomic. The record contains run identity, requested and observed revision, direct child PID, optional Codex session id, timestamps, state, exit code, retry count, output fingerprints, and bounded failure diagnostics. Raw JSON event output and final-message output are stored beside the state record with bounded retention suitable for debugging.

## CLI

Initial interface:

`python3 scripts/reviewer_supervisor.py run --repo <repo> --base <branch> --output <optional-path>`

The command is synchronous from the caller's perspective: it supervises the child until a terminal state and exits only after the state record is durable. A later asynchronous wrapper can be added without changing the state contract, but is outside this scope.

Exit behavior:

- `0` only for validated `PASS`
- a distinct non-zero code for `FINDINGS`
- a distinct non-zero code for `REVIEW_INFRA_FAILED`

## Safety and Compatibility

The change must not alter existing Assignment runtime state, controller registry, rule handshake, host adapter configuration, Skill ID, or installed path. Existing manual `codex exec review` remains usable; the supervisor becomes the recommended machine path for required final review.

No shell PID heuristic is accepted as proof of reviewer liveness. No review result is accepted solely from process exit code. No infrastructure retry may change candidate HEAD or silently widen the review scope.

## Tests

TDD coverage must include:

1. direct child launch with no wrapper shell and captured exact candidate HEAD;
2. `STARTING` does not become `RUNNING` without valid Codex event evidence;
3. valid event stream plus valid PASS output produces `PASS`;
4. findings output produces `FINDINGS` and is not auto-retried;
5. exit 0 with missing/empty/malformed output produces `REVIEW_INFRA_FAILED`;
6. reviewed revision mismatch fails closed;
7. first infrastructure failure retries exactly once with the same immutable review contract;
8. second infrastructure failure remains terminal with bounded diagnostics;
9. linked worktrees resolve the same Git common-dir reviewer evidence root;
10. existing runtime/controller/handshake tests remain green.

## Non-goals

No durable A2A task service, queue, daemon, SQLite event bus, second reviewer policy, automatic reviewer selection, or new project lifecycle state is introduced. The feature replaces an unreliable launch mechanism; it does not expand governance scope.
