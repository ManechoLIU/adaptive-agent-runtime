# Web Control Receipt Consumer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Adaptive Agent Runtime accept and close Web Controller control events only from one exact Host-signed LAB pre/terminal receipt chain.

**Architecture:** Runtime keeps canonical Controller identity, target/ownership generations, turn leases, replay state, and lifecycle closure. A small executable hook verifies LAB Host receipts through the registered immutable verifier, prepares one full-tuple CAS record before Bridge dispatch, and terminalizes the same record only after exact guard evidence validation; legacy AI-Bridge audit translation remains diagnostic and cannot close Web control debt.

**Tech Stack:** Python 3.11 standard library, Node verifier CLI subprocess, filesystem locks and atomic JSON state, Python unittest.

**Spec:** `/Users/echoman/Documents/ChatGPT/.worktrees/lab-production-connector-p0/docs/superpowers/specs/2026-09-11-host-tool-receipt-chain.md`

## Global Constraints

- The unique logical Controller is re-derived from the canonical registry; callers never select it.
- Runtime compares Host machine-derived conversation with the current explicit Web target and re-derives target generation, ownership generation, repository, common dir, and active turn under the registry lock.
- The receipt CAS tuple is `(controller_id, host=web, execution_target_session_id, turn_id, target_generation, ownership_generation, bridge_call_id, host_tool_execution_id, normalized_request_sha256, snapshot_sha256)`.
- Pre receipt must be verified and durably PREPARED before returning ALLOW; terminal becomes TERMINAL_PENDING before lifecycle PostToolUse and CLOSED only after exact guard evidence validation.
- Replay entries are never silently evicted; capacity exhaustion fails closed.
- `exit_code=None`, stdout substrings, AI-Bridge audit JSONL, DOM/URL/title/screenshot, caller sessions, and synthetic tool IDs never close a Web control event.
- Missing/stale Host verifier protocol, wrong receipt signature/provenance/tuple, target or ownership rotation, snapshot/ledger/projection drift, and lifecycle write failure remain pending and fail closed.
- Wake confirmation and Live-E2E acceptance stay independent.
- The worktree starts at installed Runtime revision `1c322956708469e4e025c2ece1761421c7aa2604`; canonical `main` dirty scoring files are untouched.

---

### Task 1: Canonical Host tool receipt CAS ledger

**Files:**
- Modify: `scripts/controller_target_guard.py`
- Modify: `tests/test_controller_target_guard.py`

**Interfaces:**
- Consumes: canonical controller registry, explicit Web target, `__controller_execution_ownership__`, active Runtime Web turn lease, Host pre/terminal receipt dictionaries.
- Produces: `prepare_host_tool_execution(...)`, `terminalize_host_tool_execution(...)`, `close_host_tool_execution(...)`, and registry key `__controller_host_tool_receipts__`.

- [ ] **Step 1: Write failing full-tuple CAS tests**

Use literal receipt fixtures and real temporary registry files. Prove full-tuple persistence, nonce/receipt/execution-ID replay rejection across reload, exact-idempotent terminal retry, changed terminal mismatch, concurrent single winner, target/ownership/turn drift rejection, and capacity fail-closed without eviction.

```python
prepared = target_guard.prepare_host_tool_execution(
    repo=repo,
    controller_id="controller-1",
    verified_turn=turn,
    host_pre_receipt=pre,
    snapshot_path=snapshot,
    registry_path=registry,
)
self.assertEqual(prepared["state"], "PREPARED")
```

- [ ] **Step 2: Run focused tests and capture RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_controller_target_guard
```

Expected: new tests fail because the three CAS functions and registry key do not exist.

- [ ] **Step 3: Implement minimal locked state transitions**

All three functions take the existing `registry_lock_path()` exclusive lock, reload canonical state inside the lock, reject all caller identity substitutions, and write through the existing atomic registry writer. Store receipt digests and bounded metadata, never command output or capability secrets.

```python
CONTROLLER_HOST_TOOL_RECEIPTS_KEY = "__controller_host_tool_receipts__"
HOST_TOOL_RECEIPT_STATES = {"PREPARED", "TERMINAL_PENDING", "CLOSED", "RESULT_UNKNOWN"}
```

- [ ] **Step 4: Run focused tests GREEN**

Run the Step 2 command. Expected: all pass with no warnings.

- [ ] **Step 5: Commit Task 1**

```bash
git add scripts/controller_target_guard.py tests/test_controller_target_guard.py
git commit -m "feat(runtime): persist Host tool receipt CAS"
```

### Task 2: Immutable Host verifier v2 and Bridge hook CLI

**Files:**
- Create: `scripts/runtime_host_tool_hook.py`
- Modify: `scripts/web_lifecycle_bridge.py`
- Modify: `tests/test_web_lifecycle_bridge.py`
- Modify: `tests/test_agent_target_resolution.py`

**Interfaces:**
- Consumes: Task 1 CAS functions; registered Host verifier bundle; LAB receipts `lab_host_tool_pre_receipt_v1` and `lab_host_tool_terminal_receipt_v1`.
- Produces: verifier protocol `runtime_host_verifier_cli_v2`; callable verifier methods `verify_tool_pre` and `verify_tool_terminal`; executable stdin/stdout protocol `runtime_host_tool_hook_v1`; verified Web PreToolUse and PostToolUse events sharing `host_tool_execution_id`.

- [ ] **Step 1: Write failing verifier and hook tests**

Test the real CLI boundary with a controlled executable verifier. Pre requires v2, exact receipt provenance/signature/expiry/tuple, current Host entry, exact control-guard argv/cwd/repo/ledger/snapshot identity, and current Runtime Web turn. Terminal requires the matching PREPARED record, structured exit code and response digest, exact evidence file ID/hash, and current target/ownership/turn.

```python
self.assertEqual(result["protocol"], "runtime_host_tool_hook_v1")
self.assertEqual(result["decision"], "ALLOW")
self.assertEqual(result["host_tool_execution_id"], "hte_1")
```

Add negatives for caller conversation/tool ID injection, v1 verifier, missing capability, wrong target/ownership/turn, stale receipt, command/cwd/snapshot changes, terminal without pre, changed outcome replay, and lifecycle failure leaving TERMINAL_PENDING.

- [ ] **Step 2: Run focused tests and capture RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_web_lifecycle_bridge tests.test_agent_target_resolution
```

Expected: new tests fail because v2 receipt methods and the hook executable are absent.

- [ ] **Step 3: Implement the minimal verifier and hook**

Reuse the registered verifier's executable, bundle hash, safe environment, timeout, and output limit. Never accept a caller-provided equivalent receipt. The hook reads one bounded JSON object from stdin and writes one bounded JSON object; it never invokes a shell.

```python
{
    "protocol": "runtime_host_tool_hook_v1",
    "operation": "pre" | "terminal",
    "decision": "ALLOW" | "CLOSED" | "BLOCK",
    "host_tool_execution_id": "hte_<opaque>",
    "receipt_record_sha256": "<64 hex>",
}
```

Pre dispatches verified `PreToolUse`; terminal first persists TERMINAL_PENDING, dispatches verified `PostToolUse` with the same tool ID, validates exact guard evidence, then closes the CAS record.

- [ ] **Step 4: Run focused tests GREEN**

Run the Step 2 command plus:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_governance
```

Expected: all tests pass and existing Desktop native-hook behavior is unchanged.

- [ ] **Step 5: Commit Task 2**

```bash
git add scripts/runtime_host_tool_hook.py scripts/web_lifecycle_bridge.py tests/test_web_lifecycle_bridge.py tests/test_agent_target_resolution.py
git commit -m "feat(runtime): consume verified Host tool receipts"
```

### Task 3: Remove Web text-receipt closure and install the hook contract

**Files:**
- Modify: `scripts/lifecycle_hook.py`
- Modify: `scripts/install_skill.py`
- Modify: `tests/test_governance.py`
- Modify: `tests/test_install_skill.py`

**Interfaces:**
- Consumes: Task 2 verified Web events and hook executable.
- Produces: Web-only structured closure requirement, installed v2 capability report, hook path/transitive hash registration, and unchanged Desktop receipt compatibility.

- [ ] **Step 1: Write failing legacy-rejection and installer tests**

Add behavior tests proving Web audit JSONL and any stdout marker fail even with exit 0; `exit_code=None` fails; synthetic/missing tool IDs fail; only a CLOSED Task 1 CAS record plus exact Host terminal receipt/evidence hash can clear control debt. Installer tests assert the hook executable and transitive files are copied, hashed, registered with exact revision, and fail on tamper/symlink/writable mode.

```python
self.assertFalse(lifecycle.successful_control_receipt(forged_web_event, snapshot))
self.assertEqual(capability["host_verifier_protocol"], "runtime_host_verifier_cli_v2")
self.assertEqual(capability["tool_hook_protocol"], "runtime_host_tool_hook_v1")
```

- [ ] **Step 2: Run focused tests and capture RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_governance tests.test_install_skill
```

Expected: forged Web markers are currently accepted and the v2 hook capability is absent.

- [ ] **Step 3: Implement strict Web closure and install metadata**

Keep the existing Desktop native receipt path compatible. For `controller_host=web`, require the verified Host terminal fields and CLOSED CAS digest; remove all fallback to translated AI-Bridge audit output. Preserve `post-shell` only as non-authoritative diagnostics with an explicit untrusted provenance.

- [ ] **Step 4: Run Runtime regression GREEN**

Run Task 1 and Task 2 commands, then:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_web_lifecycle_bridge tests.test_governance tests.test_install_skill tests.test_controller_target_guard tests.test_agent_target_resolution
```

Expected: all pass with no new warnings; Desktop and non-Web behavior remain green.

- [ ] **Step 5: Commit Task 3**

```bash
git add scripts/lifecycle_hook.py scripts/install_skill.py tests/test_governance.py tests/test_install_skill.py
git commit -m "fix(runtime): require Host-bound Web control closure"
```
