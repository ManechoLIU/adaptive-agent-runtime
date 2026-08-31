# Controller Scoring Window V2 Implementation Plan

1. Add model-contract tests for the 24h / max-5 valid-event window, separate unresolved-anomaly inspection, sparse-sample disclosure, and lightweight Self-Check.
2. Run focused tests and confirm RED against the current two-event model.
3. Update the authoritative scoring reference and user-facing skill/readme summaries without changing seven dimensions or caps.
4. Update score-history hook fixture to persist the new window wording while preserving backward-compatible storage.
5. Run focused scoring/self-check/governance tests, then the full Python test suite and diff check.
6. Commit, obtain an independent review of the exact head, integrate to main only if review has no Critical/Important findings, rerun merged-main tests, and install the exact revision.
