# Project Model Score Design

**Date:** 2026-09-16

## Goal

Add a Runtime-native, evidence-based scoring subsystem that evaluates every model actually used in a project without creating a second project ledger or confusing model capability with execution-chain reliability.

The system must automatically include current and future models observed in canonical Runtime evidence. This includes, but is not limited to:

- `gpt-5.6-sol`
- `gpt-5.6-terra`
- `gpt-5.6-luna`
- `grok-4.6`
- Kimi K3 routes such as `kimi-k3` / the canonical Kimi Code model identity used by the runner

No model is hard-coded as the scoring population. The population is discovered from canonical Assignment / receipt facts.

## Problem

Public model benchmarks answer “how does this model perform on a standardized benchmark?” They do not answer “how well does this model perform in this user's projects, under this Runtime, route, auth mode, reasoning effort, task type, and acceptance process?”

Existing Runtime evidence already records most of the required facts, but there is no deterministic aggregation layer that:

1. separates model-caused failures from infrastructure/external failures;
2. compares models by exact route context;
3. prevents weak or ambiguous evidence from becoming a model score;
4. exposes sample size and confidence;
5. keeps ModelDial as an external benchmark rather than mixing it into project truth.

## Non-goals

- Do not create a second `TASK_LEDGER.md`, scheduler, Controller, or Assignment store.
- Do not alter task state, routing policy, provider authorization, or Controller ownership in V1.
- Do not automatically switch models based on score in V1.
- Do not score a model from natural-language impressions or chat memory.
- Do not treat provider/process failures as model-quality failures without attribution evidence.
- Do not collapse all efforts, providers, auth modes, roles, or task types into one opaque number.

## Authoritative evidence sources

V1 reads only existing canonical or auditable Runtime facts:

1. Git common-dir `adaptive-delivery/runtime-assignments.json`.
2. Git common-dir `adaptive-delivery/reviewer-runs/*.json` where a canonical Assignment or immutable candidate can be tied back to the run.
3. Canonical delivery fields already enforced by `references/agent-delivery-contract.md`, including `transport_outcome`, `delivery_outcome`, `evidence[]`, `artifacts[]`, `candidate_revision`, and `review_verdict` when present.
4. Route facts already frozen in Runtime state: `provider`, `model`, `auth_mode`, `strategy` / `reasoning_effort`, `policy_class`, `execution_role`, `execution_transport`.
5. Traceable test and artifact locators already present in the receipt. V1 does not open arbitrary prose and attempt to infer hidden success.

Historical legacy receipts remain readable but receive lower evidence confidence when exact model/effort/route identity is missing.

## Model identity

Each scored sample is attached to a normalized identity tuple:

```text
project
provider
model
auth_mode
reasoning_effort
execution_role
policy_class
execution_transport
```

The report may aggregate upward, but raw samples retain the full tuple.

Examples:

```text
SelfAlone / grok-build / grok-4.6 / oauth / high / writer / backend / external_process
SelfAlone / kimi-code / kimi-k3 / api / medium / writer / frontend / external_process
SelfAlone / codex-native / gpt-5.6-sol / host / high / writer / backend / codex_native_subagent
```

If `reasoning_effort` cannot be recovered from a canonical field or frozen `strategy`, the sample is tagged `effort=unknown`; it may contribute to model-level totals but not effort-level comparisons.

## Dynamic model discovery

V1 discovers models by scanning canonical Runtime leases and accepted reviewer evidence, not by enumerating providers in code.

A model appears in a report when at least one eligible sample contains a non-empty canonical `model` value. Provider-specific aliases are normalized only when the existing Runtime runner already defines an unambiguous canonical identity. Unknown aliases remain separate rather than being guessed together.

This guarantees that Kimi K3 and any future model automatically enter the report once the Runtime has trustworthy evidence for them.

## Evidence eligibility

A sample is eligible for scoring only when all of the following are true:

- it belongs to the requested project/repo scope;
- it has a terminal or otherwise immutable evaluation boundary;
- model identity is machine-readable;
- the same execution is not counted twice through both Assignment and Reviewer projections;
- its outcome can be attributed at least to `model`, `infrastructure`, `external`, `mixed`, or `unknown`.

Samples with `unknown` attribution remain visible in diagnostics but do not change the Project Model Score.

## Attribution taxonomy

Every eligible sample is classified before scoring:

### `model`

Use when evidence shows the provider/model returned or executed but the work itself was deficient, for example:

- invalid implementation under a healthy transport;
- Reviewer Critical/Important findings attributable to the produced candidate;
- explicit owned-scope or task-contract violation by the model after successful launch;
- valid final model result that fails required project verification;
- repeated semantic non-compliance with the bounded task packet.

### `infrastructure`

Use when the model's semantic quality cannot fairly be judged because the execution chain failed, for example:

- provider start timeout;
- first-token timeout;
- process/CLI stall or cleanup failure;
- missing or invalid delivery receipt caused by runner/runtime plumbing;
- bridge, Host, sandbox, route, auth, or tool unavailability before a trustworthy semantic result;
- result unknown where model output cannot be reconciled.

### `external`

Use for provider/service quota, outage, credential revocation, upstream service errors, or user/environment conditions outside model reasoning quality.

### `mixed`

Use only when evidence supports both a model-quality issue and an infrastructure/external contribution. Mixed samples contribute with a reduced weight and expose the split reason in diagnostics.

### `unknown`

Use when evidence is insufficient. Unknown never silently becomes model failure.

Attribution must be derived from structured fields first (`failure_class`, `outcome_code`, `retry_class`, `result_unknown`, transport state, reviewer verdict, delivery state). Summary text may only refine an already-supported classification and may never override contradictory structured facts.

## Project Model Score

The V1 Project Model Score is 0–100 and measures model work quality only. It is not a route-health score.

Weights:

| Dimension | Weight | Evidence basis |
| --- | ---: | --- |
| Delivery success | 35% | validated `delivery_outcome`, candidate/artifact evidence |
| First-pass quality | 25% | Reviewer PASS vs Critical/Important findings, correction need |
| Verification strength | 20% | traceable tests/evidence required by the Assignment |
| Efficiency | 10% | bounded elapsed time/retries normalized within comparable task class |
| Recovery / rework burden | 10% | model-caused rework or correction, not infrastructure recovery |

Each sample produces a dimension vector only from facts it can support. Missing dimensions are marked unavailable rather than inferred. Aggregate scores are weighted over available evidence and include coverage metadata.

### Delivery success

- Validated PASS with traceable artifact/evidence: high positive contribution.
- Explicit delivery FAIL caused by semantic work quality: negative contribution.
- `unresolved` caused by infrastructure: excluded from model-quality numerator/denominator.

### First-pass quality

- Independent Reviewer PASS on exact candidate: strong positive evidence.
- Critical/Important findings: negative evidence proportional to severity.
- Reviewer transport failure: route reliability evidence only; not model-quality evidence.

### Verification strength

Credit requires traceable evidence already accepted by Runtime contracts. Free-form “tests passed” prose is insufficient.

### Efficiency

V1 compares elapsed time only within sufficiently similar `policy_class + execution_role + reasoning_effort` cohorts. It does not punish xhigh/max simply for taking longer than low/medium tasks.

### Recovery / rework burden

Only model-attributable rework counts against the model. Host/provider recovery is excluded.

## Route Reliability Score

A separate 0–100 Route Reliability Score evaluates the execution path, grouped by:

```text
provider + model + auth_mode + execution_transport
```

and optionally by reasoning effort.

It uses transport/process evidence such as:

- clean provider start;
- first valid progress;
- clean terminal result;
- valid delivery receipt persistence;
- process-group cleanup confirmation;
- `result_unknown` frequency;
- retry-safe vs non-retry-safe failures;
- route/Host/bridge availability.

This score answers “can we reliably get a trustworthy result from this route?” and must never be merged into Project Model Score without being shown separately.

## ModelDial benchmark sidecar

ModelDial remains a separate external benchmark source. V1 report schema provides optional fields:

```text
benchmark_source = modeldial
benchmark_model
benchmark_route
benchmark_effort
benchmark_score
benchmark_observed_at
benchmark_match = exact | partial | none
```

Rules:

- `exact` requires matching model + effort + route semantics.
- `partial` may show context but is never described as the user's exact project score.
- `none` leaves the public benchmark blank.
- ModelDial does not change the Project Model Score calculation.

## Confidence and sample size

Every aggregate publishes:

```text
sample_count
scored_sample_count
excluded_infrastructure_count
excluded_external_count
unknown_attribution_count
reviewed_sample_count
confidence = insufficient | low | medium | high
```

Initial thresholds:

- `insufficient`: fewer than 3 scored samples;
- `low`: 3–5 scored samples;
- `medium`: 6–14 scored samples with at least 2 independently reviewed samples;
- `high`: at least 15 scored samples with at least 5 independently reviewed samples and no material route-identity ambiguity.

These thresholds are reporting confidence, not score multipliers.

## Storage

Do not mutate project business state. Derived history lives under Git common-dir:

```text
.git/adaptive-delivery/model-performance/
  snapshots/
  latest.json
```

Snapshots are derived analytics receipts, not an authoritative project ledger. Deleting them must not alter Assignment/task state; they can be recomputed from canonical evidence.

Each snapshot stores:

- repo identity and generation timestamp;
- evidence cutoff;
- scorer schema version;
- input evidence hashes;
- per-model route/cohort aggregates;
- excluded-sample reasons;
- optional ModelDial sidecar metadata.

## CLI

Primary interface:

```bash
python3 scripts/project_model_score.py report \
  --repo <repo> \
  --window-days 30
```

Useful filters:

```text
--model <model>
--provider <provider>
--role <writer|reviewer|controller|...>
--policy-class <frontend|backend|general>
--effort <low|medium|high|xhigh|max|unknown>
--json
```

Default output lists every discovered model with sample count, Project Model Score, Route Reliability Score, confidence, and top attribution diagnostics.

## Backward compatibility

- Read legacy `outcome=success` only as historical compatibility where current Runtime already permits it; never upgrade it to modern delivery PASS if artifacts/evidence are absent.
- Missing `model` means the sample is not model-score eligible.
- Missing effort remains `unknown` rather than inferred from current routing defaults.
- Existing Controller Performance Scoring remains separate; this feature scores execution models, not Controller governance quality.

## Acceptance criteria

1. Kimi K3, Grok 4.6, GPT-5.6 Sol/Terra/Luna, and any future model present in canonical runtime evidence are discovered without hard-coded population lists.
2. Infrastructure/external failures cannot lower Project Model Score unless a `mixed` attribution has explicit model-quality evidence.
3. Route Reliability changes independently from model-quality score.
4. Duplicate Assignment/Reviewer projections are deduplicated deterministically.
5. Effort/auth/provider/task-role cohorts can be compared without merging incompatible routes.
6. Reports expose sample size, exclusions, and confidence.
7. ModelDial data is displayed only as a separate benchmark sidecar and never silently blended into project score.
8. Derived score files are reproducible and do not become project state authority.

## Model Decision Engine

The score is useful only when it improves routing decisions. V1 therefore adds a deterministic advisory decision layer that compares:

1. Project Model Score for a comparable task cohort;
2. Route Reliability Score for the exact provider/model/auth/transport route;
3. optional ModelDial benchmark sidecar for the closest comparable model/effort/route;
4. elapsed-time and rework evidence when enough comparable samples exist;
5. sample count and confidence.

The engine produces one advisory action per comparable model configuration:

```text
KEEP
TUNE_EFFORT
CHANGE_ROUTE
CHANGE_ROLE
SWITCH_MODEL
INSUFFICIENT_EVIDENCE
```

It must never silently change the project route. V1 is read-only/advisory.

### Decision rules

- `KEEP`: project score is healthy for the cohort and route reliability is healthy or the model materially outperforms alternatives with sufficient evidence.
- `TUNE_EFFORT`: same-model adjacent effort cohorts show materially similar project quality but a meaningful efficiency difference; never compare effort cohorts with inadequate sample coverage.
- `CHANGE_ROUTE`: project quality is healthy but route reliability is materially degraded. This means “keep the model, repair or change the execution route”, not “switch model”.
- `CHANGE_ROLE`: the model is strong in one execution role/task class but weak in another with sufficient samples; preserve the strong role and stop routing the weak role there.
- `SWITCH_MODEL`: project quality is persistently weak in a comparable cohort, route reliability is not the cause, and at least one alternative has materially stronger project evidence. External benchmark alone is insufficient to trigger this action.
- `INSUFFICIENT_EVIDENCE`: sample count, evidence coverage, or benchmark protocol comparability is too weak for a safe recommendation.

Default material-difference thresholds are intentionally conservative and must be surfaced in report metadata rather than hidden. V1 uses:

- minimum 5 eligible model-quality samples for a high-confidence switch recommendation;
- minimum 3 comparable samples for provisional role/effort guidance;
- at least 8 Project Model Score points or 15 percentage points of first-pass/rework delta to call a project-performance difference material;
- route reliability below 75 with at least 3 eligible route attempts blocks `SWITCH_MODEL` and prefers `CHANGE_ROUTE` when project-quality evidence is otherwise healthy;
- `result_unknown` and infrastructure-only samples never count as model-quality evidence.

### External baseline comparison

The dashboard shows the external baseline beside the project score, but does not subtract the two raw numbers as if they were on the same measurement scale. Instead it derives a qualitative comparison:

```text
ABOVE_EXPECTATION
IN_LINE
BELOW_EXPECTATION
NOT_COMPARABLE
```

The comparison requires at least a partial ModelDial match and sufficient project evidence. Exact match requires exact model + effort + route semantics. A partial match is displayed as context with an explicit protocol warning.

When project evidence is weak, the benchmark is context only. When project evidence is strong, project evidence has decision priority for the user's route/task cohort.

## Dashboard

V1 ships a self-contained read-only HTML dashboard generated from one JSON report. It must not depend on a web service, package install, or third-party JavaScript CDN.

Visual direction: a bright editorial sci-fi command center inspired by the user-provided reference image. Reproduce the visual language—not copyrighted assets or brand identity—with:

- warm-white / very-light-gray canvas rather than a dark BI background;
- deep navy editorial typography with very large statement headlines and generous negative space;
- a cobalt / electric-blue / violet central “planet / energy field” motif created with CSS gradients and SVG, used as the visual anchor rather than decorative stock imagery;
- hairline rules, small uppercase navigation/metadata, restrained borders and almost no conventional boxed-dashboard chrome;
- score comparisons rendered as orbital/radial lines, thin bars and clean SVG plots that feel integrated into the composition;
- cobalt, violet and small amber accents for model quality, reliability and decision states while keeping the page predominantly white;
- responsive desktop-first layout with a calm narrow-screen fallback.

The dashboard includes at minimum:

1. global summary: model configurations observed, scored samples, infrastructure failures excluded, current degraded/open routes;
2. model comparison chart: Project Model Score vs Route Reliability vs external benchmark when comparable;
3. role/task matrix: writer/reviewer/controller and frontend/backend/general cohorts;
4. effort comparison for models with multiple observed effort levels;
5. decision cards with action, confidence, reasons and evidence counts;
6. failure attribution chart separating model / infrastructure / external / mixed / unknown;
7. route health section for external providers;
8. sample/evidence explorer table so recommendations are auditable.

The HTML is generated by Runtime from a normalized report JSON. No model recommendation may exist only in presentation JavaScript; all decisions are produced by Python and rendered verbatim.

## Dashboard CLI

```bash
python3 scripts/project_model_score.py report --repo <repo> --window-days 30 --json <path>
python3 scripts/project_model_score.py dashboard --repo <repo> --window-days 30 --output <path.html>
```

`dashboard` internally calls the same report builder used by `report`; the two surfaces cannot drift.

## Decision/dashboard acceptance

1. All models present in canonical evidence, including Kimi K3, are discoverable without a hard-coded model population.
2. A high-quality model with a degraded route receives `CHANGE_ROUTE`, not `SWITCH_MODEL`.
3. An infrastructure failure does not reduce Project Model Score.
4. A semantically weak model on a healthy route can receive `SWITCH_MODEL` only with sufficient comparable samples and a stronger observed alternative.
5. Low sample counts always return `INSUFFICIENT_EVIDENCE` for switch decisions.
6. ModelDial values remain sidecar/context and protocol mismatches are visible.
7. Dashboard HTML is self-contained, opens without network access, and displays model comparison, route reliability, external baseline, attribution, route health and decisions.
8. Dashboard recommendation text is generated from structured decision output, not duplicated UI heuristics.
