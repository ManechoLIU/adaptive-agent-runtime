# Rule Version Handshake and Canonical Assignment Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Adaptive Delivery rule upgrades and Assignment recovery lineage machine-closed so installed rule drift is automatically surfaced/blocked and same-Assignment attempt 4 cannot spawn across Git worktrees.

**Architecture:** Introduce one Git-common-dir state helper, one installation manifest + rule handshake module, then route lifecycle and external-Agent runtime receipts through those canonical facts. Keep the existing five main task states and existing project ledger; new data is machine evidence only.

**Tech Stack:** Python 3.11+, Node.js ESM, Git CLI, unittest, Node test runner.

**Spec:** `docs/superpowers/specs/2026-08-30-rule-runtime-handshake-design.md`

## Global Constraints

- No daemon, database, second task ledger, or second controller.
- Project-wide state lives under `git rev-parse --git-common-dir` `/adaptive-delivery/`.
- Existing installations without a manifest remain backward-compatible `unmanaged`.
- `impact=live_assignments` drift blocks Assignment-bound launches until exact loaded ACK and ledger rule-version sync.
- Canonical runtime apply is mandatory for Assignment-bound execution; JSONL receipts remain optional audit evidence.
- Same Assignment permits attempts 1, 2, 3 only; attempt 4 after two recoveries must fail before spawn.

---

### Task 1: Shared Git Common-Dir State Root

**Files:**
- Create: `scripts/project_state.py`
- Modify: `scripts/assignment_runtime.py`
- Test: `tests/test_assignment_runtime.py`
- Test: `tests/test_project_state.py`

**Interfaces:**
- Produces: `repository_root(repo: Path) -> Path`, `git_common_dir(repo: Path) -> Path`, `adaptive_delivery_state_dir(repo: Path) -> Path`.
- Changes: `runtime_state_path(repo)` returns `<git-common-dir>/adaptive-delivery/runtime-assignments.json`.

- [ ] **Step 1: Write failing tests for main/worktree path identity**

Create temporary Git repo + linked worktree and assert `adaptive_delivery_state_dir(main) == adaptive_delivery_state_dir(worktree)` and `runtime_state_path(main) == runtime_state_path(worktree)`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python3 -m unittest tests.test_project_state tests.test_assignment_runtime -v`
Expected: FAIL because `project_state` does not exist and runtime path is checkout-local.

- [ ] **Step 3: Implement the Git common-dir helper and route runtime path through it**

Use `git -C <repo> rev-parse --show-toplevel` and `--git-common-dir`; resolve relative common-dir paths against the repository root. Keep `save_runtime_state()` atomic.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python3 -m unittest tests.test_project_state tests.test_assignment_runtime -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/project_state.py scripts/assignment_runtime.py tests/test_project_state.py tests/test_assignment_runtime.py
git commit -m "feat: share adaptive runtime across worktrees"
```

### Task 2: Installation Manifest and Rule Handshake

**Files:**
- Create: `scripts/install_skill.py`
- Create: `scripts/rule_handshake.py`
- Create: `tests/test_rule_handshake.py`
- Modify: `tests/test_skill_structure.py`

**Interfaces:**
- Produces: `load_install_manifest(skill_root: Path | None = None) -> dict`, `rule_state_path(repo: Path) -> Path`, `evaluate_rule_handshake(repo: Path, ledger: Path | None = None, skill_root: Path | None = None) -> dict`, `acknowledge_rule_revision(repo: Path, controller_session_id: str, revision: str, ...) -> dict`.
- CLI: `install_skill.py --source <repo> --target <dir> --summary <text> --impact none|live_assignments --stop-condition <text> [--previous-revision <rev>]`.
- CLI: `rule_handshake.py status --repo <repo>` and `rule_handshake.py ack --repo <repo> --controller-session <id> --revision <rev>`.

- [ ] **Step 1: Write failing tests for manifest integrity and ACK identity**

Cover exact revision, wrong revision, tampered installed file, registered vs unregistered controller, common-dir state sharing, and `pending_ack → ledger_stale → current` transitions.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m unittest tests.test_rule_handshake -v`
Expected: FAIL because installer/handshake modules do not exist.

- [ ] **Step 3: Implement installer and handshake state**

Installer copies `git ls-files` tracked files, writes SHA-256 map + revision metadata, and only removes previously-manifested files no longer tracked. ACK validates manifest hashes and controller registry mapping before atomic state write.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python3 -m unittest tests.test_rule_handshake tests.test_skill_structure -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/install_skill.py scripts/rule_handshake.py tests/test_rule_handshake.py tests/test_skill_structure.py
git commit -m "feat: add rule version handshake"
```

### Task 3: Canonical Runtime Apply and Pre-Spawn Hard Gates

**Files:**
- Modify: `scripts/assignment_runtime.py`
- Modify: `scripts/run_external_agent.mjs`
- Modify: `tests/test_assignment_runtime.py`
- Modify: `tests/external-agent-routing.test.mjs`

**Interfaces:**
- CLI: `assignment_runtime.py apply --repo <repo>` reads one receipt JSON object from stdin and atomically applies it to common-dir runtime state.
- Runner: Assignment-bound `--execute` always performs rule launch guard and canonical start apply before `executeExternalAgent()`; audit JSONL remains optional.

- [ ] **Step 1: Write failing Python CLI tests**

Verify applying attempt1 in main is visible from linked worktree; apply attempts2/3 failures; attempt4 apply returns nonzero and does not alter canonical state; new Assignment attempt1 succeeds.

- [ ] **Step 2: Write failing Node spawn-order tests**

Use fake Agent executable marker. Verify pending rule handshake fails before marker creation; canonical attempt4 fails before marker creation even without `--runtime-receipts`; valid current handshake + allowed attempt spawns and persists start/terminal state.

- [ ] **Step 3: Run focused tests and verify RED**

Run: `python3 -m unittest tests.test_assignment_runtime -v && node --test tests/external-agent-routing.test.mjs`
Expected: new tests FAIL because runner does not apply canonical runtime or rule launch guard.

- [ ] **Step 4: Implement runtime CLI and runner ordering**

Build the receipt once, apply through `assignment_runtime.py apply --repo`, then append the same JSON to audit file if requested. Treat start apply failure as fatal before spawn. Add `rule_handshake.py launch-guard --repo <cwd>` before start apply.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `python3 -m unittest tests.test_assignment_runtime -v && node --test tests/external-agent-routing.test.mjs`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/assignment_runtime.py scripts/run_external_agent.mjs tests/test_assignment_runtime.py tests/external-agent-routing.test.mjs
git commit -m "feat: enforce canonical runtime before agent spawn"
```

### Task 4: Lifecycle-Native Rule Notification and Closure

**Files:**
- Modify: `scripts/lifecycle_hook.py`
- Modify: `scripts/control_event_guard.py`
- Modify: `references/long-task-governance.md`
- Modify: `SKILL.md`
- Modify: `tests/test_governance.py`
- Modify: `tests/test_skill_structure.py`

**Interfaces:**
- `project_snapshot()` adds `rule_handshake`.
- Persistent lifecycle triggers: `rule_update_pending:<revision>` and `rule_ledger_stale:<revision>`.
- Continuation text carries revision, summary, impact, stop condition, and exact local ACK command.
- Successful control receipt cannot clear a blocking handshake.

- [ ] **Step 1: Write failing lifecycle tests**

Cover SessionStart/PostToolUse persistent exact-revision notification, ACK-with-stale-ledger continuation, fully-current closure, and failure of ordinary control receipt to clear blocking drift.

- [ ] **Step 2: Run governance tests and verify RED**

Run: `python3 -m unittest tests.test_governance -v`
Expected: new lifecycle tests FAIL because snapshot has no handshake evidence.

- [ ] **Step 3: Implement lifecycle integration and documentation**

Keep five main states unchanged. Add only machine evidence/triggers. Update governance docs to require `install_skill.py` for publication and remove GUI notification as a completion criterion.

- [ ] **Step 4: Run governance/structure tests and verify GREEN**

Run: `python3 -m unittest tests.test_governance tests.test_skill_structure -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/lifecycle_hook.py scripts/control_event_guard.py references/long-task-governance.md SKILL.md tests/test_governance.py tests/test_skill_structure.py
git commit -m "feat: close rule update lifecycle handshake"
```

### Task 5: Full Verification, Independent Review, Integration, and Bootstrap Install

**Files:**
- No feature-code changes unless verification/review finds a defect.

**Interfaces:**
- Final candidate must satisfy the spec acceptance criteria and retain all existing governance behavior.

- [ ] **Step 1: Run full verification**

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/external-agent-routing.test.mjs
python3 -m py_compile scripts/*.py
git diff --check
```

Expected: all PASS.

- [ ] **Step 2: Run two explicit counterexample probes**

1. Install a manifest at revision R, leave project ACK at R-1, and prove an Assignment-bound fake Agent does not spawn.
2. Build attempt1→2→3 lineage from one linked worktree and prove attempt4 from another linked worktree is rejected before fake Agent spawn.

- [ ] **Step 3: Obtain non-author review of the exact candidate revision**

Reviewer must inspect the spec, changed files, RED/GREEN evidence, and both counterexamples, then return explicit `REVIEW_PASS` or `REVIEW_FAIL` for the exact revision.

- [ ] **Step 4: Integrate against latest main and rerun full verification**

If main advanced, merge/rebase only in a temporary integration worktree, preserve both sides, and rerun the full suite before fast-forwarding main.

- [ ] **Step 5: Push and bootstrap-install through the new installer**

Run the new installer against the exact pushed source revision with:

- `previous_revision=d803562550042b2d2e90d1e1f235e107532bc3f1`
- `impact=live_assignments`
- summary describing rule handshake + canonical runtime lineage
- stop condition requiring exact loaded ACK + ledger rule-version sync before affected Assignment launch

Verify source/install hashes and manifest revision.

- [ ] **Step 6: Verify SelfAlone receives lifecycle-native rule update and closes handshake**

Without browser message injection, trigger/read the registered SelfAlone controller lifecycle. Require project state to reach `current` and `TASK_LEDGER.md` rule-version to contain the exact new revision. Then prove the affected Assignment launch gate reads the current handshake.
