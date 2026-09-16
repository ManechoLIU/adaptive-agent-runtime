# External Provider Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a provider-generic, evidence-derived health projection and pre-spawn circuit breaker for Grok, Kimi and future external model routes.

**Architecture:** `scripts/provider_health.py` derives health from canonical Runtime leases and persists only a reproducible cache snapshot under Git common-dir. `scripts/run_external_agent.mjs` invokes a read-only pre-spawn health check after route validation; it never invents fallback policy and never bypasses result-unknown fencing.

**Tech Stack:** Python 3 standard library, Node.js existing external runner, Python unittest + Node test runner.

**Spec:** `docs/superpowers/specs/2026-09-16-external-provider-health-design.md`

## Global Constraints

- Provider-generic; no Grok-only health state machine.
- Exact route identity includes provider/model/auth_mode/execution_transport.
- Model semantic failures do not degrade route health.
- `result_unknown=true` is never retryable or bypassed by cooldown/probe state.
- Breaker does not change OAuth/API billing route or invent fallback authorization.
- No synthetic paid probe; only an explicitly authorized real task can recover a route.

---

### Task 1: Derived provider health projection

**Files:**
- Create: `scripts/provider_health.py`
- Create: `tests/test_provider_health.py`

**Interfaces:**
- Produces: `derive_route_health(samples: list[dict], *, now: datetime | None = None, cooldown_seconds: int = 1800) -> list[dict]`
- Produces: `route_health_for_assignment(repo: Path, route: dict, *, now: datetime | None = None) -> dict`

- [ ] Write RED tests for HEALTHY, DEGRADED, OPEN, PROBE_REQUIRED, immediate open on cleanup uncertainty, and semantic failure exclusion.
- [ ] Run focused tests and verify RED.
- [ ] Implement minimal deterministic projection from canonical leases.
- [ ] Run focused tests to GREEN.
- [ ] Commit `feat(runtime): derive external provider health`.

### Task 2: Read-only status CLI and reproducible cache

**Files:**
- Modify: `scripts/provider_health.py`
- Modify: `tests/test_provider_health.py`

**Interfaces:**
- CLI: `python3 scripts/provider_health.py status --repo <repo>`

- [ ] Write RED tests for deterministic JSON output and cache evidence hash.
- [ ] Implement atomic `.git/adaptive-delivery/provider-health/latest.json` cache that is reproducible and non-authoritative.
- [ ] Run tests GREEN.
- [ ] Commit `feat(runtime): expose provider health status`.

### Task 3: External runner pre-spawn gate

**Files:**
- Modify: `scripts/run_external_agent.mjs`
- Modify: `tests/external-agent-routing.test.mjs`

**Interfaces:**
- Consumes provider-health CLI/projection after canonical route validation and before provider spawn.

- [ ] Write Node RED tests proving OPEN blocks before spawn, DEGRADED does not silently change route, PROBE_REQUIRED permits only the existing explicitly authorized execution, and result_unknown remains stronger than breaker state.
- [ ] Verify RED.
- [ ] Implement the minimal pre-spawn gate without modifying route fallback semantics.
- [ ] Run focused Node tests GREEN.
- [ ] Commit `feat(runtime): gate degraded external provider routes`.

### Task 4: Regression verification

**Files:**
- No source changes unless a new RED regression proves a defect.

- [ ] Run `python3 -m unittest tests.test_provider_health tests.test_assignment_runtime -q`.
- [ ] Run `node --test tests/external-agent-protocol.test.mjs`.
- [ ] Run Grok/Kimi route-health focused tests in `tests/external-agent-routing.test.mjs`.
- [ ] Run `git diff --check` and confirm no retry/side-effect safety regressions.
