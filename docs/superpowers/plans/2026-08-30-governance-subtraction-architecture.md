# Governance Subtraction Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace duplicated controller/runtime/ledger recovery semantics with a smaller architecture where Task owns project state, Execution Lineage owns retry budget, Runtime records transport facts only, and Integration owns candidate/review/main closure.

**Architecture:** Keep the existing canonical Git-common-dir runtime store and ACK-first launch gate, but move retry identity from `assignment_id` to a deterministic execution-lineage fingerprint derived from the effective contract. Separate process completion from delivery verdict so exit code 0 can never imply Assignment PASS. Remove Assignment IDs from ledger validation and make lifecycle triggers pure functions of the current snapshot rather than historical trigger accumulation.

**Tech Stack:** Python 3 stdlib, Node.js ESM, unittest, node:test, Git worktrees.

**Spec:** `docs/superpowers/specs/2026-08-30-governance-subtraction-architecture-design.md`

## Global Constraints

- Do not modify SelfAlone application code.
- Do not create a second SelfAlone controller.
- Preserve canonical runtime storage under `git rev-parse --git-common-dir`.
- Preserve ACK-before-spawn and rule-handshake fail-closed behavior.
- Do not add a second project state machine or new controller-authored runtime fields.
- Ledger remains Task-oriented; Assignment/session/lease/attempt identities stay machine runtime facts.
- Same effective execution contract must share one recovery lineage even when Assignment IDs differ.
- Exit code 0 means transport/process completion only; delivery PASS requires explicit delivery evidence.
- Lifecycle decisions must reflect current snapshot state; stale READY/ACTIVE triggers may remain only in audit history, never current decisions.
- Ordinary short tasks do not gain mandatory checkpoints or approval layers.

---

### Task 1: Execution lineage fingerprint and shared recovery budget

**Files:**
- Modify: `scripts/assignment_runtime.py`
- Modify: `scripts/run_external_agent.mjs`
- Modify: `tests/test_assignment_runtime.py`
- Modify: `tests/external-agent-routing.test.mjs`

**Interfaces:**
- Consumes: existing Assignment ACK JSON with `task_id`, `primary_goal`, `success_criteria`, `owned_scope`, provider/engine strategy.
- Produces: deterministic `execution_lineage_id: str` and runtime lineage accounting that survives Assignment ID changes when the effective contract is unchanged.

- [ ] **Step 1: Write failing Python tests for lineage-stable recovery budget**

Add tests proving that two different Assignment IDs with the same effective contract share one lineage and cumulative recovery count, while a real strategy change gets a new lineage. Use deterministic inputs and assert the fourth same-lineage execution is rejected before runtime mutation.

- [ ] **Step 2: Run focused Python tests and verify RED**

Run: `python3 -m unittest tests.test_assignment_runtime.ExecutionLineageTests -v`
Expected: FAIL because no lineage fingerprint or lineage-level budget exists.

- [ ] **Step 3: Implement deterministic lineage derivation in Python runtime**

Add a pure function such as:

```python
def execution_lineage_id(*, task_id: str, primary_goal: str, success_criteria: list[str], owned_scope: list[str], strategy: str) -> str:
    canonical = json.dumps({
        "task_id": task_id.strip(),
        "primary_goal": primary_goal.strip(),
        "success_criteria": sorted(item.strip() for item in success_criteria),
        "owned_scope": sorted(item.strip() for item in owned_scope),
        "strategy": strategy.strip(),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Store lineage counters separately from per-Assignment leases in the same canonical runtime file. Starting a new Assignment with an existing lineage must inherit the lineage recovery count rather than reset to zero. Do not delete per-Assignment audit leases.

- [ ] **Step 4: Make Node runner compute and pass lineage inputs automatically**

Read the ACK file already required before spawn, derive the same canonical contract/strategy fingerprint, and include it in the runtime start receipt. Do not add a controller-authored field.

- [ ] **Step 5: Write failing Node regression for `B-01 → B-05` behavior**

Create a test fixture that launches distinct Assignment IDs against the same effective contract and proves only the initial execution plus two recoveries are admitted. A fourth same-lineage launch must fail before provider spawn.

- [ ] **Step 6: Run focused Node tests and verify RED before implementation, then GREEN after**

Run: `node --test tests/external-agent-routing.test.mjs --test-name-pattern='lineage|recovery budget'`
Expected after implementation: PASS.

- [ ] **Step 7: Run Python lineage tests GREEN**

Run: `python3 -m unittest tests.test_assignment_runtime.ExecutionLineageTests -v`
Expected: PASS.

- [ ] **Step 8: Commit Task 1**

```bash
git add scripts/assignment_runtime.py scripts/run_external_agent.mjs tests/test_assignment_runtime.py tests/external-agent-routing.test.mjs
git commit -m "fix: bind recovery budget to execution lineage"
```

---

### Task 2: Separate process completion from delivery verdict

**Files:**
- Modify: `scripts/run_external_agent.mjs`
- Modify: `scripts/assignment_runtime.py`
- Modify: `references/agent-delivery-contract.md`
- Modify: `tests/external-agent-routing.test.mjs`
- Modify: `tests/test_assignment_runtime.py`

**Interfaces:**
- Consumes: provider exit code plus optional explicit delivery receipt/evidence.
- Produces: runtime process fact (`process_completed`/`process_failed`) and an independent delivery verdict (`pass`/`fail`/`blocked`/`unresolved`) that cannot be inferred from exit code alone.

- [ ] **Step 1: Write failing Node tests for exit-0-with-no-evidence**

Add a test where the fake external agent exits 0 without owned-path delta, evidence, artifact, or explicit delivery receipt. Assert runtime terminal result is transport-completed but delivery is `unresolved`, not `success`/PASS.

- [ ] **Step 2: Run focused Node test and verify RED**

Run: `node --test tests/external-agent-routing.test.mjs --test-name-pattern='delivery verdict|exit zero'`
Expected: FAIL because runner currently writes `outcome=success` on exit code 0.

- [ ] **Step 3: Implement transport-only terminal semantics**

Change runner terminal emission so process exit determines transport status only. Example shape:

```json
{
  "terminal_state": "completed",
  "transport_outcome": "completed",
  "delivery_outcome": "unresolved",
  "summary": "external agent process completed"
}
```

If the external transport returns a structured delivery receipt, validate and persist `delivery_outcome` plus evidence/artifacts. Otherwise remain `unresolved`. Do not synthesize PASS from prose.

- [ ] **Step 4: Update Python runtime validation/evaluation**

Validate transport and delivery fields separately. `evaluate_lease()` should report terminal transport state without pretending the task succeeded. Preserve compatibility reading older receipts during migration, but new writes must use the new semantics.

- [ ] **Step 5: Add regression for false-GREEN rejection**

Add tests showing:

```text
exit 0 + no diff + no evidence => transport completed / delivery unresolved
exit 1 => transport failed
explicit delivery FAIL => delivery fail even when process exits 0
explicit PASS + evidence/artifact => delivery pass
```

- [ ] **Step 6: Update delivery contract documentation**

Replace any wording that equates runtime terminal success with Assignment success. State that Runtime answers “did the process execute?” while Delivery answers “did the contract pass?”.

- [ ] **Step 7: Run focused Python + Node tests GREEN**

Run:
`python3 -m unittest tests.test_assignment_runtime -v`
`node --test tests/external-agent-routing.test.mjs`
Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git add scripts/run_external_agent.mjs scripts/assignment_runtime.py references/agent-delivery-contract.md tests/external-agent-routing.test.mjs tests/test_assignment_runtime.py
git commit -m "fix: separate runtime completion from delivery success"
```

---

### Task 3: Keep Assignment identity out of the Task ledger

**Files:**
- Modify: `scripts/ledger_consistency_guard.py`
- Modify: `scripts/lint_governance.py`
- Modify: `references/long-task-governance.md`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Consumes: ledger Task rows plus human-readable checkpoint text that may mention machine Assignment IDs.
- Produces: validation that requires only actual Task IDs to have ledger rows; Assignment/session/lease IDs remain runtime-only identifiers.

- [ ] **Step 1: Write failing ledger regression**

Create a minimal ledger with one declared Task `M1-F4-C-SERVER-GATE` and checkpoint text mentioning Assignment `M1-F4-C-SERVER-GATE-B-01`. Assert the ledger is valid as long as the actual Task is referenced and open.

- [ ] **Step 2: Run focused governance test and verify RED**

Run: `python3 -m unittest tests.test_governance -k assignment_id_not_required_as_task_row -v`
Expected: FAIL with current “checkpoint references undeclared task ID” behavior.

- [ ] **Step 3: Implement explicit Task-vs-Assignment parsing rule**

Do not weaken validation globally. Introduce a narrow classifier that recognizes machine Assignment identifiers from their declared parent Task context, or validates checkpoint references against actual Task IDs first and excludes known Assignment/session/lease tokens from undeclared-task detection.

- [ ] **Step 4: Add negative tests**

Ensure truly undeclared Task IDs still fail, e.g. checkpoint references `M1-F9-UNKNOWN` with no corresponding Task row. Assignment exemption must not become a generic “ignore unknown IDs” escape hatch.

- [ ] **Step 5: Update governance docs**

State explicitly: ledger rows are Task-level only; Assignment/session/lease/attempt identities belong to canonical runtime/audit receipts and must not create extra Task rows.

- [ ] **Step 6: Run governance tests GREEN**

Run: `python3 -m unittest tests.test_governance -v`
Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add scripts/ledger_consistency_guard.py scripts/lint_governance.py references/long-task-governance.md tests/test_governance.py
git commit -m "fix: keep assignment ids out of task ledger"
```

---

### Task 4: Make lifecycle triggers pure current-snapshot decisions

**Files:**
- Modify: `scripts/lifecycle_hook.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Consumes: current ledger/runtime/candidate/rule-handshake snapshot plus prior snapshot only for edge detection/audit.
- Produces: current control triggers that never retain stale READY/ACTIVE decisions after the underlying state changes.

- [ ] **Step 1: Write failing lifecycle tests**

Add scenarios:

```text
READY -> BLOCKED => current triggers must not contain READY:<task>
ACTIVE + runtime terminal -> BLOCKED => no stale agent_session_terminal or READY dispatch instruction after BLOCKED snapshot is current
rule_update_pending -> current => rule trigger disappears from current decision
```

- [ ] **Step 2: Run focused lifecycle tests and verify RED**

Run: `python3 -m unittest tests.test_governance -k lifecycle_current_snapshot -v`
Expected: FAIL because prior trigger accumulation can retain stale current instructions.

- [ ] **Step 3: Refactor trigger calculation**

Keep prior snapshot only to detect newly changed facts, but rebuild actionable current triggers from the current snapshot each evaluation. If historical audit retention is needed, store it separately from `triggers` used for decisions.

- [ ] **Step 4: Simplify continuation text**

Continuation guidance must be conditional on the current Task state. A BLOCKED task must not receive READY dispatch language. An ACTIVE task with terminal runtime should get one reconcile/verify instruction, not an indefinite stale trigger.

- [ ] **Step 5: Run lifecycle/governance tests GREEN**

Run: `python3 -m unittest tests.test_governance -v`
Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add scripts/lifecycle_hook.py tests/test_governance.py
git commit -m "fix: derive lifecycle actions from current snapshot"
```

---

### Task 5: Consolidate the three machine gates and remove obsolete duplicated semantics

**Files:**
- Modify: `references/agent-delivery-contract.md`
- Modify: `references/long-task-governance.md`
- Modify: `SKILL.md`
- Modify: `tests/test_skill_structure.py`
- Modify: any implementation file only where dead duplicated logic is proven obsolete by Tasks 1–4.

**Interfaces:**
- Produces: one documented model with `Dispatch Gate`, `Delivery Gate`, and `Integration Gate`; all existing mechanisms are internal evidence to exactly one gate.

- [ ] **Step 1: Add failing structure assertions**

Assert the skill/docs expose exactly the three controller-facing gates and explicitly map ACK/rule handshake/lineage budget to Dispatch, delivery receipt/evidence to Delivery, and candidate/review/main regression to Integration.

- [ ] **Step 2: Run structure tests RED**

Run: `python3 -m unittest tests.test_skill_structure -v`
Expected: FAIL until documentation is consolidated.

- [ ] **Step 3: Rewrite controller-facing guidance by subtraction**

Remove duplicated language that asks the controller to manually reconcile Assignment identity, runtime success, and ledger Task state independently. Keep the detailed machine evidence references, but nest them under the three gates.

- [ ] **Step 4: Delete dead duplicate code paths only when covered**

If Tasks 1–4 make any previous compatibility branch or duplicate trigger state obsolete, remove it in the same task with a regression test. Do not refactor unrelated code.

- [ ] **Step 5: Run structure tests GREEN**

Run: `python3 -m unittest tests.test_skill_structure -v`
Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```bash
git add SKILL.md references/agent-delivery-contract.md references/long-task-governance.md tests/test_skill_structure.py scripts tests
git commit -m "docs: collapse controller governance into three gates"
```

---

### Task 6: Full regression, real B-01→B-05 counterexample, independent review, and release

**Files:**
- Modify only if verification reveals a defect covered by this spec.
- Test: full Python and Node suites.

**Interfaces:**
- Consumes: candidate revision produced by Tasks 1–5.
- Produces: exact candidate with full tests, independent non-author review, main integration, install manifest, and SelfAlone controller machine ACK.

- [ ] **Step 1: Run full Python suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: all PASS.

- [ ] **Step 2: Run full Node suite**

Run: `node --test tests/external-agent-routing.test.mjs`
Expected: all PASS.

- [ ] **Step 3: Run syntax/diff checks**

Run:
`python3 -m py_compile scripts/*.py`
`git diff --check`
Expected: PASS.

- [ ] **Step 4: Run the exact incident counterexample**

Using a temporary Git repo/worktree and fake provider, replay five distinct Assignment IDs that share the same effective Checkpoint-B contract. Expected:

```text
A1 admitted
A2 admitted as recovery 1
A3 admitted as recovery 2
A4 rejected before provider spawn because lineage budget exhausted
no A5 spawn possible without a genuinely changed strategy fingerprint
exit-0/no-evidence remains delivery unresolved, never PASS
ledger contains one Task row only
READY -> BLOCKED leaves no current READY trigger
```

- [ ] **Step 5: Dispatch independent non-author reviewer**

Reviewer must inspect the exact candidate revision and explicitly attack:

1. Can changing only Assignment ID reset recovery budget?
2. Can exit code 0 still become delivery PASS without evidence?
3. Can Assignment IDs force extra ledger Task rows?
4. Can stale READY survive after BLOCKED?
5. Did implementation add a second state machine or extra manual controller field?

Required verdict: `REVIEW_PASS` or exact failures.

- [ ] **Step 6: Integrate candidate into `adaptive-delivery main` only after PASS**

Use the existing clean integration path; preserve unrelated work.

- [ ] **Step 7: Re-run full verification on main**

Repeat Steps 1–4 against exact main revision.

- [ ] **Step 8: Install exact main revision**

Run the official `scripts/install_skill.py` path and verify installed manifest revision/file hashes match source.

- [ ] **Step 9: Require SelfAlone unique controller machine ACK**

The existing unique controller `01a03c61-5dd2-7553-968b-a3bc2f5777c9` must load the exact installed revision and complete rule-handshake ACK + existing ledger rule-version sync. Do not create a second controller and do not restart completed SelfAlone product checkpoints solely because governance changed.

- [ ] **Step 10: Commit any final verification-only documentation if needed**

Only if required by existing release practice; do not create a new governance report for its own sake.
