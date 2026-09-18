# Session Opening Working Set Implementation Plan

**Goal:** Make SessionStart and project-fact prompts inject a role-scoped working set instead of the full ledger and Skill dump, without adding AWR product surface.

**Spec:** `docs/superpowers/specs/2026-09-19-session-opening-working-set-design.md`

**Architecture:** Keep `project_context_guard.py` as the only opening injector. Parse the existing ledger with `lint_governance.task_records`. Render identity always; render current-item or open-item projection; attach AGENTS.md or a resolved mechanism only when that is the asked source. Do not add a prepare command or action card.

## Constraints

- Unique Markdown ledger remains authoritative.
- Next action stays the ledger `下一步` field.
- Existing identity / mechanism / projectless tests stay meaningful; update only assertions that required the full dump.
- No push.

## Tasks

1. Write the design spec (this change set).
2. Add tests for: unique ACTIVE item packet, READY-only open projection, mechanism query without ledger secret, oversized AGENTS not truncated.
3. Implement working-set selection and attach policy in `project_context_guard.py`.
4. Sync `references/context-governance.md` and the SKILL 续接 bullets.
5. Run `tests/test_project_context_guard.py` and a focused skill-structure check.
