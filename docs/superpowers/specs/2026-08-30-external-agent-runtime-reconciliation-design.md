# External Agent Runtime Reconciliation Design

## Goal

Make external-agent liveness a reliable machine fact across Git worktrees so ACTIVE/RECOVERING cannot drift from reality, recovery resumes from durable checkpoints, and stale attempts cannot overwrite newer state.

## Root cause

`assignment_runtime.py` currently stores runtime state at `Path(repo) / ".git" / "adaptive-delivery" / "runtime-assignments.json"`. In a linked Git worktree, `.git` is a file that points at the repository common git directory, not a directory. External Grok/Kimi runs therefore do not have one canonical runtime store shared with lifecycle evaluation. When runtime evidence is absent or fragmented, lifecycle logic falls back to Ledger/Git-derived inference and can retain stale RECOVERING/ACTIVE interpretations after the actual agent has exited or after the ledger has moved to READY.

The observed failure mode is:

`external agent exits -> no canonical runtime truth -> ledger/recovery state remains -> lifecycle sees RECOVERING without live evidence -> recovery_stalled -> controller dispatches another attempt`.

## Design principles

1. Runtime liveness is the authoritative source for ACTIVE/RECOVERING health.
2. All linked worktrees of one repository use one runtime store in the Git common directory.
3. Wrapper-owned process heartbeat must not depend on the model voluntarily emitting progress.
4. Attempt + lease fencing prevents late receipts from older attempts mutating current state.
5. Terminal receipt is emitted on every external-run exit path.
6. Checkpoints are durable progress boundaries; recovery resumes from the latest accepted checkpoint rather than replaying the whole Assignment.
7. Ledger is the human-readable control surface, but contradictory transient lifecycle states must be reconciled from runtime evidence instead of remaining as ghost state.
8. DONE/READY/absorbed terminal control states must not leave stale ACTIVE/RECOVERING/candidate lifecycle triggers.

## Architecture

### 1. Canonical runtime store

Introduce a Git-aware runtime root resolver. For any repository path or linked worktree, resolve the common Git directory with `git rev-parse --git-common-dir`, normalize it to an absolute path, and store runtime state under:

`<git-common-dir>/adaptive-delivery/runtime-assignments.json`

For normal repositories this remains equivalent to `.git/adaptive-delivery/runtime-assignments.json`. For linked worktrees, root and worktree calls converge on the same file.

If Git common-dir cannot be resolved, runtime evaluation fails closed as `unknown` with an explicit reason; it must not silently create a per-worktree shadow store.

### 2. Runtime receipt lifecycle

External runner lifecycle for an Assignment attempt:

`assignment_started(seq=1) -> wrapper heartbeats/progress(seq=N) -> assignment_terminal(final seq)`.

The wrapper owns a periodic heartbeat while the child process is alive. Heartbeat receipts refresh the lease even if the model produces no useful output. Progress receipts are emitted only when there is observable progress evidence, such as a changed Git HEAD, tracked diff fingerprint, or explicit checkpoint transition. Heartbeat and progress remain distinct so a live-but-stuck process is not misclassified as productive.

The terminal receipt is emitted from a guaranteed cleanup path for success, provider non-zero exit, transport exception, cancellation, or wrapper termination. Structured terminal outcome remains mandatory.

### 3. Attempt and lease fencing

Existing `attempt`, `lease_id`, and monotonic `event_seq` remain mandatory. A receipt may update the runtime record only when:

- its attempt is the current attempt, or it is a newer `assignment_started` attempt;
- its lease_id matches the current lease for the same attempt;
- its event_seq is strictly greater than the stored event sequence.

Late terminal/heartbeat/progress from an older attempt is rejected and cannot revive or overwrite a newer recovery attempt.

### 4. Checkpoint state

Runtime records gain a small checkpoint object:

- `checkpoint_id`
- `checkpoint_status`: `started | accepted`
- `checkpoint_evidence`
- `checkpoint_at`

Only controller-accepted checkpoints are recovery anchors. An accepted checkpoint is immutable for the current attempt chain except when a later checkpoint is accepted.

Recovery dispatch receives the latest accepted checkpoint identifier/evidence and starts from the next checkpoint. It must not ask a worker to replay accepted work unless the controller explicitly invalidates that checkpoint with new evidence.

### 5. Lifecycle reconciliation

Lifecycle snapshot joins ledger state with canonical runtime state and applies deterministic reconciliation:

- Ledger ACTIVE + runtime healthy/progress_stale -> keep ACTIVE semantics.
- Ledger ACTIVE + runtime terminal/unhealthy/expired -> trigger fail-close/recovery transition.
- Ledger RECOVERING + runtime healthy/progress_stale -> recovery is live.
- Ledger RECOVERING + runtime terminal/unhealthy/expired -> `recovery_stalled`/recovery action required.
- Ledger READY + no live current attempt -> READY wins; stale recovery triggers are cleared.
- Ledger DONE/VERIFY terminal package + no live current attempt -> no ACTIVE/RECOVERING triggers.
- A live current attempt conflicting with READY/DONE is a control-plane inconsistency and must be surfaced explicitly rather than silently ignored.

Reconciliation never marks Delivery complete; it only prevents stale lifecycle state from outliving authoritative runtime/ledger transitions.

### 6. Candidate/terminal cleanup boundary

When a candidate is consumed into main and recorded as absorbed, or when a task reaches a terminal DONE state, lifecycle evaluation must not continue to report that candidate/READY from stale cached state. Candidate detection remains Git-based, but retained/absorbed semantics and current HEAD are re-evaluated on each snapshot.

This change is limited to lifecycle truth and cleanup. It does not weaken Reviewer, integration, TDD, or acceptance gates.

## Files expected to change

- `scripts/assignment_runtime.py`: Git common-dir resolver, canonical runtime persistence, checkpoint fields/helpers.
- `scripts/run_external_agent.mjs`: periodic wrapper heartbeat/progress observation and guaranteed terminal emission.
- `scripts/lifecycle_hook.py`: reconciliation rules using canonical runtime truth.
- `scripts/control_event_guard.py`: reject contradictory live runtime vs READY/DONE transitions where needed.
- `tests/test_governance.py`: worktree canonical-store and reconciliation TDD cases.
- `tests/external-agent-routing.test.mjs`: runner heartbeat/terminal/fencing behavior.
- Governance references only where behavior contracts need clarification.

## TDD cases

The implementation must begin with failing tests that demonstrate the current bug:

1. A linked worktree and root repository resolve to the same runtime state file.
2. An external attempt started from a worktree is visible to lifecycle evaluation from the root repository.
3. READY with no live runtime attempt does not emit stale `recovery_stalled` from a previous recovery state.
4. ACTIVE with a terminal runtime receipt fails closed rather than remaining ACTIVE.
5. An older-attempt late terminal/progress receipt cannot mutate a newer attempt.
6. Wrapper heartbeat extends liveness while the child remains alive without pretending progress.
7. Every runner exit path emits one valid structured terminal receipt.
8. Latest accepted checkpoint is exposed as the recovery anchor and older accepted work is not replayed by default.

## Non-goals

- No SelfAlone product-code changes.
- No full A2A server or transactional outbox.
- No new provider-specific protocol.
- No automatic claim that a checkpoint, candidate, or Goal is accepted without controller/reviewer evidence.
- No weakening of existing ACK-first, TDD Reviewer, candidate, integration, or acceptance gates.

## Success criteria

- Root repo and all linked worktrees observe one canonical runtime record.
- A dead external process cannot remain machine-healthy ACTIVE/RECOVERING after lease/terminal evaluation.
- READY/DONE with no live attempt does not retain ghost recovery triggers.
- Long external executions remain healthy through wrapper-owned heartbeat, while progress staleness is still independently detectable.
- Recovery can bind to the latest accepted checkpoint and avoid replaying already accepted work.
- Python and Node governance suites pass, `git diff --check` passes, and the change is committed/pushed to adaptive-delivery `main`.
- After push, the exact revision is sent to the SelfAlone controller; ledger rule-version sync occurs only after a visible loaded ACK.
