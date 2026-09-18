# Session Opening Working Set Design

**Date:** 2026-09-19

## Goal

Change Adaptive Agent Runtime SessionStart / fact-query injection so a new session
receives the **working set for this turn**, not the full task ledger and runtime
Skill body. The unique Markdown ledger remains the authority and stays readable on
demand. This is an opening-path reform, not a new product, command, or action card.

## Current baseline

`scripts/project_context_guard.py` already:

- loads AGENTS.md, project Skill, runtime Skill, ledger, Git, and controller identity;
- injects **full verified bodies** into `additionalContext` on every SessionStart;
- silently truncates each body at 96KB with `[TRUNCATED_BY_PROJECT_CONTEXT_GUARD]`;
- re-injects the same full dump on project-fact UserPromptSubmit, except when a
  mechanism file was resolved (the mechanism is appended **in addition to** the dump).

`scripts/ledger_access.py` already projects open work items in memory. The opening
hook does not use that projection as the model-visible working set.

## Non-goals

- Do not add SQLite, MCP, AWR session/claim, `prepare` CLI, or action cards.
- Do not replace AAR completion, four gates, unique Controller, or ledger CAS.
- Do not forbid reading `TASK_LEDGER.md` / `PROJECT_STATUS.md`.
- Do not bind a tokenizer or AWR 1000/5000 token budgets.
- Do not dump Compact occupancy protocols.

## Working-set selection

Always inject: controller identity, Git facts, ledger path, `ledger_sha256`,
source status/path/hash **without** unused bodies.

Then select **one** working set:

| This turn | Model-visible working set |
| --- | --- |
| SessionStart with exactly one `ACTIVE` / `RECOVERING` / `VERIFY` item | That item's required fields |
| SessionStart with zero or multiple current items, or no parseable rows | Open-item projection (non-terminal rows) |
| Fact question that resolves a mechanism | That file only |
| Fact question about 规则 / 治理 / AGENTS | AGENTS.md body only |
| Fact question about 进度 / 台账 / 下一步 / 项目状态 | Working-set rows, not ledger Markdown |
| Fact question about Git / Runtime identity | Identity + Git / runtime_state metadata |

Terminal statuses (`DONE`, `SUPERSEDED`) stay out of the open-item projection.
The full ledger file remains the place to read history.

SessionStart still attaches **AGENTS.md** when it fits (project entry rules are
supposed to be short). It does **not** attach runtime Skill, project Skill, or
ledger Markdown.

## Required fields for a current item

Hard facts, copied from the parsed ledger row:

- id, status, owner
- 目标与边界 (`scope`) — non-goals belong here
- 依赖 / 阻塞
- 验收
- 证据
- 下一步 (`next_action`) — this remains the only next-action authority

No second next-action card.

## Completeness

Completeness is relative to **this working set**, not the whole project.

`complete=false` when:

- the ledger is missing/unreadable or Git facts are unavailable when required;
- the unique current item is missing `next_action` or 目标与边界;
- a required attached body exceeds the size cap.

Omitted required bodies are listed. They are **not** truncated. Facts absent from
the packet must be treated as unknown; chat history cannot fill them.

## Fact-query narrowing

If `resolve_existing_mechanism` finds a file, attach that file and do not attach
ledger or Skill bodies. If it is `not_found`, keep the existing UNKNOWN gate.
Stop-correction uses the prompt or `last_assistant_message` to choose the same
attach policy so governance corrections still see AGENTS.md.

## Size cap

96KB remains a hard cap on any **attached body**. Overflow omits the body, sets
`complete=false`, and records `source_too_large`. The marker
`[TRUNCATED_BY_PROJECT_CONTEXT_GUARD]` is removed from the opening path.
