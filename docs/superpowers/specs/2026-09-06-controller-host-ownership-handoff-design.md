# Controller Host Ownership Handoff Design

## Context

SelfAlone has one logical Controller. Its identity, Goal, ledger, Assignment state, continuation debt, and governance lineage must remain stable across UI/session entry points.

The current Runtime can represent both `web` and `desktop_codex` hosts, but continuation still effectively follows a durable desktop execution target in important paths. As a result, a Web-originated continuation can resume a desktop Codex thread and consume desktop Codex usage. This violates the intended execution-boundary semantics even though logical Controller uniqueness is preserved.

## Goal

Decouple logical Controller identity from its current execution host so that the host which owns current control rights receives continuation work:

- Web-controlled execution uses the Web host and Web model quota.
- Desktop Codex-controlled execution uses `desktop_codex` and desktop Codex quota.
- Switching entry point changes only execution ownership/target, never logical Controller identity or project governance state.
- Automatic continuation always targets the currently owned host.
- No silent fallback from one host to the other.

## Non-goals

- Do not create or fork a second logical Controller.
- Do not duplicate Goal, ledger, Assignment, candidate, or continuation state per host.
- Do not introduce a second scheduler or a parallel continuation state machine.
- Do not make model/provider selection a per-call heuristic.
- Do not automatically fall back from Web to desktop Codex or vice versa when quota or host availability fails.

## Core Invariants

1. `controller_id` remains the canonical logical identity across all handoffs.
2. At most one host owns active execution rights for that Controller at a time.
3. Every ownership change increments a target/ownership generation.
4. Any continuation, wake, receipt, recovery, or target replacement is fenced by the current generation.
5. A stale Web or desktop generation cannot write a successful wake receipt, re-arm continuation, replace the current target, or regain ownership.
6. Host failure preserves continuation debt and reports degraded/blocked state; it does not silently execute on the other host.
7. User entry from a host may claim ownership only through the canonical handoff path.

## Recommended Architecture

### 1. Canonical host ownership record

Extend the existing canonical target/registry representation rather than adding a new state machine. The current Controller record must expose:

- `controller_id`
- `active_host`: `web | desktop_codex`
- `execution_target_session_id`
- `generation`
- `target_mode`
- ownership timestamp / receipt identity as already supported by target receipts

The record remains the single source of truth for execution ownership.

### 2. Atomic host handoff

Introduce one canonical handoff operation used by both Web and desktop entry paths:

`claim_controller_host(controller_id, requested_host, requested_target, expected_generation)`

The operation must:

1. lock the canonical Controller target record;
2. verify `controller_id` still matches the project Controller;
3. compare the caller's expected generation;
4. set `active_host` and target to the requested host/session;
5. increment generation exactly once;
6. persist a handoff receipt;
7. release the lock before any external model/process work begins.

No host can claim ownership by only changing lifecycle projection or a local session variable.

### 3. Entry-point semantics

When a Web Controller entry point becomes authoritative, it claims `active_host=web` using its current registered Web execution target.

When the same Controller is explicitly resumed from desktop Codex, that entry claims `active_host=desktop_codex` using the current desktop execution target.

A host transition is an execution ownership migration only. It must not change:

- logical `controller_id`;
- Goal;
- TASK_LEDGER;
- canonical Assignment leases;
- continuation debt;
- candidate/reviewer/integration state.

### 4. Continuation routing

`ensure_continuation_supervisor` and its downstream resume path must resolve the canonical current host immediately before external execution.

- `active_host=web` -> execute the existing Web re-entry path only.
- `active_host=desktop_codex` -> execute the existing native Codex resume path only.

The supervisor must not infer host from an old lifecycle snapshot when the canonical target record has a newer generation.

### 5. Stale-generation fencing

Before committing any external result, a continuation worker must re-check that:

- controller identity still matches;
- active host still matches the worker host;
- generation still matches;
- execution target still matches.

If any differ, the result is `SUPERSEDED/DEFERRED`; the stale worker cannot persist a confirmed wake or schedule a successor.

This reuses the existing target-fence principles already present in delayed native wake persistence and supervisor supersession handling.

### 6. Provider/quota failure behavior

Provider usage limits remain host-local failures.

- Desktop quota exhaustion -> `desktop_codex` stays owner unless the user explicitly enters from Web and performs a handoff.
- Web quota/host failure -> Web stays owner until an explicit desktop handoff.
- Runtime records backoff/degraded state and preserves continuation debt.
- No automatic cross-host fallback.

The bounded provider-limit backoff fix in `d743f5d` remains valid and independent of host handoff.

## Alternatives Considered

### A. Choose host dynamically on every continuation call

Rejected. This makes source detection a routing heuristic, creates race windows between Web and desktop, and makes stale entry points capable of stealing execution implicitly.

### B. Maintain separate logical Controllers for Web and desktop

Rejected. This violates the project invariant of one logical Controller and would split Goal, ledger, review, and recovery lineage.

### C. Always keep desktop Codex canonical and use Web only as a control UI

Rejected. This is the current behavior that causes Web-originated work to consume desktop Codex quota and does not match the desired product semantics.

## Error Handling

- Generation mismatch: return a superseded/deferred result with no target mutation.
- Unknown/unregistered requested target: reject handoff and preserve prior owner.
- Current host unavailable: preserve continuation debt and mark degraded/blocked.
- Provider usage limit: enter bounded backoff on the owning host; do not cross-host fallback.
- Concurrent Web/Desktop claims: serialization by target lock; later valid claim wins via a new generation, while earlier external work is fenced from committing afterward.
- Missing canonical Controller: fail closed; never synthesize another Controller.

## Testing Strategy

### Unit/contract tests

1. Web claim increments generation and preserves controller identity.
2. Desktop claim after Web increments generation and replaces only execution ownership.
3. Stale generation cannot persist a wake receipt.
4. Stale supervisor cannot re-arm after handoff.
5. Web continuation never calls native Codex resume when Web owns the target.
6. Desktop continuation never calls Web re-entry when desktop owns the target.
7. Provider/quota failure does not trigger cross-host fallback.
8. Concurrent host claims remain single-owner and generation-fenced.

### Regression suites

Run at minimum:

- `tests.test_web_lifecycle_bridge`
- lifecycle/target guard tests
- terminal continuation tests
- desktop turn recovery tests
- governance/controller session tests
- installer release regressions

### Live E2E acceptance

All three scenarios are required before calling the feature complete:

1. **Web -> Web continuation**: start/continue from Web, force a lifecycle continuation, prove no `codex exec resume` process is used and the same logical Controller continues through Web.
2. **Desktop -> Desktop continuation**: explicitly operate from desktop Codex and prove continuation uses the desktop target.
3. **Bidirectional handoff**: Web -> Desktop -> Web, proving each handoff increments generation, old host becomes fenced, and no duplicate Controller/parallel execution occurs.

Acceptance evidence must include exact Controller ID, target host, target session, generation, wake/continuation receipt, and absence of stale-host execution.

## Rollout

1. Implement behind the existing canonical target/registry contract; no separate feature flag unless tests reveal compatibility need.
2. Install exact Runtime revision atomically with release regressions.
3. Existing unique SelfAlone Controller ACKs the exact revision.
4. Run live E2E acceptance for all three host scenarios.
5. Keep Runtime branch unmerged until independent non-author Reviewer PASS and acceptance are current.

## Completion Criteria

The change is complete only when:

- Web-originated continuation uses Web execution and does not consume desktop Codex quota;
- desktop-originated continuation still uses desktop Codex;
- host switching preserves the same logical Controller and project state;
- stale hosts cannot commit or re-arm;
- provider-limit backoff is bounded;
- unit/regression suites pass;
- all three live E2E scenarios pass;
- independent non-author Runtime Reviewer gives PASS on the exact installed revision.
