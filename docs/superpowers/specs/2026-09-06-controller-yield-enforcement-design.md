# Controller Yield Enforcement and Same-Controller Continuation Design

## Context

Adaptive Agent Runtime already knows many reasons a Controller must not Yield: known executable next action, runnable work, candidate/reviewer/integration debt, current-main verification debt, project-wide recompute debt, idle-capacity dispatch debt, recoverable blockers, and terminal/recovery events. `lifecycle_hook.evaluate_event()` can return `decision=block` for a `Stop` event.

The missing closure is execution semantics. In Web flows, MODEL END_TURN can close the product turn without a reliable Runtime `Stop` callback. Even when a Web bridge path invokes `lifecycle_hook`, `dispatch_event()` currently observes only the subprocess exit code; a `decision=block` JSON result still exits 0 and is therefore treated as a successful bridge event. The detached health supervisor can re-arm continuation only when durable continuation debt already exists.

## Goal

Make logical Controller Yield a Runtime-controlled state transition independent of a model/product turn ending:

- MODEL END_TURN is only a host/UI event; it is not proof of Controller Yield.
- Controller Yield succeeds only after the canonical Yield Gate passes.
- A rejected Yield becomes durable continuation debt and cannot be recorded as a completed control cycle.
- The same logical Controller is automatically re-entered on the current canonical owning host without another user message.
- Continuation starts from fresh project facts and project-wide recompute, then dispatch/refill continues until genuine Yield conditions hold.

## Invariants

1. One logical Controller only. Yield rejection never creates/forks/reappoints a Controller.
2. A rejected Yield persists `pending_control_event=true`, `requires_user=false`, a `YIELD_GATE_REJECTED`-class trigger, and bounded rejection metadata.
3. Web bridge callers must distinguish lifecycle `decision=block` from subprocess transport success.
4. Rejected Yield must arm or remain discoverable by the existing same-controller continuation supervisor.
5. Canonical continuation debt can independently reopen continuation when no model-end callback exists.
6. Re-entry routes through canonical host ownership; there is no silent Web↔desktop fallback.
7. Stale host generations cannot commit wake results or re-arm successors.
8. Observation-only turns and genuine `requires_user=true` blockers do not generate autonomous continuation.

## Controller Layer

Treat final/END_TURN as a formal Yield intent. Before a Controller voluntarily finishes a control cycle it must have a current-turn control-loop receipt proving closure. Stage conclusions, diagnostics, or an identified next action are not closure.

The Runtime state/projection exposed to re-entered Controllers must explicitly say a prior Yield was rejected and require the standard sequence: fact projection → project-wide recompute → consume controller actions/candidates/verdicts → dispatch/refill → re-evaluate Yield Gate.

## Runtime Layer

### 1. Durable Stop rejection

Every `Stop` gate rejection persists rejection state before returning the block decision. This includes known-next-action rejection and project-wide control-loop rejection. Persisted state includes the pending event and trigger needed by the detached supervisor.

### 2. Web lifecycle decision parsing

Introduce a structured bridge result for lifecycle subprocess execution. Transport `returncode=0` with stdout `{"decision":"block"...}` is `yield_blocked=true`, not a successful logical Yield.

The Web lifecycle caller must continue into continuation scheduling after such a block and only then return a blocking result to its caller. A block is not allowed to short-circuit before the supervisor is armed.

### 3. Callback-independent debt recovery

`controller_continuation_projection()` derives debt from canonical facts on every health tick. It must include persisted non-user `next_action` debt in addition to runnable/controller-action/pending-event debt. If canonical debt exists while `pending_control_event=false`, it atomically reopens the event, advances wake generation, records runtime continuation debt and Yield-recovery metadata, and preserves the current canonical host.

This is the fallback for Web MODEL END_TURN where the product supplies no Stop callback.

### 4. Same-controller re-entry

The existing continuation supervisor performs the wake. It resolves canonical host ownership immediately before execution and is fenced again before committing/rearming. Web owner uses Web re-entry; desktop owner uses native resume. No cross-host fallback.

## Failure Semantics

- Host/session identity unavailable: preserve debt and retry/degrade; do not create another Controller.
- Provider limit: bounded host-local backoff.
- Active Web response: defer and retry without counting progress.
- Stale ownership generation: supersede the stale worker without committing/rearming.
- Explicit user decision required: stop autonomous continuation.

## Required Tests

1. Known-next-action Stop rejection persists pending event and Yield rejection metadata.
2. Control-loop-required Stop rejection persists pending event and Yield rejection metadata.
3. Web lifecycle subprocess transport success + `decision=block` is parsed as a blocked logical Yield.
4. Web bridge schedules same-controller continuation before returning blocked status.
5. Health tick reopens continuation from persisted non-user next action even without a Stop callback.
6. Canonical runnable/candidate/controller action debt reopens continuation without a user message.
7. Observation-only and requires-user states do not reopen continuation.
8. Host ownership routing and stale-generation fencing remain enforced.

## Live E2E Acceptance

A release is not complete until a real Web flow proves:

Goal unfinished → canonical runnable/known-next-action exists → Controller produces an early final/END_TURN → no user message is sent → detached Runtime observes remaining debt → same logical Controller is re-entered on Web → fact projection/project-wide recompute runs → next action is consumed → Reviewer/verdict/integration/current-main verify execute as applicable → refill continues while READY exists → only a truly closed Yield Gate allows the Controller to remain stopped.

Evidence must show the same controller_id, active host, ownership generation, wake receipt, continued control-loop receipts, and absence of desktop Codex execution during Web ownership.
