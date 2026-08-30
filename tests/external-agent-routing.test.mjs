import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { chmod, copyFile, mkdir, mkdtemp, readFile, realpath, writeFile } from "node:fs/promises";
import os from "node:os";
import crypto from "node:crypto";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { parseArgs, renderExternalAgentCard } from "../scripts/run_external_agent.mjs";

const skillRoot = fileURLToPath(new URL("../", import.meta.url));
const adapter = path.join(skillRoot, "scripts", "run_external_agent.mjs");

async function assignmentAckFile(directory, overrides = {}, repositoryRoot = skillRoot) {
  const branch = execFileSync("git", ["-C", repositoryRoot, "branch", "--show-current"], { encoding: "utf8" }).trim();
  const head = execFileSync("git", ["-C", repositoryRoot, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  const assignment = {
    assignment_id: "a1", task_id: "T1", agent_id: "writer", state: "ACKED",
    primary_goal: "finish bounded task", success_criteria: ["green"], owned_scope: ["scripts/run_external_agent.mjs"],
    forbidden_scope: [], parallelizable: true, observed_modified_files: [],
    ack: { repository_root: repositoryRoot, branch, head, status: "clean", owned_files: ["scripts/run_external_agent.mjs"], first_red: "red", stop_condition: "candidate" },
    ...overrides,
  };
  if (overrides.ack) assignment.ack = { ...assignment.ack, ...overrides.ack };
  const target = path.join(directory, `assignment-${Math.random().toString(36).slice(2)}.json`);
  await writeFile(target, JSON.stringify(assignment));
  return target;
}


async function makeAssignmentRepo(directory) {
  const repo = path.join(directory, `repo-${Math.random().toString(36).slice(2)}`);
  await mkdir(repo, { recursive: true });
  execFileSync("git", ["-C", repo, "init", "-b", "main"]);
  execFileSync("git", ["-C", repo, "config", "user.email", "test@example.com"]);
  execFileSync("git", ["-C", repo, "config", "user.name", "Test"]);
  await writeFile(path.join(repo, "TASK_LEDGER.md"), "# Tasks\n\n- 规则版本：old\n");
  execFileSync("git", ["-C", repo, "add", "TASK_LEDGER.md"]);
  execFileSync("git", ["-C", repo, "commit", "-m", "init"]);
  return repo;
}

async function fakeInstalledSkill(directory) {
  const root = path.join(directory, "installed-skill");
  const scripts = path.join(root, "scripts");
  await mkdir(scripts, { recursive: true });
  for (const file of ["run_external_agent.mjs", "assignment_lease_guard.py", "assignment_runtime.py", "project_state.py", "rule_handshake.py"]) {
    await copyFile(path.join(skillRoot, "scripts", file), path.join(scripts, file));
  }
  const rel = "scripts/rule_handshake.py";
  const bytes = await readFile(path.join(root, rel));
  const hash = crypto.createHash("sha256").update(bytes).digest("hex");
  await writeFile(path.join(root, ".adaptive-delivery-install.json"), JSON.stringify({
    schema_version: 1, revision: "new-rule-revision", previous_revision: "old",
    installed_at: "2026-08-30T00:00:00+00:00", source_root: skillRoot, summary: "runtime governance",
    impact: "live_assignments", stop_condition: "ack exact revision", changed_files: [rel], files: { [rel]: hash },
  }));
  return { root, adapter: await realpath(path.join(scripts, "run_external_agent.mjs")) };
}

async function fakeRunner(bin, name, versionArgument) {
  const target = path.join(bin, name);
  await writeFile(target, `#!/usr/bin/env node
import fs from "node:fs";
if (process.argv[2] === ${JSON.stringify(versionArgument)}) {
  process.stdout.write(${JSON.stringify(`${name} test\n`)});
} else {
  process.stdout.write(JSON.stringify({
    args: process.argv.slice(2),
    cwd: process.cwd(),
    hasKimiApiKey: Boolean(process.env.KIMI_MODEL_API_KEY),
    kimiModelName: process.env.KIMI_MODEL_NAME || null,
    kimiModelBaseUrl: process.env.KIMI_MODEL_BASE_URL || null,
    kimiThinkingEffort: process.env.KIMI_MODEL_THINKING_EFFORT || null,
    hasXaiApiKey: Boolean(process.env.XAI_API_KEY),
    grokHome: process.env.GROK_HOME || null,
  }) + "\\n");
  if (process.env.SPAWN_MARKER) fs.appendFileSync(process.env.SPAWN_MARKER, "spawned\\n");
  if (process.env.FAKE_RUNNER_TOUCH_FILE) fs.writeFileSync(process.env.FAKE_RUNNER_TOUCH_FILE, "changed\\n");
  const delay = Number(process.env.FAKE_RUNNER_DELAY_MS || 0);
  if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
  process.exitCode = Number(process.env.FAKE_RUNNER_EXIT_CODE || 0);
}
`);
  await chmod(target, 0o755);
}

test("route parsing requires explicit auth mode and the Kimi Open Platform model id", () => {
  assert.throws(() => parseArgs([
    "--check", "--engine", "grok-build", "--model", "grok-4.6", "--cwd", skillRoot,
  ]), /auth-mode/);
  assert.throws(() => parseArgs([
    "--login", "--engine", "grok-build", "--auth-mode", "oauth", "--cwd", skillRoot,
  ]), /authorized-login/);
  assert.throws(() => parseArgs([
    "--execute", "--engine", "kimi-code", "--auth-mode", "api", "--model", "kimi-k3",
    "--reasoning-effort", "low", "--cwd", skillRoot,
  ]), /authorized-external-call/);
  assert.throws(() => parseArgs([
    "--check", "--engine", "grok-build", "--auth-mode", "oauth", "--model", "grok-4.6",
    "--cwd", skillRoot,
  ]), /reasoning-effort/);
  assert.throws(() => parseArgs([
    "--check", "--engine", "grok-build", "--auth-mode", "oauth", "--model", "grok-4.6",
    "--reasoning-effort", "ultra", "--cwd", skillRoot,
  ]), /Unsupported reasoning effort/);
  assert.throws(() => parseArgs([
    "--check", "--engine", "kimi-code", "--auth-mode", "api", "--model", "k3",
    "--reasoning-effort", "low", "--cwd", skillRoot,
  ]), /not allowed/);

  const legacy = parseArgs([
    "--check", "--engine", "kimi-code-api", "--model", "kimi-k3",
    "--reasoning-effort", "low", "--cwd", skillRoot,
  ]);
  assert.equal(legacy.engine, "kimi-code");
  assert.equal(legacy.authMode, "api");
});

test("external status cards keep provider identity separate from execution state", () => {
  assert.equal(renderExternalAgentCard({
    engine: "kimi-code",
    model: "kimi-k3",
    authMode: "api",
    reasoningEffort: "high",
    workPackage: "M1-F4-B-REVIEW",
    category: "frontend",
    status: "running",
    detail: "TDD 因果审查开始",
  }), [
    "╭─ 🟣 Kimi K3 (kimi-k3) · 🟢 运行中",
    "│ M1-F4-B-REVIEW · frontend · api · high",
    "╰─ TDD 因果审查开始",
  ].join("\n"));

  assert.equal(renderExternalAgentCard({
    engine: "grok-build",
    model: "grok-4.6",
    authMode: "oauth",
    reasoningEffort: "xhigh",
    workPackage: "B1-API",
    category: "backend",
    status: "returned",
    detail: "候选已交回总控验收",
  }), [
    "╭─ 🟦 Grok 4.6 (grok-4.6) · 🟡 已返回",
    "│ B1-API · backend · oauth · xhigh",
    "╰─ 候选已交回总控验收",
  ].join("\n"));
});

test("status-card mode renders canonical output and rejects free-form fields", () => {
  const rendered = execFileSync(process.execPath, [adapter,
    "--render-status-card", "--engine", "kimi-code", "--auth-mode", "api",
    "--model", "kimi-k3", "--reasoning-effort", "medium",
    "--work-package", "M2-MINI", "--category", "frontend", "--status", "accepted",
    "--detail", "current-main 验收通过",
  ], { encoding: "utf8" });
  assert.equal(rendered, [
    "╭─ 🟣 Kimi K3 (kimi-k3) · ✅ 已验收",
    "│ M2-MINI · frontend · api · medium",
    "╰─ current-main 验收通过",
    "",
  ].join("\n"));

  assert.throws(() => parseArgs([
    "--render-status-card", "--engine", "kimi-code", "--auth-mode", "api",
    "--model", "kimi-k3", "--reasoning-effort", "medium",
    "--work-package", "M2-MINI", "--category", "frontend", "--status", "reviewing",
    "--detail", "free-form status",
  ]), /Unsupported card status/);
  assert.throws(() => parseArgs([
    "--render-status-card", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "medium",
    "--work-package", "B1\nspoof", "--category", "backend", "--status", "running",
    "--detail", "start",
  ]), /single-line/);
});

test("all four routes report the selected credential source without a model call", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-check-"));
  const kimiHome = path.join(bin, "kimi-home");
  const grokHome = path.join(bin, "grok-home");
  await mkdir(path.join(kimiHome, "credentials"), { recursive: true });
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(kimiHome, "credentials", "kimi-code.json"), "{}");
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "kimi", "--version");
  await fakeRunner(bin, "grok", "version");

  const baseEnv = {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`,
    KIMI_CODE_HOME: kimiHome,
    GROK_HOME: grokHome,
    KIMI_K3_KEYCHAIN_SERVICE: `adaptive-test-kimi-${path.basename(bin)}`,
    XAI_GROK_KEYCHAIN_SERVICE: `adaptive-test-xai-${path.basename(bin)}`,
  };
  const cases = [
    ["kimi-code", "oauth", "kimi-code/k3", {}, "cli-session"],
    ["kimi-code", "api", "kimi-k3", { MOONSHOT_API_KEY: "test-key" }, "environment"],
    ["grok-build", "oauth", "grok-4.6", {}, "cli-session"],
    ["grok-build", "api", "grok-4.6", { XAI_API_KEY: "test-key" }, "environment"],
  ];

  for (const [engine, authMode, model, extraEnv, source] of cases) {
    const result = JSON.parse(execFileSync(process.execPath, [adapter,
      "--check", "--engine", engine, "--auth-mode", authMode, "--model", model,
      "--reasoning-effort", "medium", "--cwd", skillRoot,
    ], { encoding: "utf8", env: { ...baseEnv, ...extraEnv } }));
    assert.equal(result.available, true);
    assert.equal(result.authMode, authMode);
    assert.equal(result.reasoningEffort, "medium");
    assert.equal(result.credentialConfigured, true);
    assert.equal(result.credentialSource, source);
    assert.equal(result.provesLiveModelAccess, false);
  }
});

test("OAuth and API execution stay on distinct Kimi and Grok credential paths", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-run-"));
  const kimiHome = path.join(bin, "kimi-home");
  const grokHome = path.join(bin, "grok-home");
  await mkdir(path.join(kimiHome, "credentials"), { recursive: true });
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(kimiHome, "credentials", "kimi-code.json"), "{}");
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await writeFile(path.join(kimiHome, "region"), "mainland-cn\n");
  await fakeRunner(bin, "kimi", "--version");
  await fakeRunner(bin, "grok", "version");

  const baseEnv = {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`,
    KIMI_CODE_HOME: kimiHome,
    GROK_HOME: grokHome,
    KIMI_K3_KEYCHAIN_SERVICE: `adaptive-test-kimi-${path.basename(bin)}`,
    XAI_GROK_KEYCHAIN_SERVICE: `adaptive-test-xai-${path.basename(bin)}`,
  };

  const kimiOauth = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "kimi-code", "--auth-mode", "oauth",
    "--model", "kimi-code/k3", "--reasoning-effort", "high", "--cwd", skillRoot,
  ], { encoding: "utf8", input: "bounded contract", env: {
    ...baseEnv, KIMI_MODEL_API_KEY: "must-be-removed", MOONSHOT_API_KEY: "must-be-removed",
  } });
  assert.equal(kimiOauth.status, 0, kimiOauth.stderr);
  const kimiOauthCall = JSON.parse(kimiOauth.stdout.trim());
  assert.deepEqual(kimiOauthCall.args, [
    "-m", "kimi-code/k3", "-p", "bounded contract", "--output-format", "stream-json",
  ]);
  assert.equal(kimiOauthCall.hasKimiApiKey, false);
  assert.equal(kimiOauthCall.kimiModelName, null);
  assert.equal(kimiOauthCall.kimiThinkingEffort, "high");

  const kimiApi = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "kimi-code", "--auth-mode", "api",
    "--model", "kimi-k3", "--reasoning-effort", "max", "--cwd", skillRoot,
  ], { encoding: "utf8", input: "bounded contract", env: { ...baseEnv, MOONSHOT_API_KEY: "test-key" } });
  assert.equal(kimiApi.status, 0, kimiApi.stderr);
  const kimiApiCall = JSON.parse(kimiApi.stdout.trim());
  assert.deepEqual(kimiApiCall.args, ["-p", "bounded contract", "--output-format", "stream-json"]);
  assert.equal(kimiApiCall.hasKimiApiKey, true);
  assert.equal(kimiApiCall.kimiModelName, "kimi-k3");
  assert.equal(kimiApiCall.kimiModelBaseUrl, "https://api.moonshot.cn/v1");
  assert.equal(kimiApiCall.kimiThinkingEffort, "max");

  const grokOauth = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "xhigh", "--cwd", skillRoot,
  ], { encoding: "utf8", input: "bounded contract", env: { ...baseEnv, XAI_API_KEY: "must-be-removed" } });
  assert.equal(grokOauth.status, 0, grokOauth.stderr);
  const grokOauthCall = JSON.parse(grokOauth.stdout.trim());
  assert.equal(grokOauthCall.hasXaiApiKey, false);
  assert.equal(grokOauthCall.grokHome, grokHome);
  assert.deepEqual(grokOauthCall.args.slice(-2), ["--reasoning-effort", "xhigh"]);

  const grokApi = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "api",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", skillRoot,
  ], { encoding: "utf8", input: "bounded contract", env: { ...baseEnv, XAI_API_KEY: "test-key" } });
  assert.equal(grokApi.status, 0, grokApi.stderr);
  const grokApiCall = JSON.parse(grokApi.stdout.trim());
  assert.equal(grokApiCall.hasXaiApiKey, true);
  assert.notEqual(grokApiCall.grokHome, grokHome);
  assert.match(grokApiCall.grokHome, /adaptive-delivery-grok-api-/);
  assert.deepEqual(grokApiCall.args.slice(-2), ["--reasoning-effort", "low"]);
});

test("login mode delegates to the official CLI without a model request", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-login-"));
  await fakeRunner(bin, "kimi", "--version");
  await fakeRunner(bin, "grok", "version");
  const env = { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}` };

  const kimi = spawnSync(process.execPath, [adapter,
    "--login", "--authorized-login", "--engine", "kimi-code", "--auth-mode", "oauth",
    "--region", "global", "--cwd", skillRoot,
  ], { encoding: "utf8", env });
  assert.equal(kimi.status, 0, kimi.stderr);
  assert.deepEqual(JSON.parse(kimi.stdout.trim()).args, ["login", "--region", "global"]);

  const grok = spawnSync(process.execPath, [adapter,
    "--login", "--authorized-login", "--device-auth", "--engine", "grok-build",
    "--auth-mode", "oauth", "--cwd", skillRoot,
  ], { encoding: "utf8", env });
  assert.equal(grok.status, 0, grok.stderr);
  assert.deepEqual(JSON.parse(grok.stdout.trim()).args, ["login", "--device-auth"]);
});


test("assignment-bound execute fails before agent spawn without delivered ACK", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-ack-missing-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo,
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
  ], { encoding: "utf8", input: "bounded contract", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /assignment-ack/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("assignment-bound execute rejects stale or mismatched ACK before spawn", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-ack-bad-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const badId = await assignmentAckFile(bin, { assignment_id: "other" }, repo);
  const badHead = await assignmentAckFile(bin, { ack: { head: "deadbeef" } }, repo);
  for (const ack of [badId, badHead]) {
    const result = spawnSync(process.execPath, [adapter,
      "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
      "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo,
      "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
      "--assignment-ack", ack,
    ], { encoding: "utf8", input: "bounded contract", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } });
    assert.equal(result.status, 1);
  }
  await assert.rejects(readFile(marker, "utf8"));
});

test("assignment-bound execute spawns only after exact delivered ACK passes", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-ack-good-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, {}, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo,
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: "bounded contract", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } });
  assert.equal(result.status, 0, result.stderr);
  assert.equal((await readFile(marker, "utf8")).trim(), "spawned");
});

test("delivery verdict leaves exit zero without a receipt unresolved", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-runtime-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const receipts = path.join(bin, "receipts.jsonl");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, {}, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo,
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack, "--attempt", "1", "--lease-id", "lease-1", "--runtime-receipts", receipts,
  ], { encoding: "utf8", input: "bounded contract", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome } });
  assert.equal(result.status, 0, result.stderr);
  const events = (await readFile(receipts, "utf8")).trim().split("\n").map(JSON.parse);
  assert.deepEqual(events.map((e) => e.event_type), ["assignment_started", "assignment_terminal"]);
  assert.deepEqual(events.map((e) => e.event_seq), [1, 2]);
  assert.equal(events[0].attempt, 1); assert.equal(events[0].lease_id, "lease-1");
  assert.equal(events[1].terminal_state, "completed");
  assert.equal(events[1].transport_outcome, "completed");
  assert.equal(events[1].delivery_outcome, "unresolved");
  assert.equal(events[1].outcome, undefined);
});

test("delivery verdict preserves transport failure and explicit evidence-backed verdicts", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-delivery-verdict-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const env = { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome };

  const run = async ({ assignmentId, exitCode = 0, deliveryReceipt, primaryGoal = "finish bounded task" }) => {
    const receipts = path.join(bin, `${assignmentId}.jsonl`);
    const deliveryPath = path.join(bin, `${assignmentId}-delivery.json`);
    const args = [adapter,
      "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
      "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo,
      "--assignment-id", assignmentId, "--task-id", "T1", "--agent-id", "writer", "--session-id", `session-${assignmentId}`,
      "--assignment-ack", await assignmentAckFile(bin, { assignment_id: assignmentId, primary_goal: primaryGoal }, repo),
      "--runtime-receipts", receipts,
    ];
    if (deliveryReceipt) {
      await writeFile(deliveryPath, JSON.stringify(deliveryReceipt));
      args.push("--delivery-receipt", deliveryPath);
    }
    const result = spawnSync(process.execPath, args, {
      encoding: "utf8", input: "bounded contract", env: { ...env, FAKE_RUNNER_EXIT_CODE: String(exitCode) },
    });
    const events = (await readFile(receipts, "utf8")).trim().split("\n").map(JSON.parse);
    return { result, terminal: events.at(-1) };
  };

  const failed = await run({ assignmentId: "exit-one", exitCode: 1 });
  assert.equal(failed.result.status, 1, failed.result.stderr);
  assert.equal(failed.terminal.transport_outcome, "failed");
  assert.equal(failed.terminal.delivery_outcome, "unresolved");

  const invalidDelivery = await run({
    assignmentId: "invalid-delivery",
    primaryGoal: "validate an invalid delivery receipt",
    deliveryReceipt: { delivery_outcome: "pass", summary: "missing artifact", evidence: ["green-test:42"], artifacts: [], next_action: "review", retry_class: "none" },
  });
  assert.equal(invalidDelivery.result.status, 1, invalidDelivery.result.stderr);
  assert.equal(invalidDelivery.terminal.transport_outcome, "completed");
  assert.equal(invalidDelivery.terminal.delivery_outcome, "unresolved");

  const proseOnlyPass = await run({
    assignmentId: "prose-only-pass",
    primaryGoal: "reject prose-only pass evidence",
    deliveryReceipt: { delivery_outcome: "pass", summary: "sounds good", evidence: ["tests passed yesterday"], artifacts: ["some changed file"], next_action: "review", retry_class: "none" },
  });
  assert.equal(proseOnlyPass.result.status, 1, proseOnlyPass.result.stderr);
  assert.equal(proseOnlyPass.terminal.transport_outcome, "completed");
  assert.equal(proseOnlyPass.terminal.delivery_outcome, "unresolved");

  const explicitFail = await run({
    assignmentId: "explicit-fail",
    deliveryReceipt: { delivery_outcome: "fail", summary: "focused test failed", evidence: ["test-log:42"], artifacts: [], next_action: "fix test", retry_class: "none" },
  });
  assert.equal(explicitFail.result.status, 0, explicitFail.result.stderr);
  assert.equal(explicitFail.terminal.transport_outcome, "completed");
  assert.equal(explicitFail.terminal.delivery_outcome, "fail");
  assert.deepEqual(explicitFail.terminal.evidence, ["test-log:42"]);

  const explicitPass = await run({
    assignmentId: "explicit-pass",
    deliveryReceipt: { delivery_outcome: "pass", summary: "delivery verified", evidence: ["green-test:42"], artifacts: ["git:abc123"], next_action: "review", retry_class: "none" },
  });
  assert.equal(explicitPass.result.status, 0, explicitPass.result.stderr);
  assert.equal(explicitPass.terminal.transport_outcome, "completed");
  assert.equal(explicitPass.terminal.delivery_outcome, "pass");
  assert.deepEqual(explicitPass.terminal.artifacts, ["git:abc123"]);
});



test("long external execution emits automatic heartbeat before terminal", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-heartbeat-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const receipts = path.join(bin, "receipts.jsonl");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, {}, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo,
    "--assignment-id", "heartbeat-a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", await assignmentAckFile(bin, { assignment_id: "heartbeat-a1" }, repo),
    "--attempt", "1", "--lease-id", "heartbeat-lease-1", "--runtime-receipts", receipts,
  ], { encoding: "utf8", input: "bounded contract", env: {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`,
    GROK_HOME: grokHome,
    FAKE_RUNNER_DELAY_MS: "180",
    AD_RUNTIME_HEARTBEAT_MS: "50",
  } });
  assert.equal(result.status, 0, result.stderr);
  const events = (await readFile(receipts, "utf8")).trim().split("\n").map(JSON.parse);
  assert.equal(events[0].event_type, "assignment_started");
  assert.equal(events.at(-1).event_type, "assignment_terminal");
  assert.ok(events.some((event) => event.event_type === "assignment_heartbeat"), JSON.stringify(events));
  assert.deepEqual(events.map((event) => event.event_seq), events.map((_, index) => index + 1));
  assert.equal(events.filter((event) => event.event_type === "assignment_terminal").length, 1);
});



test("external execution emits progress when tracked worktree evidence changes", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-progress-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const receipts = path.join(bin, "receipts.jsonl");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo,
    "--assignment-id", "progress-a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", await assignmentAckFile(bin, { assignment_id: "progress-a1" }, repo),
    "--attempt", "1", "--lease-id", "progress-lease-1", "--runtime-receipts", receipts,
  ], { encoding: "utf8", input: "bounded contract", env: {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`,
    GROK_HOME: grokHome,
    FAKE_RUNNER_DELAY_MS: "180",
    FAKE_RUNNER_TOUCH_FILE: path.join(repo, "TASK_LEDGER.md"),
    AD_RUNTIME_HEARTBEAT_MS: "50",
  } });
  assert.equal(result.status, 0, result.stderr);
  const events = (await readFile(receipts, "utf8")).trim().split("\n").map(JSON.parse);
  const progress = events.find((event) => event.event_type === "assignment_progress");
  assert.ok(progress, JSON.stringify(events));
  assert.ok(progress.last_observed_status_sha256);
  assert.equal(events.at(-1).event_type, "assignment_terminal");
});

test("assignment-bound execution persists canonical runtime without audit JSONL", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-runtime-canonical-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, {}, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo,
    "--assignment-id", "canonical-a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", await assignmentAckFile(bin, { assignment_id: "canonical-a1" }, repo),
  ], { encoding: "utf8", input: "bounded", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome } });
  assert.equal(result.status, 0, result.stderr);
  const common = execFileSync("git", ["-C", repo, "rev-parse", "--git-common-dir"], { encoding: "utf8" }).trim();
  const statePath = path.join(path.resolve(repo, common), "adaptive-delivery", "runtime-assignments.json");
  const state = JSON.parse(await readFile(statePath, "utf8"));
  assert.equal(state.leases["canonical-a1"].terminal_state, "completed");
});

test("pending live rule handshake blocks before external agent spawn", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-rule-pending-"));
  const repo = await makeAssignmentRepo(bin);
  const installed = await fakeInstalledSkill(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, {}, repo);
  const result = spawnSync(process.execPath, [installed.adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo,
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: "bounded", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /rule handshake|rule-handshake/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("same assignment attempt four is rejected from another linked worktree before spawn", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-runtime-budget-"));
  const repo = await makeAssignmentRepo(bin);
  const wt = path.join(bin, "worker");
  execFileSync("git", ["-C", repo, "worktree", "add", wt, "-b", "worker"]);
  const common = execFileSync("git", ["-C", repo, "rev-parse", "--git-common-dir"], { encoding: "utf8" }).trim();
  const stateDir = path.join(path.resolve(repo, common), "adaptive-delivery");
  await mkdir(stateDir, { recursive: true });
  await writeFile(path.join(stateDir, "runtime-assignments.json"), JSON.stringify({ schema_version: 1, leases: { a1: {
    assignment_id: "a1", task_id: "T1", agent_id: "writer", provider: "grok-build", session_id: "s1", worktree: wt,
    attempt: 3, lease_id: "a1:attempt:3", last_event_seq: 2, recovery_count: 2, terminal_state: "failed",
  } } }));
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, {}, wt);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", wt,
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack, "--attempt", "4", "--lease-id", "a1:attempt:4",
  ], { encoding: "utf8", input: "bounded", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /recovery budget exhausted/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("same lineage B-01 through B-04 shares the recovery budget before provider spawn", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-lineage-budget-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const env = {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`,
    GROK_HOME: grokHome,
    SPAWN_MARKER: marker,
  };

  const launch = async (assignmentId) => {
    const ack = await assignmentAckFile(bin, { assignment_id: assignmentId }, repo);
    return spawnSync(process.execPath, [adapter,
      "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
      "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo,
      "--assignment-id", assignmentId, "--task-id", "T1", "--agent-id", "writer", "--session-id", `session-${assignmentId}`,
      "--assignment-ack", ack,
    ], { encoding: "utf8", input: "bounded contract", env });
  };

  for (const assignmentId of ["B-01", "B-02", "B-03"]) {
    const result = await launch(assignmentId);
    assert.equal(result.status, 0, result.stderr);
  }
  const common = execFileSync("git", ["-C", repo, "rev-parse", "--git-common-dir"], { encoding: "utf8" }).trim();
  const statePath = path.join(path.resolve(repo, common), "adaptive-delivery", "runtime-assignments.json");
  const before = JSON.parse(await readFile(statePath, "utf8"));

  const blocked = await launch("B-04");
  assert.equal(blocked.status, 1);
  assert.match(blocked.stderr, /recovery budget exhausted/i);
  assert.equal((await readFile(marker, "utf8")).trim().split("\n").length, 3);
  assert.deepEqual(JSON.parse(await readFile(statePath, "utf8")), before);
});

test("Python and Node normalize lineage information separators consistently while preserving ordinary Unicode", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-lineage-normalization-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const env = {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`,
    GROK_HOME: grokHome,
  };

  for (const [assignmentId, primaryGoal] of [
    ["unicode-ordinary", "修复 普通 Unicode 合同"],
    ["unicode-information-separator", "a\u001cb"],
    ["unicode-bom", "a\ufeffb"],
  ]) {
    const ack = await assignmentAckFile(bin, { assignment_id: assignmentId, primary_goal: primaryGoal }, repo);
    const result = spawnSync(process.execPath, [adapter,
      "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
      "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo,
      "--assignment-id", assignmentId, "--task-id", "T1", "--agent-id", "writer", "--session-id", `session-${assignmentId}`,
      "--assignment-ack", ack,
    ], { encoding: "utf8", input: "bounded contract", env });
    assert.equal(result.status, 0, result.stderr);
  }
});
