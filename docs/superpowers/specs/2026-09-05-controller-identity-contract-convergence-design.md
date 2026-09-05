# Controller Identity Contract Convergence Design

## Goal

Make Controller identity a durable safety guard for one logical Controller without allowing identity-verifier or capability drift to become a single point of failure for continuous governance.

## Existing Facts

- `controller_target_guard.py` already owns the canonical Controller identity projection and separates durable project ownership from host/session authorization.
- `web_lifecycle_bridge.py` already owns same-controller Web recovery and target-generation rebinding; it must not become a second identity owner.
- `project_context_guard.py` already consumes the canonical identity projection.
- Runtime rule/installation drift already has canonical manifest and handshake machinery; identity capability drift must reuse it rather than create another version store.
- The historical `web_lifecycle_bridge.py controller-identity` entry is not implemented. Governance rules that require it therefore represent contract drift, not proof that the existing Controller is invalid.

## Canonical Identity States

Identity remains a projection dimension, not a new Controller task-state machine.

- `VERIFIED`: unique registered logical Controller and current host/session binding are machine verified. Controller-exclusive mutations may proceed subject to existing action-specific gates.
- `DEGRADED`: unique registered logical Controller remains valid and no conflicting Controller/session/generation evidence exists, but a verifier/capability required for stronger session authorization is unavailable or the installed governance/runtime capability contract is drifting. Safe control-plane work continues; identity-sensitive irreversible/ownership-changing actions remain closed.
- `UNVERIFIED`: identity service is available but the presented entry has insufficient evidence to belong to the existing Controller lineage. Read-only facts and same-controller recovery may proceed; Controller-exclusive mutation does not.
- `CONFLICTED`: real contradictory ownership/session/generation evidence exists. Fail closed for Controller mutations and same-controller recovery until resolved.

`repo_access != controller_identity` remains unchanged. `identity_verifier_unavailable != controller_not_controller` becomes an explicit invariant.

## Action Classes

Safe in `DEGRADED` when the project has exactly one existing Controller and no conflict evidence:

- terminal/result/verdict consumption;
- fact projection and project-wide runnable recomputation;
- dependency/candidate/review/integration bookkeeping;
- continuation/recovery bookkeeping;
- deterministic read-only checks and validations;
- Stop/Yield evaluation, where unresolved runnable/debt still blocks Yield.

Still fail closed in `DEGRADED`:

- create/reappoint a logical Controller;
- bind a session already owned by another Controller;
- replace an active target without the existing generation/lease safeguards;
- Controller-exclusive irreversible mutations whose host/session target cannot be verified;
- any action when project Controller, session owner, or generation is actually conflicted.

## Capability Contract

The runtime exposes a machine-readable identity capability projection from the same canonical module that owns identity semantics. Minimum capabilities:

- `controller_identity_projection`
- `same_controller_recovery`
- `web_session_binding`
- `target_generation_fence`

Consumers compare governance-required capability names with the installed runtime capability projection. A missing required capability yields `RUNTIME_CONTRACT_DRIFT`; it does not rewrite the project Controller ownership to absent/unverified.

The canonical CLI for direct identity inspection is `controller_target_guard.py identity`. `web_lifecycle_bridge.py` remains lifecycle/recovery orchestration and does not duplicate identity projection.

## Continuous Execution Integration

The existing control loop remains:

`event -> consume -> fact projection -> project-wide recompute -> runnable -> dispatch/recover/integrate -> recompute -> Self-Check -> Yield`

Identity is an action gate inside this loop. If Goal is unfinished, runnable/debt exists, exactly one Controller exists, and there is no true identity conflict, a verifier/capability outage cannot by itself authorize Stop/Yield. The loop continues safe work and records degraded identity evidence.

## Recovery

Normal turns and same-session continuation reuse the durable Controller registry/binding/generation. Web recovery continues through `recover_same_controller_web_session()` and may only bind the existing `controller_id`. Successful machine attestation promotes `DEGRADED/UNVERIFIED` to `VERIFIED`. A degraded state must never allow creation of a second logical Controller.

## Contract Synchronization

- Runtime code, `SKILL.md`/governance references, installer capability manifest/tests, and project rules must reference canonical capability/CLI names.
- Project rules must not require an unimplemented command.
- Installed-runtime mismatch is reported as `RUNTIME_CONTRACT_DRIFT` with missing capability evidence.

## Regression Coverage

Cover initial appointment semantics, continuous same-session use, turn/terminal continuation, Web recovery, host/session migration, verifier unavailable, missing identity capability, runtime/governance drift, real dual-Controller conflict, stale target/generation, crash/recovery, degraded-to-verified promotion, degraded cannot create Controller, and Stop/Yield cannot use verifier outage as a reason to stop while runnable work remains.
