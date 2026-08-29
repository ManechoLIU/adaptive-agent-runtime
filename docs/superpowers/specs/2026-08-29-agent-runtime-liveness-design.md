# Agent Runtime Liveness Governance Design

## Purpose

Prevent a task from remaining `ACTIVE` when its assigned external or native Agent has stopped, disconnected, completed, or ceased making verifiable progress. Convert Agent execution state from a one-time declaration into a mechanically verifiable runtime lease that lifecycle hooks and terminal control-event receipts can enforce.

This design applies to the `adaptive-delivery` governance layer. It does not modify SelfAlone product code or directly mutate SelfAlone's task ledger/control plane.

## Problem Statement

The current governance stack validates that an `ACTIVE` assignment has a complete delivered ACK, ownership boundaries, and a declared owner, but it does not verify that the Agent is still alive after activation. `lifecycle_hook.py` snapshots main HEAD, ledger hash, main worktree status, READY items, candidate revisions, and ledger consistency errors; it does not snapshot active assignment runtime state. Native `SubagentStop` events can mark a lifecycle event pending, but external providers such as Grok/Kimi do not necessarily emit an equivalent hook event.

As a result, the system can reach this invalid state:

1. Assignment receives a delivered ACK.
2. Controller marks task `ACTIVE`.
3. Agent process/session exits or becomes terminal.
4. No lifecycle event is emitted for the external provider.
5. Ledger remains `ACTIVE` because no runtime lease is revalidated.
6. Lifecycle hook cannot detect `agent_dead`/`lease_expired` because live Agent state is absent from the snapshot.
7. A controller can continue reasoning from the last-known ACTIVE declaration instead of real execution state.

## Design Principles

1. `ACTIVE` is a runtime claim and MUST be backed by a non-expired, verifiable runtime lease.
2. PID is only one possible liveness signal; provider/session status and heartbeat receipts are first-class evidence.
3. Runtime evidence is ephemeral and belongs outside the canonical task ledger header.
4. External model routes MUST emit lifecycle receipts equivalent in semantics to native subagent start/stop events.
5. A stale or unhealthy ACTIVE assignment MUST prevent a terminal `control_event_guard.py` PASS receipt.
6. Lack of progress is time-sensitive: a lease can be alive while progress is temporarily absent, but prolonged absence must become a distinct lifecycle trigger and eventually force recovery.
7. Existing candidate, READY, retained-candidate, review, and integration semantics remain unchanged except that stale ACTIVE state becomes another blocking control-event condition.

## Runtime Lease Model

Introduce an ephemeral runtime state store keyed by canonical repository and assignment ID. Each runtime lease records:

- `schema_version`
- `assignment_id`
- `task_id`
- `agent_id`
- `provider`
- `session_id`
- optional `pid`
- `worktree`
- `baseline_head`
- `started_at`
- `last_heartbeat_at`
- `last_progress_at`
- `last_observed_head`
- `last_observed_status_sha256`
- `terminal_state`
- `terminal_at`
- `lease_expires_at`
- `progress_deadline_at`
- `evidence_receipt_id`

The runtime state is not the Assignment itself. Assignment remains the authorization/ownership record; runtime lease is the current execution proof.

### Runtime States

A lease is evaluated into one of:

- `healthy`: provider/session/process/heartbeat evidence proves the Agent is live and lease is not expired.
- `progress_stale`: the lease is still live but no verifiable progress has occurred by `progress_deadline_at`.
- `unhealthy`: process is gone, provider session is terminal/disconnected, lease expired, or contradictory evidence exists.
- `terminal`: normal completed/failed/cancelled terminal receipt has been received.
- `unknown`: insufficient runtime evidence exists to support ACTIVE.

`ACTIVE` tasks require `healthy` or, for a bounded grace interval, `progress_stale`. `unhealthy`, `terminal`, or `unknown` cannot remain ACTIVE and must be handled by the controller as recovery/termination before a control event can close.

## Evidence Hierarchy

Runtime liveness uses three evidence classes; no implementation may require all three simultaneously.

### A. Provider/session evidence

Preferred when available. Provider adapters normalize session status into:

- `running`
- `waiting`
- `completed`
- `failed`
- `cancelled`
- `disconnected`
- `unknown`

`completed`, `failed`, `cancelled`, and `disconnected` are terminal/unhealthy for ACTIVE unless a subsequent recovery receipt creates a new lease.

### B. Local process evidence

When a stable local PID exists, validate both process existence and process identity. PID existence alone is insufficient because PIDs can be reused.

### C. Heartbeat/progress evidence

Receipts can refresh liveness and/or progress. Verifiable progress includes at least one of:

- owned worktree HEAD changed from the previous observed HEAD;
- owned worktree tracked status hash changed;
- test/RED checkpoint receipt issued by the assigned Agent;
- provider tool/activity heartbeat associated with the assignment/session.

A heartbeat can refresh `last_heartbeat_at` without refreshing `last_progress_at`; this distinguishes "alive but thinking" from "making progress".

## Provider Lifecycle Receipts

Add a normalized receipt interface for all external Agent routes. Receipts use these event types:

- `assignment_started`
- `assignment_heartbeat`
- `assignment_progress`
- `assignment_terminal`

Each receipt contains assignment/task/provider/session identity and an issuance timestamp. Terminal receipts contain a normalized terminal state and optional reason.

Native `SubagentStop` remains supported and is translated into the same runtime semantics. External Grok/Kimi adapters must emit equivalent terminal receipts so lifecycle enforcement does not depend on native hooks.

## Assignment Validation Changes

`assignment_lease_guard.py` continues to validate static Assignment shape and delivered ACK requirements. It gains runtime-aware validation for `ACTIVE` when invoked with runtime state:

- `ACTIVE` requires a complete delivered ACK as today.
- `ACTIVE` additionally requires a matching runtime lease.
- assignment/task/agent/session/worktree identities must match.
- lease state must not be `unhealthy`, `terminal`, or `unknown`.
- expired lease blocks ACTIVE.

Static validation without a runtime state file remains available for compatibility, but terminal project control must use runtime-aware validation.

## Lifecycle Snapshot Changes

`lifecycle_hook.py::project_snapshot()` adds:

- `active_assignments`
- `recovering_assignments`
- `assignment_liveness`
- `stale_active_ids`
- `progress_stale_ids`
- `terminal_active_ids`

The snapshot derives active/recovering task IDs from the canonical ledger and joins them to the ephemeral runtime state store.

New trigger labels:

- `ACTIVE_UNHEALTHY:<task_id>`
- `ACTIVE_UNKNOWN:<task_id>`
- `ACTIVE_TERMINAL:<task_id>`
- `ACTIVE_LEASE_EXPIRED:<task_id>`
- `ACTIVE_PROGRESS_STALE:<task_id>`
- `RECOVERY_STALLED:<task_id>`

These triggers participate in `pending_control_event` exactly like READY/candidate/main/ledger triggers.

## No-Progress Timeout

The system distinguishes lease expiry from progress timeout.

- `lease_expires_at` protects against loss of liveness/heartbeat.
- `progress_deadline_at` protects against long ACTIVE periods with no verifiable work.

Default durations are configuration values in one module, not hard-coded throughout the codebase. The first implementation uses conservative defaults suitable for long-running reasoning:

- heartbeat lease TTL: 20 minutes
- progress deadline: 30 minutes
- progress-stale grace before unhealthy/recovery-required: 15 additional minutes

A progress receipt resets both heartbeat and progress timers. A heartbeat-only receipt resets only the heartbeat lease.

The evaluator must accept deterministic `now` injection in tests.

## Recovery Semantics

The governance layer does not directly edit a project's ledger. When a stale/unhealthy ACTIVE task is detected it:

1. marks the lifecycle event pending;
2. emits a precise continuation reason naming the task and liveness failure;
3. blocks terminal control-event PASS;
4. requires the controller to reconcile the task to a truthful state and perform a verifiable recovery action.

A recovery creates a new runtime lease or refreshes the existing lease only when a new, matching recovery/heartbeat/start receipt is observed. Merely rewriting the ledger as `ACTIVE` cannot clear the runtime failure.

`RECOVERING` remains subject to existing delivered-ACK/verifiable-recovery rules and additionally gains a configurable stalled-recovery check when there is no recovery evidence by its runtime deadline.

## Control Event Guard Changes

`control_event_guard.py --repo` reads runtime liveness for all canonical ledger ACTIVE/RECOVERING tasks and blocks `control-event: allowed` if any of the following are true:

- ACTIVE task has no runtime lease;
- runtime lease identity mismatches Assignment/task metadata;
- ACTIVE lease is expired/unhealthy/terminal;
- ACTIVE progress is stale beyond grace;
- RECOVERING task is stalled without fresh recovery evidence.

The control-event snapshot must enumerate the runtime-liveness decision for each in-flight task, so the terminal receipt contains auditable evidence rather than relying on an implicit check.

## Lifecycle Bridge Changes

`web_lifecycle_bridge.py` is extended to translate AI-Bridge/provider audit receipts into normalized runtime receipts when sufficient assignment/session identity exists. Existing shell/computer receipt translation remains unchanged.

Provider-specific extraction is isolated behind small adapter functions so Grok/Kimi/native semantics do not leak into lifecycle evaluation.

A route that cannot supply a stable PID can still remain healthy through provider session + heartbeat receipts.

## Files and Responsibilities

- `scripts/assignment_runtime.py` (new): runtime lease model, state persistence, evaluation, timeouts, and normalized receipt application.
- `scripts/assignment_lease_guard.py`: optional runtime-aware ACTIVE validation.
- `scripts/ledger_consistency_guard.py`: require ACTIVE rows to be runtime-verifiable when runtime context is supplied; preserve pure text lint mode.
- `scripts/lifecycle_hook.py`: include in-flight runtime state in project snapshots and generate liveness triggers.
- `scripts/control_event_guard.py`: block terminal receipts on stale/unhealthy in-flight assignments and require runtime decisions in snapshot.
- `scripts/web_lifecycle_bridge.py`: translate external provider lifecycle receipts into normalized runtime receipts.
- `tests/test_assignment_runtime.py` (new): runtime state/evaluation/receipt tests.
- Existing assignment, lifecycle, control-event, and web-lifecycle tests: regression and new failure cases.
- Documentation/skill governance text: state that ACTIVE is lease-backed, external routes must emit terminal receipts, and terminal control receipts require healthy in-flight state.

## Compatibility and Migration

The rollout must not instantly invalidate every historical ACTIVE record that predates runtime leases. Compatibility is handled at control-event boundaries:

- historical terminal/completed assignments do not need runtime leases;
- currently ACTIVE tasks without a lease become `ACTIVE_UNKNOWN` at the next lifecycle/control event, forcing truthful reconciliation;
- no synthetic healthy lease is created from ledger text alone;
- existing delivered ACKs remain valid authorization evidence but do not count as current liveness.

## Testing Strategy

Tests are deterministic and must cover:

1. ACTIVE with delivered ACK but no runtime lease is blocked.
2. matching healthy lease allows ACTIVE.
3. missing PID is allowed when provider/session heartbeat is healthy.
4. dead/mismatched PID makes lease unhealthy when PID is the asserted liveness evidence.
5. terminal provider receipt immediately invalidates ACTIVE.
6. heartbeat refreshes lease but not progress deadline.
7. progress receipt refreshes heartbeat and progress deadlines.
8. progress deadline produces `ACTIVE_PROGRESS_STALE` before lease expiry.
9. stale beyond grace blocks control-event PASS.
10. lifecycle snapshot detects stale ACTIVE even when main/ledger/worktree/READY/candidate are unchanged.
11. native `SubagentStop` and external terminal receipt produce equivalent runtime state.
12. recovery receipt creates/refreshes a valid lease and clears the stale trigger only after evidence is present.
13. existing retained-candidate and READY/candidate WIP tests remain green.
14. malformed/mismatched runtime receipts fail closed.

## Success Criteria

The change is complete only when all of the following are true:

- A task cannot remain mechanically valid `ACTIVE` solely because it once produced a delivered ACK.
- External Agent termination becomes observable by the same lifecycle machinery as native subagent termination.
- Long ACTIVE periods with no real progress generate deterministic lifecycle triggers.
- `control_event_guard.py --repo` refuses a success receipt while stale/unhealthy ACTIVE or stalled RECOVERING tasks exist.
- Existing candidate/READY/review/retained-candidate semantics continue to pass regression tests.
- SelfAlone can consume the upgraded adaptive-delivery rules without direct edits to SelfAlone product code.
