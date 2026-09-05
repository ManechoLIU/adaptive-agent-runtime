# Controller Identity Contract Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Converge Controller identity, Web session binding, capability drift, same-controller recovery, and continuous execution into one canonical projection without creating a second identity/state system.

**Architecture:** `controller_target_guard.py` remains the sole identity owner; `web_lifecycle_bridge.py` consumes it for same-controller recovery; `project_context_guard.py` exposes the projection to governance. Capability drift is machine evidence using existing install/handshake concepts and produces `RUNTIME_CONTRACT_DRIFT`, while true ownership conflicts remain fail-closed.

**Tech Stack:** Python 3, unittest, existing Adaptive Agent Runtime registry/lifecycle/installer machinery.

**Spec:** `docs/superpowers/specs/2026-09-05-controller-identity-contract-convergence-design.md`

## Global Constraints

- Preserve one logical Controller and the existing five main Controller states.
- Do not create a second identity state store, wake supervisor, scheduler, or recovery state machine.
- Preserve target generation CAS, outbound lease fencing, host-attested recovery, and provenance fail-closed behavior.
- `DEGRADED` cannot create/reappoint a Controller or bypass an identity-sensitive mutation gate.
- Identity verifier/capability outage alone cannot authorize Stop/Yield while project-wide runnable/debt remains.
- Preserve current dirty WIP; no intermediate commit until candidate/release gate.

---

### Task 1: Canonical Identity State and Capability Projection

**Files:**
- Modify: `scripts/controller_target_guard.py`
- Test: `tests/test_controller_target_guard.py`

**Interfaces:**
- Produces: `controller_identity_capabilities() -> dict[str, Any]`
- Produces: normalized top-level `identity_state` in `controller_identity_projection()` with `VERIFIED|DEGRADED|UNVERIFIED|CONFLICTED`.

- [ ] Add failing tests for unique Controller + unavailable host/session verifier represented as recoverable/degraded without permitting a new Controller.
- [ ] Add failing tests for true project/session conflict represented as `CONFLICTED`.
- [ ] Add failing test for machine-readable identity capabilities including canonical `controller_target_guard.py identity` entry.
- [ ] Run targeted tests and verify RED.
- [ ] Implement minimal projection/capability code and rerun targeted tests GREEN.

### Task 2: Runtime Contract Drift Projection

**Files:**
- Modify: `scripts/project_context_guard.py`
- Test: `tests/test_project_context_guard.py`

**Interfaces:**
- Consumes: `controller_identity_capabilities()`.
- Produces: `runtime_contract_state`, `required_identity_capabilities`, `missing_identity_capabilities` in project context.

- [ ] Add failing test where governance requires a missing identity capability and assert `RUNTIME_CONTRACT_DRIFT` while project Controller remains `EXISTING/UNIQUE` and `create_new_controller_allowed=false`.
- [ ] Add passing-current contract test.
- [ ] Implement minimal contract comparison without introducing a new persistent state file.
- [ ] Verify project-context targeted tests GREEN.

### Task 3: Web Recovery Degraded-to-Verified Convergence

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Test: `tests/test_web_lifecycle_bridge.py`

**Interfaces:**
- Consumes canonical identity state/capabilities.
- Preserves: `recover_same_controller_web_session(...)` as the sole Web rebind path.

- [ ] Add failing test for verifier unavailable returning safe degraded recovery semantics while keeping existing Controller ownership and forbidding new Controller creation.
- [ ] Add/extend test proving successful attestation promotes the same Controller to `VERIFIED` without changing `controller_id` and with generation advancement.
- [ ] Add conflict test that stays fail-closed.
- [ ] Implement minimal recovery-state mapping and verify targeted tests GREEN.

### Task 4: Governance and Stop/Yield Integration

**Files:**
- Modify: `scripts/control_event_guard.py` only if required by failing integration test.
- Test: `tests/test_governance.py`
- Modify: `SKILL.md`
- Modify: `references/long-task-governance.md`

**Interfaces:**
- Consumes identity/runtime-contract facts; does not own identity state.
- Produces Stop/Yield behavior where degraded verifier/capability evidence is not itself a hard stop if safe runnable/control work remains.

- [ ] Add failing governance test for Goal unfinished + runnable + unique Controller + identity verifier/capability degraded => Yield rejected and safe control actions still required.
- [ ] Add conflict counterpart proving genuine `CONFLICTED` remains fail-closed for Controller mutations.
- [ ] Implement the smallest gate change needed; if existing runnable/debt gate already enforces this, keep production code unchanged and document the proven behavior.
- [ ] Update governance text to canonical CLI/capability semantics and remove any requirement for `web_lifecycle_bridge.py controller-identity`.
- [ ] Verify governance targeted tests GREEN.

### Task 5: Installer Capability Contract and Release Regression

**Files:**
- Modify: `scripts/install_skill.py`
- Test: `tests/test_install_skill.py`

**Interfaces:**
- Installed manifest reports identity capability contract without a parallel state store.
- Release regression list includes the new identity/degraded/contract-drift tests.

- [ ] Add failing installer tests for identity capability report and immutable release regression membership.
- [ ] Implement manifest capability reporting using canonical capability projection names.
- [ ] Update immutable test fixtures and release regression list.
- [ ] Verify installer tests GREEN.

### Task 6: Cross-Cutting Verification and Project Rule Synchronization

**Files:**
- Runtime files above.
- Related project `AGENTS.md` only after the reviewed Runtime contract is finalized; do not use project-rule edits to mask Runtime failure.

- [ ] Run controller-target/project-context/Web/governance focused suites.
- [ ] Run full Python suite and Node routing gate without parallel overload.
- [ ] Audit diff for duplicate state ownership or weakened provenance/generation fences.
- [ ] Run immutable release regression from candidate commit.
- [ ] Obtain independent non-author Reviewer verdict.
- [ ] After Reviewer PASS, install exact candidate and verify manifest/hash/capabilities.
- [ ] Synchronize project rule text to canonical `controller_target_guard.py identity` / runtime capability contract.
- [ ] Run E2E for normal continuation, verifier outage/degraded safe loop, degraded-to-verified recovery, and true conflict fail-closed.
