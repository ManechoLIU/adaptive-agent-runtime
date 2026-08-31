# Adaptive Agent Runtime — Controller Health / Wake Supervisor Design

Date: 2026-08-31
Status: Approved in chat for spec formalization
Scope: Controller continuity, wake delivery, worktree-aware lifecycle binding

## 1. Problem

Adaptive Agent Runtime already has most of the required low-level pieces for continuous controller execution: one registered controller per Git common-dir, controller-host tracking, Web/Desktop host adapters, native same-thread resume, active-writer classification, pending lifecycle control events, Assignment liveness, Agent heartbeat/lease/progress health, peer-host fallback policy, rule handshake and control receipts.

The missing part is architectural convergence. Today those facts are spread across controller registry, lifecycle state, Web bridge auto-stop state, host errors and runtime assignments. Some lifecycle paths can wake the existing controller, while other equally important events only become `pending_control_event=true`. This allows a failure mode where Runtime detects a dead/stale Agent but the unique controller is never actually resumed to consume the event.

A second related gap exists in feature worktrees: lifecycle authority is intentionally anchored to canonical main, but the top-level controller may execute development work from a bound worktree. A branch-only `main` guard can therefore suppress controller governance even though the same Git common-dir and unique controller still apply.

The goal is to close both gaps without adding a second controller, second project state machine, or permanent supervisor Agent.

## 2. Design principles

1. **One controller identity, separate execution health.** Controller ownership answers “who is the unique controller?” Controller Health answers “can that same controller execute now?” The two must never be conflated.
2. **Git common-dir is the project identity boundary.** main and linked worktrees share the same controller ownership, canonical runtime and lifecycle authority.
3. **Health is a projection, not a new source of truth.** Health derives from existing machine facts; no independent controller-runtime ledger is introduced.
4. **Wake is deterministic infrastructure, not orchestration.** Wake Supervisor may decide whether/how to resume the existing controller, but it never chooses project priorities, writes code, reviews work, changes Goal, or acts as a second controller.
5. **No response is not proof of death.** `DEAD` requires positive machine evidence that no safe continuation path remains.
6. **Current host first, authorized fallback second.** Peer-host fallback is allowed only under existing host-fallback safety rules and preserves controller identity, lineage, checkpoint, runtime and ledger.
7. **DEAD never auto-Replaces.** Replacement requires an explicit ownership transition; Runtime must fail closed rather than create a second controller.
8. **One wake path for all pending controller work.** Event source must not decide whether the controller can be resumed.
9. **No rule-per-incident growth.** Existing lifecycle signals are converged into a common mechanism instead of adding special handling for each anomaly class.

## 3. Controller identity and health

Controller Binding remains the canonical ownership mechanism. The registry continues to define exactly one controller session for a canonical Git common-dir. No duplicate controller registry or alternate worktree ownership file is introduced.

Controller Health is a derived projection over current machine facts. It has exactly five externally meaningful states:

- `ACTIVE`: the registered controller has a confirmed active execution/writer on the relevant controller channel. Runtime must not start another continuation.
- `DEFERRED`: the controller identity is valid, but a safe continuation is temporarily blocked, most notably by `already has an active writer`. Pending work remains pending; Runtime does not cross hosts or create another controller merely because the writer is busy.
- `DEGRADED`: controller identity is valid but the current host adapter is incomplete or temporarily unhealthy. Runtime should attempt bounded current-host recovery when that is machine-actionable.
- `FALLBACK_NEEDED`: the current host has a terminal failure in the existing eligible failure set, current attempt safety is known, and an already-authorized peer host is available. Runtime may continue the same controller through that peer host.
- `DEAD`: no safe current-host continuation exists, no legal peer-host continuation exists, and machine evidence is sufficient to rule out an active/deferred writer. Runtime fails closed and requires explicit Resume/Replace handling.

Health must also expose the evidence used to derive the state: registered controller id, canonical common-dir, last controller host, pending event status, same-thread resume status, active-writer evidence where applicable, host capability/failure classification, fallback eligibility and the exact wake receipt/result.

The projection may be computed on demand and included in bounded wake receipts. It must not become a mutable parallel truth that can drift from registry/lifecycle/runtime.

## 4. Wake Supervisor

Introduce a thin deterministic Wake Supervisor function/module within the existing Runtime control plane. It is infrastructure, not an Agent.

Its permitted responsibilities are limited to:

1. read the canonical controller binding and lifecycle state;
2. derive Controller Health from current machine evidence;
3. decide whether a pending control event requires controller execution;
4. choose only among: no-op because ACTIVE, defer, bounded current-host continuation, legal peer-host fallback, or fail-closed DEAD;
5. launch/ask the existing host adapter to resume the exact registered controller;
6. persist a bounded auditable wake receipt containing decision, evidence and result;
7. leave `pending_control_event=true` until the controller actually consumes and closes the control event with existing control-event evidence.

It must not:

- create/fork a controller;
- choose Writer/Reviewer work;
- modify project Goal or task priority;
- accept/reject candidate code;
- perform product implementation;
- clear pending merely because a resume process was launched;
- treat a successful process spawn as proof that controller work was consumed.

## 5. Unified pending-control-event wake path

Every lifecycle event that leaves controller work pending must converge on the same wake decision path. Event producers continue to produce domain-specific triggers, but they do not own continuation policy.

Target flow:

```text
machine/project event
  -> lifecycle evaluates canonical snapshot
  -> pending_control_event = true
  -> Wake Supervisor derives Controller Health
  -> ACTIVE: no duplicate wake
     DEFERRED: retain pending and retry only on a meaningful new signal/bounded schedule
     DEGRADED: bounded current-host recovery
     FALLBACK_NEEDED: authorized peer-host continuation of same controller
     DEAD: fail closed, require explicit ownership recovery
  -> exact same registered controller executes
  -> controller reconciles real main/ledger/runtime/live Agents/candidates
  -> recovery / fallback / review / integration / goal rollover as appropriate
  -> existing control_event_guard receipt proves closure
  -> pending_control_event clears
```

This path applies uniformly to at least:

- `active_lease_expired`;
- assignment unhealthy/progress-stale/terminal-failed events;
- READY/runnable changes;
- candidate queue changes;
- Reviewer completion requiring controller action;
- main/ledger/material worktree changes;
- Goal rollover;
- rule handshake/control wake events;
- Web Stop/native-stop equivalents;
- Desktop lifecycle Stop/PostToolUse/SubagentStop equivalents;
- bound-controller worktree lifecycle events.

The wake layer must remain generic: new lifecycle triggers become wake-eligible by virtue of leaving a real pending controller event, not through another one-off wake rule.

## 6. Host continuation policy

Wake Supervisor reuses the existing host-fallback contract.

### 6.1 Same-host first

The controller’s most recent verified `controller_host` is the preferred continuation surface. Same-thread/same-controller continuation is attempted before any peer-host crossing.

### 6.2 Active writer

If the host returns evidence equivalent to `already has an active writer`, health is `DEFERRED`:

- `pending_control_event` remains true;
- no second resume is started concurrently;
- no peer-host fallback occurs merely due to active-writer contention;
- no second controller is created;
- a future bounded wake check may retry after new machine evidence indicates the writer released or another relevant lifecycle signal arrives.

### 6.3 Peer-host fallback

Peer-host fallback is eligible only for the existing terminal machine failure classes, including the currently defined set such as usage/quota exhaustion, model/service/runtime unavailability or invalid authentication, and only when the existing safety conditions hold:

- prior controller execution attempt is terminal/known;
- there is no unknown side-effect outcome;
- there is no unresolved partial write requiring reconciliation;
- peer host is already authorized;
- crossing host does not silently cross a new billing/credential boundary;
- model tier and execution lineage remain unchanged except for recorded host fallback level/reason.

Fallback continues the same controller ownership and project state. It is not Replace.

### 6.4 DEAD

`DEAD` requires positive evidence that:

- unique controller binding still identifies the intended controller or has a known invalid binding condition;
- no active writer is currently proven;
- current host continuation is unavailable/terminal;
- no legal authorized peer-host continuation exists;
- bounded continuation attempts have produced conclusive machine failures rather than ambiguous timeout/unknown-result states.

DEAD is a control-plane blocker. It must not mutate ownership automatically.

## 7. Worktree-aware lifecycle binding

The current canonical-main principle remains unchanged: main is the sole authoritative integration surface. Writer and Reviewer worktrees must not become independent controllers.

Controller lifecycle eligibility, however, must be determined by Git common-dir plus controller binding rather than by a simplistic “current branch must be main” rule.

Required behavior:

- resolve the invocation repo/worktree to its Git common-dir;
- locate the unique controller binding and canonical lifecycle/runtime under that common-dir;
- distinguish a **bound controller execution surface** from ordinary Writer/Reviewer execution surfaces;
- allow the registered controller’s lifecycle events from an explicitly bound controller worktree to project into the same canonical lifecycle state;
- continue to ignore ordinary Writer/Reviewer worktree lifecycle as controller events;
- all candidate/integration authority remains anchored to main and existing candidate rules;
- no per-worktree controller state is introduced.

The implementation should reuse existing controller identity/session evidence and host adapter receipts to decide whether a worktree event belongs to the unique controller. If binding cannot be proven, fail closed/degrade rather than guess.

## 8. Wake receipts and auditability

A bounded wake receipt may be added as audit evidence. It is not a new state machine.

Minimum fields should include:

- schema version;
- canonical repo/common-dir identity;
- registered controller session id;
- lifecycle event or pending-event fingerprint that motivated the decision;
- derived health state;
- preferred host and selected host;
- decision (`NOOP_ACTIVE`, `DEFER`, `RESUME_CURRENT_HOST`, `FALLBACK_PEER_HOST`, `DEAD_BLOCK`);
- exact machine reason/failure class;
- attempt timestamps and bounded diagnostics;
- exact continuation command/adapter operation where applicable;
- result (`CONFIRMED`, `DEFERRED`, `FAILED`, `BLOCKED`);
- whether pending remains true.

Receipts must be bounded and atomically written under existing Git common-dir or host-state conventions, protected by existing resource-lock patterns where shared state is updated.

A resume launch may write `CONFIRMED` only when the host adapter provides the existing level of confirmation that the same registered controller continuation succeeded. It must never clear the lifecycle event by itself; only the controller’s normal control-event closure does that.

## 9. Error handling and safety

- Missing/multiple controller bindings: fail closed; do not guess or create one.
- Ambiguous host failure: retain pending, classify degraded/failed, do not peer-fallback unless failure class is eligible.
- Active-writer conflict: DEFER, no cross-host fallback.
- Wake process crash/timeout: retain pending; preserve bounded diagnostics; classify with existing failure semantics.
- Unknown side-effect or partial-write evidence: block automatic fallback/retry until reconciliation.
- Stale wake receipt: never override fresher lifecycle/host evidence.
- Concurrent wake attempts for one controller/common-dir: serialize or reject using deterministic common-dir-scoped locking.
- Controller Replace: remains an explicit ownership operation outside automatic Wake Supervisor behavior.

## 10. Implementation boundary

This change should be kept to the smallest compatible surface. Expected primary areas:

- `scripts/lifecycle_hook.py`: expose/consume canonical pending events without branch-only loss of controller lifecycle; provide common-dir-aware controller-surface projection.
- `scripts/web_lifecycle_bridge.py`: factor existing resume/failure classification into the common wake decision path rather than keeping wake semantics tied mainly to Stop/audit paths.
- a focused pure module (name to be finalized in implementation plan, e.g. `controller_health.py` or `controller_wake.py`) for health derivation and wake decisions if this materially reduces coupling; otherwise keep pure functions in an existing appropriately bounded module.
- existing host routing/fallback helpers and controller registry logic: reused rather than duplicated.
- `references/long-task-governance.md` / `SKILL.md` / `README.md`: document the unified semantics and fail-closed boundaries.
- tests: add focused health/wake/worktree cases while preserving all existing regression coverage.

Do not add A2A, SQLite/durable DB, HTTP control plane, permanent Supervisor Agent, per-worktree controller ownership or a second controller state machine in this iteration.

## 11. Test and acceptance matrix

The implementation is not complete until machine tests prove at least:

1. Agent lease expiry creates pending lifecycle work and invokes the generic wake decision rather than only logging the event.
2. ACTIVE controller does not receive a duplicate wake.
3. Active-writer conflict yields DEFERRED, keeps pending and does not fallback/create controller.
4. Current-host successful resume continues the exact registered controller.
5. Eligible current-host terminal failure with an authorized peer host yields same-controller peer-host fallback.
6. Ineligible/ambiguous failures do not cross hosts.
7. Unknown side-effect/partial-write conditions prevent automatic fallback/retry where existing contract requires it.
8. No safe current or peer host produces DEAD/fail-closed without automatic Replace.
9. main and a bound controller feature worktree resolve to the same common-dir ownership/lifecycle state.
10. ordinary Writer/Reviewer worktrees do not become controller lifecycle surfaces.
11. pending control event is not cleared by wake launch/resume alone; closure still requires existing control-event evidence.
12. concurrent wake attempts for the same common-dir cannot produce duplicate continuations.
13. existing Web Stop/native-stop behavior continues to work through the unified wake path.
14. existing Desktop lifecycle, rule handshake, Assignment runtime, Reviewer Supervisor and scoring behavior regressions remain green.
15. no duplicate technical Skill/runtime identity is introduced; `adaptive-delivery` remains the stable technical ID/path.

Final delivery requirements remain the existing project standard: TDD, full Python/Node regression, `git diff --check`, independent Reviewer Supervisor against exact candidate HEAD with no Critical/Important findings, integration to main, fresh merged-main regression, and only then update the installed Skill revision. No push unless explicitly requested.

## 12. Success criterion

Adaptive Agent Runtime is considered complete for this lifecycle scope when:

> From Web or Desktop, main or an explicitly bound controller worktree, any machine event requiring controller action can be projected to the one canonical project lifecycle, the Runtime can derive the real execution health of the one registered controller, safely resume that same controller on the current host or an already-authorized peer host when allowed, defer without duplication when a writer is active, and fail closed as DEAD when no safe continuation exists — without creating a second controller, losing runtime/ledger continuity, or depending on the user to type “continue.”
