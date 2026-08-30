# Controller Governance Convergence Design

## 1. Problem

Adaptive Delivery has accumulated strong local safeguards but weak global convergence. The current implementation can produce contradictory control decisions because the same concept is independently re-derived by multiple components. The clearest example is rule drift: `rule_handshake.py` derives cumulative `effective_impact=live_assignments`, while `derive_rule_wake_policy()` still reads the latest manifest's single `impact`, allowing `blocking=true` and `wake_policy=next_turn` to coexist.

The goal of this refactor is not to add more governance. It is to reduce the control system until all four gates consume one canonical projection built from raw facts.

## 2. Non-goals

This change does not add new project lifecycle states, new controller roles, new project ledgers, new provider routes, new scoring dimensions, or new user-facing workflow. It does not rewrite SelfAlone product code. It does not use SelfAlone as the integration test for governance changes.

## 3. Architecture

The system is reduced to three layers.

### 3.1 Project State

`TASK_LEDGER.md` remains the only durable project-state authority. It owns task identity, five main states (`READY / ACTIVE / VERIFY / BLOCKED / CLOSED`), dependency declarations, current Goal, explicit blockers, and durable evidence references.

No runtime, resume, rule-update, candidate, scoring, provider, or host state becomes project state.

### 3.2 Execution Evidence

Machine files contain only raw evidence and receipts. Examples include assignment leases/results, candidate inventory, Git facts, rule install/ACK receipts, controller registration, verification receipts, and host resume receipts.

These files may prove facts, but they must not independently decide `blocking`, `wake_policy`, `can_yield`, `runnable`, or the next controller action.

### 3.3 Canonical Controller Projection

One pure projection function consumes the ledger plus raw evidence and emits one immutable control snapshot for the current event. At minimum it emits:

- `runnable_tasks`
- `live_assignments`
- `candidate_actions`
- `effective_rule_impact`
- `rule_ack_required`
- `control_event_state`
- `wake_action`
- `dispatch_allowed`
- `yield_allowed`
- `blocking_reasons`
- `required_controller_actions`

Every downstream gate reads these fields. No gate re-derives them from lower-level evidence.

## 4. Four Gates After Convergence

### Dispatch Gate

Consumes projection only. It checks `dispatch_allowed`, assignment contract/ACK, execution lineage/recovery budget, selected route, and the exact task to dispatch. Provider/host fallback resolution remains a routing concern, but the gate cannot reinterpret rule drift or runnable state.

### Delivery Gate

Consumes the terminal execution envelope and validates transport vs delivery evidence. It does not mutate lifecycle state or decide whether the controller may stop.

### Integration Gate

Consumes candidate/review/Git evidence from the projection. It decides whether the exact candidate can move through review/integration. It does not independently scan project runnable work or rules.

### Stop/Yield Gate

Consumes `yield_allowed`, `required_controller_actions`, `runnable_tasks`, `candidate_actions`, and `wake_action`. It must never recompute those from `impact`, raw ledger text, or host-specific state.

## 5. Rule Upgrade Model

Rule installation records the exact installed revision and raw manifest metadata. The canonical projection compares `loaded_revision..installed_revision` and derives cumulative `effective_rule_impact` once.

Rules:

- A later non-impacting update cannot erase an earlier unacknowledged live-impact change.
- `wake_action` is derived from cumulative `effective_rule_impact`, current live execution, and event liveness.
- `impact` from the latest manifest remains provenance only; it is not a control decision input after projection.
- ACK to the exact installed revision clears the accumulated rule debt only after ledger rule-version synchronization is confirmed.

## 6. Control Event Liveness

`pending_control_event` is replaced as the primary decision primitive by a structured event projection. Raw lifecycle evidence may persist event timestamps, but the decision is derived each time.

The event evidence must support:

- `event_id`
- `started_at`
- `last_progress_at`
- `last_receipt_at`
- current live assignment/candidate/runnable facts

The projection classifies the event as `active`, `quiescent`, or `stale` for control purposes. This classification is evidence, not a new project state.

A rule update that normally waits for `after_event` is promoted to immediate same-controller wake when the event is stale or quiescent and no live atomic work remains. This prevents infinite waiting without inventing arbitrary model judgment.

## 7. Web/Desktop Boundary

Host adapters are transport only.

Canonical flow:

`host event -> normalized controller event -> canonical projection -> requested action -> host adapter executes action -> raw receipt`

The Web adapter may translate AI-Bridge receipts, invoke same-controller native resume, and persist bounded diagnostics. It must not derive wake policy, runnable work, project blocking, or controller priority. Desktop native hooks follow the same projection/action contract.

`RESUME_CONFIRMED` is raw host evidence. It does not independently clear controller debt; the next canonical projection observes the receipt and current project facts.

## 8. State Reduction

The refactor must remove or demote duplicated decision state.

Decision fields such as `blocking`, `rule_wake_policy`, and `pending_control_event` must not be persistently owned by multiple modules. If retained for compatibility during migration, they are derived cache fields with no authority; tests must prove deleting/rebuilding them yields the same projection.

No new JSON state file may be added unless it stores raw evidence that cannot be reconstructed from an existing source.

## 9. Migration Strategy

Migration is staged to avoid replacing all control logic at once.

1. Introduce the canonical projection as a pure function with compatibility output matching current successful cases.
2. Move cumulative rule-impact and wake-action derivation into it.
3. Move runnable, candidate, liveness, and yield decision derivation into it.
4. Change four gates to consume projection fields instead of raw facts.
5. Reduce Web/Desktop adapters to normalized-event input and action execution.
6. Remove duplicated derivation code and obsolete persisted decision fields.

At every stage, old and new projections may be compared in tests, but there must never be two authoritative runtime decisions.

## 10. E2E Governance Qualification

No governance revision may be installed into SelfAlone after implementation until a fixed hermetic E2E fixture passes the complete control lifecycle:

1. stable old rule revision and registered controller;
2. runnable task dispatch and ACK;
3. live rule upgrade;
4. cumulative impact derivation;
5. safe event-boundary handling;
6. same-controller wake/resume;
7. exact revision ACK and ledger sync;
8. runnable re-derivation;
9. safe provider failure and authorized fallback;
10. valid delivery evidence;
11. candidate creation;
12. non-author review/integration evidence;
13. Goal/Stop-Yield closure.

The fixture must also include controlled failures: missing runtime, unknown/partial provider result, failed resume, stale event, and later `impact=none` after an unacknowledged live-impact update.

Unit tests remain necessary but cannot qualify a governance release by themselves.

## 11. Release Freeze During Refactor

Until the E2E qualification passes:

- no new governance concepts or lifecycle states;
- no new host-specific policy;
- no new gate variants;
- no new provider behavior beyond existing contracts;
- only convergence fixes, deletion of duplicate derivation, compatibility migration, and test harness work are allowed.

SelfAlone remains on its currently loaded safe revision until a qualified convergence release is available. A governance change must not be installed merely because local unit tests pass.

## 12. Success Criteria

The refactor is complete when all of the following are true:

- exactly one canonical projection owns control decisions;
- all four gates consume it;
- Web/Desktop adapters contain no policy derivation;
- `impact` cannot contradict `effective_rule_impact` in downstream decisions;
- no quiescent/stale event can wait indefinitely for a nonexistent atomic task;
- persisted machine files are evidence, not competing state machines;
- complete E2E qualification passes before installation;
- existing Delivery/Integration safety guarantees remain intact;
- total control-path complexity is measurably reduced by deleting duplicated logic, not merely moving it.
