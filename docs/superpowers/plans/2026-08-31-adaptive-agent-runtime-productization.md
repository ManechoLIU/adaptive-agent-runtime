# Adaptive Agent Runtime Productization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Productize `adaptive-delivery` as Adaptive Agent Runtime without breaking current installations, while adding host capability detection, Web restore/fallback, Controller Self-Check, side-effect idempotency protection, and Assignment strategy freezing.

**Architecture:** Keep the existing four-Gate/canonical-runtime architecture as the only execution truth. Add compatibility metadata and adapters around it, then add two narrow machine guards inside existing Assignment/Delivery semantics. Do not add a second task state machine, outbox, or Web-specific ledger.

**Tech Stack:** Python 3 stdlib, Node.js runner, Git common-dir state, existing Codex lifecycle hooks, AI-Bridge Web bridge, unittest/node:test.

**Spec:** `docs/superpowers/specs/2026-08-31-adaptive-agent-runtime-productization-design.md`

## Global Constraints

- Existing `adaptive-delivery` machine state remains readable and canonical during migration.
- No second runtime/state directory is created for the renamed product.
- No new A2A server, outbox, event queue, or project state machine.
- New behavior is TDD-first and existing 285-test baseline must remain green.
- No push to remote; local commits are allowed.

---

### Task 1: Product identity compatibility and installer capability report

**Files:**
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `scripts/install_skill.py`
- Modify: `scripts/project_state.py`
- Modify: `tests/test_skill_structure.py`
- Modify: `tests/test_rule_handshake.py`
- Create/Modify: `tests/test_install_skill.py` if a focused installer test file is clearer.

**Interfaces:**
- Produces canonical product metadata for `Adaptive Agent Runtime` and legacy alias `adaptive-delivery`.
- Produces an installer capability report with `enabled/degraded/blocked` status without changing canonical runtime location.

- [ ] Write failing tests proving the public name/new preferred ID and legacy alias coexist while `.git/adaptive-delivery` remains the single canonical state root.
- [ ] Run focused tests and confirm RED.
- [ ] Implement minimal identity metadata/installer capability detection; do not rename existing machine-state paths.
- [ ] Run focused tests and confirm GREEN.
- [ ] Commit locally.

### Task 2: Side-effect idempotency retry guard

**Files:**
- Modify: `scripts/assignment_runtime.py`
- Modify: `scripts/run_external_agent.mjs` only if runner receipts need the new facts.
- Modify: `references/agent-delivery-contract.md`
- Modify: `tests/test_assignment_runtime.py`
- Modify: `tests/external-agent-routing.test.mjs` if runner integration is changed.

**Interfaces:**
- Produces `side_effect_retry_decision(...)` or equivalent deterministic decision used by existing recovery paths.
- Consumes current terminal outcome/retry class, idempotency facts, and recovery budget.

- [ ] Write failing tests: unknown/ambiguous terminal outcome + non-idempotent side effect must not auto-retry; explicit idempotency guarantee may retry only within existing recovery budget.
- [ ] Run focused tests and confirm RED.
- [ ] Implement the smallest deterministic guard in existing runtime semantics.
- [ ] Run focused Python/Node tests and confirm GREEN.
- [ ] Commit locally.

### Task 3: Assignment execution strategy freeze

**Files:**
- Modify: `scripts/assignment_runtime.py`
- Modify: `scripts/run_external_agent.mjs`
- Modify: `references/agent-delivery-contract.md`
- Modify: `references/agent-model-routing.md`
- Modify: `tests/test_assignment_runtime.py`
- Modify: `tests/external-agent-routing.test.mjs`

**Interfaces:**
- Stores a deterministic `strategy_snapshot`/digest on first Assignment start.
- Rejects same-Assignment recovery if provider/model/tier/core transport drifts outside the frozen contract.
- Allows explicitly authorized peer-host fallback when host policy is frozen as allowed and records actual execution host.

- [ ] Write failing tests for silent provider/model drift and allowed host-only fallback.
- [ ] Run focused tests and confirm RED.
- [ ] Implement strategy snapshot/digest validation in canonical runtime and runner receipt generation.
- [ ] Run focused tests and confirm GREEN.
- [ ] Commit locally.

### Task 4: Controller Self-Check derived from formal scoring model

**Files:**
- Create: `scripts/controller_self_check.py`
- Create: `references/controller-self-check.md` only as generated/derived compact behavioral contract if needed; source of truth remains scoring model.
- Modify: `scripts/lifecycle_hook.py`
- Modify: `SKILL.md`
- Modify: `tests/test_controller_performance_scoring.py`
- Create/Modify: `tests/test_controller_self_check.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Derives/checks an exact short behavioral checklist from the installed formal scoring model digest.
- Exposes no numeric score or dimension score to the controller.
- Lifecycle injection uses current installed model and fails closed on model mismatch when self-check is required.

- [ ] Write failing tests that require checklist criteria but reject numeric score exposure.
- [ ] Run focused tests and confirm RED.
- [ ] Implement derivation/injection with exact installed model binding and no new scoring state machine.
- [ ] Run focused tests and confirm GREEN.
- [ ] Commit locally.

### Task 5: Host adapter capability abstraction and Web restore/fallback

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Modify: `scripts/lifecycle_hook.py`
- Modify: `references/long-task-governance.md`
- Modify: `references/agent-model-routing.md`
- Modify: `README.md`
- Modify: `tests/test_web_lifecycle_bridge.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Reports host adapter capabilities independently from AI-Bridge availability.
- Adds a deterministic Web SessionStart-equivalent restore payload bound to repo/controller when such binding is available.
- Handles `active writer` resume conflict as a machine-classified condition and uses existing same-controller/peer-host rules rather than creating a second controller.

- [ ] Write failing tests for no-AI-Bridge degraded mode, restore payload order, and active-writer classification/fallback behavior.
- [ ] Run focused tests and confirm RED.
- [ ] Implement adapter/capability helpers and same-controller resume/fallback behavior without new project state.
- [ ] Run focused tests and confirm GREEN.
- [ ] Commit locally.

### Task 6: Backward-compatible migration and install verification

**Files:**
- Modify: `scripts/install_skill.py`
- Modify: `scripts/rule_handshake.py`
- Modify: `scripts/controller_scoring_guard.py` only if installed-root discovery needs alias support.
- Modify: `scripts/controller_scoring_hook.py` only if installed-root discovery needs alias support.
- Modify: `README.md`
- Modify: relevant tests.

**Interfaces:**
- Existing install is upgraded in place without duplicating runtime/controller/receipt stores.
- Fresh install exposes new product identity while preserving legacy command alias.
- Old manifest/handshake remains consumable.

- [ ] Write clean-install and existing-install migration RED tests in temporary homes/repos.
- [ ] Run focused tests and confirm RED.
- [ ] Implement compatibility discovery/migration metadata and idempotent installer behavior.
- [ ] Run focused tests and confirm GREEN.
- [ ] Commit locally.

### Task 7: Full regression, independent review, and installed-copy rollout

**Files:**
- No feature code unless a failing regression identifies a scoped defect.
- Update docs/tests only for verified corrections.

**Interfaces:**
- Produces one candidate revision that passes full Python and Node suites plus install/handshake verification.

- [ ] Run `python3 -m unittest discover -s tests -v`.
- [ ] Run `node --test tests/external-agent-routing.test.mjs`.
- [ ] Run clean temporary install and existing-install upgrade checks.
- [ ] Perform an independent non-author review of the exact candidate revision; fix Critical/Important findings with TDD.
- [ ] Re-run full regression.
- [ ] Install the exact verified revision using `scripts/install_skill.py`; verify manifest hashes, hooks, handshake behavior, and existing project runtime state remains readable.
- [ ] Commit local integration state; do not push.
