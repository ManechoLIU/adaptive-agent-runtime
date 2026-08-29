# Simplified Controller State Machine Design

## Goal

Reduce controller management overhead while making long-running Agent delivery harder to stall, game, or leave half-closed. Keep the controller-facing lifecycle to five main states and move detailed execution conditions into machine-derived evidence, health, and transition gates.

## Non-goals

- Do not create a second project ledger or a second controller.
- Do not add more controller-facing lifecycle states.
- Do not require existing projects to rewrite all historical task rows before continuing work.
- Do not turn low-risk one-shot work into mandatory Goal/lifecycle ceremony.
- Do not make the runtime choose product priority, task boundaries, or acceptance meaning for the controller.

## Controller-facing lifecycle

The controller sees only five main states:

1. `READY` — work is open and not currently executing or verifying.
2. `ACTIVE` — an Agent or controller-owned execution is actively producing the work, including recovery execution.
3. `VERIFY` — an implementation/result exists and is waiting for named review, integration, regression, acceptance, or release evidence.
4. `BLOCKED` — no internal recovery action remains for this work package and progress depends on a real external condition.
5. `CLOSED` — the promised result has reached its committed delivery boundary, or the work has been formally superseded.

Detailed conditions are not new main states. They are machine fields or evidence:

- `dispatchable=true|false` for whether an open `READY` item can run now.
- `health=normal|stale|recovering|budget_exhausted` for `ACTIVE`.
- `verification_gate=review|integration|regression|acceptance|release` for `VERIFY`.
- `closure_reason=done|superseded` for `CLOSED`.

### Backward-compatible mapping

Existing ledgers remain readable during migration:

| Legacy ledger state | Controller-facing state | Machine detail |
| --- | --- | --- |
| `PENDING` | `READY` | `dispatchable=false` with dependency/wake reason |
| `READY` | `READY` | `dispatchable=true` |
| `ACTIVE` | `ACTIVE` | `health=normal` unless runtime evidence says otherwise |
| `RECOVERING` | `ACTIVE` | `health=recovering` |
| `VERIFY` | `VERIFY` | named verification gate |
| `BLOCKED` | `BLOCKED` | external wake condition required |
| `DONE` | `CLOSED` | `closure_reason=done` |
| `SUPERSEDED` | `CLOSED` | `closure_reason=superseded` |

New governance text should teach the five-state model first. Legacy spellings remain parser compatibility, not new controller concepts.

## Machine evidence instead of status inflation

The runtime must derive detailed execution facts from existing authoritative evidence rather than asking the controller to manually maintain more statuses.

Authoritative inputs include:

- canonical Git `HEAD` and worktree status hash;
- Assignment identity and delivered ACK receipt;
- runtime lease and checkpoint receipts;
- test / RED / GREEN evidence identifiers;
- candidate revision and candidate lifecycle;
- independent Reviewer receipt;
- main integration revision and current-main regression evidence;
- Goal rollover receipt.

The ledger remains the durable task contract and human-readable projection. It must not be the only source of truth for live runtime health.

## Evidence-delta progress watchdog

A heartbeat proves liveness, not progress. `assignment_progress` may refresh the progress deadline only when it carries a new evidence delta relative to the current lease.

A valid evidence delta is at least one changed, non-empty fingerprint from:

- observed Git `HEAD`;
- observed tracked worktree-status SHA-256;
- test/evidence receipt identifier;
- artifact fingerprint;
- blocker-evidence fingerprint.

If an Agent only emits another heartbeat or repeats the same fingerprints, the heartbeat lease may remain alive but the progress deadline is not extended. When the deadline is exceeded, the runtime derives `health=stale`; beyond grace it derives `health=recovering` / unhealthy and lifecycle handling must create a controller action.

This prevents "still running", PID existence, repeated reads, repeated model calls, or repeated no-delta receipts from counting as progress.

## Recovery budget

Recovery is bounded per Assignment lineage. Re-entering execution after a stale/unhealthy/failed attempt increments a machine `recovery_count`.

Default policy:

- first execution: `recovery_count=0`;
- at most two recovery attempts using the same task contract (`max_recoveries=2`);
- after the budget is exhausted, the runtime must not silently retry the same path.

`budget_exhausted` requires a strategy-changing action before another execution can be accepted, such as:

- shrink the scope;
- change Agent/provider/session;
- repair the environment;
- split the Assignment;
- escalate to a controller-owned bounded recovery;
- produce a real external `BLOCKED` proof.

A successful final result does not erase recovery count from audit/efficiency evidence.

## Pre-execution delivered-ACK gate

Repository-capable Agent execution must not start through Adaptive Delivery unless a complete delivered ACK has already been machine-validated for the exact Assignment, repository root, worktree/branch, baseline revision, owned scope, first checkpoint/RED, and stop condition.

The external-agent runner must require a validated ACK receipt reference for Assignment-bound `--execute` calls. Missing, mismatched, stale, or wrong-revision ACK evidence fails before spawning the Agent process.

This is a launch gate, not an after-the-fact scoring rule. A semantically good Writer/Reviewer result produced through a non-compliant launch can remain auxiliary evidence but cannot satisfy a required Assignment/Review receipt.

## Automatic transition obligations

The controller still decides priority and meaning, but it must not need to remember routine next steps. Machine gates derive the next required control action from evidence:

- live candidate without required review receipt -> require review Assignment/ACK;
- required Review `FAIL` -> require same-work-package rework/recovery decision;
- required Review `PASS` -> require candidate integration decision before the control event can close, unless an explicit ordered-integration constraint is recorded;
- integration decision -> require exact main revision plus current-main regression/acceptance evidence before the work can become `CLOSED`;
- Goal closure -> existing `goal_rollover` gate requires project recompute and next Goal / project-blocked proof / project-complete proof.

These are evidence obligations inside control receipts, not additional main states.

## Fact-derived controller projection

Add one reusable canonical-state projection so lifecycle, guards, and documentation use the same mapping. The projection accepts the legacy ledger state plus runtime/candidate/review facts and returns:

```text
main_state: READY | ACTIVE | VERIFY | BLOCKED | CLOSED
dispatchable: bool | null
health: normal | stale | recovering | budget_exhausted | null
verification_gate: review | integration | regression | acceptance | release | null
closure_reason: done | superseded | null
```

The projection must be deterministic and side-effect free. It does not mutate the ledger.

## Minimal implementation shape

Prefer extending existing components instead of adding a new orchestration subsystem:

- `scripts/assignment_runtime.py`
  - evidence-delta progress semantics;
  - recovery-count / recovery-budget evaluation.
- `scripts/assignment_lease_guard.py`
  - validate exact delivered ACK evidence before execution/reuse.
- `scripts/run_external_agent.mjs`
  - require validated ACK input before Assignment-bound execution.
- `scripts/lifecycle_hook.py`
  - expose derived five-state/health projection and trigger stale/budget-exhausted control events without creating new ledger states.
- `scripts/control_event_guard.py`
  - enforce Review PASS/FAIL -> integration/rework transition obligations and integration -> regression evidence.
- a small focused state-projection helper only if duplication would otherwise occur across the files above.
- `references/long-task-governance.md`, `references/agent-delivery-contract.md`, and `SKILL.md`
  - teach the five-state controller model and mark legacy detailed states as compatibility vocabulary.

Do not add a daemon, database, queue service, second ledger, or separate state store beyond existing runtime/candidate local state.

## Compatibility and rollout

1. New parsers accept both legacy detailed states and the five-state projection.
2. Existing projects are not blocked merely because their ledger still contains `PENDING`, `RECOVERING`, `DONE`, or `SUPERSEDED`.
3. The installed Adaptive Delivery copy is updated only after source tests pass and source `main` is clean.
4. Live projects receive an exact revision notification and loaded ACK before the new gates are treated as active for them.
5. In-flight Assignments are not invalidated retroactively. The stronger ACK launch gate applies on their next new execution/attempt; current valid work is reconciled from evidence.

## Test strategy

Use TDD for each behavior change.

Required tests include:

- repeated heartbeat/no-delta `assignment_progress` does not extend progress deadline;
- changed Git/evidence/artifact/blocker fingerprint does extend progress deadline;
- recovery count increments across attempts and budget exhaustion is derived after two recoveries;
- budget exhaustion produces a lifecycle control trigger even when PID/heartbeat remain healthy;
- Assignment-bound external execution without an exact validated ACK fails before Agent spawn;
- stale/wrong Assignment/revision ACK fails before Agent spawn;
- legacy ledger states map deterministically to the five controller-facing states;
- Review PASS cannot close a control event while the same candidate lacks integration/ordered-queue decision;
- Review FAIL cannot close without a rework/recovery disposition;
- integration cannot close without exact main revision and regression evidence;
- existing candidate-retention, Goal rollover, routing, runtime lease, and governance suites remain green.

## Success criteria

The change is successful when:

- the controller-facing lifecycle has only five states;
- no-delta activity cannot masquerade as progress;
- repeated recovery cannot continue indefinitely on the same strategy;
- Assignment-bound external Agent work cannot launch through Adaptive Delivery without validated delivered ACK evidence;
- candidate/review/integration/regression/Goal closure forms a machine-enforced continuation chain;
- existing ledgers continue to work without an immediate mass rewrite;
- full Python governance tests, external-agent routing tests, structure tests, and diff checks pass.
