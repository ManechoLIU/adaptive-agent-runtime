# External Agent Runtime Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make external-agent liveness authoritative across Git worktrees while reducing controller recovery bookkeeping.

**Architecture:** Resolve runtime state through Git common-dir so root checkout and linked worktrees share one store. The external-agent wrapper owns automatic start/heartbeat/progress/terminal receipts; lifecycle reconciliation consumes runtime truth and closes stale recovery projections when Ledger is READY/DONE. Checkpoints preserve useful progress without adding mandatory controller steps for short/low-risk tasks.

**Tech Stack:** Python 3 stdlib, Node.js ESM, Git CLI, existing unittest/node:test suites.

**Spec:** `docs/superpowers/specs/2026-08-30-external-agent-runtime-reconciliation-design.md`

## Global Constraints

- Do not add a second lifecycle state machine.
- Do not add required controller-authored fields to normal external-agent dispatch.
- Runtime receipts are wrapper-owned machine evidence.
- Checkpoints remain optional except where long/high-risk task policy already requires them.
- Preserve attempt/lease fencing and monotonic event sequencing.
- Prefer deleting/replacing stale inference paths over layering parallel recovery logic.
- SelfAlone business code is out of scope.

---

### Task 1: Worktree-safe shared runtime store

**Files:**
- Modify: `scripts/assignment_runtime.py`
- Test: `tests/test_assignment_runtime.py`

**Interfaces:**
- Consumes: repository/worktree path accepted by existing runtime functions.
- Produces: `runtime_state_path(repo)` resolving `<git-common-dir>/adaptive-delivery/runtime-assignments.json` for both root and linked worktrees.

- [ ] Add a failing test creating a temporary Git repo plus linked worktree and assert root/worktree `runtime_state_path()` are identical.
- [ ] Run the focused test and verify RED because current implementation uses `<worktree>/.git/...`.
- [ ] Implement common-dir resolution with `git rev-parse --git-common-dir`, canonicalize relative output against the supplied repo, and retain a safe fallback only for non-Git test fixtures.
- [ ] Run focused runtime tests and verify GREEN, including existing fencing/event-seq behavior.
- [ ] Commit `fix: share runtime state across git worktrees`.

### Task 2: Wrapper-owned heartbeat/progress and guaranteed terminal

**Files:**
- Modify: `scripts/run_external_agent.mjs`
- Test: `tests/external-agent-routing.test.mjs`
- Test: `tests/test_assignment_runtime.py` if receipt fixtures need cross-language assertions.

**Interfaces:**
- Consumes: existing assignment id, attempt, lease id, repo path, child process lifecycle.
- Produces: monotonic `assignment_started`, periodic `assignment_heartbeat`/`assignment_progress`, exactly one terminal receipt for each launched attempt.

- [ ] Add failing Node tests using a short-lived fake provider process to prove start→heartbeat/progress→terminal event ordering and exactly-one terminal on success/failure.
- [ ] Verify RED against the current start→terminal-only runner.
- [ ] Add a wrapper timer that emits heartbeat without controller input and progress only when machine-observable evidence changes (child/process state or repository HEAD/diff fingerprint); keep event_seq monotonic.
- [ ] Ensure timer cleanup and terminal emission live in the existing `finally`/exit boundary so provider failure, normal exit, and thrown errors cannot leave a live lease.
- [ ] Run focused Node + Python runtime tests GREEN.
- [ ] Commit `feat: emit automatic external agent liveness receipts`.

### Task 3: Runtime-first lifecycle reconciliation

**Files:**
- Modify: `scripts/lifecycle_hook.py`
- Modify: `scripts/control_event_guard.py` only if the existing receipt validator cannot express reconciliation proof.
- Test: `tests/test_lifecycle_hook.py`
- Test: `tests/test_control_event_guard.py`

**Interfaces:**
- Consumes: Ledger projection plus shared runtime lease classification.
- Produces: deterministic conflict handling where runtime is authoritative for liveness while Ledger remains management projection.

- [ ] Add failing tests for `Ledger=READY + no live runtime/terminal old attempt` and `Ledger=DONE + stale recovering runtime projection`; expected result is no `recovery_stalled` ghost trigger.
- [ ] Add failing test for `Ledger=ACTIVE + terminal runtime`; expected fail-close/recovery trigger remains present.
- [ ] Verify RED and capture exact trigger differences.
- [ ] Implement minimal reconciliation: READY/DONE close obsolete recovery projection; ACTIVE/RECOVERING may not override terminal/unhealthy runtime; stale attempts cannot resurrect state.
- [ ] Remove superseded inference branches rather than keeping both paths.
- [ ] Run lifecycle/control-event focused tests GREEN.
- [ ] Commit `fix: reconcile lifecycle from runtime truth`.

### Task 4: Optional checkpoint recovery semantics and complexity budget

**Files:**
- Modify: `references/long-task-governance.md`
- Modify: `references/agent-delivery-contract.md` only where checkpoint/recovery contract is defined.
- Modify: `tests/behavioral-scenarios.md`
- Test: existing policy/contract tests matching these references.

**Interfaces:**
- Consumes: existing checkpoint and recovery evidence.
- Produces: recovery resumes from latest accepted checkpoint without mandatory A/B/C ceremony for ordinary tasks.

- [ ] Add a failing policy test/scenario proving a short ordinary task remains valid without checkpoints and a long/high-risk recovery may resume from an accepted checkpoint without replaying earlier accepted work.
- [ ] Verify RED against current wording/validator where applicable.
- [ ] Update contract text with complexity budget: zero new controller-required normal fields, no second state machine, checkpoints optional by default, accepted checkpoints are recovery anchors.
- [ ] Run policy/behavior tests GREEN.
- [ ] Commit `docs: bound checkpoint recovery complexity`.

### Task 5: Full verification, integration, and rollout evidence

**Files:**
- No new production scope unless verification reveals a defect directly caused by Tasks 1-4.

**Interfaces:**
- Consumes: all prior commits.
- Produces: clean `main`, pushed exact revision, installation/load handoff evidence.

- [ ] Run all Python tests and record pass count.
- [ ] Run all Node tests and record pass count.
- [ ] Run diff/scope checks and verify no SelfAlone business files changed.
- [ ] Run a temporary root+worktree behavioral harness: launch an external-agent fixture from worktree, confirm root observes same runtime record, terminal closes lease, READY/DONE produces no ghost recovery.
- [ ] Merge/fast-forward implementation to `main` only after all verification is GREEN; push `origin/main`.
- [ ] Record exact revision and notify SelfAlone controller using the established `exact revision → loaded ACK → ledger sync` rollout protocol.
