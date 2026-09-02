# Controller Governance Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse Adaptive Delivery control decisions into one canonical projection, thin the host adapters, remove duplicate decision state, and qualify the release with a hermetic end-to-end governance lifecycle before any SelfAlone installation.

**Architecture:** `TASK_LEDGER.md` remains the only project-state authority; machine files remain raw execution evidence; a new pure controller projection consumes those facts once and emits every control decision used by Dispatch, Delivery, Integration, and Stop/Yield. Host adapters normalize events and execute requested actions only; they do not derive policy. The refactor proceeds by compatibility-first migration, then deletion of duplicate derivation, then E2E qualification.

**Tech Stack:** Python 3 standard library + `unittest`, Node.js built-in test runner, Git CLI, existing Adaptive Delivery scripts and JSON receipts.

**Spec:** `docs/superpowers/specs/2026-08-30-controller-governance-convergence-design.md`

## Global Constraints

- `TASK_LEDGER.md` remains the only durable project-state authority.
- Main project states remain exactly `READY / ACTIVE / VERIFY / BLOCKED / CLOSED`.
- Do not add controller roles, project ledgers, provider routes, scoring dimensions, host policy, gate variants, or lifecycle states.
- Do not modify SelfAlone product code.
- Do not install any implementation revision into SelfAlone until the hermetic E2E qualification passes.
- Existing Delivery and Integration fail-closed guarantees must remain intact.
- No new JSON state file may be added unless it stores raw evidence that cannot be reconstructed from an existing source.
- Every task must reduce or centralize decision logic; moving duplicate logic without deleting the old authority does not satisfy the task.
- Use TDD for every behavior change: failing test first, observe RED, minimal implementation, GREEN, then refactor.

---

## File Map

- `scripts/controller_projection.py` — new pure projection module; sole owner of `runnable_tasks`, `live_assignments`, `candidate_actions`, `effective_rule_impact`, `rule_ack_required`, `control_event_state`, `wake_action`, `dispatch_allowed`, `yield_allowed`, `blocking_reasons`, and `required_controller_actions`.
- `scripts/controller_state.py` — retains parsing and five-state normalization helpers only; no host/rule/yield policy.
- `scripts/rule_handshake.py` — owns install/ACK evidence validation only; cumulative rule-impact helper becomes raw-input support for projection, not a downstream policy owner.
- `scripts/lifecycle_hook.py` — builds raw evidence, calls canonical projection, emits normalized controller actions; removes policy re-derivation.
- `scripts/control_event_guard.py` — validates event decisions against canonical projection; does not independently derive rule wake/runnable/yield policy.
- `scripts/preblock_guard.py` — consumes projection/project scan output rather than owning a second runnable derivation path.
- `scripts/web_lifecycle_bridge.py` — Web transport adapter only: translate event, execute requested same-controller resume, persist bounded raw diagnostics.
- `scripts/run_external_agent.mjs` — preserves route resolver and delivery envelope; Dispatch Gate receives projection permission rather than reinterpreting rule drift.
- `tests/test_controller_projection.py` — focused canonical-projection contract and migration equivalence tests.
- `tests/test_governance.py` — four-gate integration tests using canonical projection.
- `tests/test_web_lifecycle_bridge.py` — adapter-only transport tests; no policy fixtures owned by Web.
- `tests/test_rule_handshake.py` — install/ACK/cumulative evidence tests.
- `tests/e2e/test_governance_lifecycle.py` — hermetic release-qualification lifecycle.
- `references/long-task-governance.md`, `SKILL.md`, `README.md` — document one projection, adapter boundary, and E2E release qualification.

---

### Task 1: Introduce the Canonical Controller Projection

**Files:**
- Create: `scripts/controller_projection.py`
- Create: `tests/test_controller_projection.py`
- Modify: `scripts/controller_state.py`

**Interfaces:**
- Consumes: `ledger_records: list[dict[str, object]]`, `rule_status: dict[str, object]`, `assignment_runtime: dict[str, object]`, `candidate_evidence: list[dict[str, object]]`, `event_evidence: dict[str, object]`, `host_evidence: dict[str, object]`.
- Produces: `build_controller_projection(...) -> dict[str, object]` with exact keys `runnable_tasks`, `live_assignments`, `candidate_actions`, `effective_rule_impact`, `rule_ack_required`, `control_event_state`, `wake_action`, `dispatch_allowed`, `yield_allowed`, `blocking_reasons`, `required_controller_actions`.

- [ ] **Step 1: Write the failing projection shape and immutability tests**

```python
from scripts.controller_projection import build_controller_projection


def test_projection_emits_the_only_control_decision_shape():
    projection = build_controller_projection(
        ledger_records=[],
        rule_status={"state": "current", "effective_impact": "none", "blocking": False},
        assignment_runtime={},
        candidate_evidence=[],
        event_evidence={},
        host_evidence={},
    )
    assert set(projection) == {
        "runnable_tasks", "live_assignments", "candidate_actions",
        "effective_rule_impact", "rule_ack_required", "control_event_state",
        "wake_action", "dispatch_allowed", "yield_allowed",
        "blocking_reasons", "required_controller_actions",
    }
    assert projection["dispatch_allowed"] is True
    assert projection["yield_allowed"] is True
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python3 -m unittest -v tests.test_controller_projection`

Expected: FAIL because `scripts.controller_projection` does not exist.

- [ ] **Step 3: Implement the smallest pure projection skeleton**

Create `scripts/controller_projection.py` with a pure `build_controller_projection(...)` that initially delegates runnable parsing to existing `controller_state.derive_runnable_tasks`, copies validated `effective_impact` from rule evidence, and returns immutable-by-convention plain data with no file I/O, subprocess, environment lookup, or global state.

```python
def build_controller_projection(*, ledger_records, rule_status, assignment_runtime,
                                candidate_evidence, event_evidence, host_evidence):
    runnable = derive_runnable_tasks(ledger_records)["runnable_task_ids"]
    effective = str(rule_status.get("effective_impact", "none"))
    ack_required = str(rule_status.get("state", "")) in {"pending_ack", "ledger_stale", "integrity_error"}
    blocking = bool(rule_status.get("blocking"))
    return {
        "runnable_tasks": list(runnable),
        "live_assignments": [],
        "candidate_actions": [],
        "effective_rule_impact": effective,
        "rule_ack_required": ack_required,
        "control_event_state": "quiescent",
        "wake_action": "none",
        "dispatch_allowed": not blocking,
        "yield_allowed": not runnable and not blocking,
        "blocking_reasons": ["rule_handshake"] if blocking else [],
        "required_controller_actions": [],
    }
```

- [ ] **Step 4: Add tests proving the function is pure and deterministic**

Add a test that calls the function twice with deep-copied identical inputs, asserts identical output, and asserts inputs remain unchanged.

- [ ] **Step 5: Run focused tests to GREEN**

Run: `python3 -m unittest -v tests.test_controller_projection tests.test_controller_state`

Expected: PASS.

- [ ] **Step 6: Commit the independently testable projection foundation**

```bash
git add scripts/controller_projection.py scripts/controller_state.py tests/test_controller_projection.py
git commit -m "refactor: introduce canonical controller projection"
```

---

### Task 2: Move Cumulative Rule Impact and Wake Decisions into the Projection

**Files:**
- Modify: `scripts/controller_projection.py`
- Modify: `scripts/rule_handshake.py`
- Modify: `tests/test_controller_projection.py`
- Modify: `tests/test_rule_handshake.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Consumes: rule evidence with `impact`, `effective_impact`, `state`, `blocking`, `loaded_revision`, `installed_revision`, `unacked_changed_files`; event evidence with current live atomic work.
- Produces: `effective_rule_impact`, `rule_ack_required`, `wake_action` where `wake_action` is one of `none | natural_turn | after_event | resume_same_controller_now`.

- [ ] **Step 1: Add the exact regression test for the current split-brain bug**

```python
def test_later_none_impact_cannot_make_live_rule_debt_wait_for_next_turn():
    projection = build_controller_projection(
        ledger_records=[],
        rule_status={
            "state": "pending_ack",
            "impact": "none",
            "effective_impact": "live_assignments",
            "blocking": True,
            "loaded_revision": "old",
            "installed_revision": "new",
        },
        assignment_runtime={},
        candidate_evidence=[],
        event_evidence={"has_live_atomic_work": False},
        host_evidence={"controller_host": "web"},
    )
    assert projection["effective_rule_impact"] == "live_assignments"
    assert projection["wake_action"] == "resume_same_controller_now"
    assert projection["dispatch_allowed"] is False
```

- [ ] **Step 2: Run the regression and verify RED**

Run: `python3 -m unittest -v tests.test_controller_projection.ControllerProjectionTests.test_later_none_impact_cannot_make_live_rule_debt_wait_for_next_turn`

Expected: FAIL because the skeleton does not derive wake action from cumulative impact.

- [ ] **Step 3: Implement wake derivation in one projection helper**

Implement `_derive_wake_action(rule_status, event_state, has_live_atomic_work)` inside `controller_projection.py` with these exact semantics:

```python
if rule_status["state"] not in {"pending_ack", "ledger_stale", "integrity_error"}:
    return "none"
if rule_status["effective_impact"] != "live_assignments":
    return "natural_turn"
if has_live_atomic_work and event_state == "active":
    return "after_event"
return "resume_same_controller_now"
```

`impact` from the latest manifest is provenance only and must not be read by this helper.

- [ ] **Step 4: Demote `derive_rule_wake_policy()` in `rule_handshake.py`**

Remove it as an authoritative decision function. If compatibility callers still need it during this task, replace its implementation with a projection-backed compatibility wrapper and mark it for deletion in Task 5; it must not inspect `status["impact"]`.

- [ ] **Step 5: Update governance tests to construct rule evidence, not hand-written wake policy**

Replace tests that pass only `{"impact": ...}` with fixtures that include `effective_impact` and assert projection output. Preserve `test_later_nonimpacting_install_cannot_clear_unacked_live_impact_debt` in `tests/test_rule_handshake.py` as evidence validation, not wake-policy validation.

- [ ] **Step 6: Run rule/projection/governance tests**

Run: `python3 -m unittest -v tests.test_controller_projection tests.test_rule_handshake tests.test_governance`

Expected: PASS.

- [ ] **Step 7: Commit the rule-policy convergence**

```bash
git add scripts/controller_projection.py scripts/rule_handshake.py tests/test_controller_projection.py tests/test_rule_handshake.py tests/test_governance.py
git commit -m "refactor: centralize rule wake decisions"
```

---

### Task 3: Replace Boolean Pending Events with Evidence-Based Event Liveness

**Files:**
- Modify: `scripts/controller_projection.py`
- Modify: `scripts/lifecycle_hook.py`
- Modify: `tests/test_controller_projection.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Consumes raw event evidence: `event_id`, `started_at`, `last_progress_at`, `last_receipt_at`; current projection facts for live assignments, candidates, runnable work.
- Produces `control_event_state: active | quiescent | stale`, `wake_action`, `required_controller_actions`, `yield_allowed`.

- [ ] **Step 1: Add failing tests for quiescent and stale event promotion**

```python
def test_after_event_promotes_to_resume_when_no_atomic_work_remains():
    projection = build_controller_projection(
        ledger_records=[],
        rule_status={"state": "pending_ack", "effective_impact": "live_assignments", "blocking": True},
        assignment_runtime={},
        candidate_evidence=[],
        event_evidence={
            "event_id": "e1",
            "started_at": "2026-08-30T08:00:00+00:00",
            "last_progress_at": "2026-08-30T08:00:10+00:00",
            "last_receipt_at": None,
            "now": "2026-08-30T08:00:11+00:00",
        },
        host_evidence={},
    )
    assert projection["control_event_state"] == "quiescent"
    assert projection["wake_action"] == "resume_same_controller_now"
```

Add a stale variant whose last progress exceeds the existing bounded lifecycle continuation window; assert `control_event_state == "stale"` and the same resume action.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest -v tests.test_controller_projection`

Expected: FAIL because event state is still hard-coded.

- [ ] **Step 3: Implement a pure event classifier**

Add `_classify_event(...)` that uses evidence rather than persistent boolean authority. Exact rules:

```python
if has_live_atomic_work or candidate_actions or runnable_tasks:
    return "active"
if event_id and last_progress_at and event_age_exceeds_existing_stop_continuation_bound:
    return "stale"
return "quiescent"
```

Do not introduce a new durable lifecycle state; the classification exists only in the projection.

- [ ] **Step 4: Change lifecycle evidence persistence to timestamps, not decisions**

In `lifecycle_hook.py`, persist/refresh raw `event_id`, `started_at`, `last_progress_at`, and `last_receipt_at`. Keep `pending_control_event` only as a temporary compatibility cache if necessary; no branch may treat it as authoritative after projection is available.

- [ ] **Step 5: Add rebuild-equivalence test**

Persist an event evidence fixture, delete compatibility keys `pending_control_event` and `rule_wake_policy`, rebuild the projection, and assert the same `control_event_state`, `wake_action`, `dispatch_allowed`, and `yield_allowed`.

- [ ] **Step 6: Run focused and lifecycle tests**

Run: `python3 -m unittest -v tests.test_controller_projection tests.test_governance`

Expected: PASS.

- [ ] **Step 7: Commit event-liveness convergence**

```bash
git add scripts/controller_projection.py scripts/lifecycle_hook.py tests/test_controller_projection.py tests/test_governance.py
git commit -m "refactor: derive control event liveness from evidence"
```

---

### Task 4: Make All Four Gates Consume the Same Projection

**Files:**
- Modify: `scripts/lifecycle_hook.py`
- Modify: `scripts/control_event_guard.py`
- Modify: `scripts/preblock_guard.py`
- Modify: `scripts/run_external_agent.mjs`
- Modify: `tests/test_governance.py`
- Modify: `tests/external-agent-routing.test.mjs`

**Interfaces:**
- Consumes: canonical projection from `build_controller_projection(...)`.
- Produces: gate decisions without re-reading `impact`, re-deriving runnable tasks, or independently deciding yield/wake policy.

- [ ] **Step 1: Add source-boundary tests that fail while gates still re-derive policy**

Add a test in `tests/test_governance.py` that reads source files and asserts these forbidden patterns are absent from downstream gates after migration:

```python
for path in ["scripts/lifecycle_hook.py", "scripts/control_event_guard.py", "scripts/preblock_guard.py"]:
    text = Path(path).read_text()
    assert 'get("impact"' not in text
    assert "derive_rule_wake_policy(" not in text
```

Also assert `control_event_guard.py` and `preblock_guard.py` call the canonical projection or consume projection data supplied by a shared helper rather than invoking `derive_runnable_tasks` independently.

- [ ] **Step 2: Run the boundary test and verify RED**

Run: `python3 -m unittest -v tests.test_governance`

Expected: FAIL on current duplicate derivation.

- [ ] **Step 3: Convert Stop/Yield and control-event validation to projection fields**

Replace local branches with checks against:

```python
projection["yield_allowed"]
projection["required_controller_actions"]
projection["runnable_tasks"]
projection["candidate_actions"]
projection["wake_action"]
```

`control_event_guard.py` may validate the controller's proposed disposition against those fields, but it must not recalculate them from ledger/rule evidence.

- [ ] **Step 4: Convert preblock validation to projection-based blocking reasons**

`preblock_guard.py` receives/openly constructs the same canonical projection and rejects project block if `projection["runnable_tasks"]`, `projection["live_assignments"]`, `projection["candidate_actions"]`, or executable `required_controller_actions` are non-empty.

- [ ] **Step 5: Gate external Agent spawn on `dispatch_allowed`**

Expose projection permission through the existing assignment-bound launch contract. In `run_external_agent.mjs`, the pre-spawn gate must fail closed when the supplied canonical dispatch permission is false; the script must not re-evaluate rule handshake impact itself. Preserve route resolver, recovery budget, ACK, transport/delivery separation, and fallback semantics.

- [ ] **Step 6: Run Python and Node gate suites**

Run:

```bash
python3 -m unittest -v tests.test_controller_projection tests.test_governance tests.test_rule_handshake
node --test tests/external-agent-routing.test.mjs
```

Expected: all PASS.

- [ ] **Step 7: Commit four-gate convergence**

```bash
git add scripts/lifecycle_hook.py scripts/control_event_guard.py scripts/preblock_guard.py scripts/run_external_agent.mjs tests/test_governance.py tests/external-agent-routing.test.mjs
git commit -m "refactor: make four gates consume one projection"
```

---

### Task 5: Thin Web/Desktop Lifecycle Adapters and Delete Duplicate Decision State

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Modify: `scripts/lifecycle_hook.py`
- Modify: `scripts/rule_handshake.py`
- Modify: `tests/test_web_lifecycle_bridge.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Host adapter input: normalized host event + canonical requested action.
- Host adapter output: raw execution receipt such as `RESUME_PENDING`, `RESUME_CONFIRMED`, or `RESUME_FAILED` with bounded diagnostics.

- [ ] **Step 1: Add failing adapter-boundary tests**

Add tests asserting Web adapter source does not derive policy:

```python
text = Path("scripts/web_lifecycle_bridge.py").read_text()
assert "rule_wake_schedule_decision" not in text
assert 'get("rule_wake_policy"' not in text
assert "derive_runnable_tasks" not in text
```

Add a behavior test where canonical action `resume_same_controller_now` schedules the exact registered controller/revision and `natural_turn` performs no forced resume.

- [ ] **Step 2: Run Web tests and verify RED**

Run: `python3 -m unittest -v tests.test_web_lifecycle_bridge`

Expected: FAIL because `rule_wake_schedule_decision()` still exists.

- [ ] **Step 3: Replace Web policy with an action executor**

Replace `rule_wake_schedule_decision(lifecycle_state)` with an executor interface such as:

```python
def execute_controller_action(*, action: str, session_id: str, repo: Path,
                              revision: str | None, ...):
    if action == "resume_same_controller_now":
        return schedule_same_controller_resume(...)
    if action in {"none", "natural_turn", "after_event"}:
        return action
    raise ValueError(f"unknown canonical controller action: {action}")
```

The adapter may perform preflight, PATH setup, detached launch, diagnostics, and receipt persistence; it cannot decide which action applies.

- [ ] **Step 4: Demote/remove persisted decision cache fields**

Stop persisting `rule_wake_policy` as authoritative state. Keep reading legacy keys only for migration compatibility if required by old receipts. Prove that deleting `rule_wake_policy` and `pending_control_event` from a legacy lifecycle file does not change the rebuilt projection.

- [ ] **Step 5: Preserve fail-closed same-controller resume semantics**

Keep and rerun tests for missing runtime/node, ambiguous controller registration, stale receipt supersession, bounded stderr/stdout, and `RESUME_CONFIRMED` only after successful same-controller resume.

- [ ] **Step 6: Run adapter/lifecycle tests**

Run: `python3 -m unittest -v tests.test_web_lifecycle_bridge tests.test_governance tests.test_controller_projection`

Expected: PASS.

- [ ] **Step 7: Commit adapter slimming**

```bash
git add scripts/web_lifecycle_bridge.py scripts/lifecycle_hook.py scripts/rule_handshake.py tests/test_web_lifecycle_bridge.py tests/test_governance.py
git commit -m "refactor: reduce host lifecycle adapters to transport"
```

---

### Task 6: Add Hermetic End-to-End Governance Qualification

**Files:**
- Create: `tests/e2e/__init__.py`
- Create: `tests/e2e/test_governance_lifecycle.py`
- Modify: `scripts/install_skill.py`
- Modify: `tests/test_rule_handshake.py`

**Interfaces:**
- E2E fixture constructs temporary Git repositories/worktrees, a temporary installed skill root, temporary controller registry/state directories, and fake provider/host adapters.
- Produces one release-qualification result; SelfAlone installation remains prohibited unless it passes.

- [ ] **Step 1: Write the failing happy-path E2E lifecycle**

Create a test that executes the real Python/Node entry points where practical and covers this exact sequence:

```text
stable old revision
→ registered controller
→ runnable task
→ assignment ACK/start
→ live-impact rule install
→ cumulative impact projection
→ safe boundary
→ same-controller resume receipt
→ exact rule ACK
→ ledger rule revision sync
→ runnable re-derivation
→ safe external provider failure
→ authorized current-host fallback
→ valid delivery evidence
→ candidate
→ non-author review/integration evidence
→ Goal/Stop-Yield closure
```

The first RED should occur because no E2E qualification fixture exists and the release installer has no qualification input.

- [ ] **Step 2: Add controlled-failure E2E cases**

Add separate tests for:

```text
missing runtime lease → dispatch blocked
unknown/partial provider result → no automatic fallback/delivery success
resume failure → RESUME_FAILED and controller debt remains
stale event with no atomic work → same-controller wake promoted
live-impact revision followed by impact=none before ACK → effective impact remains live
```

- [ ] **Step 3: Run E2E tests and verify RED**

Run: `python3 -m unittest -v tests.e2e.test_governance_lifecycle`

Expected: FAIL until qualification plumbing exists.

- [ ] **Step 4: Add a release-qualification gate to the installer**

Extend `install_skill.py` so a governance source revision that changes control-path files requires a machine-readable qualification receipt generated by the E2E suite. The receipt must bind exact source revision and test suite identity. Do not create a new long-lived project-state file; keep the qualification receipt in the governance source/install evidence domain.

Example validation contract:

```python
if changes_control_path(source_revision):
    receipt = read_qualification_receipt(source_root)
    require(receipt["revision"] == source_revision)
    require(receipt["e2e_governance_lifecycle"] == "pass")
```

- [ ] **Step 5: Run the full E2E suite to GREEN**

Run: `python3 -m unittest -v tests.e2e.test_governance_lifecycle`

Expected: all happy-path and failure-path tests PASS.

- [ ] **Step 6: Prove installer fails closed without qualification and accepts exact qualified revision**

Run: `python3 -m unittest -v tests.test_rule_handshake tests.e2e.test_governance_lifecycle`

Expected: unqualified control-path revision rejected; exact qualified revision accepted.

- [ ] **Step 7: Commit E2E release qualification**

```bash
git add tests/e2e scripts/install_skill.py tests/test_rule_handshake.py
git commit -m "test: qualify governance releases end to end"
```

---

### Task 7: Delete Compatibility Authorities, Update Contracts, and Measure the Reduction

**Files:**
- Modify: `scripts/controller_projection.py`
- Modify: `scripts/rule_handshake.py`
- Modify: `scripts/lifecycle_hook.py`
- Modify: `scripts/control_event_guard.py`
- Modify: `scripts/preblock_guard.py`
- Modify: `scripts/web_lifecycle_bridge.py`
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `references/long-task-governance.md`
- Modify: `tests/test_skill_structure.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Produces the final architecture: one authoritative projection; all duplicate decision helpers removed or reduced to evidence parsers.

- [ ] **Step 1: Add source-structure tests for deleted authorities**

Assert the final source tree has no authoritative `derive_rule_wake_policy`, no Web `rule_wake_schedule_decision`, and no downstream `impact` policy read. Assert documentation says four gates consume `controller_projection` and host adapters are transport only.

- [ ] **Step 2: Run structure tests and verify RED if any compatibility authority remains**

Run: `python3 -m unittest -v tests.test_skill_structure tests.test_governance`

Expected: FAIL until obsolete authority is deleted.

- [ ] **Step 3: Delete obsolete helpers and compatibility decision branches**

Remove dead code made unnecessary by Tasks 1–6. Legacy persisted fields may still be tolerated when reading historical files, but no current write path should persist them as authority.

- [ ] **Step 4: Update documentation to the actual executable behavior**

Document only:

```text
Project State → Execution Evidence → Canonical Controller Projection → Four Gates → Host Action Executor
```

State explicitly that `impact` is provenance, `effective_rule_impact` is projected control fact, event liveness is projected from timestamps/current work, and E2E qualification is required before installation.

- [ ] **Step 5: Measure control-path complexity reduction**

Record pre-refactor baseline from this plan: core control path = `4187` lines across the nine audited scripts. Run:

```bash
wc -l scripts/controller_state.py scripts/controller_projection.py scripts/rule_handshake.py scripts/lifecycle_hook.py scripts/web_lifecycle_bridge.py scripts/control_event_guard.py scripts/preblock_guard.py scripts/assignment_runtime.py scripts/assignment_lease_guard.py scripts/run_external_agent.mjs
```

Acceptance: total authoritative policy code must decrease in duplicated branches/functions even if the new projection file adds lines. Document exact before/after counts in the commit message body or release note; do not claim slimming if total duplicated decision paths remain.

- [ ] **Step 6: Run the complete regression matrix**

Run:

```bash
python3 -m unittest discover -v
node --test tests/external-agent-routing.test.mjs
python3 -m compileall -q scripts tests
python3 scripts/lint_governance.py .
```

Expected: all PASS.

- [ ] **Step 7: Run the hermetic governance E2E qualification again on the exact final HEAD**

Run: `python3 -m unittest -v tests.e2e.test_governance_lifecycle`

Expected: PASS and qualification receipt bound to exact final HEAD.

- [ ] **Step 8: Commit the final convergence cleanup**

```bash
git add scripts SKILL.md README.md references tests
git commit -m "refactor: converge controller governance on one projection"
```

---

### Task 8: Release Candidate Verification Without Touching SelfAlone Product Code

**Files:**
- No SelfAlone product files modified.
- Governance install target only after exact final qualification.

**Interfaces:**
- Consumes: final qualified Adaptive Delivery revision and its exact E2E receipt.
- Produces: installed governance revision ready for the existing unique SelfAlone controller to load/ACK; no controller impersonation.

- [ ] **Step 1: Verify exact final source revision and clean source tree**

Run:

```bash
git rev-parse HEAD
git status --short
```

Expected: exact candidate revision captured; no unexpected source modifications.

- [ ] **Step 2: Re-run full Python, Node, compile, lint, and E2E qualification on that exact revision**

Use the commands from Task 7 Steps 6–7. Expected: all PASS.

- [ ] **Step 3: Install only the exact qualified revision**

Use `scripts/install_skill.py` with the final source revision and qualification receipt. Mark impact according to the actual changed control files; do not weaken it to `none` for convenience.

- [ ] **Step 4: Verify SelfAlone remains fail-closed until its registered controller loads the new revision**

Run the installed `rule_handshake.py status` and launch guard against `/path/to/SelfAlone`. Expected before controller ACK: pending exact revision and launch blocked if effective live impact exists.

- [ ] **Step 5: Do not ACK on behalf of the controller**

The existing unique controller session must read the installed rules and issue its own exact ACK. This implementation session only verifies machine state.

- [ ] **Step 6: After controller ACK, verify one real post-migration control cycle**

Observe, without taking over the controller, that the same controller produces a current handshake, synchronized ledger rule version, canonical projection, and a legitimate next action. If a product Assignment is runnable, verify dispatch uses the new projection and no host adapter re-derives policy.

- [ ] **Step 7: Record release evidence**

Capture exact governance revision, test counts, E2E qualification result, handshake state, and first real control-cycle outcome in the governance release evidence. Do not represent governance installation itself as SelfAlone product Delivery.
