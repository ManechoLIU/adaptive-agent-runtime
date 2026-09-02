# Rule Version Handshake and Canonical Assignment Runtime Design

## Problem

Adaptive Delivery currently has two governance gaps that can coexist:

1. The installed skill can advance to a newer revision while a live project controller still records an older `LOADED ACK`. The rule-update contract is validated only when a control receipt explicitly declares `rule_update`; there is no machine-owned comparison between the installed revision and the controller-loaded revision.
2. `run_external_agent.mjs` emits runtime receipts to an audit JSONL file, while `assignment_runtime.py` owns the recovery lineage state. The runner does not atomically apply those receipts to the canonical runtime state. In addition, `runtime_state_path()` is based on `<checkout>/.git`, which is not a shared state root across linked Git worktrees. As a result, recovery history can disappear and an apparent same-Assignment attempt 4 can be launched even though the policy says the same contract has exhausted its recovery budget.

These are control-plane consistency failures, not prompting failures.

## Goals

1. Make the installed Adaptive Delivery revision machine-readable and immutable for one installation event.
2. Persist one project-level controller-loaded revision receipt in the repository Git common directory.
3. Detect `installed_revision != controller_loaded_revision` automatically from the lifecycle hook and inject the exact revision into the registered controller without browser/GUI messaging.
4. Prevent affected Assignment-bound external Agent launches while rule handshake or ledger rule-version synchronization is incomplete.
5. Persist Assignment runtime lineage in one state file shared by the main checkout and all linked worktrees.
6. Atomically apply `assignment_started` / terminal runtime receipts before or immediately after the corresponding execution boundary so recovery budget is enforced before spawning attempt 4.
7. Preserve the existing five controller main states; rule handshake and recovery lineage remain machine evidence, not new task states.

## Non-goals

- No daemon, database, second task ledger, or second controller.
- No automatic rewriting of arbitrary project ledger prose.
- No attempt to prove that a language model semantically understood every changed rule line. The machine receipt proves exact installed revision, installation integrity, registered-controller identity, and explicit acknowledgement.
- No redesign of Agent provider routing or the five-state controller projection.

## Architecture

### 1. Canonical Git common-dir state root

Create `scripts/project_state.py` with:

- `repository_root(repo: Path) -> Path`
- `git_common_dir(repo: Path) -> Path`
- `adaptive_delivery_state_dir(repo: Path) -> Path`

`git_common_dir()` resolves `git rev-parse --git-common-dir` relative to the repository root when Git returns a relative path. Every project-wide Adaptive Delivery runtime/governance state file uses:

`<git-common-dir>/adaptive-delivery/`

This makes main checkout, Writer worktrees, and Reviewer worktrees see one lineage.

### 2. Installation manifest

Create `scripts/install_skill.py` as the supported installation/sync path.

It copies only source-Git tracked files into the target skill directory and writes:

`<installed-skill>/.adaptive-delivery-install.json`

Required manifest fields:

- `schema_version`
- `revision`
- `previous_revision`
- `installed_at`
- `source_root`
- `summary`
- `impact` (`none` or `live_assignments`)
- `stop_condition`
- `changed_files`
- `files` mapping tracked relative paths to SHA-256

The installer validates a clean source checkout, exact source HEAD, and copied-file hashes. It removes only paths that were listed in the previous install manifest but are no longer tracked; it never recursively deletes unrelated target files.

For the bootstrap installation where no prior manifest exists, `--previous-revision` may supply the currently acknowledged revision.

### 3. Rule-version handshake

Create `scripts/rule_handshake.py`.

Project state is stored at:

`<git-common-dir>/adaptive-delivery/rule-handshake.json`

The state records:

- `installed_revision`
- `loaded_revision`
- `controller_session_id`
- `acknowledged_at`
- `manifest_sha256`

`ack` must fail unless:

- the requested revision exactly equals the installed manifest revision;
- every tracked file hash in the install manifest still matches the installed copy;
- `controller_session_id` is registered to this repository in the existing Adaptive Delivery controller registry.

Handshake evaluation also inspects the project ledger `规则版本` line. The machine states are evidence only:

- `unmanaged`: no install manifest; backward-compatible, no launch block;
- `pending_ack`: installed revision has no matching loaded ACK;
- `ledger_stale`: ACK matches installed revision but ledger rule-version does not contain the exact revision;
- `current`: installed revision, controller loaded receipt, and ledger revision agree.

When manifest `impact=none`, revision drift is surfaced but does not block Assignment launch. When `impact=live_assignments`, `pending_ack` and `ledger_stale` block Assignment-bound external Agent launch.

### 4. Lifecycle-native notification

`lifecycle_hook.project_snapshot()` includes `rule_handshake` evidence. The lifecycle trigger is persistent while drift exists, not only edge-triggered:

- `rule_update_pending:<revision>` for `pending_ack`;
- `rule_ledger_stale:<revision>` for `ledger_stale`.

The continuation message includes the exact installed revision, summary, impact, stop condition, and the local acknowledgement command. Because this is emitted by the registered controller lifecycle hook, GUI navigation is not a dependency.

A normal control receipt cannot clear a lifecycle event while rule handshake is not `current` for a `live_assignments` update.

### 5. Canonical Assignment runtime lineage

Change `assignment_runtime.runtime_state_path(repo)` to use `adaptive_delivery_state_dir(repo) / "runtime-assignments.json"`.

Add a CLI:

`python3 scripts/assignment_runtime.py apply --repo <worktree>`

It reads one runtime receipt from stdin, loads the common-dir state, applies `apply_receipt()`, and atomically saves the new state. Existing recovery rules stay unchanged:

- new Assignment begins at attempt 1;
- same Assignment recovery increments by exactly one;
- recovery count survives worktree changes;
- after two recoveries, a fourth same-Assignment attempt is rejected and strategy change requires a new Assignment.

### 6. Runner hard gates

For Assignment-bound `--execute`, `run_external_agent.mjs` performs, in order:

1. delivered-ACK contract validation;
2. rule-handshake launch guard;
3. canonical `assignment_started` receipt apply;
4. external Agent spawn;
5. canonical terminal receipt apply on normal exit or exception;
6. optional append-only JSONL audit receipt.

Canonical apply failure is fatal and occurs before Agent spawn for `assignment_started`. Therefore recovery budget exhaustion cannot be bypassed by omitting `--runtime-receipts` or by launching from a different linked worktree.

The optional JSONL file remains audit evidence only; it is no longer the source of truth.

## Ledger and compatibility

Existing projects without an installation manifest remain `unmanaged` and behave as before until installed via `install_skill.py`. Existing runtime state under the old checkout-local path is not silently merged, because automatic reconciliation could combine unrelated stale histories. The first canonical start after upgrade establishes the new common-dir lineage; live projects must complete the rule handshake before affected launches resume.

The project ledger remains human-readable. The controller updates its existing `规则版本` line; no second ledger or generated project document is introduced.

## Failure behavior

- Manifest missing: backward-compatible unmanaged mode; lifecycle may report unmanaged but does not block.
- Manifest hash mismatch: fail closed as installation-integrity failure; ACK and affected launches are blocked.
- Controller not registered to repo: ACK rejected.
- Installed revision differs from loaded revision: lifecycle continuously injects exact rule update; affected launches blocked.
- Loaded revision matches but ledger is stale: lifecycle requests ledger sync; affected launches remain blocked.
- Runtime receipt cannot be applied: runner fails before spawn for start receipt.
- Recovery budget exhausted: same Assignment attempt 4 fails before spawn; new Assignment attempt 1 remains allowed.

## Acceptance criteria

1. Main checkout and two linked worktrees resolve the same runtime and rule-handshake state paths.
2. Installation manifest detects exact revision drift and file-integrity drift.
3. Registered controller can ACK the exact installed revision; a wrong revision or unregistered session cannot.
4. Lifecycle emits persistent exact-revision notification until ACK + ledger sync are both complete.
5. Assignment-bound runner refuses launch before spawn while a `live_assignments` handshake is pending.
6. Runner applies runtime receipts to canonical state even when no audit JSONL path is provided.
7. A same-Assignment attempt 4 is rejected before external Agent spawn across linked worktrees.
8. A new Assignment after budget exhaustion starts at attempt 1 and is allowed.
9. Existing Python and Node governance tests remain green; new tests cover the above failure paths.
