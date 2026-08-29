# Simplified Controller State Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved five-state controller projection, evidence-delta watchdog, bounded recovery budget, pre-execution ACK gate, and automatic review/integration/regression continuation gates without adding a second orchestration subsystem.

**Architecture:** Extend the existing runtime, lifecycle, lease, external-agent, and control-event guards. Add one small pure projection helper so all components share the same five-state vocabulary while legacy ledger states remain accepted as compatibility input.

**Tech Stack:** Python 3.11 stdlib, Node.js ESM stdlib, unittest, node:test, Git.

**Spec:** `docs/superpowers/specs/2026-08-29-simplified-controller-state-machine-design.md`

## Global Constraints

- Controller-facing main states are exactly `READY | ACTIVE | VERIFY | BLOCKED | CLOSED`.
- Legacy `PENDING / RECOVERING / DONE / SUPERSEDED` remain parser-compatible inputs and are mapped, not rejected.
- No daemon, database, second ledger, queue service, or second controller.
- Runtime progress advances only on a changed non-empty evidence fingerprint.
- Same-contract recovery budget is at most two recoveries; exhausted paths require a strategy-changing decision.
- Assignment-bound external execution must fail before spawn without exact delivered ACK evidence.
- Existing candidate retention, TDD review, Goal rollover, routing, and lifecycle behavior must stay green.

---

### Task 1: Canonical five-state projection

**Files:**
- Create: `scripts/controller_state.py`
- Create: `tests/test_controller_state.py`
- Modify: `scripts/lifecycle_hook.py`

**Interfaces:**
- Produces: `project_task_state(legacy_state, *, runtime=None, verification_gate=None, closure_reason=None, dispatchable=None) -> dict[str, object]`.
- Output keys: `main_state`, `dispatchable`, `health`, `verification_gate`, `closure_reason`.

- [ ] Write failing tests for all eight legacy-state mappings and runtime health overrides (`progress_stale`, unhealthy recovery, budget exhausted).
- [ ] Run `python3 -m unittest tests.test_controller_state -v` and confirm RED because helper is missing.
- [ ] Implement the pure mapping helper with no file I/O.
- [ ] Expose `task_projection` from `lifecycle_hook.project_snapshot()` for ledger tasks, deriving runtime health only from current lease evidence.
- [ ] Run controller-state and governance tests; confirm GREEN.
- [ ] Commit `feat: add five-state controller projection`.

### Task 2: Evidence-delta watchdog and recovery budget

**Files:**
- Modify: `scripts/assignment_runtime.py`
- Modify: `tests/test_assignment_runtime.py`
- Modify: `scripts/lifecycle_hook.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Progress fingerprint fields: `last_observed_head`, `last_observed_status_sha256`, `evidence_receipt_id`, `artifact_fingerprint`, `blocker_evidence_fingerprint`.
- `RuntimePolicy.max_recoveries = 2`.
- `recovery_count` persists across higher attempts for the same Assignment identity.

- [ ] Add failing runtime tests proving repeated/no-delta `assignment_progress` refreshes heartbeat only, not progress deadline; each changed fingerprint refreshes progress.
- [ ] Add failing tests proving attempts 1/2/3 preserve `recovery_count` 0/1/2, and an unhealthy/terminal attempt at count 2 derives `budget_exhausted` for another same-strategy recovery.
- [ ] Implement fingerprint comparison and recovery-count persistence minimally.
- [ ] Add lifecycle failing test that budget exhaustion surfaces `recovery_budget_exhausted:<task>` even with a current heartbeat/PID.
- [ ] Implement lifecycle trigger using derived runtime decision; do not create a new ledger state.
- [ ] Run runtime + governance suites; confirm GREEN.
- [ ] Commit `feat: bound assignment recovery and evidence progress`.

### Task 3: Pre-execution delivered-ACK launch gate

**Files:**
- Modify: `scripts/assignment_lease_guard.py`
- Modify: `scripts/run_external_agent.mjs`
- Modify: `tests/test_governance.py`
- Modify: `tests/external-agent-routing.test.mjs`

**Interfaces:**
- New runner argument: `--assignment-ack <json-file>` for Assignment-bound `--execute` calls.
- ACK JSON is the existing Assignment object validated by `assignment_lease_guard.py` and must exactly match CLI `assignment_id`, current `cwd`, current branch, and current `HEAD`.

- [ ] Add failing Node tests: Assignment-bound execute without `--assignment-ack` fails before fake runner spawn; wrong assignment ID/repository root/HEAD fails before spawn; exact ACK allows spawn.
- [ ] Extend lease guard with exact expected repository/head/assignment checks while keeping existing callers compatible.
- [ ] Implement runner preflight: load JSON, invoke/replicate validated lease semantics, compare exact Git facts, then spawn only after success.
- [ ] Keep unbound compatibility executions (no Assignment identity fields) unchanged.
- [ ] Run governance + Node routing tests; confirm GREEN.
- [ ] Commit `feat: gate external agent launch on delivered ack`.

### Task 4: Automatic continuation obligations

**Files:**
- Modify: `scripts/control_event_guard.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- `required_reviews[].verdict` drives candidate disposition.
- Candidate `decision=integrate` must carry exact integration evidence: `main_revision` and `regression_evidence` unless explicitly `queued` with `reason_code=ordered_integration`.
- Review `FAIL` requires candidate `decision=rework` with writer task + delivered ACK.
- Review `PASS` requires same candidate to be `integrate` or ordered-integration `queued` before event close.

- [ ] Add failing tests for PASS without integrate/ordered queue, FAIL without rework, integrate without main revision, integrate without regression evidence.
- [ ] Add positive tests for PASS+integrate+main/regression, PASS+ordered queue, FAIL+rework.
- [ ] Implement cross-validation by candidate revision; require required review `candidate_revision` when verdict is present.
- [ ] Preserve existing low-risk reviews without verdict semantics unless they declare a candidate revision/verdict.
- [ ] Run governance suite; confirm GREEN.
- [ ] Commit `feat: enforce review to integration closure chain`.

### Task 5: Documentation, compatibility, and full verification

**Files:**
- Modify: `references/long-task-governance.md`
- Modify: `references/agent-delivery-contract.md`
- Modify: `SKILL.md`
- Modify structure tests only if the new helper/doc must be explicitly packaged.

**Interfaces:**
- Teach five states first; label legacy detailed states as compatibility vocabulary/evidence details.
- Explain `RECOVERING` as legacy projection to `ACTIVE + health=recovering`, not a sixth main state.

- [ ] Update docs without duplicating the entire spec.
- [ ] Run `python3 -m unittest discover -s tests -p 'test_*.py'`.
- [ ] Run `node --test tests/external-agent-routing.test.mjs`.
- [ ] Run `git diff --check` and `python3 -m py_compile scripts/*.py`.
- [ ] Review diff for accidental state proliferation or unrelated refactors.
- [ ] Commit `docs: teach simplified controller lifecycle`.
