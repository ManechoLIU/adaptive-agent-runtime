# Web Controller Target Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add canonical same-controller Web target replace/unbind operations with generation fencing and safe manual resume-lease rotation, without upgrading manual Bootstrap identity to host-attested identity.

**Architecture:** Keep Web alias/binding and manual lease behavior in `web_lifecycle_bridge.py`, reuse `controller_target_guard.py` as the authoritative target-resolution/fencing layer, and mirror the existing desktop target CAS contract. Replacement writes one explicit Web target under the existing logical Controller, preserves aliases, rotates only an existing valid `manual_user_authorized/resume_only` lease, and records temporary/manual/non-host-attested provenance. Strong identity verification remains unchanged.

**Tech Stack:** Python 3 stdlib, unittest, Git-backed Runtime registry/state.

**Spec:** User-provided Adaptive Agent Runtime Web Controller execution target replacement requirements (2026-09-07).

## Global Constraints

- Never create or replace the logical Controller while rotating a Web execution target.
- `host_attested` remains false for manual Bootstrap replacement.
- Replacement requires exact repo/controller ownership, no cross-controller session conflict, active-outbound-lease fence, and generation CAS.
- Existing aliases remain audit history; only one explicit current Web target resolves for outbound work.
- Manual resume authorization may rotate only if it is existing, unexpired, same-controller, same-repo, `manual_user_authorized/resume_only`.
- Existing desktop target behavior must not change.

---

### Task 1: Web target CAS contract

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Test: `tests/test_web_lifecycle_bridge.py`

**Interfaces:**
- Produces: `replace_web_session(...) -> dict`, `unbind_web_session(...) -> dict` and CLI commands `replace-web-session`, `unbind-web-session`.
- Reuses: `target_guard.validate_target_record`, `target_guard.require_no_active_outbound_lease`, canonical registry lock, existing Web alias ownership checks.

- [ ] Write failing tests for generation-0 bootstrap replace, generation increment, old alias staleness/current resolution, cross-controller/repo/outbound-lease rejection, same-target idempotency, and unbind tombstone.
- [ ] Run focused tests and verify failures are due to missing Web replacement API/CLI.
- [ ] Implement minimal Web target CAS mutation preserving aliases and manual/non-host-attested provenance.
- [ ] Run focused tests to GREEN.

### Task 2: Manual resume lease rotation and fencing

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Test: `tests/test_web_lifecycle_bridge.py`
- Test: `tests/test_controller_target_guard.py`

**Interfaces:**
- Reuses: `rotate_existing_manual_web_resume_lease(...)`.
- Produces: replacement receipt fields `resume_lease_rotated`, `binding=temporary`, `host_attested=false`.

- [ ] Write failing tests proving valid lease rotation preserves authorization provenance/expiry and unapproved sessions cannot become current.
- [ ] Add old-generation wake/outbound assertions using existing target guard fencing.
- [ ] Implement atomic post-target rotation of the existing manual lease without creating a second authorization.
- [ ] Verify old generation/session operations fail closed and current resolution never falls back to logical Controller ID.

### Task 3: Release regression, review, install, live Local Agent Bridge rebind

**Files:**
- Modify if required: `scripts/install_skill.py` release regression list.
- Test: relevant Runtime regression suites.

**Interfaces:**
- Input: exact validated Runtime candidate revision.
- Output: installed revision and Local Agent Bridge machine evidence for target/lease/generation.

- [ ] Add/confirm release regression coverage for Web replacement and existing desktop replacement.
- [ ] Run focused + broader target/lifecycle/install tests and `git diff --check`.
- [ ] Obtain independent non-author review against exact candidate.
- [ ] Commit candidate and install exact revision through `install_skill.py`.
- [ ] Execute real Local Agent Bridge `replace-web-session` from generation 0 to `6a9e4158-cc8c-83ea-9763-05a7fd51f597`.
- [ ] Verify unique Controller unchanged, old alias historical only, generation advanced, lease rotated, manual/non-host-attested state preserved, `6a9e3b9a-...` absent from current target, and old-target routing rejected.
