# Controller Health / Wake Supervisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close controller-continuation gaps by deriving Controller Health from existing machine facts, routing every pending control event through one wake-existing-controller path, and making controller lifecycle worktree-aware without creating a second controller or state machine.

**Architecture:** Add one focused pure `scripts/controller_health.py` module that projects health/decision facts but stores no parallel truth. `lifecycle_hook.py` remains the canonical pending-event producer and becomes Git-common-dir/worktree-aware for bound controller surfaces; `web_lifecycle_bridge.py` becomes the host adapter/wake executor and persists only bounded wake receipts while preserving pending until the existing `control_event_guard` closes it. Existing host-fallback contracts, technical Skill identity, runtime assignments, reviewer/scoring/handshake flows are reused unchanged.

**Tech Stack:** Python 3.11, unittest, Git CLI, existing JSON state/locking helpers, Node.js regression suite.

**Spec:** `docs/superpowers/specs/2026-08-31-controller-health-wake-supervisor-design.md`

## Global Constraints

- Exactly one controller ownership remains canonical per Git common-dir; no second controller registry/state machine.
- `adaptive-delivery` remains the stable technical Skill ID, install path, and `.git/adaptive-delivery` runtime path.
- Controller Health is derived machine state only; no mutable health ledger.
- ACTIVE and active-writer evidence must never trigger duplicate wake or peer-host fallback.
- Peer-host fallback is permitted only for the existing eligible machine failure classes and existing side-effect/partial-write/authorization safety rules.
- `DEAD` never auto-Replaces ownership; it fails closed.
- Wake launch/resume never clears `pending_control_event`; only existing control-event closure evidence may clear it.
- main remains the sole authoritative integration surface; only explicitly bound controller worktrees may emit controller lifecycle events.
- No A2A, SQLite, HTTP control plane, permanent Supervisor Agent, or per-worktree controller ownership in this iteration.
- Final delivery requires full Python/Node regression, `git diff --check`, exact-head independent Reviewer Supervisor PASS with no Critical/Important, main integration, fresh merged-main regression, and only then exact-revision Skill installation. No push.

---

### Task 1: Pure Controller Health Projection and Wake Decision

**Files:**
- Create: `scripts/controller_health.py`
- Create: `tests/test_controller_health.py`

**Interfaces:**
- Consumes: registered controller/session id, canonical repo/common-dir, lifecycle pending/triggers, last verified controller host, host continuation result/failure class, active-writer evidence, peer-host availability, fallback safety facts.
- Produces: `derive_controller_health(facts: dict[str, Any]) -> dict[str, Any]` and `decide_controller_wake(health: dict[str, Any]) -> dict[str, Any]` with states `ACTIVE|DEFERRED|DEGRADED|FALLBACK_NEEDED|DEAD` and decisions `NOOP_ACTIVE|DEFER|RESUME_CURRENT_HOST|FALLBACK_PEER_HOST|DEAD_BLOCK`.

- [ ] **Step 1: Write failing projection tests**

```python
from scripts.controller_health import derive_controller_health, decide_controller_wake


def test_active_writer_is_active_or_deferred_without_fallback():
    health = derive_controller_health({
        "registered_controller": "controller-1",
        "pending_control_event": True,
        "controller_host": "web",
        "active_writer": True,
        "resume_state": "RESUME_DEFERRED_ACTIVE_WRITER",
        "peer_host_available": True,
    })
    assert health["state"] == "DEFERRED"
    assert decide_controller_wake(health)["decision"] == "DEFER"
    assert decide_controller_wake(health)["selected_host"] is None


def test_eligible_terminal_host_failure_requires_peer_fallback():
    health = derive_controller_health({
        "registered_controller": "controller-1",
        "pending_control_event": True,
        "controller_host": "web",
        "active_writer": False,
        "resume_state": "RESUME_FAILED",
        "failure_class": "quota_exhausted",
        "fallback_eligible": True,
        "peer_host_available": True,
        "fallback_safe": True,
    })
    assert health["state"] == "FALLBACK_NEEDED"
    decision = decide_controller_wake(health)
    assert decision["decision"] == "FALLBACK_PEER_HOST"
    assert decision["selected_host"] == "desktop_codex"


def test_no_safe_continuation_is_dead_but_never_replace():
    health = derive_controller_health({
        "registered_controller": "controller-1",
        "pending_control_event": True,
        "controller_host": "web",
        "active_writer": False,
        "resume_state": "RESUME_FAILED",
        "failure_class": "runtime_unavailable",
        "fallback_eligible": True,
        "peer_host_available": False,
        "fallback_safe": True,
        "failure_conclusive": True,
    })
    assert health["state"] == "DEAD"
    assert decide_controller_wake(health)["decision"] == "DEAD_BLOCK"
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python3 -m unittest tests.test_controller_health -v`
Expected: FAIL because `scripts.controller_health` does not exist.

- [ ] **Step 3: Implement minimal pure projection**

Implement deterministic validation plus the five-state projection. Required invariants:

```python
HEALTH_STATES = {"ACTIVE", "DEFERRED", "DEGRADED", "FALLBACK_NEEDED", "DEAD"}
WAKE_DECISIONS = {"NOOP_ACTIVE", "DEFER", "RESUME_CURRENT_HOST", "FALLBACK_PEER_HOST", "DEAD_BLOCK"}
ELIGIBLE_FAILURES = {
    "usage_limit_exceeded", "quota_exhausted", "model_unavailable",
    "service_unavailable", "auth_invalid", "runtime_unavailable",
}
```

Projection order must be fail-safe: missing/multiple binding -> DEAD/block; active writer -> DEFERRED; confirmed active execution -> ACTIVE; no pending -> ACTIVE/no-op projection; current-host actionable recovery -> DEGRADED; eligible safe peer fallback -> FALLBACK_NEEDED; conclusive no-path -> DEAD; otherwise DEGRADED/DEFER rather than guessing DEAD.

- [ ] **Step 4: Run focused tests GREEN**

Run: `python3 -m unittest tests.test_controller_health -v`
Expected: PASS.

- [ ] **Step 5: Add ambiguous/unsafe failure cases**

Add tests proving `resume_failed`/timeout/unknown side-effect/partial write do not peer-fallback and do not auto-Replace.

- [ ] **Step 6: Run focused tests GREEN and commit**

Run: `python3 -m unittest tests.test_controller_health -v`
Expected: PASS.

Commit:
```bash
git add scripts/controller_health.py tests/test_controller_health.py
git commit -m "feat: derive controller health and wake decisions"
```

---

### Task 2: Git-Common-Dir Controller Surface Binding

**Files:**
- Modify: `scripts/lifecycle_hook.py`
- Modify: `tests/test_governance.py`

**Interfaces:**
- Consumes: invocation repo/worktree, registered controller session, event session/host, Git common-dir, canonical main repo.
- Produces: a controller-surface projection that maps main and explicitly bound controller worktrees to one lifecycle state while ordinary Writer/Reviewer worktrees remain ignored.

- [ ] **Step 1: Write failing worktree binding tests**

Add tests that create a repo plus linked feature worktree sharing the same common-dir. Prove:

```python
self.assertEqual(main_snapshot["git_common_dir"], feature_snapshot["git_common_dir"])
self.assertTrue(bound_controller_event_is_managed)
self.assertFalse(writer_event_is_managed)
```

Use explicit event/session binding in the fixture; do not infer controller ownership from branch name alone.

- [ ] **Step 2: Run targeted tests RED**

Run: `python3 -m unittest tests.test_governance -v`
Expected: new controller-worktree case fails under the branch-only/main-only guard.

- [ ] **Step 3: Replace branch-only lifecycle eligibility with common-dir + binding proof**

Add focused helpers in `lifecycle_hook.py` to:
- resolve `git rev-parse --git-common-dir`;
- retain canonical main as snapshot/integration authority;
- accept controller lifecycle events from a worktree only when the event/session is the registered controller surface;
- ignore Writer/Reviewer worktrees when controller binding cannot be proven;
- persist lifecycle state under the same existing controller state path, not per worktree.

- [ ] **Step 4: Run targeted tests GREEN**

Run: `python3 -m unittest tests.test_governance -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/lifecycle_hook.py tests/test_governance.py
git commit -m "fix: bind controller lifecycle across linked worktrees"
```

---

### Task 3: Unified Wake Supervisor and Bounded Wake Receipts

**Files:**
- Modify: `scripts/web_lifecycle_bridge.py`
- Modify: `tests/test_web_lifecycle_bridge.py`
- Use: `scripts/controller_health.py`

**Interfaces:**
- Consumes: canonical pending lifecycle state plus current host adapter facts.
- Produces: `wake_existing_controller(...) -> dict[str, Any]` bounded receipt with schema/common-dir/controller/event fingerprint/health/preferred+selected host/decision/reason/timestamps/operation/result/pending=true.

- [ ] **Step 1: Write failing generic wake tests**

Add a test where an `active_lease_expired` lifecycle state is passed to generic wake logic without any Stop/audit receipt. Assert same-controller current-host resume is scheduled.

```python
receipt = wake_existing_controller(...)
self.assertEqual(receipt["decision"], "RESUME_CURRENT_HOST")
self.assertEqual(receipt["controller_session_id"], "controller-1")
self.assertTrue(receipt["pending_control_event"])
```

Add separate tests for ACTIVE no-op, active-writer DEFER, eligible peer fallback, ambiguous failure no fallback, DEAD block, and concurrent wake lock rejection/serialization.

- [ ] **Step 2: Run targeted tests RED**

Run: `python3 -m unittest tests.test_web_lifecycle_bridge -v`
Expected: FAIL because generic wake path/receipt does not yet exist.

- [ ] **Step 3: Factor current resume classification into generic supervisor path**

Refactor existing `classify_native_resume_failure`, `preflight_native_resume`, `native_resume_command`, auto-stop state writing, and common-dir resource locking so Stop/audit and generic lifecycle wake use the same executor. Do not duplicate host policy in the bridge; call `controller_health.py` for decisions.

Wake receipt must remain bounded and atomically written. A host resume success may record `CONFIRMED`, but the receipt must still say `pending_control_event=true` until the existing control guard closes the lifecycle event.

- [ ] **Step 4: Implement same-host then peer-host adapter selection**

Reuse existing host failure classes and authorization facts. Peer-host crossing must preserve the exact registered controller and must reject active-writer, ambiguous failure, unsafe side-effect, partial-write, or new authorization/billing boundary.

- [ ] **Step 5: Run targeted tests GREEN**

Run: `python3 -m unittest tests.test_web_lifecycle_bridge -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/web_lifecycle_bridge.py tests/test_web_lifecycle_bridge.py
git commit -m "feat: unify pending event controller wake path"
```

---

### Task 4: Route All Pending Lifecycle Events Through Wake Supervisor

**Files:**
- Modify: `scripts/lifecycle_hook.py`
- Modify: `scripts/web_lifecycle_bridge.py`
- Modify: `tests/test_governance.py`
- Modify: `tests/test_web_lifecycle_bridge.py`

**Interfaces:**
- Consumes: lifecycle result where `pending_control_event` became/stayed true.
- Produces: one generic wake request/decision independent of trigger type; no event-specific wake policy other than existing rule-wake compatibility adapter.

- [ ] **Step 1: Write failing end-to-end lifecycle tests**

Add cases proving `active_lease_expired`, READY change, candidate change, Goal rollover/rule wake, and bound worktree change all reach the same wake entry point. Verify repeated unchanged pending state does not storm duplicate continuations.

- [ ] **Step 2: Run targeted tests RED**

Run:
`python3 -m unittest tests.test_governance tests.test_web_lifecycle_bridge -v`
Expected: at least the liveness-event generic wake case fails.

- [ ] **Step 3: Connect post-shell/audit/native-stop to one pending-event dispatcher**

After lifecycle dispatch/evaluation, when canonical state is pending, call/schedule the same Wake Supervisor. Preserve debounce/idempotency using event fingerprint + common-dir/controller-scoped wake lock. Remove event-source ownership of continuation decisions; retain old CLI commands as compatibility wrappers over the unified path.

- [ ] **Step 4: Prove pending clears only through control-event closure**

Add a test sequence: wake receipt CONFIRMED -> lifecycle remains pending -> successful `control_event_guard` receipt -> lifecycle pending false.

- [ ] **Step 5: Run targeted tests GREEN**

Run:
`python3 -m unittest tests.test_governance tests.test_web_lifecycle_bridge -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/lifecycle_hook.py scripts/web_lifecycle_bridge.py tests/test_governance.py tests/test_web_lifecycle_bridge.py
git commit -m "fix: wake controller for every pending lifecycle event"
```

---

### Task 5: Documentation and Compatibility Contracts

**Files:**
- Modify: `references/long-task-governance.md`
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `tests/test_skill_structure.py`

**Interfaces:**
- Produces: documented machine contract matching implementation: five health states, generic wake path, worktree binding, DEAD fail-closed, no second controller/state machine, stable technical ID.

- [ ] **Step 1: Write failing structure assertions**

Assert documentation contains the exact concepts `Controller Health`, `Wake Supervisor`, `DEAD`, `DEFERRED`, `Git common-dir`, `pending_control_event`, same-controller continuation, and the rule that wake success does not clear pending.

- [ ] **Step 2: Run structure tests RED**

Run: `python3 -m unittest tests.test_skill_structure -v`
Expected: FAIL on missing new contract text.

- [ ] **Step 3: Update docs without changing technical identity**

Document the unified path and explicitly state:
- product name Adaptive Agent Runtime;
- technical Skill ID/path remain `adaptive-delivery`;
- no second controller or mutable health ledger;
- controller worktree events require binding proof;
- DEAD requires explicit Resume/Replace handling.

- [ ] **Step 4: Run structure tests GREEN and commit**

Run: `python3 -m unittest tests.test_skill_structure -v`
Expected: PASS.

Commit:
```bash
git add references/long-task-governance.md SKILL.md README.md tests/test_skill_structure.py
git commit -m "docs: define controller health wake contract"
```

---

### Task 6: Full Feature Regression and Exact-Head Independent Review

**Files:**
- No intentional product changes; only fix regressions discovered by tests/reviewer in separate commits.

**Interfaces:**
- Consumes: candidate feature HEAD.
- Produces: machine evidence that exact HEAD is green and independently reviewed.

- [ ] **Step 1: Run full Python regression**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v`
Expected: all PASS.

- [ ] **Step 2: Run full Node regression**

Run: `node --test tests/external-agent-routing.test.mjs`
Expected: all PASS.

- [ ] **Step 3: Run whitespace/integrity checks**

Run: `git diff --check`
Expected: no output.

- [ ] **Step 4: Run exact-head Reviewer Supervisor**

Run the repository's `scripts/reviewer_supervisor.py` against the exact candidate HEAD and immutable base SHA, read-only. Required verdict: no Critical and no Important findings. Infrastructure failure is not PASS; use only an already-approved model fallback if the reviewer model itself is unavailable, preserving exact-head/read-only/schema semantics.

- [ ] **Step 5: Fix any findings using TDD and repeat review**

For every Critical/Important finding: add a failing regression test, implement the smallest fix, rerun focused + full regression, commit, then review the new exact HEAD. Do not auto-retry actual findings without code change.

---

### Task 7: Integrate to Main, Regress Again, Then Install Exact Revision

**Files:**
- Integration/install state only.

**Interfaces:**
- Consumes: reviewed feature HEAD with PASS.
- Produces: merged main exact revision, fresh merged-main regressions, installed exact revision, manifest/hash/adapter verification, no duplicate identity.

- [ ] **Step 1: Verify isolated worktree clean and reviewer PASS binds current HEAD**

Run: `git status --short` and compare Reviewer `reviewed_head` to `git rev-parse HEAD`.
Expected: clean and exact match.

- [ ] **Step 2: Fast-forward/integrate reviewed feature branch into main**

No push. Reject integration if main advanced incompatibly; rebase/review again if needed.

- [ ] **Step 3: Run fresh merged-main Python + Node regressions and `git diff --check`**

Expected: all PASS on main.

- [ ] **Step 4: Install exact merged revision in place**

Use existing `scripts/install_skill.py` transactional path targeting `/Users/echoman/.agents/skills/adaptive-delivery`. Do not create `/Users/echoman/.agents/skills/adaptive-agent-runtime` or `.git/adaptive-agent-runtime`.

- [ ] **Step 5: Verify installed hashes/manifest/host adapters and rule handshake semantics**

Confirm installed revision equals merged main HEAD, all manifest hashes match, controller registry/runtime remain single and preserved, hooks/zshenv still point to stable technical path, and SelfAlone handshake is either current or correctly fail-closed pending ACK according to actual impact.

- [ ] **Step 6: Report completion only with evidence**

Report exact merged revision, test counts, reviewer run/verdict, installed revision, handshake state, and any intentional uncommitted project-state synchronization. Do not claim final completion if any Critical/Important, regression failure, duplicate controller/runtime identity, or unacknowledged blocking handshake remains.
