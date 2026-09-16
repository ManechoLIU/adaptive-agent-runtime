# Global Model Intelligence Design

**Date:** 2026-09-16

## Goal

Upgrade Project Model Score into a cross-project model-intelligence layer that answers:

1. How does each model actually perform across the user's real projects?
2. How does that real-world performance compare with external baselines such as ModelDial?
3. Is a weak result caused by model quality, route/provider reliability, or insufficient evidence?
4. Should the Runtime keep the model, tune reasoning effort, repair/change route, change role, or consider a different model?

The system remains advisory in this phase. It does not autonomously change model routing.

## Scope

The global layer must include every model configuration observed in trustworthy Runtime evidence across participating repositories, including GPT-5.6 Sol/Terra/Luna, Grok 4.6, Kimi K3, and future models without hard-coding a population list.

The dashboard exposes two views over the same evidence:

- **Global view:** cross-project performance for each model / effort / role / task class.
- **Project view:** the existing project-scoped score and recommendations.

Project-local truth remains intact. Global aggregation is derived analytics only.

## Non-goals

- No second project ledger, scheduler, Controller registry, Assignment store, or lifecycle database.
- No filesystem-wide home-directory scan.
- No automatic route/model switching in this phase.
- No inference from chat memory or free-form impressions.
- No merging infrastructure failures into model-quality scores.
- No cross-project averaging that hides role, effort, task class, or route differences.

## Repository discovery

Global aggregation discovers repositories in three bounded ways:

1. **Existing Controller registry**: read registered canonical repository roots already known to Runtime governance.
2. **Explicit CLI roots**: zero or more `--repo` arguments allow inclusion of Runtime-enabled repositories that are not in the Controller registry.
3. **Git common-dir deduplication**: worktrees that share one Git common-dir are one project evidence source, not multiple projects.

The collector never recursively scans the user's home directory.

A repository is eligible only if it is a valid Git repository and has Runtime model evidence under its Git common-dir. Missing/unreadable repositories are reported as diagnostics and skipped, not converted to negative model evidence.

## Architecture

```text
Project A Runtime evidence ─┐
Project B Runtime evidence ─┼─> Global Evidence Collector
Project C Runtime evidence ─┘          │
                                      v
                           Normalized Evidence Samples
                                      │
                 ┌────────────────────┼─────────────────────┐
                 v                    v                     v
          Project Scores       Route Reliability      ModelDial Sidecar
                 └────────────────────┼─────────────────────┘
                                      v
                         Global Model Intelligence
                                      │
                 ┌────────────────────┴─────────────────────┐
                 v                                          v
          Global Dashboard                          Decision Engine
       (global / project views)              (KEEP/TUNE/ROUTE/ROLE/SWITCH)
```

## Evidence model

The existing normalized sample schema remains the atomic unit. Each sample retains:

```text
project_id
project_root
project_common_dir
provider
model
auth_mode
reasoning_effort
execution_role
policy_class
execution_transport
assignment_id
task_id
terminal_at
transport_outcome
delivery_outcome
review_verdict
attribution
traceable evidence/artifacts
```

`project_id` is derived from canonical Git identity / common-dir context, not display-name guessing.

Global aggregation never discards the originating project.

## Cross-project grouping

The global layer exposes several distinct groupings rather than one opaque model number.

### 1. Model family summary

Example:

```text
grok-4.6
  projects_observed = 4
  quality_samples = 27
  project_quality_summary = ...
  route_reliability_summary = ...
  preferred_roles = [reviewer, backend_writer]
```

This is a presentation summary, not the unit used for route decisions.

### 2. Comparable configuration cohort

Decision-grade comparisons require:

```text
model + reasoning_effort + execution_role + policy_class
```

and keep route identity visible:

```text
provider + auth_mode + execution_transport
```

A Grok reviewer High sample is not silently averaged with a Grok writer Medium sample.

### 3. Per-project breakdown

Every global group publishes project contributions so a single project cannot silently dominate the result.

## Global score semantics

### Global Project Performance

The global performance score is a weighted aggregation of **project-quality samples only**, using the existing Project Model Score dimensions. Infrastructure/external/unknown samples stay excluded from model quality.

Weighting rules:

1. Compute quality from normalized eligible samples, not from already-rounded project scores.
2. Cap per-project contribution so one high-volume project cannot overwhelm all other projects.
3. Preserve sample count and project count.
4. Publish confidence separately; confidence is never a hidden score multiplier.
5. If comparable quality evidence is insufficient, publish `待积累`, not `0`.

### Global Route Reliability

Route reliability remains route-specific. A global model summary may show several routes, for example:

```text
grok-4.6 / oauth / external_process
kimi-k3 / api / external_process
```

The dashboard may summarize route status, but decision logic uses the exact route group.

Historical failures before a known route fix remain visible as historical evidence. When Provider Health reports `PROBE_REQUIRED`, the UI must say `修复后待复测` rather than imply current model quality is zero.

## External baseline comparison

ModelDial stays a sidecar benchmark.

Comparison precedence:

1. Exact model + effort + route semantics when available.
2. Partial same-model + effort + capability-axis context, clearly labeled partial.
3. No benchmark if effort is unknown or no defensible match exists.

Capability-axis alignment follows task class:

- backend -> ModelDial backend
- frontend -> ModelDial frontend
- general/research/reviewer without narrower mapping -> overall or reasoning only when the benchmark schema supports the mapping

External baseline never directly causes `SWITCH_MODEL`.

## Decision engine

The global decision engine answers both configuration-level and role-level questions.

Allowed actions remain:

```text
KEEP
TUNE_EFFORT
CHANGE_ROUTE
CHANGE_ROLE
SWITCH_MODEL
INSUFFICIENT_EVIDENCE
```

Decision order:

1. **Evidence gate**: insufficient quality evidence -> `INSUFFICIENT_EVIDENCE`.
2. **Route gate**: degraded/open/probe-required route with otherwise promising model evidence -> `CHANGE_ROUTE` or `修复后待复测`, never `SWITCH_MODEL`.
3. **Role fit**: model weak in one role but strong in another -> `CHANGE_ROLE`.
4. **Effort efficiency**: comparable lower effort with similar quality and materially lower latency -> `TUNE_EFFORT`.
5. **Switch evidence**: only consider `SWITCH_MODEL` when the current route is healthy, sample confidence is sufficient, and a comparable observed alternative is materially stronger across quality / rework / verification evidence.
6. ModelDial may support explanation but cannot be the sole switch trigger.

## Global dashboard

The current Chinese dark-purple neon dashboard becomes the visual surface for both scopes.

### Top controls

```text
[全局视角] [当前项目]
项目筛选: 全部 / SelfAlone / LAB / Adaptive Agent Runtime / ...
时间窗口: 30天 / 90天 / 全部
```

### Main content

Keep the dashboard concise. Default sections:

1. **模型表现对比** — one card per model, no duplicate config noise.
   - 全局实战 / 当前项目实战
   - ModelDial 外部基线
   - 执行稳定性
   - 样本数 / 项目数
   - 当前建议
2. **能力对比图** — concise bars for project quality, external baseline, route reliability.
3. **最佳岗位** — Controller / backend / frontend / reviewer / research where evidence supports it.
4. **智能建议** — only actionable recommendations and short reasons.

Detailed raw evidence stays in JSON/report output and is not shown by default in the dashboard.

### Missing-score language

Never render missing model-quality evidence as `0`.

Use:

- `待积累` — no sufficient quality evidence.
- `待复测` — route was repaired and Provider Health requires a real probe.
- `样本不足` — evidence exists but confidence threshold is not met.
- numeric `0.0` only when the metric itself is validly measured as zero.

## Derived global cache

Optional derived cache lives outside project business state:

```text
~/.adaptive-delivery/model-intelligence/
  latest.json
  snapshots/
```

This cache is disposable and fully recomputable from project Runtime evidence. It has no scheduling, ownership, lifecycle, or task authority.

No project reads this cache as authoritative input for business state.

## CLI

Proposed commands:

```bash
python3 scripts/global_model_intelligence.py report \
  --registry <controller-registry> \
  --repo <extra-repo> ... \
  --window-days 90 \
  --benchmark-json <modeldial.json> \
  --json <output.json>

python3 scripts/global_model_intelligence.py dashboard \
  --registry <controller-registry> \
  --repo <extra-repo> ... \
  --window-days 90 \
  --benchmark-json <modeldial.json> \
  --output <dashboard.html>
```

`--registry` may be omitted when the existing Runtime default registry is available. Repeated `--repo` is additive.

## Error handling

Fail closed on malformed evidence for that sample, not on the entire global report.

- invalid repository -> diagnostic, skip repo;
- duplicate worktree/common-dir -> dedupe;
- malformed sample -> exclude with diagnostic;
- unknown model/effort -> keep visible, restrict comparisons;
- ModelDial unavailable -> render project/global evidence without external baseline;
- one project unreadable -> report partial coverage and continue with other valid projects.

The report publishes `coverage` with discovered, included, skipped, and errored projects.

## Testing

### Unit tests

- registry discovery returns canonical repo roots only;
- explicit repos are additive;
- common-dir duplicates are counted once;
- all models are dynamically discovered;
- project origin survives normalization;
- infrastructure failures never reduce global model-quality score;
- per-project contribution cap prevents one project from dominating;
- missing quality evidence renders `待积累`, not zero;
- `PROBE_REQUIRED` renders `待复测`;
- ModelDial route/effort mismatch remains partial/none;
- decision engine does not switch on benchmark alone;
- role and effort recommendations use comparable cohorts only.

### Integration tests

Build at least three temporary repos with canonical Runtime evidence:

- Sol quality success in two projects;
- Grok historical route failures in one project plus successful quality evidence in another;
- Kimi frontend success in multiple projects.

Verify global report shows cross-project Grok capability while SelfAlone project view still shows its local `待积累 / 待复测` state.

### Dashboard tests

- Chinese-only user-facing copy except model/provider identifiers;
- dark purple / pink neon reference style retained;
- one card per model in global summary;
- global/project scope controls rendered;
- no raw evidence table by default;
- missing scores use semantic labels, not misleading numeric zero.

## Rollout

1. Add global collector/report without changing existing project scoring.
2. Add global dashboard scope and project filtering.
3. Validate against real SelfAlone + Adaptive Agent Runtime + LAB evidence.
4. Compare Grok/Kimi/Sol cross-project results manually for attribution sanity.
5. Keep recommendations advisory until enough real samples accumulate.
6. Only after a later explicit design may Runtime consume these recommendations for automatic routing.

## Acceptance criteria

The feature is complete when:

- Grok evidence from other projects appears in the global view even when SelfAlone lacks quality samples;
- SelfAlone project view still correctly shows local evidence scarcity;
- Sol, Terra, Luna, Grok, Kimi, and future observed models are dynamically included;
- project quality, route reliability, and ModelDial remain separate visible metrics;
- the UI distinguishes `待积累`, `待复测`, and true numeric zero;
- recommendations explain whether the issue is model quality, role fit, effort, or execution route;
- no project ledger/controller/lifecycle state is mutated by global analytics.
