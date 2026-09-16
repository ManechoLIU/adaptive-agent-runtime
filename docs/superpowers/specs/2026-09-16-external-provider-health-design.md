# External Provider Health and Circuit Breaker Design

**Date:** 2026-09-16

## Goal

Add a deterministic Runtime health projection and circuit breaker for external model routes so repeated Grok/Kimi/provider-chain failures are contained before they waste allowance, duplicate paid calls, or repeatedly occupy the critical path.

This subsystem is provider-generic. Grok is the immediate motivation; Kimi and future external providers use the same health contract.

## Current baseline

The Runtime already has bounded external execution after the External Agent Stall P0:

- pre-spawn input validation;
- provider-start timeout;
- first-valid-progress timeout;
- generation/heartbeat stall timeout;
- absolute provider deadline;
- process-group TERM → grace → KILL cleanup;
- result-unknown fencing;
- no same-route blind retry;
- explicit route/fallback contracts.

The remaining gap is cross-attempt health memory: each call is bounded, but Runtime does not yet project recent route degradation into a deterministic “do not dispatch this route again right now” decision.

## Non-goals

- Do not replace existing Assignment recovery budgets.
- Do not add another scheduler or provider daemon.
- Do not silently switch OAuth ↔ API or change billing boundaries.
- Do not retry `result_unknown` attempts.
- Do not infer provider health from browser UI, DOM, screenshots, or model self-report.
- Do not mark a provider unhealthy because of semantic model-quality failures; those belong to Project Model Score.

## Route key

Health is tracked by exact route identity:

```text
provider + model + auth_mode + execution_transport
```

Optionally reasoning effort is retained for diagnostics but does not split the breaker unless evidence shows effort-specific transport behavior.

Examples:

```text
grok-build / grok-4.6 / oauth / external_process
kimi-code / kimi-k3 / api / external_process
```

OAuth and API are distinct health routes because they cross different authorization, quota, and billing paths.

## Input evidence

The breaker consumes only canonical terminal/runtime evidence already persisted by the runner:

- `failure_class`
- `outcome_code`
- `retry_class`
- `retry_safe`
- `result_unknown`
- `transport_outcome`
- `delivery_outcome`
- `provider_started`
- cleanup confirmation
- timestamps and exact route contract

Project semantic failures with healthy transport do not count as provider-chain failures.

## Health states

Health is a derived projection, not a mutable second ledger:

```text
HEALTHY
DEGRADED
OPEN
PROBE_REQUIRED
```

### HEALTHY

Normal dispatch allowed.

### DEGRADED

Recent infrastructure/external failures exceed the warning threshold. New dispatch is allowed only when the Controller has an ordinary authorized reason to use the route; reports surface the degradation. No extra paid call is generated merely to test health.

### OPEN

Automatic/default dispatch to the route is denied before provider spawn. Runtime must resolve an already-authorized safe fallback or return a structured blocker.

### PROBE_REQUIRED

The breaker has cooled down or a relevant environment change occurred, but health is not restored. The next explicitly authorized real task may act as the probe. Runtime does not generate a synthetic paid probe by itself.

## Failure classes that affect route health

Count strongly toward degradation/opening:

- provider start timeout;
- first-token timeout;
- generation/heartbeat stall;
- provider timeout;
- process-group cleanup failure;
- runner/CLI unavailable after preflight was previously healthy;
- provider/service unavailable;
- auth/session failure for the exact auth route;
- repeated invalid/missing provider terminal envelope where Runtime transport is otherwise healthy.

Do not count as route-health failure:

- Reviewer findings;
- failed project tests after a valid model result;
- owned-scope semantic violation;
- bad implementation;
- Controller cancellation unrelated to provider health;
- route-contract rejection before the provider route was actually selected/spawned.

`result_unknown=true` is handled as a safety latch regardless of health state and can never be auto-retried.

## Opening policy

Use a bounded recent window per route. Initial deterministic thresholds:

- enter `DEGRADED` after 2 infrastructure/external failures in the last 5 eligible attempts;
- enter `OPEN` after 3 infrastructure/external failures in the last 5 eligible attempts, or 2 consecutive provider-boundary failures;
- any `process_group_cleanup_failed` with uncertainty may open immediately because retry safety is not established;
- semantic model failures do not affect the breaker.

The window is attempt-count based, with timestamps retained for cooldown. This avoids low-volume routes being punished indefinitely by one old incident.

## Cooldown and recovery

`OPEN` does not auto-close to HEALTHY on time alone.

After the configured cooldown, state becomes `PROBE_REQUIRED`. A real, explicitly authorized task on the same route restores `HEALTHY` only when all required transport evidence succeeds:

```text
provider start
→ valid model progress
→ valid final result
→ valid delivery receipt or canonical semantic terminal
→ process group fully reaped
→ result_unknown=false
```

For Reviewer routes, a valid structured PASS or FINDINGS verdict counts as semantic terminal success; PASS is not required for route recovery.

If the probe fails for an infrastructure/external reason, the route returns to `OPEN`.

## Fallback behavior

When route health is `OPEN`:

1. preserve the original Assignment/task lineage;
2. do not replay the external call;
3. inspect canonical prior terminal and `result_unknown`;
4. use existing `safe_fallback` machinery only if the project/user already authorized that alternative route and no billing/auth/side-effect boundary is crossed silently;
5. otherwise return a structured blocker.

The breaker does not invent fallback policy.

## Interaction with Project Model Score

Provider health failures feed Route Reliability Score and health diagnostics, not Project Model Score.

A route can therefore be:

```text
Project Model Score: high
Route Reliability: low
Provider Health: OPEN
```

This is intentional and explains “the model is capable, but this execution path is currently unreliable.”

## Storage

Derived health snapshots live under Git common-dir:

```text
.git/adaptive-delivery/provider-health/
  latest.json
```

This file is reproducible from canonical Runtime evidence and is not authoritative project state. The launch gate may recompute health directly before dispatch or verify the cached snapshot against its input evidence hash.

## CLI / diagnostic surface

Read-only diagnostic command:

```bash
python3 scripts/provider_health.py status --repo <repo>
```

Example fields:

```text
route
state
eligible_attempts
recent_failures
consecutive_failures
last_failure_class
opened_at
cooldown_until
probe_eligible
result_unknown_blockers
```

## Launch integration

The external-agent launch path gains one pre-spawn health gate after canonical route validation and before provider spawn.

Rules:

- `HEALTHY`: proceed.
- `DEGRADED`: proceed, emit diagnostic metadata.
- `OPEN`: fail closed before provider spawn; resolve authorized safe fallback outside the runner invocation.
- `PROBE_REQUIRED`: proceed only because the current task already carries explicit authorization for that exact external route; mark the attempt as the health probe.
- `result_unknown` or cleanup uncertainty remains a stronger blocker than breaker state.

## Acceptance criteria

1. Grok and Kimi use the same provider-generic breaker implementation.
2. Repeated provider-chain failures can block a new external spawn before allowance is consumed.
3. Semantic model failures do not open the breaker.
4. OAuth and API routes do not share health state.
5. `result_unknown` can never be bypassed by cooldown or probe state.
6. The breaker never performs its own paid health-check call.
7. Recovery to HEALTHY requires one fully trustworthy real attempt on the exact route.
8. Existing safe-fallback authorization, route-contract, recovery-budget, and side-effect fences remain authoritative.
