# Agent Runtime Liveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ACTIVE` and `RECOVERING` mechanically depend on current runtime evidence, with 20-minute heartbeat leases, 30-minute progress deadlines, and a 15-minute stale-progress grace.

**Architecture:** Add one canonical runtime-lease module and normalized lifecycle receipt store, then join that evidence into assignment validation, lifecycle snapshots, terminal control-event gating, and external/native lifecycle bridges. Static lint compatibility remains, but project control-event closure fails closed for stale or missing runtime evidence.

**Tech Stack:** Python 3 standard library, existing unittest governance suite, Node.js external-agent route tests.

**Spec:** `docs/superpowers/specs/2026-08-29-agent-runtime-liveness-design.md`

## Global Constraints

- Heartbeat lease TTL: exactly 20 minutes by default.
- Progress deadline: exactly 30 minutes by default.
- Progress-stale grace: exactly 15 additional minutes by default.
- Do not require PID when provider/session heartbeat evidence is available.
- Do not synthesize healthy runtime evidence from ledger text or delivered ACK.
- Do not modify SelfAlone product code or directly mutate its task ledger/control plane.
- Preserve READY/candidate/review/retained-candidate semantics.

---

### Task 1: Runtime lease model and normalized receipts

**Files:**
- Create: `scripts/assignment_runtime.py`
- Create: `tests/test_assignment_runtime.py`

**Interfaces:**
- Produces: `RuntimePolicy`, `apply_receipt(state, receipt, now=None)`, `evaluate_lease(lease, now=None, process_probe=None)`, `load_runtime_state(repo)`, `save_runtime_state(repo, state)`.

- [ ] Write deterministic failing tests for start, heartbeat, progress, terminal, identity mismatch, provider-without-PID, lease expiry, 30-minute progress stale, and 15-minute grace escalation.
- [ ] Run `python3 -m unittest tests.test_assignment_runtime -v` and verify RED because the module/behavior does not exist.
- [ ] Implement the minimal runtime state/receipt evaluator with centralized 20/30/15 defaults and atomic JSON persistence under `.git/adaptive-delivery/runtime-assignments.json`.
- [ ] Re-run the runtime tests and verify GREEN.
- [ ] Commit runtime model + tests.

### Task 2: Assignment and ledger runtime-aware validation

**Files:**
- Modify: `scripts/assignment_lease_guard.py`
- Modify: `scripts/ledger_consistency_guard.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Consumes: Task 1 lease evaluator/state loader.
- Produces: optional runtime-aware ACTIVE/RECOVERING validation while preserving pure static lint mode.

- [ ] Add failing governance tests: delivered ACK without runtime lease cannot satisfy runtime-aware ACTIVE; healthy matching lease can; terminal/unknown/mismatched lease cannot; RECOVERING with stale recovery evidence is rejected.
- [ ] Run the scoped governance tests and verify RED.
- [ ] Add runtime-context validation with no change to existing static-only callers.
- [ ] Run scoped governance tests and verify GREEN.
- [ ] Commit validation changes.

### Task 3: Lifecycle snapshot and liveness triggers

**Files:**
- Modify: `scripts/lifecycle_hook.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Consumes: Task 1 runtime decisions plus canonical ledger ACTIVE/RECOVERING IDs.
- Produces snapshot keys `active_assignments`, `recovering_assignments`, `assignment_liveness`, `stale_active_ids`, `progress_stale_ids`, `terminal_active_ids`; trigger labels from the spec.

- [ ] Add failing tests proving an Agent death/stale lease changes lifecycle triggers even when main HEAD, ledger hash, worktree status, READY, and candidate set are unchanged.
- [ ] Verify RED.
- [ ] Join runtime state into `project_snapshot()` and emit deterministic ACTIVE/RECOVERING liveness triggers.
- [ ] Verify GREEN and existing lifecycle regressions.
- [ ] Commit lifecycle changes.

### Task 4: Terminal control-event fail-closed gate

**Files:**
- Modify: `scripts/control_event_guard.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Consumes: runtime state/evaluator and in-flight ledger tasks.
- Produces: explicit per-task runtime decision in control snapshot; blocks PASS for missing, stale, unhealthy, terminal ACTIVE or stalled RECOVERING.

- [ ] Add failing tests for stale ACTIVE and stalled RECOVERING blocking `--repo`, plus healthy ACTIVE compatibility.
- [ ] Verify RED.
- [ ] Add runtime liveness gate before terminal receipt generation.
- [ ] Verify GREEN.
- [ ] Commit control-event changes.

### Task 5: External/native lifecycle receipt bridge

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Modify: `scripts/run_external_agent.mjs`
- Modify: `tests/test_web_lifecycle_bridge.py`
- Modify: `tests/external-agent-routing.test.mjs`

**Interfaces:**
- Consumes: normalized receipt schema from Task 1.
- Produces external `assignment_started`, `assignment_heartbeat`, `assignment_progress`, `assignment_terminal`; translates native SubagentStop into equivalent terminal runtime semantics when assignment identity exists.

- [ ] Add failing tests for external start/terminal receipt emission, no-PID provider heartbeat, malformed identity fail-closed, and native/external terminal equivalence.
- [ ] Verify Python and Node RED tests.
- [ ] Implement provider-neutral receipt emission/translation without coupling the evaluator to Grok/Kimi-specific payloads.
- [ ] Verify scoped Python/Node GREEN tests.
- [ ] Commit bridge changes.

### Task 6: Governance documentation and full regression

**Files:**
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `tests/behavioral-scenarios.md`

**Interfaces:**
- Documents the runtime-backed ACTIVE contract and recovery semantics implemented by Tasks 1-5.

- [ ] Add behavioral scenario asserting ACK-only ACTIVE is invalid and external Agent disappearance is detected without Git/ledger/candidate movement.
- [ ] Update governance docs with 20/30/15 defaults, receipt contract, and control-event blocking behavior.
- [ ] Run `python3 -m unittest discover -s tests -p 'test_*.py'` and `node --test tests/external-agent-routing.test.mjs`.
- [ ] Inspect `git diff --check` and final branch diff against this plan/spec.
- [ ] Commit docs/regression updates.
