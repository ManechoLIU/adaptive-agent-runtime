# Controller Target Guard Implementation Plan

> **For Codex:** Execute this plan with test-driven development. Preserve the logical Controller ID for shared lifecycle state, but never reuse it as a host task target unless the registry explicitly says that task is current.

**Goal:** Prevent a retired canonical task or stale desktop alias from receiving native resume, message, or navigation operations after a different desktop task becomes the current Controller entry.

**Architecture:** Keep the top-level registry key as the stable logical `controller_id`. Add a separate, generation-fenced `__controller_targets__` projection for the one active execution target per host. Desktop Hook ingress resolves only the active target to the logical Controller; outbound operations resolve the same target through a deterministic guard. Legacy registries with no aliases may continue to use their canonical task, while registries containing aliases but no explicit target fail closed until an operator performs an explicit replace.

**Tech Stack:** Python 3 standard library, `unittest`, Codex lifecycle hooks, JSON registry, Git.

---

### Task 1: Lock the failing behavior with RED tests

**Files:**

- Modify: `tests/test_governance.py`
- Modify: `tests/test_web_lifecycle_bridge.py`
- Create: `tests/test_controller_target_guard.py`

1. Add tests showing that a registry with canonical `C-OLD`, bound desktop alias `D-NEW`, and no explicit current target cannot choose either task for outbound work.
2. Add tests showing that explicit replacement activates `D-NEW`, rejects `C-OLD` and stale aliases for desktop Hook ingress, and increments a binding generation.
3. Add tests showing that unbinding the current target leaves an explicit inactive tombstone and cannot fall back to the old canonical task.
4. Add tests showing native resume uses `D-NEW` while lifecycle state and wake receipts retain logical Controller `C-OLD`.
5. Run only these tests and confirm they fail for the missing target guard/replace/unbind behavior rather than for fixture or syntax errors.

### Task 2: Add the registry lifecycle and deterministic guard

**Files:**

- Modify: `scripts/lifecycle_hook.py`
- Create: `scripts/controller_target_guard.py`

1. Add `__controller_targets__` records shaped as `{controller_id: {host: {status, session_id, generation}}}`.
2. Add an atomic `replace_desktop_session` operation that verifies repository ownership, rejects cross-Controller reuse, advances generation, and retains older aliases only as inert known entries.
3. Add an atomic `unbind_desktop_session` operation that removes the alias and writes an `unbound` generation tombstone when it was current.
4. Change desktop Hook ingress so an explicit current target is the only accepted source. A legacy canonical source is accepted only when neither desktop aliases nor target metadata exist.
5. Implement `controller_target_guard.py resolve/check` receipts for `native_resume`, `message`, and `navigate`; supplied canonical IDs and stale aliases must fail closed when another task is current.
6. Expose replace/unbind through stable CLI commands and return structured receipts.

### Task 3: Route every native wake through the current target

**Files:**

- Modify: `scripts/web_lifecycle_bridge.py`
- Modify: `scripts/terminal_continuation.py`
- Modify: `tests/test_web_lifecycle_bridge.py`
- Modify: `tests/test_terminal_continuation.py`

1. Resolve the logical Controller from the repository as before.
2. Before building `codex exec resume`, resolve the current `desktop_codex` execution target through the guard.
3. Keep `controller_id` and lifecycle state paths canonical, but record `execution_target_session_id` and binding generation in wake receipts.
4. Reject explicit stale/canonical task targets before starting the Codex process.
5. Verify terminal receipt continuation dispatches logical Controller state to the current execution target without creating a second Controller.

### Task 4: Tighten the Skill contract from the observed RED rationalization

**Files:**

- Modify: `SKILL.md`
- Modify: `references/long-task-governance.md`
- Modify: `README.md`

1. State the three distinct identities: stable logical `controller_id`, inbound `source_session_id`, and outbound `execution_target_session_id`.
2. Require `controller_target_guard.py check` before app-level message or navigation actions; a task title, pasted URL, remembered alias, list order, or canonical lineage is never target authority.
3. Require explicit replace for a new current desktop task and explicit unbind for retirement; aliases without a current target are inert.
4. Document the host boundary: the Skill can machine-block its native resume paths and produce guard receipts for app calls, but the Codex app must invoke the guard before external message/navigation tools.
5. Add a concise rationalization table and red flags based on the baseline failure: “canonical is the execution thread,” “the alias list has one item,” and “the user pasted the old link again.”

### Task 5: Verify, review, deploy, and activate safely

**Files:**

- Test all changed modules and the full Python/Node suites.
- Install to the existing managed directory `~/.agents/skills/adaptive-delivery`.

1. Re-run the same three combined-pressure scenarios with the updated Skill and require convergent fail-closed/current-target answers.
2. Run focused tests, full tests, structural validation, `git diff --check`, and installer tests.
3. Ask an independent non-author reviewer to inspect identity separation, migration behavior, wake routing, and bypasses.
4. Create one scoped commit, push `main` normally, and verify the remote SHA.
5. Install the exact pushed revision and verify manifest hashes plus installed-copy tests.
6. Explicitly replace the affected project's retired desktop target with its verified current task only after the installed guard passes the same repository check.
7. Send the rule-update notice only to the guard-approved current task and require an exact loaded-revision ACK. Until that ACK exists, report source/install completion separately from downstream adoption.
