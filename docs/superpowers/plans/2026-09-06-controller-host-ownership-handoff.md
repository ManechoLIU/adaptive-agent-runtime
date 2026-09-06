# Controller Host Ownership Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing unique logical Controller continue on the host that currently owns control rights, so Web work stays on Web quota, desktop Codex work stays on desktop quota, and Web↔Desktop handoff never creates a second Controller or allows a stale host to commit/re-arm.

**Architecture:** Keep `controller_id`, Goal, ledger, Assignment state and continuation debt unchanged. Extend the existing controller registry with one controller-level execution-ownership record that serializes host ownership across `web` and `desktop_codex`; reuse existing per-host target records for session aliases/target validation. All handoffs are compare-and-swap under the existing registry lock, increment one ownership generation, and continuation resolves/revalidates the ownership record immediately before and after external work.

**Tech Stack:** Python 3 stdlib, JSON registry state, `fcntl` locking, `unittest`, existing Adaptive Agent Runtime lifecycle/target guard APIs.

**Spec:** `docs/superpowers/specs/2026-09-06-controller-host-ownership-handoff-design.md`

## Global Constraints

- Exactly one logical Controller; never create, replace, fork, or reappoint a second Controller.
- Web ownership must never silently fall back to desktop Codex; desktop ownership must never silently fall back to Web.
- Host handoff changes execution ownership only; it must not mutate Goal, TASK_LEDGER, Assignment leases, candidates, review/integration state, or continuation debt.
- Every cross-host ownership change is generation-fenced and atomic under the canonical controller registry lock.
- Existing legacy/per-host target records remain backward compatible during rollout.
- Provider usage-limit backoff from commit `d743f5d` remains host-local and preserved.
- TDD is mandatory: every production behavior change requires a failing test observed first.
- Runtime branch remains unmerged until exact installed revision has independent non-author Reviewer PASS plus live E2E acceptance.

---

### Task 1: Canonical Controller Execution Ownership CAS

**Files:**
- Modify: `scripts/controller_target_guard.py`
- Test: `tests/test_controller_target_guard.py`

**Interfaces:**
- Consumes: existing `CONTROLLER_TARGETS_KEY`, `host_sessions()`, `target_record()`, `validate_target_record()`, `registered_controller_for_repo()`, and `registry_lock_path()`.
- Produces:
  - `CONTROLLER_EXECUTION_OWNERSHIP_KEY = "__controller_execution_ownership__"`
  - `execution_ownership_record(registry, *, controller_id) -> dict[str, Any] | None`
  - `validate_execution_ownership_record(record) -> tuple[str, str, int]` returning `(active_host, execution_target_session_id, generation)`
  - `resolve_execution_ownership(*, repo, registry_path=DEFAULT_REGISTRY) -> dict[str, Any]`
  - `claim_controller_host(*, repo, controller_id, requested_host, requested_target_session_id, expected_generation, registry_path=DEFAULT_REGISTRY, provenance=None) -> dict[str, Any]`

- [ ] **Step 1: Write failing tests for first Web ownership claim and identity preservation**

Add tests proving a registry with one existing logical Controller and a verified/bound Web target can claim `web` from ownership generation `0`, producing generation `1`, `active_host="web"`, exact Web execution target, while the project Controller mapping remains unchanged.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python3 -m unittest tests.test_controller_target_guard.ControllerTargetGuardTests.test_claim_controller_host_web_initializes_cross_host_ownership`

Expected: FAIL because canonical execution ownership/CAS APIs do not exist.

- [ ] **Step 3: Write failing tests for Desktop-after-Web CAS and stale-generation rejection**

Add tests proving: Web gen1 -> desktop claim with expected gen1 yields gen2 and only ownership changes; a concurrent/stale claim still using expected gen1 raises `PermissionError` and leaves gen2 intact.

- [ ] **Step 4: Run the focused CAS tests and verify RED**

Run the two new tests directly with `python3 -m unittest ...`.

Expected: FAIL for missing behavior.

- [ ] **Step 5: Implement minimal canonical execution ownership record and CAS**

Under the existing registry exclusive lock:
1. Verify exactly one registered Controller for the repo and that its ID equals `controller_id`.
2. Validate `requested_host in SUPPORTED_HOSTS`.
3. Resolve the requested per-host current target; reject unbound/unknown target and require exact `requested_target_session_id`.
4. Read controller-level ownership; absent means generation `0` only.
5. Require `expected_generation` to equal current ownership generation.
6. Persist `{active_host, execution_target_session_id, generation: current+1, provenance?}`.
7. Do not mutate the other host's historical per-host target record.
8. Return a receipt with the same `controller_id`, `active_host`, target, and new generation.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the new Task 1 tests.

Expected: PASS.

- [ ] **Step 7: Run target-guard regression suite**

Run: `python3 -m unittest tests.test_controller_target_guard`

Expected: all tests PASS.

- [ ] **Step 8: Commit Task 1**

```bash
git add scripts/controller_target_guard.py tests/test_controller_target_guard.py
git commit -m "feat(runtime): add canonical controller host ownership"
```

---

### Task 2: Web and Desktop Entry Paths Claim Canonical Ownership

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Modify: `scripts/lifecycle_hook.py`
- Test: `tests/test_web_lifecycle_bridge.py`
- Test: `tests/test_governance.py`

**Interfaces:**
- Consumes: Task 1 `claim_controller_host()` and `resolve_execution_ownership()`.
- Produces:
  - Web same-controller recovery/bind returns `ownership_generation` and claims `active_host="web"` only after host/session verification.
  - Desktop explicit target replacement/resume path can claim `active_host="desktop_codex"` with CAS.

- [ ] **Step 1: Write failing Web claim test**

Extend the existing same-controller Web recovery tests so a verified/recovered Web session must atomically claim canonical Web ownership after target binding and return the new ownership generation. Assert logical `controller_id` is unchanged and existing desktop target metadata is preserved as historical state.

- [ ] **Step 2: Run Web claim test and verify RED**

Run the exact new Web test.

Expected: FAIL because recovery currently updates only the per-host Web target.

- [ ] **Step 3: Write failing Desktop claim test**

Add a governance/lifecycle test proving an explicit desktop target replacement for the same Controller can claim desktop ownership using the prior canonical ownership generation; generation increments once and Web target remains bound but non-owning.

- [ ] **Step 4: Run Desktop claim test and verify RED**

Expected: FAIL because desktop replacement currently lacks cross-host ownership CAS.

- [ ] **Step 5: Implement minimal entry-point ownership claims**

For Web recovery, keep host attestation and per-host target generation checks intact, then call canonical host claim under a fresh CAS expectation before reporting recovery success. For desktop replacement/resume ownership acquisition, keep existing desktop target replacement semantics, then claim desktop ownership; do not synthesize a second Controller. Ensure a CAS mismatch reports superseded/deferred rather than mutating ownership twice.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the exact new Web and Desktop tests.

- [ ] **Step 7: Run entry-point regressions**

Run:
- `python3 -m unittest tests.test_governance`
- `python3 -m unittest tests.test_web_lifecycle_bridge.WebLifecycleIdentityRecoveryTests` if class exists; otherwise run the focused identity/recovery tests by exact method names.

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git add scripts/web_lifecycle_bridge.py scripts/lifecycle_hook.py tests/test_web_lifecycle_bridge.py tests/test_governance.py
git commit -m "feat(runtime): claim controller ownership at host entry"
```

---

### Task 3: Continuation Routes Only to Canonical Owning Host

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Test: `tests/test_web_lifecycle_bridge.py`

**Interfaces:**
- Consumes: Task 1 `resolve_execution_ownership()`.
- Produces: `resolve_controller_host()` and continuation paths prefer canonical ownership when present; lifecycle snapshots become hints only for legacy registries without ownership.

- [ ] **Step 1: Write failing Web-owned continuation test**

Create a test where lifecycle state incorrectly says `desktop_codex`, both per-host targets exist, but canonical ownership says `web`. Assert `run_auto_native_stop()`/wake routing invokes `execute_web_reentry()` and never `execute_native_resume()`.

- [ ] **Step 2: Run test and verify RED**

Expected: FAIL because current host resolution still prefers lifecycle/host facts before a cross-host canonical owner exists.

- [ ] **Step 3: Write failing Desktop-owned continuation test**

Mirror the case: lifecycle hint says Web, canonical ownership says desktop. Assert native resume only, no Web re-entry.

- [ ] **Step 4: Run test and verify RED**

Expected: FAIL for the same reason.

- [ ] **Step 5: Implement canonical-owner-first routing with legacy fallback**

`resolve_controller_host()` must return canonical `active_host` whenever a valid ownership record exists. Only if ownership is absent may existing lifecycle/host-fact/per-host-target inference run. `dispatch_pending_lifecycle_wake()` and `_run_auto_native_stop_impl()` must resolve ownership immediately before choosing the external adapter; do not carry an old host decision across a long wait.

- [ ] **Step 6: Run focused routing tests and verify GREEN**

Run both new tests.

- [ ] **Step 7: Run full Web lifecycle suite**

Run: `python3 -m unittest tests.test_web_lifecycle_bridge`

Expected: all tests PASS.

- [ ] **Step 8: Commit Task 3**

```bash
git add scripts/web_lifecycle_bridge.py tests/test_web_lifecycle_bridge.py
git commit -m "fix(runtime): route continuation to owning controller host"
```

---

### Task 4: Fence Stale Host Results and Re-Arms by Ownership Generation

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Modify: `scripts/controller_target_guard.py` only if a read-only ownership fence helper is needed
- Test: `tests/test_web_lifecycle_bridge.py`
- Test: `tests/test_controller_target_guard.py` if helper added

**Interfaces:**
- Consumes: canonical ownership `{active_host, execution_target_session_id, generation}`.
- Produces: every external continuation attempt captures an ownership receipt before work and revalidates exact controller/host/target/generation before persisting a confirmed wake or scheduling its successor.

- [ ] **Step 1: Write failing stale-Web-result test**

Start a Web continuation under ownership generation N, switch canonical ownership to desktop generation N+1 while the mocked Web adapter is blocked, then release it. Assert the old Web result becomes `SUPERSEDED/DEFERRED`, writes no confirmed wake, and schedules no successor.

- [ ] **Step 2: Run test and verify RED**

Expected: FAIL if stale Web work can still commit/re-arm.

- [ ] **Step 3: Write failing stale-desktop-result test**

Start native resume under desktop ownership N, handoff to Web N+1 while external work is blocked, then return success. Assert delayed native wake persistence and supervisor re-arm are refused.

- [ ] **Step 4: Run test and verify RED**

Expected: FAIL if per-host target fencing alone cannot detect cross-host ownership change.

- [ ] **Step 5: Implement cross-host ownership fence**

Capture canonical ownership receipt before adapter/process launch. After external work and before any state commit/re-arm/wake receipt, re-resolve ownership and require exact equality of controller ID, host, execution target and generation. On mismatch, return/store a superseded state without treating the external result as authoritative.

- [ ] **Step 6: Run focused stale-generation tests and verify GREEN**

Run both new tests.

- [ ] **Step 7: Run concurrency/recovery regressions**

Run:
- `python3 -m unittest tests.test_web_lifecycle_bridge`
- `python3 -m unittest tests.test_desktop_turn_recovery`
- `python3 -m unittest tests.test_terminal_continuation`
- `python3 -m unittest tests.test_controller_target_guard`

Expected: PASS.

- [ ] **Step 8: Commit Task 4**

```bash
git add scripts/web_lifecycle_bridge.py scripts/controller_target_guard.py tests/test_web_lifecycle_bridge.py tests/test_controller_target_guard.py
git commit -m "fix(runtime): fence stale controller host continuations"
```

---

### Task 5: Release Verification, Install, Live E2E, Independent Review

**Files:**
- Modify if required by release contract: `references/long-task-governance.md`
- Runtime installer/manifest outputs are generated by existing install flow; do not hand-edit installed copies.

**Interfaces:**
- Consumes: Tasks 1-4 exact candidate revision.
- Produces: installed/loaded exact Runtime revision, current handshake, live E2E evidence for Web→Web, Desktop→Desktop, and Web→Desktop→Web, plus independent non-author Reviewer verdict.

- [ ] **Step 1: Run broad pre-install regression**

Run at minimum:
- `python3 -m unittest tests.test_web_lifecycle_bridge`
- `python3 -m unittest tests.test_controller_target_guard`
- `python3 -m unittest tests.test_governance`
- `python3 -m unittest tests.test_terminal_continuation`
- `python3 -m unittest tests.test_desktop_turn_recovery`
- `python3 -m unittest tests.test_rule_handshake`
- `git diff --check`

Expected: PASS.

- [ ] **Step 2: Commit any final documentation-only contract updates**

Document canonical host ownership, no silent cross-host fallback, and ownership-generation fencing in the existing governance reference only if the implementation introduces terminology not already covered by the spec.

- [ ] **Step 3: Install exact candidate revision atomically**

Use the repository's existing installer flow. Require release regressions to PASS; on failure rely on installer rollback and do not manually patch the installed copy.

- [ ] **Step 4: ACK exact installed revision with the existing unique SelfAlone Controller**

Do not create a second Controller. Confirm `installed_revision == loaded_revision`, handshake `state=current`, `blocking=false` before launching new Assignments.

- [ ] **Step 5: Live E2E Web -> Web**

From Web ownership, trigger a real pending continuation. Capture controller ID, `active_host=web`, ownership generation, Web target session and wake receipt. Prove no `codex exec resume` process is used for that continuation.

- [ ] **Step 6: Live E2E Desktop -> Desktop**

Explicitly claim desktop ownership from a desktop entry, trigger continuation, and prove it uses the desktop target and the same logical Controller.

- [ ] **Step 7: Live E2E Web -> Desktop -> Web**

Perform both handoffs, prove ownership generation increments each time, stale host is fenced, no duplicate Controller exists, and each continuation stays on current owner.

- [ ] **Step 8: Independent non-author Runtime Reviewer**

Reviewer must inspect the exact installed revision and live evidence. Required verdict: PASS before calling the governance change fully complete.

- [ ] **Step 9: Final verification report**

Record exact revision, test counts, handshake state, Controller ID, ownership generations/targets for all three E2Es, and Reviewer verdict. Do not merge Runtime branch automatically.
