# Control-Plane Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the machine-verifiable gap where a live Git candidate exists while the canonical Assignment lease still reports its baseline head and no candidate revision.

**Architecture:** Reuse the existing four gates, canonical controller action projection, continuation debt, and deviation/correction recurrence machinery. Do not add a scheduler or fifth gate. Extend Assignment progress receipts to carry a candidate revision only when it is bound to the same observed Git head, derive a `control_plane_reconcile:<assignment_id>` mandatory Controller action whenever the live worktree candidate and current nonterminal lease disagree, and require the ordinary project-wide control receipt to execute or hard-defer that action before Stop/Yield.

**Tech Stack:** Python 3 stdlib, unittest, Git worktrees, existing Adaptive Agent Runtime JSON receipts.

**Spec:** `references/long-task-governance.md`

## Global Constraints

- Preserve the existing unique logical Controller; never create a second controller or scheduler.
- Reuse Dispatch / Delivery / Integration / Stop-Yield gates and existing `controller_actions` + continuation debt.
- Candidate facts come from real Git worktree HEAD and canonical Runtime lease; prose cannot satisfy reconciliation.
- Runtime upgrades must advance from the currently installed revision and pass exact-candidate release gates before installation.

---

### Task 1: Candidate-bound progress receipt

**Files:**
- Modify: `scripts/assignment_runtime.py`
- Test: `tests/test_assignment_runtime.py`

**Interfaces:**
- Consumes: existing `assignment_progress` receipt identity and `last_observed_head`.
- Produces: canonical lease `candidate_revision` synchronized only when the progress receipt carries the same non-empty value as `last_observed_head`.

- [ ] Add a failing test proving a progress receipt can promote an observed Git HEAD to `candidate_revision`.
- [ ] Add a failing test rejecting candidate revision when it does not equal the same receipt's observed HEAD.
- [ ] Run the focused tests and verify RED.
- [ ] Implement the minimal receipt validation and persistence.
- [ ] Run the focused tests and verify GREEN.

### Task 2: Machine-derived control-plane reconciliation debt

**Files:**
- Modify: `scripts/control_event_guard.py`
- Test: `tests/test_governance.py`

**Interfaces:**
- Consumes: `candidates` mapping of worktree path to real unmerged HEAD and canonical runtime leases.
- Produces: `control_plane_reconcile:<assignment_id>` from `canonical_controller_action_projection` with exact assignment/task/worktree/expected revision facts.

- [ ] Add a failing test with a live candidate whose matching nonterminal lease still has baseline `last_observed_head` / null `candidate_revision`.
- [ ] Verify the projection currently omits the reconciliation action.
- [ ] Implement minimal action derivation without mutating Runtime state in the guard.
- [ ] Verify the action disappears once the lease is synchronized.
- [ ] Verify existing continuation-debt validation blocks closure until this action is resolved.

### Task 3: Lifecycle visibility and recurrence

**Files:**
- Modify only if required: `scripts/lifecycle_hook.py`
- Test: `tests/test_governance.py`

**Interfaces:**
- Consumes: canonical control action / validation failure.
- Produces: existing FAILED cycle evidence -> deviation -> mandatory correction -> recurrence escalation, with project-wide recompute on L3+.

- [ ] Add focused regression coverage that a missed control-plane reconciliation cannot silently yield.
- [ ] Confirm its validation failure enters the existing deviation/correction classifier rather than creating a new state machine.
- [ ] Run governance/lifecycle focused tests.

### Task 4: Release and SelfAlone installation alignment

**Files:**
- No hand-edited SelfAlone runtime files; use existing installer/handshake commands only.

**Interfaces:**
- Consumes: clean candidate revision descendant of installed `78285b066e37decd0836799f1ce875a7d24eeb2b`.
- Produces: installed manifest, loaded ACK, rule handshake, and live-E2E acceptance as required by impact classification.

- [ ] Run focused and release regression suites on the exact candidate revision.
- [ ] Review diff and commit only the Runtime governance fix; keep unrelated `main` scoring WIP isolated.
- [ ] Install the exact candidate revision into SelfAlone through `scripts/install_skill.py`.
- [ ] Re-evaluate rule handshake; complete required Controller ACK / ledger version sync / live E2E using the existing unique Controller.
- [ ] Re-check the real M1-F5-B candidate so canonical state and live Git facts converge.
