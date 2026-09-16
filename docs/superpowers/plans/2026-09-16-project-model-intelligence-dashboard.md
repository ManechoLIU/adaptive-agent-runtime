# Project Model Intelligence Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only Runtime subsystem that scores every model actually used in a project, separates model quality from route reliability, compares project evidence with optional external benchmark context, generates advisory routing decisions, and renders a self-contained space-themed HTML dashboard.

**Architecture:** `scripts/project_model_score.py` owns evidence normalization, attribution, scoring, decision output, JSON serialization and CLI. `scripts/model_score_dashboard.py` owns presentation-only HTML/SVG rendering from normalized report JSON and contains no decision rules. Existing `runtime-assignments.json` and reviewer evidence remain authoritative inputs; no new project ledger is created.

**Tech Stack:** Python 3 standard library only; HTML/CSS/SVG/vanilla JavaScript embedded in generated output; `unittest` for tests.

**Spec:** `docs/superpowers/specs/2026-09-16-project-model-score-design.md`

## Global Constraints

- Discover models dynamically from canonical Runtime evidence; do not hard-code Sol/Grok/Kimi as the scoring population.
- Project Model Score and Route Reliability Score remain separate outputs.
- Infrastructure/external/result-unknown failures never become model-quality failures by default.
- ModelDial is an optional benchmark sidecar and never changes Project Model Score.
- Decision engine is advisory only and cannot mutate route policy or Assignment state.
- Dashboard is self-contained and has no CDN/network dependency.
- Recommendation logic lives in Python report generation, not browser JavaScript.

---

### Task 1: Evidence normalization and dynamic model discovery

**Files:**
- Create: `scripts/project_model_score.py`
- Create: `tests/test_project_model_score.py`

**Interfaces:**
- Produces: `load_project_samples(repo: Path, *, window_days: int | None = None, now: datetime | None = None) -> list[dict]`
- Produces: `normalize_lease_sample(project: str, assignment_id: str, lease: dict) -> dict | None`
- Produces: `parse_reasoning_effort(lease: dict) -> str`

- [ ] **Step 1: Write failing tests** proving model identities are discovered from canonical leases, Kimi aliases remain evidence-driven, reasoning effort is parsed from frozen `strategy`, and missing model identity is excluded from model scoring.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_project_model_score.ProjectModelSampleTests -v` and confirm failures are due to missing module/functions.
- [ ] **Step 3: Implement minimal normalizer** using `assignment_runtime.runtime_state_path/load_runtime_state`, terminal immutable leases, exact route fields, ISO timestamps and strategy parsing.
- [ ] **Step 4: Re-run the focused tests** to GREEN.
- [ ] **Step 5: Commit** `feat(runtime): normalize project model evidence`.

### Task 2: Attribution, Project Model Score and Route Reliability

**Files:**
- Modify: `scripts/project_model_score.py`
- Modify: `tests/test_project_model_score.py`

**Interfaces:**
- Produces: `classify_attribution(sample: dict) -> tuple[str, list[str]]`
- Produces: `score_model_groups(samples: list[dict]) -> list[dict]`
- Produces: `score_route_groups(samples: list[dict]) -> list[dict]`

- [ ] **Step 1: Write failing tests** covering semantic FAIL on healthy transport → model attribution; provider timeout/first-output/stall/cleanup/result_unknown → infrastructure; quota/service/auth outage → external; ambiguous evidence → unknown; infrastructure failures excluded from model-quality denominator.
- [ ] **Step 2: Run** the attribution/scoring test classes and verify RED for missing scoring behavior.
- [ ] **Step 3: Implement deterministic attribution and scoring.** Delivery, reviewer quality, verification, comparable efficiency and rework dimensions expose coverage; unsupported dimensions remain unavailable. Route reliability uses eligible transport attempts and returns score + counts, never modifies Project Model Score.
- [ ] **Step 4: Re-run tests** and verify all scoring cases GREEN.
- [ ] **Step 5: Commit** `feat(runtime): score model quality and route reliability`.

### Task 3: Decision engine and benchmark sidecar

**Files:**
- Modify: `scripts/project_model_score.py`
- Modify: `tests/test_project_model_score.py`

**Interfaces:**
- Produces: `build_decisions(model_groups: list[dict], route_groups: list[dict], benchmarks: list[dict] | None = None) -> list[dict]`
- Produces: `build_report(repo: Path, *, window_days: int = 30, benchmarks: list[dict] | None = None, now: datetime | None = None) -> dict`

- [ ] **Step 1: Write failing tests** for `KEEP`, `CHANGE_ROUTE`, `CHANGE_ROLE`, `TUNE_EFFORT`, `SWITCH_MODEL`, and `INSUFFICIENT_EVIDENCE`; specifically prove degraded route blocks a switch verdict and low sample size cannot trigger switch.
- [ ] **Step 2: Add benchmark tests** proving exact/partial/none matching is surfaced and external score never changes Project Model Score.
- [ ] **Step 3: Run focused decision tests** and verify RED.
- [ ] **Step 4: Implement conservative deterministic decision rules** using the thresholds in the spec and structured reason codes. Keep benchmark comparison qualitative (`ABOVE_EXPECTATION`, `IN_LINE`, `BELOW_EXPECTATION`, `NOT_COMPARABLE`).
- [ ] **Step 5: Re-run tests** and verify GREEN.
- [ ] **Step 6: Commit** `feat(runtime): add model routing decision engine`.

### Task 4: JSON/CLI report surface

**Files:**
- Modify: `scripts/project_model_score.py`
- Modify: `tests/test_project_model_score.py`

**Interfaces:**
- CLI: `python3 scripts/project_model_score.py report --repo <repo> --window-days 30 [--benchmark-json <path>] [--json <path>]`

- [ ] **Step 1: Write failing CLI tests** using a temporary Git repo/common-dir fixture with canonical runtime state, asserting machine-readable output and deterministic ordering.
- [ ] **Step 2: Verify RED.**
- [ ] **Step 3: Implement argparse CLI** with stdout JSON by default and optional atomic JSON file output.
- [ ] **Step 4: Verify focused CLI tests GREEN.**
- [ ] **Step 5: Commit** `feat(runtime): expose project model report cli`.

### Task 5: Self-contained HTML dashboard renderer

**Files:**
- Create: `scripts/model_score_dashboard.py`
- Create: `tests/test_model_score_dashboard.py`
- Modify: `scripts/project_model_score.py`

**Interfaces:**
- Produces: `render_dashboard(report: dict) -> str`
- CLI: `python3 scripts/project_model_score.py dashboard --repo <repo> --window-days 30 --output <path.html> [--benchmark-json <path>]`

- [ ] **Step 1: Write failing renderer tests** asserting doctype, no remote `http(s)` assets/scripts, embedded normalized report data, presence of overview/model comparison/decision/attribution/route-health/evidence sections, SVG chart markup, and escaped untrusted strings.
- [ ] **Step 2: Verify RED.**
- [ ] **Step 3: Implement renderer** with bright editorial sci-fi CSS matching the supplied reference: warm-white canvas, deep navy type, cobalt/violet planet motif, hairline structure, generous whitespace, responsive grid, and embedded SVG comparisons, accessible labels, and a compact evidence table. Use only report decisions; browser JS may filter/sort but may not derive recommendations.
- [ ] **Step 4: Add CLI dashboard output** through the report builder and atomic HTML write.
- [ ] **Step 5: Re-run renderer/CLI tests** to GREEN.
- [ ] **Step 6: Commit** `feat(runtime): add model intelligence dashboard`.

### Task 6: Real-project read-only smoke report

**Files:**
- No production source changes unless a proven bug is found through a new RED test.

**Interfaces:**
- Consumes: SelfAlone and Local-Agent-Bridge canonical Runtime evidence.

- [ ] **Step 1: Run read-only report** against `/Users/echoman/Documents/SelfAlone` and verify dynamically observed models include all canonical identities with enough evidence, including Grok and any Kimi/GPT routes actually present.
- [ ] **Step 2: Generate dashboard HTML** into a temporary output path and open it locally for visual inspection.
- [ ] **Step 3: Confirm recommendations distinguish model-quality vs infrastructure failures and expose sample confidence.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_project_model_score tests.test_model_score_dashboard tests.test_assignment_runtime tests.test_controller_scoring_model -q` and `git diff --check`.
- [ ] **Step 5: Commit any test-driven corrections** with a scoped commit; otherwise leave source unchanged.

