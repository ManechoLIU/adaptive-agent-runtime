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

### Task 5: Derive runnable work before BLOCKED or yield

**Files:**
- Modify: `scripts/controller_state.py`
- Modify: `scripts/control_event_guard.py`
- Modify: `scripts/preblock_guard.py`
- Modify: `scripts/lifecycle_hook.py`
- Modify: `tests/test_controller_state.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Consumes: current canonical Task rows plus Git/runtime/candidate/authorization facts already available to the controller snapshot.
- Produces: a machine-derived `runnable_task_ids` projection and Stop/Yield decisions that do not trust READY labels alone.

- [ ] **Step 1: Write failing regressions for mislabeled PENDING work**

Create a project snapshot where Task A is BLOCKED and Task B is still `PENDING` in the ledger but has satisfied dependencies, no file/environment conflict, no integration ordering constraint, and no authorization blocker. Assert Task B is mechanically runnable and controller yield is rejected.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.test_controller_state tests.test_governance -k 'runnable or yield' -v`
Expected: FAIL because current guards inspect declared READY rows rather than deriving readiness from all open Tasks.

- [ ] **Step 3: Add one pure runnable derivation helper**

Implement a pure helper in the existing controller-state projection layer. It must derive from the canonical Task table and existing machine facts; it must not create a second state database or controller-authored readiness field. Return exact runnable Task IDs plus structured exclusion reasons for non-runnable open Tasks.

- [ ] **Step 4: Feed the same derived set into control and preblock gates**

`control_event_guard.py`, `preblock_guard.py`, and lifecycle Stop handling consume the same projection. Any runnable counterexample rejects project idle/block even when the ledger has not yet been rewritten to READY.

- [ ] **Step 5: Add true-block negative control**

Prove a project where every open Task waits on the same real external condition may still pass project-level BLOCKED/yield.

- [ ] **Step 6: Run focused tests GREEN and commit**

Run: `python3 -m unittest tests.test_controller_state tests.test_governance -v`
Commit: `fix: derive runnable work before controller yield`

---

### Task 6: Centralize authorized provider fallback in Dispatch Gate

**Files:**
- Modify: `references/agent-model-routing.md`
- Modify: `scripts/run_external_agent.mjs`
- Create only if needed for single responsibility: `scripts/dispatch_route.py` or `scripts/dispatch_route.mjs`
- Modify: `tests/external-agent-routing.test.mjs`
- Modify: `tests/test_skill_structure.py`

**Interfaces:**
- Consumes: preferred route, task category, complexity/risk, reasoning effort policy, failure classification, authorization constraints.
- Produces: one normalized route decision: `preferred`, `fallback`, or `blocked`, with exact provider/model/reasoning effort and structured reason.

- [ ] **Step 1: Write failing route-decision tests**

Cover: safe Grok failure -> authorized Codex fallback; safe Kimi failure -> authorized Codex fallback; simple work -> Luna, normal implementation/debug/review -> Terra, architecture/high-risk/root-cause -> Sol; unknown result/possible partial write, billing/credential boundary, or explicit provider pin -> no automatic fallback.

- [ ] **Step 2: Verify RED**

Run: `node --test tests/external-agent-routing.test.mjs --test-name-pattern='fallback|route decision'`
Expected: FAIL because fallback is not currently a single machine decision.

- [ ] **Step 3: Implement one shared resolver**

Keep provider authorization policy explicit. The resolver may select only routes already authorized by the project policy; it must not silently cross billing, credential, side-effect, or user/provider-pin boundaries.

- [ ] **Step 4: Replace contradictory controller-facing routing prose**

Update `agent-model-routing.md` so Desktop and Web both defer to the same resolver rather than one host silently downgrading while another blocks.

- [ ] **Step 5: Verify GREEN and commit**

Run: `node --test tests/external-agent-routing.test.mjs` and `python3 -m unittest tests.test_skill_structure -v`.
Commit: `fix: centralize authorized provider fallback`

---

### Task 7: Make Web native resume preflighted, observable, and fail-closed

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Modify: `tests/test_web_lifecycle_bridge.py`
- Modify: installer/LaunchAgent generation code only where the existing lifecycle LaunchAgent is authored.

**Interfaces:**
- Consumes: repo, unique controller registry, Codex path, runtime environment, receipt ID.
- Produces: explicit `RESUME_PENDING`, `RESUME_CONFIRMED`, or `RESUME_FAILED` state with bounded diagnostics; non-zero resume never closes lifecycle.

- [ ] **Step 1: Write failing 127 preflight regression**

Run the native-resume preflight under a LaunchAgent-like PATH that excludes `/opt/homebrew/bin`. Assert the bridge returns a structured runtime failure naming missing Node/runtime instead of launching into an opaque 127.

- [ ] **Step 2: Write failing success-path and fail-closed tests**

Assert a PATH containing required runtime starts the exact registered thread. Assert a fake Codex returning non-zero persists `RESUME_FAILED`, preserves the pending control condition, and never records normal Stop closure.

- [ ] **Step 3: Write failing diagnostics-retention test**

Assert detached execution no longer routes stderr to `/dev/null`; state retains return code, command metadata, timestamps, and a bounded `stderr_tail`. Exercise log truncation/rotation limits so diagnostics cannot grow without bound.

- [ ] **Step 4: Verify RED**

Run: `python3 -m unittest tests.test_web_lifecycle_bridge.WebLifecycleNativeStopTests -v`
Expected: failures for missing preflight/fail-closed/bounded diagnostics.

- [ ] **Step 5: Implement preflight and deterministic LaunchAgent environment**

Preflight repository existence, exactly one registered controller, Codex executable, Node/runtime resolution, and lightweight Codex execution. Ensure the authored LaunchAgent PATH contains the runtime search path required by the installed Codex/Node location.

- [ ] **Step 6: Preserve detached execution without discarding diagnostics**

Keep non-blocking/background behavior, but write stdout/stderr to bounded lifecycle diagnostics. Store a short stderr tail in the state receipt. Do not add a second lifecycle database.

- [ ] **Step 7: Implement fail-closed resume state machine as adapter state only**

The bridge adapter may track resume-attempt state but must not create a second project/task state machine. `RESUME_FAILED` leaves lifecycle pending and surfaces `WEB_LIFECYCLE_RESUME_FAILED`; bounded recovery reuses the same registered controller only.

- [ ] **Step 8: Verify GREEN and commit**

Run: `python3 -m unittest tests.test_web_lifecycle_bridge -v`.
Commit: `fix: fail closed on web controller resume`

---

### Task 8: Collapse controller guidance into four gates and prove Desktop/Web parity

**Files:**
- Modify: `SKILL.md`
- Modify: `references/agent-delivery-contract.md`
- Modify: `references/long-task-governance.md`
- Modify: `references/agent-model-routing.md`
- Modify: `tests/test_skill_structure.py`
- Modify: `tests/test_governance.py`
- Modify: `tests/test_web_lifecycle_bridge.py`

**Interfaces:**
- Produces: exactly four controller-facing decisions: Dispatch, Delivery, Integration, Stop/Yield. Desktop native hooks and Web adapter consume the same current-snapshot decisions.

- [ ] **Step 1: Add failing structure assertions**

Assert docs expose exactly the four controller-facing gates and map ACK/rule-handshake/lineage/fallback/runnable derivation to Dispatch, evidence to Delivery, Git/review to Integration, and BLOCKED/rollover/idle/resume closure to Stop/Yield.

- [ ] **Step 2: Add parity fixtures**

Feed equivalent snapshots through Desktop lifecycle decision code and Web translation/resume path. Required outcomes must match for READY, derived-runnable-PENDING, local BLOCKED with alternative work, candidate pending, true project block, and safe/unsafe fallback scenarios.

- [ ] **Step 3: Verify RED**

Run focused structure/governance/Web tests and confirm missing four-gate/parity behavior fails.

- [ ] **Step 4: Rewrite guidance by subtraction**

Delete or collapse duplicated controller instructions rather than layering new prose. Web bridge remains an adapter; no Web-only READY/task database is introduced.

- [ ] **Step 5: Verify GREEN and commit**

Run: `python3 -m unittest tests.test_skill_structure tests.test_governance tests.test_web_lifecycle_bridge -v`.
Commit: `docs: collapse controller governance into four gates`

---

### Task 9: Full regression, incident replay, independent review, install, and parity acceptance

**Files:**
- Modify only if verification reveals a defect covered by the approved spec.

**Interfaces:**
- Consumes: candidate revision from Tasks 1–8.
- Produces: exact reviewed candidate, full regression evidence, installed revision, and unique SelfAlone controller rule-handshake ACK without redoing accepted product checkpoints.

- [ ] **Step 1: Run full Python and Node suites**

Run: `python3 -m unittest discover -s tests -v` and `node --test tests/external-agent-routing.test.mjs`.
Expected: all PASS.

- [ ] **Step 2: Run syntax and diff checks**

Run: `python3 -m py_compile scripts/*.py` and `git diff --check`.
Expected: PASS.

- [ ] **Step 3: Replay the B-01 -> B-05 false-green/recovery incident**

Prove same-lineage budget rejects execution four before provider spawn, exit-0/no-evidence remains delivery unresolved, and only one Task row exists.

- [ ] **Step 4: Replay local-BLOCKED/global-progress counterexample**

Prove a BLOCKED Server Gate plus another mechanically runnable open package rejects yield and dispatches/activates the runnable package path.

- [ ] **Step 5: Replay Web 127 incident**

With LaunchAgent-like missing PATH, preflight reports missing runtime and preserves lifecycle pending. With corrected environment, native resume starts the exact registered thread. Fake non-zero resume produces `RESUME_FAILED` with bounded stderr evidence and no false Stop closure.

- [ ] **Step 6: Replay fallback safety matrix**

Prove safe external-route failure selects authorized Luna/Terra/Sol by task class, while unknown/partial-write/billing/credential/provider-pin cases block fallback.

- [ ] **Step 7: Dispatch independent non-author reviewer**

Reviewer attacks lineage reset, false success, ledger bloat, stale READY, PENDING-but-runnable omission, unsafe fallback, Web resume false closure, unbounded logging, Desktop/Web divergence, and accidental second state machine/controller.

- [ ] **Step 8: Integrate only after review PASS and rerun exact main verification**

Preserve unrelated work. Do not merge/push without the normal explicit integration boundary.

- [ ] **Step 9: Install exact verified main revision and verify manifest hashes**

Use the official installer; verify source revision and installed file hashes match.

- [ ] **Step 10: Require the existing unique SelfAlone controller to load and ACK the exact revision**

Reuse controller `01a03c61-5dd2-7553-968b-a3bc2f5777c9`; do not create a second controller and do not redo accepted SelfAlone checkpoints solely because governance changed.
