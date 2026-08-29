# Reviewer TDD Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make high-risk bugfix reviewers verify the causal RED→candidate→GREEN chain rather than accepting a green test claim alone.

**Architecture:** Extend existing `required_reviews` receipts instead of creating a second review system. Risk-scoped reviews declare `tdd_required=true`; the control-event guard then requires pre-fix RED evidence, exact candidate revision, post-fix GREEN evidence, same-case confirmation, a reviewer counterexample/edge check, and a PASS/FAIL verdict.

**Tech Stack:** Python governance guards + unittest; Markdown governance contract.

**Spec:** `references/agent-delivery-contract.md`

- [ ] Add failing guard tests for missing TDD causal evidence and a complete receipt.
- [ ] Run focused tests and verify RED for the intended missing validation.
- [ ] Add minimal `required_reviews` validation for TDD-required reviews.
- [ ] Run focused tests to GREEN.
- [ ] Document risk-scoped reviewer TDD evidence in the delivery contract and methods.
- [ ] Run full Python/Node suites and diff-check; commit and push main.
