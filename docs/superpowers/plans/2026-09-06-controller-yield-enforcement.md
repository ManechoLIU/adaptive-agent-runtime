# Controller Yield Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Yield Gate rejection into durable same-controller continuation and recover Web MODEL END_TURN without requiring another user message.

**Architecture:** Persist rejected Stop/Yield as canonical lifecycle debt, make the Web subprocess bridge parse logical block decisions instead of transport status alone, and let the existing detached supervisor reopen continuation from canonical work/next-action debt when no model-end callback exists. Re-entry continues to use the canonical host-ownership and generation-fencing work already implemented on this branch.

**Tech Stack:** Python 3, unittest, JSON lifecycle state, launchd health supervisor, AI-Bridge Web re-entry adapter.

**Spec:** `docs/superpowers/specs/2026-09-06-controller-yield-enforcement-design.md`

## Global Constraints

- Keep exactly one logical Controller.
- MODEL END_TURN is not Controller Yield proof.
- Never silently fall back across Web and desktop hosts.
- Preserve continuation debt on recoverable infrastructure failures.
- Use TDD for every behavior change and run Live E2E before completion.

---

### Task 1: Persist rejected Yield as continuation debt

**Files:**
- Modify: `scripts/lifecycle_hook.py`
- Test: `tests/test_governance.py`

**Interfaces:**
- Produces: rejected `Stop` state with `pending_control_event=true`, `requires_user=false`, `YIELD_GATE_REJECTED` trigger and rejection reason metadata.

- [ ] Add failing tests for known-next-action and control-loop-required Stop rejection persistence.
- [ ] Run focused tests and verify RED for missing rejection state.
- [ ] Implement one helper that marks a blocked Stop/Yield as durable continuation debt before returning `decision=block`.
- [ ] Run focused tests and governance regressions.
- [ ] Commit.

### Task 2: Make Web lifecycle bridge honor logical block decisions

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Test: `tests/test_web_lifecycle_bridge.py`

**Interfaces:**
- Produces: structured lifecycle dispatch outcome containing transport return code and `yield_blocked`.
- Consumes: Task 1 persisted lifecycle debt.

- [ ] Add a failing test where lifecycle subprocess exits 0 but outputs `decision=block`.
- [ ] Add a failing post-shell test proving blocked Yield still arms same-controller continuation before the bridge returns blocked status.
- [ ] Implement structured dispatch parsing while preserving compatibility for non-block events.
- [ ] Route post-shell blocked decisions through wake/supervisor scheduling before returning 78.
- [ ] Run bridge tests and commit.

### Task 3: Recover callback-less Web END_TURN from canonical debt

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Test: `tests/test_web_agent_health_supervisor.py`

**Interfaces:**
- Consumes: canonical controller actions/runnable facts and lifecycle `next_action`.
- Produces: reopened `pending_control_event`, incremented wake generation, `runtime_continuation_debt_ids`, and Yield recovery metadata.

- [ ] Add a failing health-tick test for `next_action` with `requires_user=false`, `pending_control_event=false`, and no Stop callback.
- [ ] Verify RED.
- [ ] Add next-action debt to `controller_continuation_projection()` and atomically reopen continuation.
- [ ] Verify observation-only/requires-user negative cases remain closed.
- [ ] Run health-supervisor and bridge regressions and commit.

### Task 4: Combined regression and release readiness

**Files:**
- Modify docs only if behavior differs from spec.

**Interfaces:**
- Validates host ownership, Yield enforcement, terminal continuation, desktop recovery, governance and installer release contract together.

- [ ] Run `tests.test_controller_target_guard`.
- [ ] Run `tests.test_web_lifecycle_bridge`.
- [ ] Run `tests.test_web_agent_health_supervisor`.
- [ ] Run `tests.test_governance`, `tests.test_terminal_continuation`, `tests.test_desktop_turn_recovery`, and `tests.test_rule_handshake`.
- [ ] Run installer release regressions required by the manifest/installer.
- [ ] Run `git diff --check` and verify clean worktree after commits.
- [ ] Install exact candidate revision atomically; do not merge Runtime main.
- [ ] Obtain exact-revision non-author review if an independent executor is available.
- [ ] Run Live E2E: Web early END_TURN with no user follow-up; Web→Web continuation; Desktop→Desktop continuation; Web↔Desktop ownership handoff; verify no stale/cross-host execution.
