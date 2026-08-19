# Platform-Neutral Adaptive Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `adaptive-delivery` platform-neutral while retaining concise, verified host-specific installation examples.

**Architecture:** Keep one vendor-neutral `SKILL.md`, reference set, template set, and initializer. Treat automatic discovery, explicit invocation syntax, installation directories, and global instruction files as host adapters documented only in README; unsupported hosts fall back to reading `SKILL.md` explicitly.

**Tech Stack:** Markdown Agent Skill, Python 3 standard library, `unittest`, Git, official host documentation for installation claims.

**Spec:** `docs/superpowers/specs/2026-08-19-adaptive-delivery-design.md`

## Global Constraints

- Core Skill behavior must not require Codex, Claude Code, Gemini CLI, Cursor, or another named product.
- Do not promise automatic discovery on a host unless its first-party documentation confirms it.
- Keep `AGENTS.md` as the default portable project entry; other host instruction files link to the existing authority documents rather than copying them.
- `$adaptive-delivery` is an optional host-specific example, not a required invocation syntax.
- Do not create per-platform copies of the Skill, references, templates, or tests.
- Preserve the current three delivery levels, authorization gates, cross-session state model, visual governance, and stalled-task recovery rule.
- Git commit, push, and GitHub repository metadata changes remain separate authorization gates.

---

### Task 1: Prove the current core is vendor-coupled

**Files:**
- Modify: `tests/test_skill_structure.py`
- Modify after baseline: `tests/behavioral-scenarios.md`

**Interfaces:**
- Consumes: current `SKILL.md` and `references/*.md` text.
- Produces: a mechanical invariant that core runtime instructions contain no vendor names, plus a recorded portability behavior scenario.

- [ ] **Step 1: Run an independent read-only baseline**

Give an evaluator only the public repository and ask whether the Skill can be adopted by a non-Codex coding agent without translating core instructions. Require citations to repository-relative paths; do not reveal the desired answer.

- [ ] **Step 2: Add the failing structural test**

Add this test to `SkillStructureTests`:

```python
def test_core_runtime_instructions_are_vendor_neutral(self):
    core = "\n".join(
        [read_entrypoint(), *[path.read_text(encoding="utf-8") for path in REFERENCE_PATHS.values()]]
    ).casefold()
    vendor_names = ("codex", "chatgpt", "claude code", "gemini cli", "cursor")

    self.assertEqual([], [name for name in vendor_names if name in core])
```

- [ ] **Step 3: Run the targeted test and verify RED**

Run:

```bash
python3 -m unittest tests.test_skill_structure.SkillStructureTests.test_core_runtime_instructions_are_vendor_neutral -v
```

Expected: FAIL because the current `SKILL.md` description contains `Codex sessions` and `references/experience-catalog.md` contains `Codex`.

- [ ] **Step 4: Record the baseline without inventing history**

Add scenario 10 to `tests/behavioral-scenarios.md` with the original request, the evaluator's actual cited result, and this pass condition: core instructions are vendor-neutral; host-specific installation and invocation remain README examples only.

### Task 2: Make the runtime core platform-neutral

**Files:**
- Modify: `SKILL.md`
- Modify: `references/experience-catalog.md`
- Test: `tests/test_skill_structure.py`

**Interfaces:**
- Consumes: the failing invariant from Task 1.
- Produces: one Agent Skills-compatible core with no named-host dependency.

- [ ] **Step 1: Replace the only vendor-specific trigger**

In `SKILL.md`, replace:

```yaml
resuming work across Codex sessions
```

with:

```yaml
resuming work across agent sessions
```

- [ ] **Step 2: Generalize the experience rule**

In `references/experience-catalog.md`, replace the Codex-specific duplication rule with:

```markdown
4. 不重复宿主平台、全局指令文件或其他已存在的规则。
```

- [ ] **Step 3: Run the targeted test and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_skill_structure.SkillStructureTests.test_core_runtime_instructions_are_vendor_neutral -v
```

Expected: PASS.

- [ ] **Step 4: Check entrypoint size**

Run:

```bash
wc -w SKILL.md
```

Expected: no more than 150 words; no new platform-routing prose in the entrypoint.

### Task 3: Rewrite README around a universal core

**Files:**
- Modify: `README.md`
- Modify: `tests/behavioral-scenarios.md`

**Interfaces:**
- Consumes: platform-neutral core from Task 2 and first-party host documentation.
- Produces: generic adoption instructions and short, verified host examples without changing runtime behavior.

- [ ] **Step 1: Verify host claims before writing them**

Check first-party documentation for each candidate host. Include a host only if the documentation confirms Agent Skills support and its current install location or invocation method. At minimum retain the already verified Codex example; omit any unverified Claude Code, Gemini CLI, or Cursor path instead of guessing.

- [ ] **Step 2: Replace the product-specific opening**

Use this opening:

```markdown
让 AI 编程代理按项目风险选择最小够用的工程方法，并把需求、设计、开发、测试、验收和发布连接成可验证的交付过程。
```

Replace “Codex 会话”“Codex 自动记住旧聊天”等 core descriptions with “Agent 会话”“AI 编程代理不会自动共享旧会话”等 platform-neutral wording.

- [ ] **Step 3: Add one compatibility boundary before installation**

State these three facts concisely:

```markdown
- 兼容 Agent Skills 的宿主可以直接安装并自动或显式调用。
- 其他 AI 编程代理可以按自然语言要求读取 `SKILL.md` 后使用同一方法。
- 自动发现、调用语法、安装目录和全局指令文件由宿主决定。
```

- [ ] **Step 4: Split installation into generic and host examples**

The generic path says to install or clone this repository into the host's documented skills directory. Keep each verified host example under its own short heading. Do not present `~/.codex/skills` or `$adaptive-delivery` as universal syntax.

- [ ] **Step 5: Generalize usage examples**

Keep the existing three explicit development intents, but introduce them as natural-language examples. Show `$adaptive-delivery` only inside a labeled host-specific example; also show the host-neutral form “请使用 adaptive-delivery …”.

- [ ] **Step 6: Complete scenario 10**

Record the forward result: a non-Codex evaluator can identify the same delivery levels, state files, authorization gates, and stop conditions without translating named-product instructions. If an independent evaluator is unavailable after two bounded waits, stop it and record the missing evidence instead of declaring behavioral success.

### Task 4: Sync, verify, and stop at the publication gate

**Files:**
- Sync: `SKILL.md`, `README.md`, and changed `references/` files to the installed skill directory.
- Verify: all source files and the installed copy.

**Interfaces:**
- Consumes: completed source edits from Tasks 1–3.
- Produces: a locally installed platform-neutral Skill and a reviewable, uncommitted Git diff.

- [ ] **Step 1: Run the full deterministic suite**

Run:

```bash
python3 -m unittest discover -s tests -v
git diff --check
```

Expected: all tests PASS and `git diff --check` emits no output.

- [ ] **Step 2: Scan platform names by responsibility**

Run:

```bash
rg -n -i 'codex|chatgpt|claude code|gemini cli|cursor' SKILL.md references assets scripts
```

Expected: no matches in runtime core. Matches are allowed only in README compatibility examples, tests, or historical design/plan context.

- [ ] **Step 3: Sync the local installation**

Update only changed runtime files in `~/.agents/skills/adaptive-delivery/`; do not copy `docs/`, `tests/`, `.git/`, or cache files. Compare each synced file byte-for-byte with its source counterpart.

- [ ] **Step 4: Re-run installed-copy checks**

Confirm the installed `SKILL.md` has valid frontmatter, all four reference links resolve, and the entrypoint remains no more than 150 words.

- [ ] **Step 5: Stop before external publication**

Report the exact diff, tests, installed-copy evidence, and any unverified host integrations. Do not commit, push, or change the GitHub repository description until the user explicitly authorizes those Git and remote mutations.

### Task 5: Optional authorized publication

**Files:**
- Commit: the exact reviewed source diff.
- Remote: `ManechoLIU/adaptive-delivery` metadata and `main` branch.

**Interfaces:**
- Consumes: Task 4 evidence and explicit user authorization.
- Produces: a public commit whose repository description targets AI coding agents generally.

- [ ] **Step 1: Re-run the full Task 4 evidence set**

Expected: unchanged passing results immediately before commit.

- [ ] **Step 2: Commit only reviewed files**

Use a focused message such as:

```bash
git commit -m "Generalize adaptive-delivery across coding agents"
```

- [ ] **Step 3: Push and verify the exact remote commit**

Push `main`, then compare `git rev-parse HEAD` with `git ls-remote origin refs/heads/main`.

- [ ] **Step 4: Update the repository description**

Set a platform-neutral description such as:

```text
Adaptive software delivery methodology for AI coding agents
```

Verify repository visibility, default branch, description, and README URL after the update.
