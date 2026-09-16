# Global Model Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only cross-project model intelligence layer that aggregates trustworthy Runtime evidence from registered and explicitly supplied repositories, compares global/project performance with ModelDial, and renders a concise Chinese global/project dashboard.

**Architecture:** Keep `project_model_score.py` as the project-local scoring engine. Add a focused `global_model_intelligence.py` collector that discovers repositories, deduplicates by Git common-dir, loads normalized project samples, computes capped cross-project model summaries and configuration cohorts, and emits a global report with embedded per-project reports. Reuse `model_score_dashboard.py` for both project and global scopes; raw evidence stays in JSON only.

**Tech Stack:** Python 3.11 standard library, existing Adaptive Agent Runtime JSON/Git state helpers, `unittest`, self-contained HTML/CSS.

**Spec:** `docs/superpowers/specs/2026-09-16-global-model-intelligence-design.md`

## Global Constraints

- No second project ledger, scheduler, Controller registry, Assignment store, or lifecycle database.
- No filesystem-wide home-directory scan.
- No automatic route/model switching.
- Repository discovery is bounded to Controller registry entries plus explicit `--repo` roots, deduplicated by Git common-dir.
- Infrastructure/external/unknown outcomes never reduce model-quality scores.
- Missing model-quality evidence renders `待积累`, never `0`.
- Provider Health `PROBE_REQUIRED` renders `修复后待复测`, not a current zero-quality implication.
- Decision-grade comparisons preserve model + effort + role + task class and keep route identity visible.
- ModelDial is advisory sidecar evidence and cannot solely trigger `SWITCH_MODEL`.

---

### Task 1: Repository Discovery and Cross-Project Sample Collection

**Files:**
- Create: `scripts/global_model_intelligence.py`
- Create: `tests/test_global_model_intelligence.py`

**Interfaces:**
- Consumes: `scripts.project_state.repository_root`, `scripts.project_state.git_common_dir`, `scripts.project_model_score.load_project_samples`, Controller registry JSON at `~/.codex/adaptive-delivery-controllers.json`.
- Produces: `discover_repositories(registry_path: Path, explicit_repos: Sequence[Path]) -> tuple[list[dict[str, str]], list[dict[str, str]]]` and `collect_global_samples(repositories: Sequence[dict[str, str]], window_days: int | None, now: datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]`.

- [ ] **Step 1: Write failing discovery tests**

Add tests proving that registry string-valued repo roots and explicit repos are included, non-repository metadata keys are ignored, worktrees sharing one Git common-dir deduplicate, and invalid/missing repos become diagnostics instead of model failures.

- [ ] **Step 2: Run discovery tests and verify RED**

Run: `python3 -m unittest -v tests.test_global_model_intelligence.GlobalRepositoryDiscoveryTests`
Expected: FAIL because `scripts.global_model_intelligence` does not exist.

- [ ] **Step 3: Implement bounded repository discovery**

Implement `discover_repositories` using Git root/common-dir canonicalization. Return repository rows containing `project_id`, `project_root`, `project_common_dir`, and `source`, plus diagnostics containing `repo`, `source`, and `error`.

- [ ] **Step 4: Write failing sample collection tests**

Create two temporary Git repositories with Runtime state fixtures. Assert `collect_global_samples` preserves `identity.project`, adds `project_id`, `project_root`, and `project_common_dir`, and never merges identical assignment IDs from different projects.

- [ ] **Step 5: Implement cross-project collection and pass tests**

Run: `python3 -m unittest -v tests.test_global_model_intelligence.GlobalRepositoryDiscoveryTests tests.test_global_model_intelligence.GlobalSampleCollectionTests`
Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add scripts/global_model_intelligence.py tests/test_global_model_intelligence.py
git commit -m "feat(runtime): collect cross-project model evidence"
```

### Task 2: Global Model Summaries, Per-Project Caps, and Configuration Cohorts

**Files:**
- Modify: `scripts/global_model_intelligence.py`
- Modify: `tests/test_global_model_intelligence.py`

**Interfaces:**
- Consumes: normalized samples from Task 1; `score_model_groups`, `score_route_groups`, `attach_benchmarks`, and `build_decisions` from `project_model_score.py`.
- Produces: `build_global_report(repositories, window_days, benchmarks, now=None) -> dict[str, Any]` with `scope="global"`, `projects`, `model_summaries`, `configuration_groups`, `route_groups`, `decisions`, and `project_reports`.

- [ ] **Step 1: Write failing model-summary tests**

Test that one model used in two projects appears once in `model_summaries`, preserves `projects_observed`, `quality_sample_count`, and `project_breakdown`, and that a high-volume project is capped so it cannot silently dominate another project's quality evidence.

- [ ] **Step 2: Run model-summary tests and verify RED**

Run: `python3 -m unittest -v tests.test_global_model_intelligence.GlobalModelSummaryTests`
Expected: FAIL because global summary functions are missing.

- [ ] **Step 3: Implement capped model-family summaries**

For each canonical model, classify samples with existing attribution rules. Build a per-project quality sample set; cap each project's contribution at the median non-zero project quality-sample count for that model, with a minimum cap of 1. Re-score the capped merged samples using existing Project Model Score dimensions by cloning samples with `identity.project="GLOBAL"`. Publish `project_model_score`, coverage, project/sample counts, confidence, and `project_breakdown`.

- [ ] **Step 4: Write failing configuration-cohort tests**

Assert global configuration groups combine the same `model + reasoning_effort + execution_role + policy_class + provider + auth_mode + execution_transport` across projects but keep distinct efforts/roles/routes separate.

- [ ] **Step 5: Implement configuration cohorts and route groups**

Build cloned global samples with `identity.project="GLOBAL"`, call existing group scorers, restore per-group `projects_observed` and `project_breakdown`, and keep route reliability route-specific across projects.

- [ ] **Step 6: Write and pass global report schema tests**

Run: `python3 -m unittest -v tests.test_global_model_intelligence.GlobalModelSummaryTests tests.test_global_model_intelligence.GlobalConfigurationCohortTests tests.test_global_model_intelligence.GlobalReportTests`
Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add scripts/global_model_intelligence.py tests/test_global_model_intelligence.py
git commit -m "feat(runtime): aggregate global model intelligence"
```

### Task 3: Global Decision Semantics and Provider-Health Status

**Files:**
- Modify: `scripts/global_model_intelligence.py`
- Modify: `tests/test_global_model_intelligence.py`

**Interfaces:**
- Consumes: configuration groups, route groups, ModelDial benchmarks, and `provider_health.derive_route_health`.
- Produces: configuration decisions plus model-family `recommended_action`, `recommended_reason`, `preferred_roles`, and `route_status`.

- [ ] **Step 1: Write failing global-decision tests**

Cover: insufficient global quality -> `INSUFFICIENT_EVIDENCE`; strong quality + degraded route -> `CHANGE_ROUTE`; `PROBE_REQUIRED` -> user-facing state `修复后待复测`; strong reviewer but weak writer -> `CHANGE_ROLE`; healthy weak model with stronger comparable observed alternative -> `SWITCH_MODEL`; ModelDial alone never triggers switch.

- [ ] **Step 2: Run decision tests and verify RED**

Run: `python3 -m unittest -v tests.test_global_model_intelligence.GlobalDecisionTests`
Expected: FAIL until global recommendation projection exists.

- [ ] **Step 3: Implement global recommendation projection**

Reuse configuration-level `build_decisions`, then summarize per canonical model conservatively: route actions outrank switch actions when route health is degraded/probe-required; role recommendations require at least 3 quality samples in the stronger role; model-family switch requires a healthy route and sufficient comparable configuration evidence.

- [ ] **Step 4: Integrate Provider Health without changing dispatch policy**

Call `derive_route_health` only for diagnostics. Map `PROBE_REQUIRED` to `修复后待复测`, `OPEN` to `执行链已熔断`, `DEGRADED` to `执行链需优化`, and `HEALTHY` to `稳定`.

- [ ] **Step 5: Run global-decision tests**

Run: `python3 -m unittest -v tests.test_global_model_intelligence.GlobalDecisionTests`
Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add scripts/global_model_intelligence.py tests/test_global_model_intelligence.py
git commit -m "feat(runtime): add global model recommendations"
```

### Task 4: Global CLI and Derived Cache

**Files:**
- Modify: `scripts/global_model_intelligence.py`
- Modify: `scripts/install_skill.py`
- Modify: `tests/test_global_model_intelligence.py`
- Modify: `tests/test_install_skill.py`

**Interfaces:**
- Produces CLI commands:
  - `python3 scripts/global_model_intelligence.py report [--registry PATH] [--repo PATH ...] [--window-days N] [--benchmark-json PATH] [--json PATH]`
  - `python3 scripts/global_model_intelligence.py dashboard [same discovery args] --output PATH`
- Derived cache: `~/.codex/adaptive-delivery/model-intelligence/latest.json` plus timestamped snapshots; cache is recomputable and non-authoritative.

- [ ] **Step 1: Write failing CLI tests**

Test explicit repo inclusion, registry discovery, JSON output, direct-script imports, invalid repo diagnostics, and deterministic model/project ordering.

- [ ] **Step 2: Implement CLI and atomic derived-cache writes**

Reuse the existing atomic text-write pattern. Cache only derived global report JSON; never write project Assignment/Ledger state.

- [ ] **Step 3: Add release/install dependency tests**

Require `scripts/global_model_intelligence.py` and `tests/test_global_model_intelligence.py` in Runtime release files and regression gates.

- [ ] **Step 4: Run CLI/release tests**

Run: `python3 -m unittest -v tests.test_global_model_intelligence.GlobalCliTests tests.test_install_skill.InstallMigrationContractTests.test_runtime_release_gate_includes_host_ownership_and_yield_enforcement_regressions`
Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add scripts/global_model_intelligence.py scripts/install_skill.py tests/test_global_model_intelligence.py tests/test_install_skill.py
git commit -m "feat(runtime): expose global model intelligence cli"
```

### Task 5: Concise Chinese Global/Project Dashboard

**Files:**
- Modify: `scripts/model_score_dashboard.py`
- Modify: `tests/test_model_score_dashboard.py`
- Modify: `scripts/global_model_intelligence.py`

**Interfaces:**
- `render_dashboard(report: dict[str, Any]) -> str` accepts both `scope="project"` and `scope="global"` reports.
- Global HTML uses one card per model and embeds project breakdown in compact chips/labels, not raw evidence tables.

- [ ] **Step 1: Write failing global-dashboard tests**

Require Chinese controls `全局视角` / `当前项目`, concise sections `模型表现对比`, `能力对比`, `最佳岗位`, `智能建议`, one card per canonical model, project count/sample count, and absence of raw evidence tables.

- [ ] **Step 2: Add missing-score semantic tests**

Assert model-quality `None` -> `待积累`; Provider Health `PROBE_REQUIRED` -> `修复后待复测`; insufficient project scope does not display `0` as model quality.

- [ ] **Step 3: Implement global dashboard projection**

Keep the approved dark-purple/pink neon reference language. In global scope show global score, ModelDial baseline, route status, projects/sample counts, preferred role, and concise action. In project scope preserve current concise cards.

- [ ] **Step 4: Run dashboard tests**

Run: `python3 -m unittest -v tests.test_model_score_dashboard tests.test_global_model_intelligence.GlobalDashboardCliTests`
Expected: PASS.

- [ ] **Step 5: Generate a real global dashboard**

Use registry-discovered SelfAlone/LAB plus explicit Adaptive Agent Runtime repo and the existing ModelDial benchmark JSON. Generate `/tmp/model-intelligence/global-model-dashboard.html` and `/tmp/model-intelligence/global-model-report.json`.

- [ ] **Step 6: Commit Task 5**

```bash
git add scripts/model_score_dashboard.py scripts/global_model_intelligence.py tests/test_model_score_dashboard.py tests/test_global_model_intelligence.py
git commit -m "feat(runtime): add global model intelligence dashboard"
```

### Task 6: Full Verification and Real-Data Acceptance

**Files:**
- Modify only if verification exposes a real regression.

**Interfaces:**
- Acceptance evidence is fresh test output plus real global report/dashboard artifacts.

- [ ] **Step 1: Run model-intelligence Python suite**

Run: `python3 -m unittest -v tests.test_project_model_score tests.test_model_score_dashboard tests.test_provider_health tests.test_global_model_intelligence`
Expected: all PASS.

- [ ] **Step 2: Run external routing regression**

Run: `node --test tests/external-agent-routing.test.mjs`
Expected: 117 tests PASS, 0 FAIL.

- [ ] **Step 3: Run focused release/install gate**

Run the Runtime release tests that verify required files and immutable-revision regression execution.
Expected: PASS.

- [ ] **Step 4: Run static Git checks**

Run: `git diff --check && git status --short`
Expected: no whitespace errors; only intended changes before final commit, then clean after commit.

- [ ] **Step 5: Inspect real global Grok/Kimi/Sol output**

Verify Grok can have a global project-performance score from other projects even when SelfAlone remains `待积累`; verify SelfAlone route history is visible separately; verify Kimi K3 and GPT models appear if canonical evidence exists.

- [ ] **Step 6: Final commit if verification required fixes**

Commit only verified fixes with a focused message.
