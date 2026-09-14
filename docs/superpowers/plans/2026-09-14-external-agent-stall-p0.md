# External Agent / Grok Stall P0 Implementation Plan

**Goal:** Make external-agent execution bounded, diagnosable, and safe against paid-call duplication while preserving existing CLI and receipt compatibility.

**Base:** `00bb19d599f8b2a1970901d0e006d444b48d1684`

**Spec authority:** The user requirements recorded in SelfAlone `TASK_LEDGER.md` task `RUNTIME-EXTERNAL-AGENT-STALL-P0`, plus the existing Runtime contracts in `references/external-agent-auth.md` and `scripts/run_external_agent.mjs`.

## Global Constraints

- Do not call Grok, Kimi, or any other paid/external model. Use deterministic fake child processes and injected clocks/spawn functions only.
- Preserve existing public CLI flags, existing durable receipt fields, existing Grok pure-packet reviewer schema, prompt-size fencing, process-group cleanup, and safe fallback behavior unless this plan explicitly tightens them.
- Treat external agents as advisory reviewers by default; their failure must not be encoded as proof that a mainline product task failed or as an automatic critical-path retry.
- Once `PROVIDER_STARTED` is crossed, automatic retry is forbidden by default. A retry may occur only with an explicit per-attempt authorization, must be a downgrade/transient retry, and has a hard maximum of one. Existing implicit reviewer retry must be removed or placed behind this gate.
- Prefer the existing private prompt file for Grok. The runner must not depend on an interactive PTY or an open stdin to signal prompt completion. Stdin/pipe input acquisition must be bounded and must finish before provider spawn.
- Timeouts are phase-relative: provider start uses the pre-start clock; first progress and heartbeat clocks start at `PROVIDER_STARTED`; model-generation clocks must not include process spawn/precheck time. An absolute provider deadline may remain as a separate bound but must also start at `PROVIDER_STARTED`.
- Every failure must retain legacy `failure_class` compatibility while also publishing one canonical P0 outcome code and phase history sufficient to identify where execution stopped.
- Do not broaden into provider credential, billing, controller lifecycle, UI, or product changes.

## Task 1: Implement the bounded execution protocol and regression suite

**Files owned by the implementer:**

- Modify: `scripts/run_external_agent.mjs`
- Create or modify, only if a focused protocol module materially reduces runner complexity: `scripts/external_agent_protocol.mjs`
- Modify: `tests/external-agent-routing.test.mjs`
- Create or modify, only if tests are clearer when isolated: `tests/external-agent-protocol.test.mjs`
- Modify: `references/external-agent-auth.md`

**Required protocol:**

1. Add the ordered execution states `PRECHECK → PACKET_VALIDATED → PROVIDER_STARTING → PROVIDER_STARTED → FIRST_PROGRESS → ANALYZING → FINAL_RESULT`. Illegal transitions fail closed and the ordered phase history is emitted in terminal/runtime evidence.
2. Add canonical outcome codes: `LOCAL_PRECHECK_FAILED`, `PROVIDER_START_TIMEOUT`, `FIRST_TOKEN_TIMEOUT`, `HEARTBEAT_TIMEOUT`, `RESULT_PARSE_FAILED`, `MODEL_CONTRADICTED_BY_LOCAL_EVIDENCE`, and `SUCCESS`. Preserve legacy failure classes as compatibility aliases/details rather than deleting them.
3. Separate the provider start timeout, first valid token/progress timeout, heartbeat/stall timeout, result parse failure, and absolute provider timeout. All post-start timers are anchored at the observed provider-start event, not the initial `spawn()` call or packet preparation.
4. Recognize structured progress/heartbeat only from validated provider events. Metadata, whitespace, malformed JSON, stderr noise, and empty placeholder records must not refresh progress. A validated final result transitions to `FINAL_RESULT`; malformed/missing required final output maps to `RESULT_PARSE_FAILED`.
5. Bound stdin/pipe acquisition before spawn and surface an unclosed input as `LOCAL_PRECHECK_FAILED`. Grok execution must continue to pass the bounded packet through the existing private `0600` prompt file and start the child with stdin closed/ignored. No PTY-based completion detection is allowed.
6. Add a small-hypothesis advisory review packet/result mode with structured per-hypothesis verdicts `supported | contradicted | insufficient`. It must reject open-ended root-cause prompts in that mode, require bounded hypotheses/evidence, and allow local evidence to mark a returned model conclusion as `MODEL_CONTRADICTED_BY_LOCAL_EVIDENCE` without upgrading it to a review finding.
7. Default every external result to advisory/non-critical-path metadata. Preserve explicit immutable code-review behavior where already contracted, but do not make provider liveness a prerequisite for unrelated mainline continuation.
8. Remove the current implicit Grok Reviewer two-attempt loop. Add an explicit authorization gate for at most one safe downgrade retry before model output; after provider start/model output or unknown cleanup, retry remains false. No test may make a real provider call.

**TDD acceptance cases:**

- local cwd/precheck failure produces `LOCAL_PRECHECK_FAILED` without spawning a provider;
- stdin/pipe never closes within the configured bound, provider is never spawned, and terminal code is `LOCAL_PRECHECK_FAILED`;
- spawn event never arrives and produces `PROVIDER_START_TIMEOUT`;
- provider starts but emits no valid model event and produces `FIRST_TOKEN_TIMEOUT` measured from provider start;
- provider emits valid first progress, then only noise/metadata, and produces `HEARTBEAT_TIMEOUT`;
- final output cannot be parsed/validated and produces `RESULT_PARSE_FAILED`;
- a valid normal run records all ordered states and `SUCCESS`;
- structured heartbeat/progress advances only on valid events;
- advisory hypothesis result supports all three verdict values and local-evidence contradiction maps to `MODEL_CONTRADICTED_BY_LOCAL_EVIDENCE`;
- no automatic retry occurs after provider boundary; explicit safe retry authorization permits at most one retry and never after model output/result-unknown;
- existing external-agent routing/reviewer tests remain compatible.

**Verification:**

- Capture a RED run that fails against exact base behavior for the new cases.
- Run focused Node tests for the owned test files to GREEN.
- Run the repository's complete Node/Python test command(s) once before commit, or report the precise established full-suite command and any unrelated failure with evidence.
- Run `git diff --check` and confirm no files outside ownership changed.
- Commit a single scoped candidate and write the SDD report with RED/GREEN evidence, changed files, root-cause findings, and remaining risk.

**Stop conditions:**

- Stop before any paid/external model invocation, credential access, install, push, main integration, or SelfAlone product edit.
- Stop and report `NEEDS_CONTEXT` if compatibility requires changing files outside the ownership list.
