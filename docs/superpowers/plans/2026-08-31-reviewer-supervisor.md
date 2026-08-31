# Reviewer Supervisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fragile background-shell Reviewer launching with a directly supervised, revision-bound, fail-closed Codex review execution path.

**Architecture:** Add one focused Python supervisor that launches `codex exec review` directly, consumes JSON events, persists ephemeral run evidence under the existing Git common-dir runtime root, validates a structured terminal verdict against the captured candidate HEAD, and retries infrastructure failure at most once. It does not create project task state or alter when review is required.

**Tech Stack:** Python 3 standard library (`subprocess`, `json`, `hashlib`, `pathlib`, `tempfile`), existing unittest suite, Codex CLI.

**Spec:** `docs/superpowers/specs/2026-08-31-reviewer-supervisor-design.md`

## Global Constraints

- Keep technical Skill ID `adaptive-delivery`, installed path, Git common-dir runtime root, controller registry, handshake, and Assignment runtime unchanged.
- Reviewer Supervisor states are execution evidence only: `STARTING`, `RUNNING`, `PASS`, `FINDINGS`, `REVIEW_INFRA_FAILED`.
- A PID or exit code alone never proves `RUNNING` or `PASS`.
- `reviewed_head` must exactly equal the immutable candidate HEAD captured before launch.
- Infrastructure failure retries at most once with the same immutable review contract; findings are never auto-retried.
- Do not add SQLite, a daemon, a queue, a second reviewer policy, or a second project lifecycle state machine.

---

### Task 1: Supervisor state and verdict core

**Files:**
- Create: `scripts/reviewer_supervisor.py`
- Create: `tests/test_reviewer_supervisor.py`

**Interfaces:**
- Produces: `git_common_state_root(repo: Path) -> Path`, `validate_verdict(payload: dict, expected_head: str) -> dict`, `atomic_write_json(path: Path, payload: dict) -> None`.
- State root: `<git-common-dir>/adaptive-delivery/reviewer-runs/`.

- [ ] **Step 1: Write failing tests for common-dir resolution and verdict validation**

Test linked-worktree common-dir resolution, valid PASS/FINDINGS payloads, missing fields, invalid severity arrays, and revision mismatch.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `python3 -m unittest tests.test_reviewer_supervisor -v`
Expected: FAIL because `scripts.reviewer_supervisor` does not exist.

- [ ] **Step 3: Implement the minimal state/verdict helpers**

Use `git rev-parse --git-common-dir`, canonicalize relative output, require `verdict in {PASS,FINDINGS}`, require `critical/important/minor` lists, and reject any `reviewed_head != expected_head`.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python3 -m unittest tests.test_reviewer_supervisor -v`
Expected: helper tests PASS.

- [ ] **Step 5: Commit**

`git add scripts/reviewer_supervisor.py tests/test_reviewer_supervisor.py && git commit -m "feat: add reviewer supervisor state contract"`

### Task 2: Direct Codex launch and observable startup

**Files:**
- Modify: `scripts/reviewer_supervisor.py`
- Modify: `tests/test_reviewer_supervisor.py`

**Interfaces:**
- Produces: `run_attempt(contract: ReviewContract, attempt: int, popen_factory=subprocess.Popen) -> AttemptResult`.
- Direct argv begins with resolved Codex executable and contains `exec`, global isolation flags, then `review`, `--base`, exact base, `--json`, and `-o` output path in positions accepted by the installed CLI.

- [ ] **Step 1: Write failing fake-process tests**

Assert there is no `sh -c`/`nohup`; `start_new_session=True`; candidate HEAD is captured before spawn; `STARTING` stays until a parseable Codex JSON event arrives; a child that exits before any valid event becomes infrastructure failure.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `python3 -m unittest tests.test_reviewer_supervisor.ReviewerSupervisorLaunchTests -v`
Expected: FAIL because launch supervision is absent.

- [ ] **Step 3: Implement direct process/event supervision**

Stream stdout JSONL, mirror bounded events to the run event log, mark `RUNNING` only after a valid JSON object is observed, capture any exposed thread/session id, and capture the direct child PID plus exit code.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the same focused command; expected PASS.

- [ ] **Step 5: Commit**

`git add scripts/reviewer_supervisor.py tests/test_reviewer_supervisor.py && git commit -m "feat: supervise codex reviewer launch"`

### Task 3: Terminal verdict, retry, and durable evidence

**Files:**
- Modify: `scripts/reviewer_supervisor.py`
- Modify: `tests/test_reviewer_supervisor.py`

**Interfaces:**
- Produces: `run_review(repo, base, instructions, max_infra_retries=1) -> ReviewRunResult` and CLI `run` command.
- Exit codes: `0=PASS`, `10=FINDINGS`, `20=REVIEW_INFRA_FAILED`.

- [ ] **Step 1: Write failing terminal/retry tests**

Cover valid PASS, FINDINGS without retry, exit 0 with missing/empty/malformed output, revision mismatch, first infrastructure failure followed by PASS using identical repo/base/head/instructions, and two infrastructure failures ending terminally.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `python3 -m unittest tests.test_reviewer_supervisor.ReviewerSupervisorRunTests -v`
Expected: FAIL before orchestration exists.

- [ ] **Step 3: Implement orchestration and atomic evidence**

Generate `run_id`, snapshot canonical repo/head/base/instructions fingerprint once, keep that contract immutable across retry, atomically persist state transitions, hash event/final outputs, bound diagnostic text, and map terminal states to the specified exit codes.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the same focused command; expected PASS.

- [ ] **Step 5: Commit**

`git add scripts/reviewer_supervisor.py tests/test_reviewer_supervisor.py && git commit -m "feat: fail closed reviewer supervision"`

### Task 4: Document machine path and run real regression

**Files:**
- Modify: `README.md`
- Modify: `references/agent-delivery-contract.md`
- Modify: `tests/test_reviewer_supervisor.py` only if documentation contract needs a structural assertion.

**Interfaces:**
- Required final review machine path: `python3 scripts/reviewer_supervisor.py run --repo <repo> --base <branch>`.
- Existing manual `codex exec review` remains supported for interactive/manual use.

- [ ] **Step 1: Add concise documentation**

Document that required machine review uses the supervisor; PID/exit-code-only evidence is invalid; infrastructure failure is distinct from findings and can retry once.

- [ ] **Step 2: Run Supervisor tests**

Run: `python3 -m unittest tests.test_reviewer_supervisor -v`
Expected: PASS.

- [ ] **Step 3: Run complete regression**

Run: `python3 -m unittest discover -s tests -v`
Expected: all Python tests PASS.

Run: `node --test tests/external-agent-routing.test.mjs`
Expected: all Node tests PASS.

Run: `git diff --check`
Expected: no output and exit 0.

- [ ] **Step 4: Exercise the supervisor against the exact feature HEAD**

Run the supervisor synchronously from the feature worktree against `main`. Confirm a durable reviewer-run state exists, the direct Codex execution emitted valid events, and the terminal result is either a trustworthy `PASS`/`FINDINGS` or a diagnosed `REVIEW_INFRA_FAILED` rather than silent disappearance.

- [ ] **Step 5: Commit documentation**

`git add README.md references/agent-delivery-contract.md && git commit -m "docs: require supervised machine review"`
