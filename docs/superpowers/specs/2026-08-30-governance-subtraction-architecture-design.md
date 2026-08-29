# Governance Subtraction Architecture Design

Date: 2026-08-30
Status: Proposed / user-approved direction, pending written-spec review

## 1. Problem Statement

Adaptive Delivery has accumulated several individually reasonable controls that now overlap in responsibility across Assignment identity, runtime state, ledger state, lifecycle triggers, and recovery policy. The observed failure chain in `M1-F4-C-SERVER-GATE` demonstrates that this overlap can reduce delivery throughput instead of improving it.

The concrete incident sequence was:

- one business work package remained `M1-F4-C-SERVER-GATE`;
- execution was repeatedly reissued as `...-B-01` through `...-B-05`;
- every new Assignment restarted at attempt 1 and therefore reset recovery accounting;
- the external runner reported `outcome=success` whenever the provider process exited with code 0, even when no valid owned-scope delivery existed;
- lifecycle/ledger checks could simultaneously surface stale READY-style guidance, BLOCKED state, and undeclared Assignment-like IDs;
- controller effort shifted toward reconciling governance metadata rather than advancing the product candidate.

This design removes duplicated state ownership instead of adding another guard layer.

## 2. Design Goals

1. Prevent recovery-budget reset by merely changing `assignment_id` while the underlying execution contract is materially unchanged.
2. Separate transport/process completion from delivery/business success.
3. Keep the project ledger task-oriented; do not require Assignment rows in the ledger.
4. Make lifecycle decisions derive from current canonical facts rather than accumulated stale triggers.
5. Reduce controller-authored governance data and total number of concepts the controller must actively manage.
6. Preserve the useful safety properties already gained: pre-spawn ACK validation, shared Git-common-dir runtime, rule handshake, bounded recovery, evidence-aware progress, independent review, and integration verification.
7. Preserve accepted checkpoints during recovery; do not force rework of already verified stages.

## 3. Non-Goals

- Do not introduce a second project state machine.
- Do not add a new project-level lifecycle state.
- Do not require ordinary short Assignments to author checkpoint metadata.
- Do not move product implementation details into Adaptive Delivery.
- Do not weaken ACK, ownership, rule-handshake, review, or integration gates.
- Do not automatically authorize provider/model route changes that remain project/user authorization decisions.

## 4. Target Architecture

The control model has four responsibilities and only one owner for each fact type.

### 4.1 Task: project delivery state

Source of truth: canonical project ledger.

A Task answers: **What product/governance work package is the project currently responsible for?**

Task owns only:

- task ID;
- main state (`READY / ACTIVE / VERIFY / BLOCKED / CLOSED`, with legacy parse compatibility);
- owner;
- current checkpoint label when needed;
- current blocker and wake condition;
- next product/control action;
- stable evidence links required for later recovery/audit.

Task does **not** own:

- Assignment IDs;
- attempts;
- leases;
- runtime PIDs;
- heartbeat timestamps;
- execution sessions;
- provider process exit status.

An execution may use multiple Assignments, but the ledger keeps one Task row.

### 4.2 Execution Lineage: bounded retry identity

Source of truth: machine runtime state under Git common-dir.

Execution Lineage answers: **Is this a materially new execution strategy, or merely another retry of the same contract?**

The runtime derives a deterministic `contract_fingerprint` from canonicalized Assignment contract fields. Minimum inputs:

- `task_id`;
- normalized `primary_goal`;
- normalized `success_criteria`;
- normalized `owned_scope`;
- provider / execution strategy class;
- optional explicit strategy discriminator only when the strategy actually changes.

Fields that must not create a new lineage by themselves:

- `assignment_id`;
- `agent_id` naming changes;
- `session_id`;
- prose-only wording changes that normalize to the same contract;
- changing a stop-condition sentence without changing delivery semantics;
- reissuing the same task to the same strategy after a no-write / false-green result.

A lineage stores recovery accounting independent of Assignment IDs.

Default policy:

- initial execution = attempt 1;
- at most two recoveries for the same lineage;
- a fourth execution with the same lineage is rejected before provider spawn;
- a new lineage is permitted only when contract/strategy materially changes.

Examples of valid new-lineage reasons:

- provider/model route changes under valid authorization;
- owned scope is materially narrowed/split;
- execution strategy changes (for example implementation writer -> environment-repair task -> implementation writer after verified repair);
- a real environment defect is fixed and the new execution contract explicitly binds that repaired precondition;
- one Task is split into genuinely distinct deliverables.

The machine, not the controller, calculates the fingerprint. The controller may state a strategy change in the Assignment contract, but cannot reset the budget by inventing a new ID.

### 4.3 Runtime Fact: transport/process truth only

Source of truth: canonical runtime ledger under Git common-dir.

Runtime answers: **What did the execution transport actually do?**

Runtime owns:

- started / heartbeat / progress / terminal transport events;
- process or provider exit status;
- observed Git HEAD/status fingerprints;
- evidence/artifact fingerprints emitted by the execution transport;
- lineage fingerprint and lineage recovery count;
- worktree / provider / session binding.

Runtime must not infer delivery PASS from provider exit code 0.

Terminal transport semantics become:

- `process_completed` — provider process exited normally;
- `process_failed` — provider/transport failed;
- `process_cancelled` — execution cancelled;
- optionally `process_blocked` only for a transport-level blocked condition with machine evidence.

Delivery result is separate:

- `PASS` — success criteria have verifiable delivery evidence;
- `FAIL` — execution completed but criteria are not satisfied;
- `BLOCKED` — execution returned a concrete external/authorization/environment blocker that cannot be resolved inside the same lineage;
- `UNRESOLVED` — provider process completed but no authoritative delivery verdict/evidence was produced.

For code-producing Assignments, `PASS` cannot be synthesized from exit code. It requires the Assignment-delivery contract's evidence requirements, such as an authoritative Git delta/artifact plus the required test/evidence receipt. A no-write process exit 0 therefore becomes `process_completed + delivery=UNRESOLVED/FAIL`, never runtime success.

### 4.4 Integration: candidate/main truth

Source of truth: Git + review/integration receipts.

Integration answers: **Did a verified delivery become an accepted project result?**

It owns:

- exact candidate revision;
- non-author reviewer result when required;
- candidate ancestry in current main;
- current-main regression/acceptance evidence;
- final Task transition to VERIFY/CLOSED or next checkpoint.

No runtime or ledger text may substitute for candidate ancestry or current-main verification.

## 5. Simplified Machine Gates

Controller-facing governance is reduced to three gates.

### 5.1 Dispatch Gate

Question: **May this execution start?**

Internally checks:

- Task is dispatchable / valid owner;
- delivered ACK matches exact task/repo/head/scope;
- rule handshake is current where required;
- ownership/worktree rules are satisfied;
- execution lineage recovery budget is available.

Controller sees one result: allowed or blocked with one structured reason.

### 5.2 Delivery Gate

Question: **What did this execution actually deliver?**

Consumes:

- transport terminal fact;
- Git/evidence/artifact delta;
- Assignment success criteria;
- delivery receipt/verdict.

Produces one normalized result:

- PASS;
- FAIL;
- BLOCKED;
- UNRESOLVED.

A provider process exiting 0 is not sufficient for PASS.

### 5.3 Integration Gate

Question: **Can this delivery safely change main/project state?**

Checks candidate/review/main ancestry/regression evidence. This preserves current review/integration guarantees without exposing every internal receipt as a controller-managed state.

## 6. Ledger Simplification

The canonical ledger must remain Task-oriented.

Changes:

1. `ledger_consistency_guard` must distinguish Task IDs from Assignment IDs and must not require an Assignment ID to become a task row merely because it appears in execution evidence.
2. The "next visible checkpoint" should reference the Task (`M1-F4-C-SERVER-GATE`) and optional human checkpoint label (`Checkpoint B`), not a generated Assignment ID.
3. Assignment/session/lease/PID details belong in ephemeral machine receipts, not ledger rows or ledger headers.
4. The controller may retain one concise blocker/wake condition in BLOCKED state, but repeated execution history stays in runtime/audit data.

Migration behavior:

- existing ledgers are not bulk rewritten;
- only touched rows are normalized as work naturally progresses;
- existing recovery evidence remains valid historical evidence.

## 7. Lifecycle Simplification

Lifecycle must be a projection of the **current snapshot**, not an accumulated history of old decision triggers.

Current-state trigger rules:

- if Task is READY now, lifecycle may request dispatch;
- if Task is ACTIVE and current bound execution is terminal/unhealthy, lifecycle requests delivery/recovery handling;
- if Task is BLOCKED now, lifecycle must not simultaneously emit READY guidance for the same Task;
- if Task is VERIFY, lifecycle focuses on review/integration/acceptance, not dispatch;
- historical triggers remain audit data only and cannot continue affecting current decisions after their source condition disappears.

`pending_control_event` may persist until the controller closes the current discrepancy, but the human/system guidance is regenerated from the fresh snapshot every time.

This removes stale combinations such as `BLOCKED + READY:<same-task>`.

## 8. Recovery Semantics

Recovery is a property of Execution Lineage, not a way to create endless Assignments.

Flow:

1. Dispatch Gate calculates lineage fingerprint and checks budget.
2. Execution attempt starts and runtime records facts.
3. Delivery Gate returns PASS / FAIL / BLOCKED / UNRESOLVED.
4. PASS moves to Integration Gate.
5. FAIL/UNRESOLVED may recover within the same lineage while budget remains.
6. Budget exhaustion requires one of:
   - a genuinely changed strategy/contract -> new lineage;
   - a real BLOCKED condition with wake evidence;
   - explicit termination/supersession.

The controller may not claim "strategy changed" merely because the prompt says "do it for real this time" or changes the Agent/session name.

## 9. Evidence Semantics

For code/versioned artifacts, a Delivery PASS must bind to authoritative evidence. Minimum default behavior:

- provider exit 0 + no Git/evidence/artifact delta -> UNRESOLVED/FAIL;
- Git/status delta without required tests -> not PASS when tests are success criteria;
- prose saying tests passed without authoritative test evidence -> not PASS;
- valid blocker evidence may produce BLOCKED even if no code delta exists;
- accepted checkpoint evidence is reused across recovery and must not be rerun unless invalidated by the changed scope/HEAD.

The exact evidence policy remains risk-tailored; this architecture does not force heavy tests on ordinary low-risk tasks.

## 10. Complexity Budget / Deletion Requirement

This change is successful only if it reduces controller-facing complexity.

Implementation constraints:

- no new main project states;
- no new mandatory controller-authored runtime fields;
- lineage fingerprint is machine-derived;
- do not create a second ledger/runtime database;
- prefer replacing/removing old outcome/trigger logic over layering new parallel checks;
- delete or simplify obsolete code paths made unnecessary by the new model;
- documentation must present the three gates as the primary controller mental model.

A patch that merely adds lineage metadata while preserving all old duplicate success/trigger semantics does not satisfy this design.

## 11. Compatibility and Migration

Runtime state schema may be extended compatibly. Existing leases without lineage metadata are treated as legacy lineages and may be normalized when next touched; no project worktree is discarded solely for migration.

Existing Assignment receipts remain audit history. The implementation must not retroactively reinterpret old provider exits as verified delivery PASS.

Current `M1-F4-C-SERVER-GATE` remains BLOCKED under its existing authorization gate until a legitimate backend route/strategy is available. Implementing this governance architecture must not silently resume or redo Checkpoint A.

## 12. Test Strategy

Required failing tests before implementation:

1. Same Task + materially identical contract + new Assignment IDs B-01/B-02/B-03/B-04 must exhaust the same lineage budget and reject the fourth execution before spawn.
2. Changing only agent/session/wording must not reset lineage.
3. A material authorized provider/strategy change must produce a new lineage.
4. Provider exit 0 with no delivery evidence must not produce delivery PASS/success.
5. Provider exit 0 with valid required evidence may produce Delivery PASS.
6. Ledger validator must allow Assignment execution evidence without requiring an Assignment task row.
7. Next checkpoint referring to the Task + `Checkpoint B` must validate without generated Assignment IDs.
8. Lifecycle must not emit READY guidance when the same Task is currently BLOCKED.
9. Lifecycle must not retain stale terminal/READY triggers after the source condition resolves.
10. Existing rule-handshake, ACK-before-spawn, cross-worktree runtime, independent-review and integration tests must remain green.
11. Regression scenario matching the real `M1-F4-C-SERVER-GATE B-01...B-05` pattern must demonstrate that the new architecture stops repeated same-lineage execution after the configured budget.

## 13. Acceptance Criteria

The architecture is complete when all are true:

- same-contract recovery cannot be reset by changing Assignment ID;
- runtime distinguishes process completion from delivery verdict;
- provider exit 0 cannot create a false delivery success;
- ledger remains one Task row for repeated execution attempts;
- lifecycle guidance cannot contradict the current Task state;
- existing safety gates remain functional;
- full Python/Node governance suites pass;
- independent non-author review attacks lineage reset, false-success, stale-trigger, and ledger-bloat counterexamples;
- installed Adaptive Delivery revision is synchronized and loaded by the unique SelfAlone controller through the existing machine handshake;
- no existing accepted SelfAlone product checkpoint is redone solely because of this governance migration.

## 14. Expected Operational Result

The same incident should become:

`SERVER-GATE ACTIVE`
→ lineage L1 attempt 1
→ process completes but delivery FAIL/UNRESOLVED
→ L1 recovery 1
→ FAIL
→ L1 recovery 2
→ FAIL
→ lineage budget exhausted
→ controller must choose a materially new authorized strategy or BLOCKED.

The ledger remains one `SERVER-GATE` row throughout. Runtime retains the execution evidence. The controller handles three decisions—dispatch, delivery, integration—rather than reconciling several overlapping state systems.
