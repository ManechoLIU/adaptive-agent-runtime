import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { chmod, mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { parseArgs } from "../scripts/run_external_agent.mjs";

const skillRoot = fileURLToPath(new URL("../", import.meta.url));
const adapter = path.join(skillRoot, "scripts", "run_external_agent.mjs");

async function assignmentAckFile(directory, overrides = {}) {
  const branch = execFileSync("git", ["-C", skillRoot, "branch", "--show-current"], { encoding: "utf8" }).trim();
  const head = execFileSync("git", ["-C", skillRoot, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  const assignment = {
    assignment_id: "a1", task_id: "T1", agent_id: "writer", state: "ACKED",
    primary_goal: "finish bounded task", success_criteria: ["green"], owned_scope: ["scripts/run_external_agent.mjs"],
    forbidden_scope: [], parallelizable: true, observed_modified_files: [],
    ack: { repository_root: skillRoot, branch, head, status: "clean", owned_files: ["scripts/run_external_agent.mjs"], first_red: "red", stop_condition: "candidate" },
    ...overrides,
  };
  if (overrides.ack) assignment.ack = { ...assignment.ack, ...overrides.ack };
  const target = path.join(directory, `assignment-${Math.random().toString(36).slice(2)}.json`);
  await writeFile(target, JSON.stringify(assignment));
  return target;
}

async function fakeRunner(bin, name, versionArgument) {
  const target = path.join(bin, name);
  await writeFile(target, `#!/usr/bin/env node
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
  if (process.env.SPAWN_MARKER) require("node:fs").appendFileSync(process.env.SPAWN_MARKER, "spawned\\n");
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
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", skillRoot,
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
  ], { encoding: "utf8", input: "bounded contract", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /assignment-ack/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("assignment-bound execute rejects stale or mismatched ACK before spawn", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-ack-bad-"));
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const badId = await assignmentAckFile(bin, { assignment_id: "other" });
  const badHead = await assignmentAckFile(bin, { ack: { head: "deadbeef" } });
  for (const ack of [badId, badHead]) {
    const result = spawnSync(process.execPath, [adapter,
      "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
      "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", skillRoot,
      "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
      "--assignment-ack", ack,
    ], { encoding: "utf8", input: "bounded contract", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } });
    assert.equal(result.status, 1);
  }
  await assert.rejects(readFile(marker, "utf8"));
});

test("assignment-bound execute spawns only after exact delivered ACK passes", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-ack-good-"));
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", skillRoot,
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: "bounded contract", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } });
  assert.equal(result.status, 0, result.stderr);
  assert.equal((await readFile(marker, "utf8")).trim(), "spawned");
});

test("external execution emits provider-neutral start and terminal runtime receipts", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-runtime-"));
  const grokHome = path.join(bin, "grok-home");
  const receipts = path.join(bin, "receipts.jsonl");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", skillRoot,
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack, "--attempt", "2", "--lease-id", "lease-2", "--runtime-receipts", receipts,
  ], { encoding: "utf8", input: "bounded contract", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome } });
  assert.equal(result.status, 0, result.stderr);
  const events = (await readFile(receipts, "utf8")).trim().split("\n").map(JSON.parse);
  assert.deepEqual(events.map((e) => e.event_type), ["assignment_started", "assignment_terminal"]);
  assert.deepEqual(events.map((e) => e.event_seq), [1, 2]);
  assert.equal(events[0].attempt, 2); assert.equal(events[0].lease_id, "lease-2");
  assert.equal(events[1].outcome, "success"); assert.equal(events[1].terminal_state, "completed");
});
