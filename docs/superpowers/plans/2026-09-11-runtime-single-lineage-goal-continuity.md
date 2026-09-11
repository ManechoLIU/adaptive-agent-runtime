# Runtime Single-Lineage and Goal Continuity Implementation Plan

> **For Codex:** Execute this plan on the canonical `main` branch only. Do not use a feature branch as an install source.

**Goal:** Keep source, remote, installed Runtime, Controller ACK, and Controller Goal continuity on one verifiable lineage.

**Architecture:** The formal installer must accept only a clean local `main` whose exact revision is published to its configured `origin/main`. The install manifest records that release proof, and rule ACK rejects a formal manifest without it. Target rotation records a durable Goal-rebind obligation so a replacement execution target cannot perform Controller work until the same ledger Goal is restored and read back.

**Tech Stack:** Python 3, `unittest`, Git plumbing, JSON state files.

---

### Task 1: Canonical release-source fence

- Add behavior tests for a verified `main == origin/main`, a feature-branch source, and an unpublished local `main`.
- Implement one read-only Git proof function.
- Call it before every formal installer mutation and persist the proof in the install manifest.
- Make formal rule ACK fail closed when that proof is absent or inconsistent.

### Task 2: Goal continuity on target rotation

- Add a regression test reproducing a target rotation with an existing open Goal.
- Persist the exact Goal identity/objective and target generation as a rebind obligation.
- Block Controller-exclusive work until the replacement target restores and reads back that Goal.

### Task 3: Installed-host recovery

- Run focused and release regression suites.
- Push canonical `main` by fast-forward only and verify remote SHA.
- Install only from the verified canonical revision; verify manifest, Hook definitions, Controller ACK, and Goal state.
- Leave Hook trust fail-closed if the host requires one explicit user trust action; do not self-approve it.
