import assert from "node:assert/strict";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { chmod, copyFile, mkdir, mkdtemp, readFile, realpath, writeFile } from "node:fs/promises";
import os from "node:os";
import crypto from "node:crypto";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { parseArgs, renderExternalAgentCard, resolveDispatchRoute } from "../scripts/run_external_agent.mjs";

const skillRoot = fileURLToPath(new URL("../", import.meta.url));
const adapter = path.join(skillRoot, "scripts", "run_external_agent.mjs");

async function terminateSingleProcessFixture(child) {
  const exited = () => child.exitCode !== null || child.signalCode !== null;
  if (!exited()) {
    try {
      child.kill("SIGKILL");
    } catch (error) {
      if (error?.code !== "ESRCH") {
        return { confirmed: false, diagnostic: `fixture SIGKILL failed: ${error.message}` };
      }
    }
  }
  if (!exited()) {
    await new Promise((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        child.off("exit", finish);
        resolve();
      };
      const timer = setTimeout(finish, 1000);
      child.once("exit", finish);
    });
  }
  return {
    confirmed: exited(),
    diagnostic: exited() ? "fixture child exited" : "fixture child did not exit",
  };
}

async function assignmentAckFile(directory, overrides = {}, repositoryRoot = skillRoot) {
  const branch = execFileSync("git", ["-C", repositoryRoot, "branch", "--show-current"], { encoding: "utf8" }).trim();
  const head = execFileSync("git", ["-C", repositoryRoot, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  const policyPath = path.join(directory, "assignment-route-policy.md");
  const policyText = [
    "backend default provider=grok-build、model=grok-4.6、auth_mode=oauth。",
    "frontend default provider=kimi-code、model=kimi-k3、auth_mode=api。",
    "frontend fallback provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。",
  ].join(String.fromCharCode(10)) + String.fromCharCode(10);
  await writeFile(policyPath, policyText);
  const route = {
    decision: "default",
    policy_class: "backend",
    provider: "grok-build",
    model: "grok-4.6",
    auth_mode: "oauth",
    policy_source: {
      path: policyPath,
      sha256: crypto.createHash("sha256").update(policyText).digest("hex"),
    },
  };
  const assignment = {
    assignment_id: "a1", task_id: "T1", agent_id: "writer", state: "ACKED",
    primary_goal: "finish bounded task", success_criteria: ["green"], owned_scope: ["scripts/run_external_agent.mjs"],
    assignment_contract_version: 2, side_effect: false, idempotency_key: null,
    forbidden_scope: [], parallelizable: true, observed_modified_files: [],
    route,
    ack: { repository_root: repositoryRoot, branch, head, status: "clean", owned_files: ["scripts/run_external_agent.mjs"], first_red: "red", stop_condition: "candidate" },
    ...overrides,
  };
  if (overrides.ack) assignment.ack = { ...assignment.ack, ...overrides.ack };
  const target = path.join(directory, "assignment-" + Math.random().toString(36).slice(2) + ".json");
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

function reviewPacket({ candidateRevision, reviewPhase = "full", diffs = ["git diff --check"] } = {}) {
  return {
    schema_version: 1,
    work_type: "review",
    role: "reviewer",
    review_phase: reviewPhase,
    candidate_revision: candidateRevision,
    scope: ["scripts/run_external_agent.mjs"],
    diffs,
    source_artifacts: [],
    test_evidence: ["node --test tests/external-agent-routing.test.mjs"],
    acceptance_criteria: ["Provider must not inspect the repository outside this packet."],
  };
}

async function fakeInstalledSkill(directory) {
  const root = path.join(directory, "installed-skill");
  const scripts = path.join(root, "scripts");
  await mkdir(scripts, { recursive: true });
  for (const file of ["run_external_agent.mjs", "external_agent_protocol.mjs", "assignment_lease_guard.py", "assignment_runtime.py", "route_contract.py", "project_state.py", "rule_handshake.py", "controller_target_guard.py", "agent_target_resolution.py"]) {
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
  const beforeOutput = Number(process.env.FAKE_RUNNER_DELAY_BEFORE_OUTPUT_MS || 0);
  if (beforeOutput > 0) await new Promise((resolve) => setTimeout(resolve, beforeOutput));
  const promptFileIndex = process.argv.indexOf("--prompt-file");
  const promptFilePath = promptFileIndex >= 0 ? process.argv[promptFileIndex + 1] : null;
  const promptFileMode = promptFilePath ? (fs.statSync(promptFilePath).mode & 0o777) : null;
  if (process.env.FAKE_RUNNER_MODEL_PROGRESS === "1") {
    process.stdout.write(JSON.stringify({ sessionId: "fake", update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "progress" } } }) + "\\n");
  }
  process.stdout.write(JSON.stringify({
    args: process.argv.slice(2),
    promptFileMode,
    cwd: process.cwd(),
    hasKimiApiKey: Boolean(process.env.KIMI_MODEL_API_KEY),
    kimiModelName: process.env.KIMI_MODEL_NAME || null,
    kimiModelProviderType: process.env.KIMI_MODEL_PROVIDER_TYPE || null,
    kimiModelBaseUrl: process.env.KIMI_MODEL_BASE_URL || null,
    kimiThinkingEffort: process.env.KIMI_MODEL_THINKING_EFFORT || null,
    hasXaiApiKey: Boolean(process.env.XAI_API_KEY),
    grokHome: process.env.GROK_HOME || null,
    adaptiveIdempotencyKey: process.env.ADAPTIVE_AGENT_IDEMPOTENCY_KEY || null,
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


test("safe external failures resolve to authorized Codex fallback by task complexity", () => {
  const base = { preferredEngine: "grok-build", category: "backend", failureClass: "provider_unavailable", controllerHost: "desktop_codex" };
  assert.deepEqual(resolveDispatchRoute({ ...base, workType: "mechanical", complexity: "low" }), {
    decision: "fallback", executionRoute: "native-subagent", model: "gpt-5.6-luna", reasoningEffort: "low",
    controllerHost: "desktop_codex", executionHost: "desktop_codex", hostFallbackLevel: 1, reason: "safe_external_failure",
  });
  assert.deepEqual(resolveDispatchRoute({ ...base, workType: "implementation", complexity: "normal" }), {
    decision: "fallback", executionRoute: "native-subagent", model: "gpt-5.6-terra", reasoningEffort: "medium",
    controllerHost: "desktop_codex", executionHost: "desktop_codex", hostFallbackLevel: 1, reason: "safe_external_failure",
  });
  assert.deepEqual(resolveDispatchRoute({ ...base, workType: "root-cause", complexity: "high", highRisk: true }), {
    decision: "fallback", executionRoute: "native-subagent", model: "gpt-5.6-sol", reasoningEffort: "xhigh",
    controllerHost: "desktop_codex", executionHost: "desktop_codex", hostFallbackLevel: 1, reason: "safe_external_failure",
  });
  const frontend = resolveDispatchRoute({ preferredEngine: "kimi-code", category: "frontend", failureClass: "no_valid_result", workType: "review", complexity: "normal", controllerHost: "web" });
  assert.equal(frontend.decision, "fallback");
  assert.equal(frontend.model, "gpt-5.6-terra");
});


test("safe external failure falls back to the current controller host first", () => {
  const web = resolveDispatchRoute({
    preferredEngine: "grok-build", category: "backend", failureClass: "provider_unavailable",
    workType: "implementation", complexity: "normal", controllerHost: "web",
  });
  assert.deepEqual(web, {
    decision: "fallback", executionRoute: "native-subagent", model: "gpt-5.6-terra", reasoningEffort: "medium",
    controllerHost: "web", executionHost: "web", hostFallbackLevel: 1, reason: "safe_external_failure",
  });
  const desktop = resolveDispatchRoute({
    preferredEngine: "grok-build", category: "backend", failureClass: "provider_unavailable",
    workType: "implementation", complexity: "normal", controllerHost: "desktop_codex",
  });
  assert.equal(desktop.controllerHost, "desktop_codex");
  assert.equal(desktop.executionHost, "desktop_codex");
  assert.equal(desktop.hostFallbackLevel, 1);
  assert.equal(desktop.model, "gpt-5.6-terra");
});

test("current-host quota or service exhaustion falls back to the peer host with the same model tier", () => {
  const base = {
    preferredEngine: "grok-build", category: "backend", failureClass: "provider_unavailable",
    workType: "implementation", complexity: "normal", peerHostAvailable: true,
  };
  const webToDesktop = resolveDispatchRoute({ ...base, controllerHost: "web", currentHostFailureClass: "usage_limit_exceeded" });
  assert.deepEqual(webToDesktop, {
    decision: "fallback", executionRoute: "native-subagent", model: "gpt-5.6-terra", reasoningEffort: "medium",
    controllerHost: "web", executionHost: "desktop_codex", hostFallbackLevel: 2,
    reason: "web_internal_usage_limit_exceeded",
  });
  const desktopToWeb = resolveDispatchRoute({ ...base, controllerHost: "desktop_codex", currentHostFailureClass: "quota_exhausted" });
  assert.equal(desktopToWeb.executionHost, "web");
  assert.equal(desktopToWeb.hostFallbackLevel, 2);
  assert.equal(desktopToWeb.model, "gpt-5.6-terra");
  assert.equal(desktopToWeb.reason, "desktop_codex_internal_quota_exhausted");
});

test("cross-host fallback is blocked for unknown execution or unavailable peer adapter", () => {
  const base = {
    preferredEngine: "kimi-code", category: "frontend", failureClass: "no_valid_result",
    workType: "review", complexity: "normal", controllerHost: "web", currentHostFailureClass: "model_unavailable",
  };
  assert.deepEqual(resolveDispatchRoute({ ...base, peerHostAvailable: false }), {
    decision: "blocked", reason: "peer_host_unavailable", controllerHost: "web", requestedPeerHost: "desktop_codex",
  });
  assert.deepEqual(resolveDispatchRoute({ ...base, peerHostAvailable: true, resultUnknown: true }), {
    decision: "blocked", reason: "result_unknown",
  });
  assert.deepEqual(resolveDispatchRoute({ ...base, peerHostAvailable: true, partialWritePossible: true }), {
    decision: "blocked", reason: "partial_write_possible",
  });
});

test("ordinary task failure does not masquerade as host exhaustion", () => {
  const decision = resolveDispatchRoute({
    preferredEngine: "grok-build", category: "backend", failureClass: "provider_unavailable",
    workType: "implementation", complexity: "normal", controllerHost: "web",
    currentHostFailureClass: "test_failed", peerHostAvailable: true,
  });
  assert.deepEqual(decision, { decision: "blocked", reason: "current_host_failure_not_fallback_eligible" });
});

test("unsafe or pinned external failures remain blocked instead of silently falling back", () => {
  const base = { preferredEngine: "grok-build", category: "backend", failureClass: "provider_unavailable", workType: "implementation", complexity: "normal", controllerHost: "web" };
  for (const [field, reason] of [
    ["providerPinned", "provider_pinned"],
    ["resultUnknown", "result_unknown"],
    ["partialWritePossible", "partial_write_possible"],
    ["billingBoundary", "billing_boundary"],
    ["authorizationBoundary", "authorization_boundary"],
  ]) {
    const decision = resolveDispatchRoute({ ...base, [field]: true });
    assert.equal(decision.decision, "blocked");
    assert.equal(decision.reason, reason);
  }
});

test("route decision CLI exposes the same resolver without a model call", () => {
  const output = execFileSync(process.execPath, [adapter,
    "--resolve-route", "--engine", "grok-build", "--category", "backend",
    "--failure-class", "provider_unavailable", "--work-type", "implementation", "--complexity", "normal",
    "--controller-host", "web",
  ], { encoding: "utf8" });
  assert.deepEqual(JSON.parse(output), {
    decision: "fallback", executionRoute: "native-subagent", model: "gpt-5.6-terra", reasoningEffort: "medium",
    controllerHost: "web", executionHost: "web", hostFallbackLevel: 1, reason: "safe_external_failure",
  });
});

test("route decision CLI exposes peer-host fallback only after an eligible local-host failure", () => {
  const output = execFileSync(process.execPath, [adapter,
    "--resolve-route", "--engine", "grok-build", "--category", "backend",
    "--failure-class", "provider_unavailable", "--work-type", "implementation", "--complexity", "normal",
    "--controller-host", "web", "--current-host-failure-class", "usage_limit_exceeded", "--peer-host-available",
  ], { encoding: "utf8" });
  assert.deepEqual(JSON.parse(output), {
    decision: "fallback", executionRoute: "native-subagent", model: "gpt-5.6-terra", reasoningEffort: "medium",
    controllerHost: "web", executionHost: "desktop_codex", hostFallbackLevel: 2, reason: "web_internal_usage_limit_exceeded",
  });
});

test("non-safe failure classes do not trigger automatic fallback", () => {
  const decision = resolveDispatchRoute({
    preferredEngine: "kimi-code", category: "frontend", failureClass: "delivery_failed",
    workType: "implementation", complexity: "normal", controllerHost: "web",
  });
  assert.deepEqual(decision, { decision: "blocked", reason: "failure_not_safe_for_fallback" });
});

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
  assert.equal(kimiApiCall.kimiModelProviderType, "openai");
  assert.equal(kimiApiCall.kimiModelBaseUrl, "https://api.moonshot.cn/v1");
  assert.equal(kimiApiCall.kimiThinkingEffort, "max");

  const grokOauth = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "xhigh", "--cwd", skillRoot, "--work-type", "implementation",
  ], { encoding: "utf8", input: "bounded contract", env: { ...baseEnv, XAI_API_KEY: "must-be-removed" } });
  assert.equal(grokOauth.status, 0, grokOauth.stderr);
  const grokOauthCall = JSON.parse(grokOauth.stdout.trim());
  assert.equal(grokOauthCall.hasXaiApiKey, false);
  assert.equal(grokOauthCall.grokHome, grokHome);
  assert.deepEqual(grokOauthCall.args.slice(-2), ["--reasoning-effort", "xhigh"]);

  const grokApi = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "api",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", skillRoot, "--work-type", "implementation",
  ], { encoding: "utf8", input: "bounded contract", env: { ...baseEnv, XAI_API_KEY: "test-key" } });
  assert.equal(grokApi.status, 0, grokApi.stderr);
  const grokApiCall = JSON.parse(grokApi.stdout.trim());
  assert.equal(grokApiCall.hasXaiApiKey, true);
  assert.notEqual(grokApiCall.grokHome, grokHome);
  assert.match(grokApiCall.grokHome, /adaptive-delivery-grok-api-/);
  assert.deepEqual(grokApiCall.args.slice(-2), ["--reasoning-effort", "low"]);
});

test("heterogeneous frontend and backend tasks stay on Kimi and Grok canonical executors", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-heterogeneous-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const kimiMarker = path.join(bin, "kimi-spawned.txt");
  const grokMarker = path.join(bin, "grok-spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "kimi", "--version");
  await fakeRunner(bin, "grok", "version");

  const baseEnv = {
    ...process.env,
    PATH: [bin, process.env.PATH || ""].join(path.delimiter),
    GROK_HOME: grokHome,
    KIMI_K3_KEYCHAIN_SERVICE: "adaptive-test-kimi-" + path.basename(bin),
    XAI_GROK_KEYCHAIN_SERVICE: "adaptive-test-xai-" + path.basename(bin),
  };

  const frontend = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call",
    "--engine", "kimi-code", "--auth-mode", "api",
    "--model", "kimi-k3", "--reasoning-effort", "medium",
    "--cwd", repo,
  ], {
    encoding: "utf8",
    input: "frontend bounded task",
    env: { ...baseEnv, MOONSHOT_API_KEY: "test-key", SPAWN_MARKER: kimiMarker },
  });
  assert.equal(frontend.status, 0, frontend.stderr);
  const frontendCall = JSON.parse(frontend.stdout.trim());
  assert.equal(frontendCall.kimiModelName, "kimi-k3");
  assert.equal(frontendCall.kimiModelProviderType, "openai");
  assert.equal(frontendCall.hasKimiApiKey, true);
  assert.equal(frontendCall.hasXaiApiKey, false);
  assert.equal(frontendCall.kimiThinkingEffort, "medium");

  const backend = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call",
    "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "high",
    "--cwd", repo, "--work-type", "implementation",
  ], {
    encoding: "utf8",
    input: "backend bounded task",
    env: { ...baseEnv, XAI_API_KEY: "must-be-removed", SPAWN_MARKER: grokMarker },
  });
  assert.equal(backend.status, 0, backend.stderr);
  const backendCall = JSON.parse(backend.stdout.trim());
  assert.equal(backendCall.hasXaiApiKey, false);
  assert.equal(backendCall.grokHome, grokHome);
  assert.deepEqual(backendCall.args.slice(-2), ["--reasoning-effort", "high"]);
  assert.equal(backendCall.kimiModelName, null);

  assert.equal((await readFile(kimiMarker, "utf8")).trim(), "spawned");
  assert.equal((await readFile(grokMarker, "utf8")).trim(), "spawned");
  assert.doesNotMatch(frontend.stdout + backend.stdout, /chatgpt_web|web-agent-execution/i);
});

test("Grok execute fails closed before provider spawn without an explicit work type or a complete immutable review packet", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-work-type-gate-"));
  const repo = await makeAssignmentRepo(bin);
  const installed = await fakeInstalledSkill(bin);
  const manifestPath = path.join(installed.root, ".adaptive-delivery-install.json");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  manifest.impact = "test_only";
  await writeFile(manifestPath, JSON.stringify(manifest));
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const head = execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  const reviewerAck = await assignmentAckFile(bin, {
    assignment_id: "reviewer", task_id: "reviewer", agent_id: "reviewer", role: "reviewer",
    candidate_revision: head, reviewer_for_revision: head, review_phase: "full",
  }, repo);
  const mismatchAck = await assignmentAckFile(bin, {
    assignment_id: "candidate-mismatch", task_id: "candidate-mismatch", agent_id: "reviewer", role: "reviewer",
    candidate_revision: head, reviewer_for_revision: head, review_phase: "full",
  }, repo);
  const incompleteAck = await assignmentAckFile(bin, {
    assignment_id: "incomplete-packet", task_id: "incomplete-packet", agent_id: "reviewer", role: "reviewer",
    candidate_revision: head, reviewer_for_revision: head, review_phase: "full",
  }, repo);
  const reviewerImplementationAck = await assignmentAckFile(bin, {
    assignment_id: "reviewer-implementation", task_id: "reviewer-implementation", agent_id: "reviewer", role: "reviewer",
    candidate_revision: head, reviewer_for_revision: head, review_phase: "full",
  }, repo);
  const sideEffectReviewAck = await assignmentAckFile(bin, {
    assignment_id: "review-side-effect", task_id: "review-side-effect", agent_id: "reviewer", role: "reviewer",
    candidate_revision: head, reviewer_for_revision: head, review_phase: "full",
    side_effect: true, idempotency_key: "review:must-not-run",
  }, repo);
  const shardAck = await assignmentAckFile(bin, {
    assignment_id: "shard", task_id: "shard", agent_id: "reviewer", role: "reviewer",
    candidate_revision: head, reviewer_for_revision: head, review_phase: "shard",
  }, repo);
  const missingCandidateAck = await assignmentAckFile(bin, {
    assignment_id: "missing-candidate", task_id: "missing-candidate", agent_id: "reviewer", role: "reviewer",
    candidate_revision: undefined, reviewer_for_revision: undefined, review_phase: "full",
  }, repo);
  const writerAck = await assignmentAckFile(bin, { assignment_id: "writer-review", role: "writer" }, repo);
  const base = ["--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth", "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo];
  const env = { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker };
  const cases = [
    { name: "missing work type", args: base, input: "ordinary request", error: /work-type/i },
    { name: "unknown work type", args: [...base, "--work-type", "investigate"], input: "ordinary request", error: /unsupported Grok work_type/i },
    { name: "unbound review", args: [...base, "--work-type", "review"], input: JSON.stringify(reviewPacket({ candidateRevision: head })), error: /assignment-bound reviewer/i },
    { name: "writer review", args: [...base, "--work-type", "review", "--assignment-id", "writer-review", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1", "--assignment-ack", writerAck], input: JSON.stringify(reviewPacket({ candidateRevision: head })), error: /reviewer role/i },
    { name: "reviewer implementation", args: [...base, "--work-type", "implementation", "--assignment-id", "reviewer-implementation", "--task-id", "reviewer-implementation", "--agent-id", "reviewer", "--session-id", "s1", "--assignment-ack", reviewerImplementationAck], input: "ordinary request", error: /reviewer role requires work_type=review/i },
    { name: "side-effect review", args: [...base, "--work-type", "review", "--assignment-id", "review-side-effect", "--task-id", "review-side-effect", "--agent-id", "reviewer", "--session-id", "s1", "--assignment-ack", sideEffectReviewAck], input: JSON.stringify(reviewPacket({ candidateRevision: head })), error: /review must be read-only/i },
    { name: "shard review", args: [...base, "--work-type", "review", "--assignment-id", "shard", "--task-id", "shard", "--agent-id", "reviewer", "--session-id", "s1", "--assignment-ack", shardAck], input: JSON.stringify(reviewPacket({ candidateRevision: head, reviewPhase: "shard" })), error: /full\|synthesis/i },
    { name: "missing immutable candidate", args: [...base, "--work-type", "review", "--assignment-id", "missing-candidate", "--task-id", "missing-candidate", "--agent-id", "reviewer", "--session-id", "s1", "--assignment-ack", missingCandidateAck], input: JSON.stringify(reviewPacket({ candidateRevision: head })), error: /candidate_revision/i },
    { name: "unstructured stdin", args: [...base, "--work-type", "review", "--assignment-id", "reviewer", "--task-id", "reviewer", "--agent-id", "reviewer", "--session-id", "s1", "--assignment-ack", reviewerAck], input: "not a JSON packet", error: /structured JSON review packet/i },
    { name: "candidate mismatch", args: [...base, "--work-type", "review", "--assignment-id", "candidate-mismatch", "--task-id", "candidate-mismatch", "--agent-id", "reviewer", "--session-id", "s1", "--assignment-ack", mismatchAck], input: JSON.stringify(reviewPacket({ candidateRevision: "0".repeat(40) })), error: /candidate_revision.*immutable Assignment/i },
    { name: "incomplete packet", args: [...base, "--work-type", "review", "--assignment-id", "incomplete-packet", "--task-id", "incomplete-packet", "--agent-id", "reviewer", "--session-id", "s1", "--assignment-ack", incompleteAck], input: JSON.stringify({ schema_version: 1, work_type: "review", role: "reviewer", review_phase: "full", candidate_revision: head }), error: /scope.*non-empty string array/i },
  ];
  for (const scenario of cases) {
    const result = spawnSync(process.execPath, [installed.adapter, ...scenario.args], { encoding: "utf8", input: scenario.input, env });
    assert.equal(result.status, 1, `${scenario.name}: ${result.stderr}`);
    assert.match(result.stderr, scenario.error, scenario.name);
  }
  await assert.rejects(readFile(marker, "utf8"));
});

test("external execute persists terminal receipt and invokes controller continuation helper", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-terminal-continuation-"));
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const terminalReceipt = path.join(bin, "terminal.json");
  const resultPath = path.join(bin, "review-output.log");
  const helperMarker = path.join(bin, "helper-args.json");
  await writeFile(resultPath, "review verdict: FINDINGS\n");
  const helper = path.join(bin, "continuation-helper.py");
  await writeFile(helper, `#!/usr/bin/env python3
import json, os, sys
open(os.environ["HELPER_MARKER"], "w", encoding="utf-8").write(json.dumps(sys.argv[1:]))
`);
  await chmod(helper, 0o755);

  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "high", "--cwd", skillRoot, "--work-type", "implementation",
    "--terminal-receipt", terminalReceipt, "--result-path", resultPath,
  ], { encoding: "utf8", input: "bounded review", env: {
    ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome,
    AD_TERMINAL_CONTINUATION_HELPER: helper, HELPER_MARKER: helperMarker,
  } });

  assert.equal(result.status, 0, result.stderr);
  const receipt = JSON.parse(await readFile(terminalReceipt, "utf8"));
  assert.equal(receipt.event_type, "external_agent_terminal");
  assert.equal(receipt.engine, "grok-build");
  assert.equal(receipt.exit_code, 0);
  assert.equal(receipt.result_path, resultPath);
  const helperArgs = JSON.parse(await readFile(helperMarker, "utf8"));
  assert.deepEqual(helperArgs, ["consume", "--repo", skillRoot, "--receipt", terminalReceipt]);
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
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
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
      "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
      "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
      "--assignment-ack", ack,
    ], { encoding: "utf8", input: "bounded contract", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } });
    assert.equal(result.status, 1);
  }
  await assert.rejects(readFile(marker, "utf8"));
});

test("fresh legacy v1 assignment ACK cannot launch external provider", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-legacy-ack-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, { assignment_contract_version: undefined, side_effect: undefined, idempotency_key: undefined }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: "bounded", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome } });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /v2 canonical route contract|assignment-bound external launch/i);
});

test("v2 side-effect execution propagates stable idempotency key to provider contract", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-idempotency-propagation-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, { side_effect: true, idempotency_key: "publish:release-42" }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: "publish once", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome } });
  assert.equal(result.status, 0, result.stderr);
  const payload = JSON.parse(result.stdout.trim().split("\n").find((line) => line.startsWith("{")));
  assert.equal(payload.adaptiveIdempotencyKey, "publish:release-42");
  assert.doesNotMatch(payload.args.join(" "), /publish:release-42/);
  assert.ok(payload.args.includes("--prompt-file"));
});

test("side-effect PASS propagates provider reconciliation evidence into terminal runtime receipt", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-side-effect-reconciliation-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const receipts = path.join(bin, "receipts.jsonl");
  const deliveryPath = path.join(bin, "delivery.json");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const key = "publish:release-42";
  const ack = await assignmentAckFile(bin, { side_effect: true, idempotency_key: key }, repo);
  await writeFile(deliveryPath, JSON.stringify({
    delivery_outcome: "pass", summary: "provider confirmed publish",
    evidence: [`receipt:provider/${key}`], artifacts: ["artifact:publish-1"],
    next_action: "done", retry_class: "none",
    reconciliation_evidence: [{ provider: "grok-build", resource: "release-42", locator: "receipt:provider/grok-build/release-42", idempotency_key: key }],
  }));
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack, "--runtime-receipts", receipts, "--delivery-receipt", deliveryPath,
  ], { encoding: "utf8", input: "publish once", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome } });
  assert.equal(result.status, 0, result.stderr);
  const events = (await readFile(receipts, "utf8")).trim().split("\n").map(JSON.parse);
  assert.deepEqual(events.at(-1).reconciliation_evidence, [{ provider: "grok-build", resource: "release-42", locator: "receipt:provider/grok-build/release-42", idempotency_key: key }]);
  assert.equal(events.at(-1).result_unknown, false);
});

test("assignment-bound execute rejects missing side-effect contract before spawn", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-side-effect-missing-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, { side_effect: undefined }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: "bounded contract", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /side_effect contract/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("assignment-bound execute rejects CLI route mismatch before provider spawn", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-route-mismatch-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, {
    route: {
      decision: "default",
      policy_class: "frontend",
      provider: "kimi-code",
      model: "kimi-k3",
      auth_mode: "api",
      policy_source: JSON.parse(await readFile(await assignmentAckFile(bin, {}, repo), "utf8")).route.policy_source,
    },
  }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: "bounded", env: { ...process.env, PATH: [bin, process.env.PATH || ""].join(path.delimiter), GROK_HOME: grokHome, SPAWN_MARKER: marker } });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /route|provider|model|auth/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("assignment-bound safe fallback requires canonical prior terminal before provider spawn", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-safe-fallback-proof-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const baseAckPath = await assignmentAckFile(bin, {}, repo);
  const baseAck = JSON.parse(await readFile(baseAckPath, "utf8"));
  const ack = await assignmentAckFile(bin, {
    route: {
      decision: "safe_fallback",
      policy_class: "backend",
      provider: "grok-build",
      model: "grok-4.6",
      auth_mode: "oauth",
      policy_source: baseAck.route.policy_source,
      fallback_from: { provider: "kimi-code", model: "kimi-k3", auth_mode: "api" },
      prior_assignment_id: "missing-prior",
      failure_evidence: "receipt:kimi/missing-prior",
      prior_attempt_terminal: true,
      result_unknown: false,
    },
  }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: "bounded", env: { ...process.env, PATH: [bin, process.env.PATH || ""].join(path.delimiter), GROK_HOME: grokHome, SPAWN_MARKER: marker } });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /canonical|prior|fallback|runtime/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("assignment-bound external start persists exact canonical route contract", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-route-runtime-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, { assignment_id: "route-runtime-a1" }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "route-runtime-a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "route-runtime-s1",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: "bounded", env: { ...process.env, PATH: [bin, process.env.PATH || ""].join(path.delimiter), GROK_HOME: grokHome } });
  assert.equal(result.status, 0, result.stderr);
  const common = execFileSync("git", ["-C", repo, "rev-parse", "--git-common-dir"], { encoding: "utf8" }).trim();
  const state = JSON.parse(await readFile(path.join(path.resolve(repo, common), "adaptive-delivery", "runtime-assignments.json"), "utf8"));
  const lease = state.leases["route-runtime-a1"];
  assert.equal(lease.provider, "grok-build");
  assert.equal(lease.model, "grok-4.6");
  assert.equal(lease.auth_mode, "oauth");
  assert.equal(lease.route_decision, "default");
  assert.equal(lease.route_contract.provider, "grok-build");
  assert.equal(lease.route_contract.model, "grok-4.6");
  assert.equal(lease.route_contract.auth_mode, "oauth");
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
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
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
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
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

  const run = async ({ assignmentId, exitCode = 0, deliveryReceipt, rawDeliveryReceipt, primaryGoal = "finish bounded task" }) => {
    const receipts = path.join(bin, `${assignmentId}.jsonl`);
    const deliveryPath = path.join(bin, `${assignmentId}-delivery.json`);
    const args = [adapter,
      "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
      "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
      "--assignment-id", assignmentId, "--task-id", "T1", "--agent-id", "writer", "--session-id", `session-${assignmentId}`,
      "--assignment-ack", await assignmentAckFile(bin, { assignment_id: assignmentId, primary_goal: primaryGoal }, repo),
      "--runtime-receipts", receipts,
    ];
    if (deliveryReceipt || rawDeliveryReceipt !== undefined) {
      await writeFile(deliveryPath, rawDeliveryReceipt !== undefined ? rawDeliveryReceipt : JSON.stringify(deliveryReceipt));
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

  const malformedDelivery = await run({
    assignmentId: "malformed-delivery",
    primaryGoal: "close runtime after malformed delivery receipt",
    rawDeliveryReceipt: "{not-json",
  });
  assert.equal(malformedDelivery.result.status, 1, malformedDelivery.result.stderr);
  assert.equal(malformedDelivery.terminal.transport_outcome, "completed");
  assert.equal(malformedDelivery.terminal.delivery_outcome, "unresolved");
  assert.match(malformedDelivery.terminal.summary, /delivery-receipt is unreadable/);

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



test("reviewer delivery receipt persists structured review verdict", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-review-verdict-"));
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-verdict=${Date.now()}`);
  const deliveryPath = path.join(bin, "delivery.json");
  const head = "a".repeat(40);
  const verdict = { reviewed_head: head, verdict: "PASS", critical: [], important: [], minor: [] };
  await writeFile(deliveryPath, JSON.stringify({
    delivery_outcome: "pass", summary: "review passed", evidence: [`git:${head}`], artifacts: [`git:${head}`],
    next_action: "integrate candidate", retry_class: "none", review_verdict: verdict,
  }));
  const receipt = runtimeModule.readDeliveryReceipt(deliveryPath, {
    assignmentRole: "reviewer", candidateRevision: head, reviewPhase: "full",
  });
  assert.deepEqual(receipt.review_verdict, verdict);
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
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
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
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
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
  assert.ok(progress.last_observed_head);
  assert.ok(progress.last_observed_status_sha256);
  assert.ok(progress.progress_evidence);
  assert.ok(Array.isArray(progress.progress_evidence.changed_fields));
  assert.ok(progress.progress_evidence.changed_fields.includes("last_observed_status_sha256"));
  assert.equal(progress.progress_evidence.last_observed_status_sha256, progress.last_observed_status_sha256);
  assert.notEqual(progress.last_progress_phase, "GREEN");
  assert.notEqual(progress.last_progress_phase, "RED");
  JSON.stringify(progress.progress_evidence);
  assert.equal(events.at(-1).event_type, "assignment_terminal");
});

test("short assignment-bound execution reconciles final Git progress before terminal", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-final-progress-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const receipts = path.join(bin, "receipts.jsonl");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "final-progress-a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", await assignmentAckFile(bin, { assignment_id: "final-progress-a1" }, repo),
    "--attempt", "1", "--lease-id", "final-progress-lease-1", "--runtime-receipts", receipts,
  ], { encoding: "utf8", input: "bounded contract", env: {
    ...process.env,
    PATH: [bin, process.env.PATH || ""].join(path.delimiter),
    GROK_HOME: grokHome,
    FAKE_RUNNER_TOUCH_FILE: path.join(repo, "TASK_LEDGER.md"),
    FAKE_RUNNER_DELAY_MS: "0",
    AD_RUNTIME_HEARTBEAT_MS: "1000",
  } });
  assert.equal(result.status, 0, result.stderr);
  const events = (await readFile(receipts, "utf8")).trim().split(String.fromCharCode(10)).map(JSON.parse);
  const progressIndex = events.findIndex((event) => event.event_type === "assignment_progress");
  const terminalIndex = events.findIndex((event) => event.event_type === "assignment_terminal");
  assert.ok(progressIndex >= 0, JSON.stringify(events));
  assert.ok(terminalIndex > progressIndex, JSON.stringify(events));
  assert.ok(events[progressIndex].progress_evidence.changed_fields.includes("last_observed_status_sha256"));
});

test("assignment-bound start carries a ten-minute implementation progress budget", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-progress-budget-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const receipts = path.join(bin, "receipts.jsonl");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "budget-a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", await assignmentAckFile(bin, { assignment_id: "budget-a1" }, repo),
    "--attempt", "1", "--lease-id", "budget-lease-1", "--runtime-receipts", receipts,
  ], { encoding: "utf8", input: "bounded contract", env: {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`,
    GROK_HOME: grokHome,
  } });
  assert.equal(result.status, 0, result.stderr);
  const events = (await readFile(receipts, "utf8")).trim().split("\n").map(JSON.parse);
  assert.equal(events[0].event_type, "assignment_started");
  assert.equal(events[0].progress_deadline_minutes, 10);
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
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "canonical-a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", await assignmentAckFile(bin, { assignment_id: "canonical-a1" }, repo),
  ], { encoding: "utf8", input: "bounded", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome } });
  assert.equal(result.status, 0, result.stderr);
  const common = execFileSync("git", ["-C", repo, "rev-parse", "--git-common-dir"], { encoding: "utf8" }).trim();
  const statePath = path.join(path.resolve(repo, common), "adaptive-delivery", "runtime-assignments.json");
  const state = JSON.parse(await readFile(statePath, "utf8"));
  assert.equal(state.leases["canonical-a1"].terminal_state, "completed");
});


test("synthetic candidate workspace binds runtime to explicit canonical repo", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-runtime-candidate-binding-"));
  const canonicalRepo = await makeAssignmentRepo(bin);
  const candidateRepo = await makeAssignmentRepo(bin);
  const candidateGitDir = path.join(candidateRepo, ".git-candidate");
  execFileSync("mv", [path.join(candidateRepo, ".git"), candidateGitDir]);
  await writeFile(path.join(candidateRepo, ".git"), `gitdir: ${candidateGitDir}\n`);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, { assignment_id: "candidate-review-a1", agent_id: "reviewer" }, candidateRepo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", candidateRepo, "--work-type", "implementation",
    "--runtime-repo", canonicalRepo,
    "--assignment-id", "candidate-review-a1", "--task-id", "T1", "--agent-id", "reviewer", "--session-id", "s1",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: "review bounded candidate", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome } });
  assert.equal(result.status, 0, result.stderr);
  const canonicalCommon = execFileSync("git", ["-C", canonicalRepo, "rev-parse", "--git-common-dir"], { encoding: "utf8" }).trim();
  const canonicalState = JSON.parse(await readFile(path.join(path.resolve(canonicalRepo, canonicalCommon), "adaptive-delivery", "runtime-assignments.json"), "utf8"));
  assert.equal(canonicalState.leases["candidate-review-a1"].terminal_state, "completed");
  await assert.rejects(readFile(path.join(candidateGitDir, "adaptive-delivery", "runtime-assignments.json"), "utf8"));
});

test("assignment-bound synthetic candidate workspace fails closed without runtime repo", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-runtime-candidate-no-binding-"));
  const candidateRepo = await makeAssignmentRepo(bin);
  const candidateGitDir = path.join(candidateRepo, ".git-candidate");
  execFileSync("mv", [path.join(candidateRepo, ".git"), candidateGitDir]);
  await writeFile(path.join(candidateRepo, ".git"), `gitdir: ${candidateGitDir}\n`);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, { assignment_id: "candidate-review-a2", agent_id: "reviewer" }, candidateRepo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", candidateRepo, "--work-type", "implementation",
    "--assignment-id", "candidate-review-a2", "--task-id", "T1", "--agent-id", "reviewer", "--session-id", "s2",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: "review bounded candidate", env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /runtime repo.*required|canonical runtime/i);
  await assert.rejects(readFile(marker, "utf8"));
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
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
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
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", wt, "--work-type", "implementation",
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
      "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
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
      "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
      "--assignment-id", assignmentId, "--task-id", "T1", "--agent-id", "writer", "--session-id", `session-${assignmentId}`,
      "--assignment-ack", ack,
    ], { encoding: "utf8", input: "bounded contract", env });
    assert.equal(result.status, 0, result.stderr);
  }
});

test("assignment-bound external terminal receipt carries current attempt and lease identity", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-routing-terminal-identity-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, { assignment_id: "a-terminal" }, repo);
  const terminalReceipt = path.join(bin, "terminal-assignment.json");
  const helper = path.join(bin, "continuation-helper.py");
  await writeFile(helper, "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n");
  await chmod(helper, 0o755);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "a-terminal", "--task-id", "T1", "--agent-id", "writer", "--session-id", "s1",
    "--assignment-ack", ack, "--attempt", "1", "--lease-id", "lease-terminal-1",
    "--terminal-receipt", terminalReceipt,
  ], { encoding: "utf8", input: "bounded contract", env: {
    ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome,
    AD_TERMINAL_CONTINUATION_HELPER: helper,
  } });
  assert.equal(result.status, 0, result.stderr);
  const receipt = JSON.parse(await readFile(terminalReceipt, "utf8"));
  assert.equal(receipt.assignment_id, "a-terminal");
  assert.equal(receipt.attempt, 1);
  assert.equal(receipt.lease_id, "lease-terminal-1");
});

test("Grok execution transports prompts through a private prompt file and removes it", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-prompt-file-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const prompt = "bounded prompt must never be exposed in argv";
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "medium", "--cwd", repo, "--work-type", "implementation",
  ], { encoding: "utf8", input: prompt, env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome } });
  assert.equal(result.status, 0, result.stderr);
  const call = JSON.parse(result.stdout.trim());
  const promptFlag = call.args.indexOf("--prompt-file");
  assert.notEqual(promptFlag, -1);
  assert.equal(call.args.includes("-p"), false);
  assert.equal(call.args.some((arg) => arg.includes(prompt)), false);
  const promptPath = call.args[promptFlag + 1];
  await assert.rejects(readFile(promptPath, "utf8"));
});

test("oversized Grok reviewer prompt fails before provider spawn with sharding evidence", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-review-shard-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  const runtimeReceipts = path.join(bin, "runtime-receipts.jsonl");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const candidateHead = execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  const ack = await assignmentAckFile(bin, { role: "reviewer", agent_id: "reviewer", candidate_revision: candidateHead, reviewer_for_revision: candidateHead, review_phase: "full" }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "high", "--cwd", repo, "--work-type", "review",
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "reviewer", "--session-id", "review-s1",
    "--assignment-ack", ack, "--runtime-receipts", runtimeReceipts,
  ], {
    encoding: "utf8",
    input: JSON.stringify(reviewPacket({ candidateRevision: candidateHead, diffs: ["X".repeat(256)] })),
    env: {
      ...process.env,
      PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`,
      GROK_HOME: grokHome,
      SPAWN_MARKER: marker,
      AD_GROK_MAX_PROMPT_BYTES: "128",
      AD_GROK_REVIEW_SHARD_TARGET_BYTES: "64",
    },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /review_sharding_required/i);
  assert.match(result.stderr, /observed_bytes=\d+/i);
  assert.match(result.stderr, /max_bytes=128/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("Grok launch deadline classifies cli_launch_timeout and terminates an unconfirmed process group", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?launch-timeout=${Date.now()}`);
  const { EventEmitter } = await import("node:events");
  const { PassThrough } = await import("node:stream");
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.pid = 424242;
  child.exitCode = null;
  child.signalCode = null;
  let cleanupCalled = false;
  const previous = {
    launch: process.env.AD_GROK_LAUNCH_TIMEOUT_MS,
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_LAUNCH_TIMEOUT_MS = "30";
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "500";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "500";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1000";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "20";
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", "process.exit(0)"], {
        cwd: os.tmpdir(), env: process.env,
        spawnChild: () => child,
        terminateGroup: async (observedChild) => {
          assert.equal(observedChild, child);
          cleanupCalled = true;
          child.exitCode = 1;
          return { confirmed: true, diagnostic: "fake launch-timeout group reaped" };
        },
      }),
      (error) => error?.failureClass === "cli_launch_timeout" && error?.retrySafe === true && error?.resultUnknown === false,
    );
    assert.equal(cleanupCalled, true);
  } finally {
    const names = {
      launch: "AD_GROK_LAUNCH_TIMEOUT_MS", first: "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS",
      stall: "AD_GROK_STALL_TIMEOUT_MS", absolute: "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS", grace: "AD_EXTERNAL_KILL_GRACE_MS",
    };
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[names[key]]; else process.env[names[key]] = value;
    }
  }
});

test("Grok post-launch child error preserves provider boundary evidence", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?post-launch-error=${Date.now()}`);
  const { EventEmitter } = await import("node:events");
  const { PassThrough } = await import("node:stream");
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.pid = 424244;
  child.exitCode = null;
  child.signalCode = null;
  const previous = {
    launch: process.env.AD_GROK_LAUNCH_TIMEOUT_MS,
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
  };
  process.env.AD_GROK_LAUNCH_TIMEOUT_MS = "500";
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "500";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "500";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1000";
  const spawnTimer = setTimeout(() => {
    child.emit("spawn");
    setTimeout(() => child.emit("error", new Error("post-launch transport error")), 10);
  }, 10);
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, [], {
        cwd: os.tmpdir(), env: process.env, spawnChild: () => child,
      }),
      (error) => error?.failureClass === "cli_launch_failed" && error?.details?.provider_started === true,
    );
  } finally {
    clearTimeout(spawnTimer);
    const names = {
      launch: "AD_GROK_LAUNCH_TIMEOUT_MS", first: "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS",
      stall: "AD_GROK_STALL_TIMEOUT_MS", absolute: "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS",
    };
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[names[key]]; else process.env[names[key]] = value;
    }
  }
});

test("Grok first-output deadline starts after launch confirmation", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?first-after-launch=${Date.now()}`);
  const { EventEmitter } = await import("node:events");
  const { PassThrough } = await import("node:stream");
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.pid = 424243;
  child.exitCode = null;
  child.signalCode = null;
  const previous = {
    launch: process.env.AD_GROK_LAUNCH_TIMEOUT_MS,
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_LAUNCH_TIMEOUT_MS = "250";
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "70";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "500";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1000";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "20";
  const started = Date.now();
  const spawnTimer = setTimeout(() => child.emit("spawn"), 90);
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", "process.exit(0)"], {
        cwd: os.tmpdir(), env: process.env,
        spawnChild: () => child,
        terminateGroup: async () => {
          child.exitCode = 1;
          return { confirmed: true, diagnostic: "fake first-output group reaped" };
        },
      }),
      (error) => error?.failureClass === "first_output_timeout",
    );
    assert.ok(Date.now() - started >= 135, `first-output clock started before launch: ${Date.now() - started}ms`);
  } finally {
    clearTimeout(spawnTimer);
    const names = {
      launch: "AD_GROK_LAUNCH_TIMEOUT_MS", first: "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS",
      stall: "AD_GROK_STALL_TIMEOUT_MS", absolute: "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS", grace: "AD_EXTERNAL_KILL_GRACE_MS",
    };
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[names[key]]; else process.env[names[key]] = value;
    }
  }
});

test("Grok first-output timeout terminates a silent provider attempt", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-first-output-timeout-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const started = Date.now();
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
  ], {
    encoding: "utf8", input: "bounded",
    env: {
      ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome,
      FAKE_RUNNER_DELAY_BEFORE_OUTPUT_MS: "1000",
      AD_GROK_FIRST_OUTPUT_TIMEOUT_MS: "50", AD_GROK_STALL_TIMEOUT_MS: "500",
      AD_EXTERNAL_ATTEMPT_TIMEOUT_MS: "1000", AD_EXTERNAL_KILL_GRACE_MS: "25",
    },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /first_output_timeout/i);
  assert.ok(Date.now() - started < 900, `timeout took ${Date.now() - started}ms`);
});

test("Grok invalid cwd is LOCAL_PRECHECK_FAILED and never spawns the provider", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-local-precheck-"));
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", path.join(bin, "missing"), "--work-type", "implementation",
  ], {
    encoding: "utf8", input: "bounded",
    env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /LOCAL_PRECHECK_FAILED/);
  await assert.rejects(readFile(marker, "utf8"));
});

test("Grok unclosed stdin times out before provider spawn with LOCAL_PRECHECK_FAILED", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-input-timeout-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const result = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [adapter,
      "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
      "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    ], {
      stdio: ["pipe", "pipe", "pipe"],
      env: {
        ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome,
        SPAWN_MARKER: marker, AD_EXTERNAL_INPUT_TIMEOUT_MS: "30",
      },
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error("bounded input fixture did not terminate"));
    }, 2_000);
    child.stdout.on("data", (chunk) => { stdout += chunk.toString("utf8"); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString("utf8"); });
    child.once("error", reject);
    child.once("close", (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, stdout, stderr });
    });
  });
  assert.equal(result.signal, null);
  assert.equal(result.code, 1);
  assert.match(result.stderr, /LOCAL_PRECHECK_FAILED|input did not close/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("Grok generation stall timeout terminates after structured output stops", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-stall-timeout-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
  ], {
    encoding: "utf8", input: "bounded",
    env: {
      ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome,
      FAKE_RUNNER_MODEL_PROGRESS: "1", FAKE_RUNNER_DELAY_MS: "1000",
      AD_GROK_FIRST_OUTPUT_TIMEOUT_MS: "1000", AD_GROK_STALL_TIMEOUT_MS: "50",
      AD_EXTERNAL_ATTEMPT_TIMEOUT_MS: "2000", AD_EXTERNAL_KILL_GRACE_MS: "25",
    },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /generation_stalled/i);
});

test("Grok normal leader exit reaps surviving process-group descendants before success", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-leader-exit-descendant-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const descendantMarker = path.join(bin, "descendant-survived-after-leader-exit.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  const runner = path.join(bin, "grok");
  await writeFile(runner, `#!/usr/bin/env node
import { spawn } from "node:child_process";
if (process.argv[2] === "version") {
  process.stdout.write("grok test\\n");
} else {
  spawn(process.execPath, ["-e", ${JSON.stringify(`setTimeout(() => require('fs').writeFileSync(${JSON.stringify(descendantMarker)}, 'survived'), 300); setTimeout(() => {}, 5000);`)}], { stdio: "ignore" }).unref();
  process.stdout.write(JSON.stringify({type:"text",data:"done"}) + "\\n");
  await new Promise((resolve) => setTimeout(resolve, 30));
  process.exit(0);
}
`);
  await chmod(runner, 0o755);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
  ], {
    encoding: "utf8", input: "bounded",
    timeout: 3000,
    env: {
      ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome,
      AD_GROK_LAUNCH_TIMEOUT_MS: "1000", AD_GROK_FIRST_OUTPUT_TIMEOUT_MS: "1000", AD_GROK_STALL_TIMEOUT_MS: "1000",
      AD_EXTERNAL_ATTEMPT_TIMEOUT_MS: "2000", AD_EXTERNAL_KILL_GRACE_MS: "25",
    },
  });
  assert.equal(result.status, 0, result.stderr);
  await new Promise((resolve) => setTimeout(resolve, 400));
  await assert.rejects(readFile(descendantMarker, "utf8"));
});

test("Grok absolute deadline kills the entire provider process group", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-process-group-timeout-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const descendantMarker = path.join(bin, "descendant-survived.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  const runner = path.join(bin, "grok");
  await writeFile(runner, `#!/usr/bin/env node
import fs from "node:fs";
import { spawn } from "node:child_process";
if (process.argv[2] === "version") {
  process.stdout.write("grok test\\n");
} else {
  spawn(process.execPath, ["-e", ${JSON.stringify(`setTimeout(() => require('fs').writeFileSync(${JSON.stringify(descendantMarker)}, 'survived'), 350); setTimeout(() => {}, 5000);`)}], { stdio: "ignore" });
  process.stdout.write(JSON.stringify({ event: "started" }) + "\\n");
  await new Promise((resolve) => setTimeout(resolve, 5000));
}
`);
  await chmod(runner, 0o755);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
  ], {
    encoding: "utf8", input: "bounded",
    env: {
      ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome,
      AD_GROK_FIRST_OUTPUT_TIMEOUT_MS: "200", AD_GROK_STALL_TIMEOUT_MS: "1000",
      AD_EXTERNAL_ATTEMPT_TIMEOUT_MS: "75", AD_EXTERNAL_KILL_GRACE_MS: "25",
    },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /provider_timeout/i);
  await new Promise((resolve) => setTimeout(resolve, 500));
  await assert.rejects(readFile(descendantMarker, "utf8"));
});

test("Grok side-effect timeout crosses provider boundary as result_unknown and disables retry", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-side-effect-unknown-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const runtimeReceipts = path.join(bin, "runtime-receipts.jsonl");
  const terminalReceipt = path.join(bin, "external-terminal.json");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, {
    assignment_id: "side-effect-timeout", side_effect: true, idempotency_key: "publish:timeout-1",
  }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "side-effect-timeout", "--task-id", "T1", "--agent-id", "writer", "--session-id", "side-effect-timeout-s1",
    "--assignment-ack", ack, "--runtime-receipts", runtimeReceipts, "--terminal-receipt", terminalReceipt,
  ], {
    encoding: "utf8", input: "bounded side-effect contract",
    env: {
      ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome,
      FAKE_RUNNER_DELAY_BEFORE_OUTPUT_MS: "1000",
      AD_GROK_LAUNCH_TIMEOUT_MS: "200", AD_GROK_FIRST_OUTPUT_TIMEOUT_MS: "50", AD_GROK_STALL_TIMEOUT_MS: "500",
      AD_EXTERNAL_ATTEMPT_TIMEOUT_MS: "1000", AD_EXTERNAL_KILL_GRACE_MS: "25",
    },
  });
  assert.equal(result.status, 1);
  const receipts = (await readFile(runtimeReceipts, "utf8")).trim().split("\n").map(JSON.parse);
  const terminal = receipts.at(-1);
  assert.equal(terminal.failure_class, "result_unknown");
  assert.equal(terminal.retry_class, "result_unknown");
  assert.equal(terminal.retry_safe, false);
  assert.equal(terminal.result_unknown, true);
  assert.equal(terminal.failure_details.provider_failure_class, "first_output_timeout");
  assert.equal(terminal.outcome_code, "FIRST_TOKEN_TIMEOUT");
  assert.deepEqual(terminal.phase_history, ["PRECHECK", "PACKET_VALIDATED", "PROVIDER_STARTING", "PROVIDER_STARTED"]);
  const durable = JSON.parse(await readFile(terminalReceipt, "utf8"));
  assert.equal(durable.failure_class, "result_unknown");
  assert.equal(durable.retry_class, "result_unknown");
  assert.equal(durable.retry_safe, false);
  assert.equal(durable.result_unknown, true);
  assert.equal(durable.failure_details.provider_failure_class, "first_output_timeout");
  assert.equal(durable.outcome_code, "FIRST_TOKEN_TIMEOUT");
  assert.equal(durable.advisory, true);
  assert.equal(durable.critical_path, false);
});

test("Grok implementation stall timeout persists structured canonical terminal classification", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-stall-terminal-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const runtimeReceipts = path.join(bin, "runtime-receipts.jsonl");
  const terminalReceipt = path.join(bin, "external-terminal.json");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, { role: "writer", agent_id: "writer", side_effect: false }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "high", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "a1", "--task-id", "T1", "--agent-id", "writer", "--session-id", "implementation-stall-s1",
    "--assignment-ack", ack, "--runtime-receipts", runtimeReceipts, "--terminal-receipt", terminalReceipt,
  ], {
    encoding: "utf8", input: "bounded implementation contract",
    env: {
      ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome,
      FAKE_RUNNER_MODEL_PROGRESS: "1", FAKE_RUNNER_DELAY_MS: "1000",
      AD_GROK_FIRST_OUTPUT_TIMEOUT_MS: "1000", AD_GROK_STALL_TIMEOUT_MS: "50",
      AD_EXTERNAL_ATTEMPT_TIMEOUT_MS: "1000", AD_EXTERNAL_KILL_GRACE_MS: "25",
    },
  });
  assert.equal(result.status, 1);
  const receipts = (await readFile(runtimeReceipts, "utf8")).trim().split("\n").map(JSON.parse);
  const terminal = receipts.at(-1);
  assert.equal(terminal.event_type, "assignment_terminal");
  assert.equal(terminal.failure_class, "generation_stalled");
  assert.equal(terminal.retry_class, "generation_stalled");
  assert.equal(terminal.retry_safe, true);
  assert.equal(terminal.result_unknown, false);
  assert.equal(terminal.failure_details.cleanup_confirmed, true);
  assert.equal(terminal.outcome_code, "HEARTBEAT_TIMEOUT");
  assert.deepEqual(terminal.phase_history, ["PRECHECK", "PACKET_VALIDATED", "PROVIDER_STARTING", "PROVIDER_STARTED", "FIRST_PROGRESS", "ANALYZING"]);
  assert.match(terminal.next_action, /inspect bounded external agent failure/i);
  const durable = JSON.parse(await readFile(terminalReceipt, "utf8"));
  assert.equal(durable.failure_class, "generation_stalled");
  assert.equal(durable.retry_class, "generation_stalled");
  assert.equal(durable.retry_safe, true);
  assert.equal(durable.result_unknown, false);
  assert.equal(durable.outcome_code, "HEARTBEAT_TIMEOUT");
  assert.equal(durable.advisory, true);
  assert.equal(durable.critical_path, false);
});


test("Grok failed attempt uses 0600 prompt file and removes it", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-prompt-permission-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
  ], {
    encoding: "utf8", input: "private bounded prompt",
    env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, FAKE_RUNNER_EXIT_CODE: "7" },
  });
  assert.equal(result.status, 7, result.stderr);
  const call = JSON.parse(result.stdout.trim());
  const promptFlag = call.args.indexOf("--prompt-file");
  assert.notEqual(promptFlag, -1);
  assert.equal(call.promptFileMode, 0o600);
  await assert.rejects(readFile(call.args[promptFlag + 1], "utf8"));
});

test("oversized non-reviewer Grok prompt fails before spawn without sharding", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-writer-oversize-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const ack = await assignmentAckFile(bin, { assignment_id: "writer-big", role: "writer", agent_id: "writer" }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "high", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "writer-big", "--task-id", "T1", "--agent-id", "writer", "--session-id", "writer-big-s1",
    "--assignment-ack", ack,
  ], {
    encoding: "utf8", input: "X".repeat(256),
    env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker, AD_GROK_MAX_PROMPT_BYTES: "128" },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /prompt_too_large/i);
  assert.doesNotMatch(result.stderr, /review_sharding_required/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("Grok stderr and assignment heartbeat do not satisfy first stdout progress", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-stderr-heartbeat-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const runtimeReceipts = path.join(bin, "receipts.jsonl");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  const runner = path.join(bin, "grok");
  await writeFile(runner, `#!/usr/bin/env node
if (process.argv[2] === "version") process.stdout.write("grok test\\n");
else { process.stderr.write("provider diagnostic only\\n"); await new Promise((resolve) => setTimeout(resolve, 1000)); }
`);
  await chmod(runner, 0o755);
  const ack = await assignmentAckFile(bin, { assignment_id: "stderr-heartbeat" }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "stderr-heartbeat", "--task-id", "T1", "--agent-id", "writer", "--session-id", "stderr-heartbeat-s1",
    "--assignment-ack", ack, "--runtime-receipts", runtimeReceipts,
  ], {
    encoding: "utf8", input: "bounded",
    env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome,
      AD_RUNTIME_HEARTBEAT_MS: "20", AD_GROK_FIRST_OUTPUT_TIMEOUT_MS: "120", AD_GROK_STALL_TIMEOUT_MS: "1000",
      AD_EXTERNAL_ATTEMPT_TIMEOUT_MS: "1500", AD_EXTERNAL_KILL_GRACE_MS: "25" },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /first_output_timeout/i);
  const receipts = (await readFile(runtimeReceipts, "utf8")).trim().split("\n").map(JSON.parse);
  assert.ok(receipts.some((item) => item.event_type === "assignment_heartbeat"));
  assert.equal(receipts.at(-1).failure_class, "first_output_timeout");
});

test("Grok unstructured stdout does not satisfy structured first-output progress", async () => {
  const runtimeModule = await import("../scripts/run_external_agent.mjs");
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-unstructured-first-"));
  const code = 'const timer=setInterval(()=>process.stdout.write("not-json\\n"),25); setTimeout(()=>{clearInterval(timer);process.exit(0)},1000);';
  const previous = {
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "120";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "1000";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1500";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "25";
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", code], {
        cwd: bin, env: process.env, terminateGroup: terminateSingleProcessFixture,
      }),
      (error) => error?.failureClass === "first_output_timeout",
    );
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      const envName = key === "first" ? "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS" : key === "stall" ? "AD_GROK_STALL_TIMEOUT_MS" : key === "absolute" ? "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS" : "AD_EXTERNAL_KILL_GRACE_MS";
      if (value === undefined) delete process.env[envName]; else process.env[envName] = value;
    }
  }
});


test("Grok 1.0.13 streaming text output satisfies first-output progress then stalls", async () => {
  const runtimeModule = await import("../scripts/run_external_agent.mjs?grok-stream-text=" + Date.now());
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-stream-text-"));
  const code = 'process.stdout.write(JSON.stringify({type:"text",data:"reviewing"})+"\\n"); setTimeout(()=>{},1000);';
  const previous = {
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "120";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "160";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1500";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "25";
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", code], {
        cwd: bin, env: process.env, terminateGroup: terminateSingleProcessFixture,
      }),
      (error) => error?.failureClass === "generation_stalled",
    );
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      const envName = key === "first" ? "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS"
        : key === "stall" ? "AD_GROK_STALL_TIMEOUT_MS"
          : key === "absolute" ? "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS"
            : "AD_EXTERNAL_KILL_GRACE_MS";
      if (value === undefined) delete process.env[envName]; else process.env[envName] = value;
    }
  }
});

test("Grok 1.0.13 streaming thought tool-call and tool-update events count as model progress", async () => {
  const runtimeModule = await import("../scripts/run_external_agent.mjs?grok-stream-thought-tool=" + Date.now());
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-stream-thought-tool-"));
  const code = 'process.stdout.write(JSON.stringify({type:"thought",data:"checking"})+"\\n"); setTimeout(()=>process.stdout.write(JSON.stringify({type:"tool_call",toolCallId:"call-1",toolName:"read_file",title:"read_file",kind:"read",status:"pending",rawInput:{target_file:"x"},content:[],locations:[]})+"\\n"),70); setTimeout(()=>process.stdout.write(JSON.stringify({type:"tool_call_update",toolCallId:"call-1",status:null,content:[],rawOutput:null,locations:[{path:"x"}]})+"\\n"),140); setTimeout(()=>{},1000);';
  const previous = {
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "120";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "160";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1500";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "25";
  try {
    const started = Date.now();
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", code], {
        cwd: bin, env: process.env, terminateGroup: terminateSingleProcessFixture,
      }),
      (error) => error?.failureClass === "generation_stalled",
    );
    assert.ok(Date.now() - started >= 260, "later valid streaming updates should extend the stall deadline");
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      const envName = key === "first" ? "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS"
        : key === "stall" ? "AD_GROK_STALL_TIMEOUT_MS"
          : key === "absolute" ? "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS"
            : "AD_EXTERNAL_KILL_GRACE_MS";
      if (value === undefined) delete process.env[envName]; else process.env[envName] = value;
    }
  }
});

test("Grok 1.0.13 bare type and metadata events cannot spoof model progress", async () => {
  const runtimeModule = await import("../scripts/run_external_agent.mjs?grok-stream-spoof=" + Date.now());
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-stream-spoof-"));
  const code = 'const items=[{type:"tool_call"},{type:"text",data:""},{type:"usage",usage:{input_tokens:1}},{type:"available_commands",tools:[],commands:[]}]; let i=0; const timer=setInterval(()=>process.stdout.write(JSON.stringify(items[(i++)%items.length])+"\\n"),25); setTimeout(()=>{clearInterval(timer);process.exit(0)},1000);';
  const previous = {
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "120";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "1000";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1500";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "25";
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", code], {
        cwd: bin, env: process.env, terminateGroup: terminateSingleProcessFixture,
      }),
      (error) => error?.failureClass === "first_output_timeout",
    );
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      const envName = key === "first" ? "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS"
        : key === "stall" ? "AD_GROK_STALL_TIMEOUT_MS"
          : key === "absolute" ? "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS"
            : "AD_EXTERNAL_KILL_GRACE_MS";
      if (value === undefined) delete process.env[envName]; else process.env[envName] = value;
    }
  }
});

test("Grok malformed stdout after one structured event does not prevent generation stall", async () => {
  const runtimeModule = await import("../scripts/run_external_agent.mjs");
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-unstructured-stall-"));
  const code = 'process.stdout.write(JSON.stringify({sessionId:"s",update:{sessionUpdate:"agent_message_chunk",content:{type:"text",text:"x"}}})+"\\n"); const timer=setInterval(()=>process.stdout.write("still-not-json\\n"),25); setTimeout(()=>{clearInterval(timer);process.exit(0)},1000);';
  const previous = {
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "500";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "120";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1500";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "25";
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", code], {
        cwd: bin, env: process.env, terminateGroup: terminateSingleProcessFixture,
      }),
      (error) => error?.failureClass === "generation_stalled",
    );
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      const envName = key === "first" ? "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS" : key === "stall" ? "AD_GROK_STALL_TIMEOUT_MS" : key === "absolute" ? "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS" : "AD_EXTERNAL_KILL_GRACE_MS";
      if (value === undefined) delete process.env[envName]; else process.env[envName] = value;
    }
  }
});

test("Grok delivery validator binds synthesis evidence to exact assigned shard receipts", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?synthesis-evidence=${Date.now()}`);
  assert.equal(typeof runtimeModule.readDeliveryReceipt, "function");
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-synthesis-delivery-validator-"));
  const head = "a".repeat(40);
  const delivery = path.join(bin, "delivery.json");
  await writeFile(delivery, JSON.stringify({
    delivery_outcome: "pass", summary: "synthesis", evidence: ["receipt:wrong-shard"], artifacts: [`git:${head}`],
    next_action: "integrate", retry_class: "none",
    review_verdict: { reviewed_head: head, verdict: "PASS", critical: [], important: [], minor: [] },
  }));
  assert.throws(() => runtimeModule.readDeliveryReceipt(delivery, {
    assignmentRole: "reviewer", candidateRevision: head, reviewPhase: "synthesis",
    reviewShardReceipts: ["receipt:expected-shard"],
  }), /exact|review_shard_receipts|shard receipt/i);
});

test("Grok delivery validator requires explicit reviewer phase", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-phase=${Date.now()}`);
  assert.equal(typeof runtimeModule.readDeliveryReceipt, "function");
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-review-delivery-phase-"));
  const head = "b".repeat(40);
  const delivery = path.join(bin, "delivery.json");
  await writeFile(delivery, JSON.stringify({
    delivery_outcome: "pass", summary: "full review", evidence: [`git:${head}`], artifacts: [`git:${head}`],
    next_action: "integrate", retry_class: "none",
    review_verdict: { reviewed_head: head, verdict: "PASS", critical: [], important: [], minor: [] },
  }));
  assert.throws(() => runtimeModule.readDeliveryReceipt(delivery, {
    assignmentRole: "reviewer", candidateRevision: head,
  }), /review_phase|full\|shard\|synthesis|explicit/i);
});

test("Grok reviewer shard assignment is rejected before provider spawn", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-review-shard-phase-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const head = execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  const ack = await assignmentAckFile(bin, {
    assignment_id: "review-shard", task_id: "review-shard", agent_id: "reviewer", role: "reviewer",
    candidate_revision: head, reviewer_for_revision: head, review_phase: "shard",
  }, repo);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "review",
    "--assignment-id", "review-shard", "--task-id", "review-shard", "--agent-id", "reviewer", "--session-id", "review-shard-s1",
    "--assignment-ack", ack,
  ], { encoding: "utf8", input: JSON.stringify(reviewPacket({ candidateRevision: head, reviewPhase: "shard" })), env: {
    ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker,
  } });
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stderr, /full\|synthesis/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("Grok cleanup uncertainty preserves cleanup failure class while remaining result unknown", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?cleanup-classification=${Date.now()}`);
  assert.equal(typeof runtimeModule.classifyExternalExecutionFailure, "function");
  const error = new Error("process group still alive");
  error.failureClass = "process_group_cleanup_failed";
  error.retrySafe = false;
  error.resultUnknown = true;
  error.details = { cleanup_confirmed: false, provider_started: true };
  const classified = runtimeModule.classifyExternalExecutionFailure(error, { sideEffect: false });
  assert.equal(classified.failureClass, "process_group_cleanup_failed");
  assert.equal(classified.retrySafe, false);
  assert.equal(classified.resultUnknown, true);
  assert.equal(classified.failureDetails.cleanup_confirmed, false);
});

test("Grok side-effect provider uncertainty elevates a known timeout to result_unknown", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?side-effect-classification=${Date.now()}`);
  assert.equal(typeof runtimeModule.classifyExternalExecutionFailure, "function");
  const error = new Error("no model output");
  error.failureClass = "first_output_timeout";
  error.retrySafe = true;
  error.resultUnknown = false;
  error.details = { cleanup_confirmed: true, provider_started: true };
  const classified = runtimeModule.classifyExternalExecutionFailure(error, { sideEffect: true });
  assert.equal(classified.failureClass, "result_unknown");
  assert.equal(classified.retrySafe, false);
  assert.equal(classified.resultUnknown, true);
  assert.equal(classified.failureDetails.provider_failure_class, "first_output_timeout");
});

test("Grok cleanup uncertainty is result unknown and not retry safe", async () => {
  const runtimeModule = await import("../scripts/run_external_agent.mjs");
  assert.equal(typeof runtimeModule.runMonitoredGrok, "function");
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-cleanup-uncertain-"));
  const runner = path.join(bin, "silent-grok");
  await writeFile(runner, `#!/usr/bin/env node
await new Promise((resolve) => setTimeout(resolve, 5000));
`);
  await chmod(runner, 0o755);
  const previous = {
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "20";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "500";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1000";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "20";
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(runner, [], {
        cwd: bin, env: process.env,
        terminateGroup: async (child) => {
          try { process.kill(-child.pid, "SIGKILL"); } catch {}
          return { confirmed: false, diagnostic: "simulated process-group probe failure" };
        },
      }),
      (error) => error?.failureClass === "process_group_cleanup_failed" && error?.retrySafe === false && error?.resultUnknown === true,
    );
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      const envName = key === "first" ? "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS" : key === "stall" ? "AD_GROK_STALL_TIMEOUT_MS" : key === "absolute" ? "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS" : "AD_EXTERNAL_KILL_GRACE_MS";
      if (value === undefined) delete process.env[envName]; else process.env[envName] = value;
    }
  }
});

test("Grok reviewer requires explicit phase and immutable candidate commit", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-review-contract-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "spawned.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const head = execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  const run = (assignmentId, overrides) => {
    const ackPromise = assignmentAckFile(bin, {
      assignment_id: assignmentId, task_id: assignmentId, agent_id: "reviewer", role: "reviewer",
      reviewer_for_revision: overrides.candidate_revision || head,
      ...overrides,
    }, repo);
    return ackPromise.then((ack) => spawnSync(process.execPath, [adapter,
      "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
      "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "review",
      "--assignment-id", assignmentId, "--task-id", assignmentId, "--agent-id", "reviewer", "--session-id", `${assignmentId}-s1`,
      "--assignment-ack", ack,
    ], { encoding: "utf8", input: JSON.stringify(reviewPacket({ candidateRevision: head })), env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, SPAWN_MARKER: marker } }));
  };
  const missingPhase = await run("review-missing-phase", { candidate_revision: head });
  assert.equal(missingPhase.status, 1);
  assert.match(missingPhase.stderr, /review_phase|full\|shard\|synthesis/i);

  const mutableCandidate = await run("review-mutable-candidate", { candidate_revision: "main", review_phase: "full" });
  assert.equal(mutableCandidate.status, 1);
  assert.match(mutableCandidate.stderr, /immutable|candidate_revision|commit/i);
  await assert.rejects(readFile(marker, "utf8"));
});

test("Grok synthesis receipt rejects a noncanonical assigned shard locator", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-synthesis-canonical-"));
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?synthesis-canonical=${Date.now()}`);
  const head = "c".repeat(40);

  const fakeDelivery = path.join(bin, "fake-synthesis.json");
  await writeFile(fakeDelivery, JSON.stringify({
    delivery_outcome: "pass", summary: "fake synthesis", evidence: ["receipt:not-real", `git:${head}`], artifacts: [`git:${head}`],
    next_action: "integrate", retry_class: "none",
    review_verdict: { reviewed_head: head, verdict: "PASS", critical: [], important: [], minor: [] },
  }));
  assert.throws(() => runtimeModule.readDeliveryReceipt(fakeDelivery, {
    assignmentRole: "reviewer", candidateRevision: head, reviewPhase: "synthesis",
    reviewShardReceipts: ["receipt:canonical-shard"],
  }), /shard|receipt|exact/i);
});

test("Grok structured metadata stdout does not satisfy model first-output progress", async () => {
  const runtimeModule = await import("../scripts/run_external_agent.mjs");
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-metadata-first-"));
  const code = 'const timer=setInterval(()=>process.stdout.write(JSON.stringify({sessionId:"s",update:{sessionUpdate:"usage_update",usage:{inputTokens:1}}})+"\\n"),25); setTimeout(()=>{clearInterval(timer);process.exit(0)},1000);';
  const previous = {
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "120";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "1000";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1500";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "25";
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", code], {
        cwd: bin, env: process.env, terminateGroup: terminateSingleProcessFixture,
      }),
      (error) => error?.failureClass === "first_output_timeout",
    );
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      const envName = key === "first" ? "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS" : key === "stall" ? "AD_GROK_STALL_TIMEOUT_MS" : key === "absolute" ? "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS" : "AD_EXTERNAL_KILL_GRACE_MS";
      if (value === undefined) delete process.env[envName]; else process.env[envName] = value;
    }
  }
});

test("Grok metadata after agent activity does not prevent generation stall", async () => {
  const runtimeModule = await import("../scripts/run_external_agent.mjs");
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-metadata-stall-"));
  const code = 'process.stdout.write(JSON.stringify({sessionId:"s",update:{sessionUpdate:"agent_message_chunk",content:{type:"text",text:"x"}}})+"\\n"); const timer=setInterval(()=>process.stdout.write(JSON.stringify({sessionId:"s",update:{sessionUpdate:"session_info_update",title:"still alive"}})+"\\n"),25); setTimeout(()=>{clearInterval(timer);process.exit(0)},1000);';
  const previous = {
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "500";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "120";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1500";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "25";
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", code], {
        cwd: bin, env: process.env, terminateGroup: terminateSingleProcessFixture,
      }),
      (error) => error?.failureClass === "generation_stalled",
    );
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      const envName = key === "first" ? "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS" : key === "stall" ? "AD_GROK_STALL_TIMEOUT_MS" : key === "absolute" ? "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS" : "AD_EXTERNAL_KILL_GRACE_MS";
      if (value === undefined) delete process.env[envName]; else process.env[envName] = value;
    }
  }
});

test("ordinary Grok provider exit and invalid delivery persist durable failure classification", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-durable-classification-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  await fakeRunner(bin, "grok", "version");
  const helper = path.join(bin, "continuation-helper.py");
  await writeFile(helper, "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n");
  await chmod(helper, 0o755);
  const baseEnv = { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, AD_TERMINAL_CONTINUATION_HELPER: helper };

  const failedReceipt = path.join(bin, "provider-exit-terminal.json");
  const failedAck = await assignmentAckFile(bin, { assignment_id: "provider-exit" }, repo);
  const failed = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "provider-exit", "--task-id", "T1", "--agent-id", "writer", "--session-id", "provider-exit-s1",
    "--assignment-ack", failedAck, "--terminal-receipt", failedReceipt,
  ], { encoding: "utf8", input: "bounded", env: { ...baseEnv, FAKE_RUNNER_EXIT_CODE: "7" } });
  assert.equal(failed.status, 7, failed.stderr);
  const failedDurable = JSON.parse(await readFile(failedReceipt, "utf8"));
  assert.equal(failedDurable.failure_class, "provider_exit");
  assert.equal(failedDurable.retry_class, "provider_exit");
  assert.equal(failedDurable.retry_safe, true);
  assert.equal(failedDurable.result_unknown, false);
  assert.equal(failedDurable.failure_details.provider_exit_code, 7);

  const invalidReceipt = path.join(bin, "invalid-delivery-terminal.json");
  const invalidDeliveryPath = path.join(bin, "invalid-delivery.json");
  await writeFile(invalidDeliveryPath, JSON.stringify({ delivery_outcome: "pass", summary: "missing artifact", evidence: ["green-test:42"], artifacts: [], next_action: "review", retry_class: "none" }));
  const invalidAck = await assignmentAckFile(bin, { assignment_id: "invalid-durable" }, repo);
  const invalid = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low", "--cwd", repo, "--work-type", "implementation",
    "--assignment-id", "invalid-durable", "--task-id", "T1", "--agent-id", "writer", "--session-id", "invalid-durable-s1",
    "--assignment-ack", invalidAck, "--delivery-receipt", invalidDeliveryPath, "--terminal-receipt", invalidReceipt,
  ], { encoding: "utf8", input: "bounded", env: baseEnv });
  assert.equal(invalid.status, 1, invalid.stderr);
  const invalidDurable = JSON.parse(await readFile(invalidReceipt, "utf8"));
  assert.equal(invalidDurable.failure_class, "delivery_receipt_invalid");
  assert.equal(invalidDurable.retry_class, "delivery_receipt_invalid");
  assert.equal(invalidDurable.retry_safe, false);
  assert.equal(invalidDurable.result_unknown, false);
  assert.match(invalidDurable.failure_details.validation_error, /delivery PASS requires evidence and artifact/i);
});

test("Grok empty tool-call updates do not count as provider progress", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?empty-tool-update=${Date.now()}`);
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-empty-tool-update-"));
  const code = 'let i=0; const vals=["",{},[]]; const timer=setInterval(()=>process.stdout.write(JSON.stringify({type:"tool_call_update",toolCallId:"call-1",rawOutput:vals[(i++)%vals.length]})+"\\n"),25); setTimeout(()=>{clearInterval(timer);process.exit(0)},1000);';
  const previous = {
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "120";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "1000";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1500";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "25";
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", code], {
        cwd: bin, env: process.env, terminateGroup: terminateSingleProcessFixture,
      }),
      (error) => error?.failureClass === "first_output_timeout",
    );
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      const envName = key === "first" ? "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS" : key === "stall" ? "AD_GROK_STALL_TIMEOUT_MS" : key === "absolute" ? "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS" : "AD_EXTERNAL_KILL_GRACE_MS";
      if (value === undefined) delete process.env[envName]; else process.env[envName] = value;
    }
  }
});

test("Grok misleading type or event fields do not count as ACP model progress", async () => {
  const runtimeModule = await import("../scripts/run_external_agent.mjs");
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-misleading-json-"));
  const code = 'let flip=false; const timer=setInterval(()=>{flip=!flip; process.stdout.write(JSON.stringify(flip?{type:"tool_call"}:{event:"agent_message_chunk"})+"\\n")},25); setTimeout(()=>{clearInterval(timer);process.exit(0)},1000);';
  const previous = {
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "120";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "1000";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1500";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "25";
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", code], {
        cwd: bin, env: process.env, terminateGroup: terminateSingleProcessFixture,
      }),
      (error) => error?.failureClass === "first_output_timeout",
    );
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      const envName = key === "first" ? "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS" : key === "stall" ? "AD_GROK_STALL_TIMEOUT_MS" : key === "absolute" ? "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS" : "AD_EXTERNAL_KILL_GRACE_MS";
      if (value === undefined) delete process.env[envName]; else process.env[envName] = value;
    }
  }
});

test("cleanup uncertainty is fail closed and result unknown", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?cleanup-fail=${Date.now()}`);
  assert.equal(typeof runtimeModule.runCleanupStack, "function");
  assert.throws(
    () => runtimeModule.runCleanupStack([
      { label: "prompt_file", cleanup: () => { throw new Error("permission denied"); } },
      { label: "grok_home", cleanup: () => {} },
    ]),
    (error) => {
      assert.equal(error?.failureClass, "cleanup_failed");
      assert.equal(error?.retrySafe, false);
      assert.equal(error?.resultUnknown, true);
      assert.match(error?.message || "", /cleanup_failed/i);
      assert.deepEqual(error?.details?.failed_resources, ["prompt_file"]);
      return true;
    },
  );
});

test("Grok payload or data wrappers cannot spoof ACP model progress", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?acp-envelope=${Date.now()}`);
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-fake-acp-wrapper-"));
  const code = 'let flip=false; const timer=setInterval(()=>{flip=!flip; process.stdout.write(JSON.stringify(flip?{payload:{sessionUpdate:"tool_call"}}:{data:{session_update:"agent_message_chunk"}})+"\\n")},25); setTimeout(()=>{clearInterval(timer);process.exit(0)},1000);';
  const previous = {
    first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS,
    stall: process.env.AD_GROK_STALL_TIMEOUT_MS,
    absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS,
    grace: process.env.AD_EXTERNAL_KILL_GRACE_MS,
  };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "120";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "1000";
  process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "1500";
  process.env.AD_EXTERNAL_KILL_GRACE_MS = "25";
  try {
    await assert.rejects(
      runtimeModule.runMonitoredGrok(process.execPath, ["-e", code], {
        cwd: bin, env: process.env, terminateGroup: terminateSingleProcessFixture,
      }),
      (error) => error?.failureClass === "first_output_timeout",
    );
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      const envName = key === "first" ? "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS" : key === "stall" ? "AD_GROK_STALL_TIMEOUT_MS" : key === "absolute" ? "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS" : "AD_EXTERNAL_KILL_GRACE_MS";
      if (value === undefined) delete process.env[envName]; else process.env[envName] = value;
    }
  }
});

test("Grok prompt preparation cleans a temp directory when prompt write fails", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?prompt-write-fail=${Date.now()}`);
  assert.equal(typeof runtimeModule.prepareGrokPrompt, "function");
  const created = "/tmp/fake-grok-prompt-dir";
  let cleaned = false;
  assert.throws(
    () => runtimeModule.prepareGrokPrompt("secret prompt", {
      createTempDir: () => created,
      writePrompt: () => { throw new Error("disk write failed"); },
      cleanupDirectory: (directory) => { assert.equal(directory, created); cleaned = true; },
    }),
    (error) => error?.failureClass === "prompt_file_write_failed" && error?.retrySafe === true && error?.resultUnknown === false,
  );
  assert.equal(cleaned, true);
});

test("Grok prompt write plus cleanup failure is fail closed", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?prompt-cleanup-fail=${Date.now()}`);
  assert.equal(typeof runtimeModule.prepareGrokPrompt, "function");
  assert.throws(
    () => runtimeModule.prepareGrokPrompt("secret prompt", {
      createTempDir: () => "/tmp/fake-grok-prompt-dir",
      writePrompt: () => { throw new Error("disk write failed"); },
      cleanupDirectory: () => { throw new Error("cleanup denied"); },
    }),
    (error) => error?.failureClass === "cleanup_failed" && error?.retrySafe === false && error?.resultUnknown === true,
  );
});

test("cleanup failure preserves prior Grok provider exit evidence", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?provider-exit-cleanup=${Date.now()}`);
  const prior = new Error("provider_exit: external agent exited 7");
  prior.failureClass = "provider_exit";
  prior.retrySafe = true;
  prior.resultUnknown = false;
  prior.details = { provider_exit_code: 7 };
  assert.throws(
    () => runtimeModule.runCleanupStack([
      { label: "prompt_file", cleanup: () => { throw new Error("cleanup denied"); } },
    ], prior),
    (error) => {
      assert.equal(error?.failureClass, "cleanup_failed");
      assert.equal(error?.retrySafe, false);
      assert.equal(error?.resultUnknown, true);
      assert.equal(error?.details?.prior_failure_class, "provider_exit");
      assert.equal(error?.details?.prior_failure_details?.provider_exit_code, 7);
      return true;
    },
  );
});

test("Grok pure-packet Reviewer uses no tools, no planning, structured verdict, and sufficient turn budget", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-policy=${Date.now()}`);
  assert.equal(typeof runtimeModule.buildGrokReviewArgs, "function");
  const args = runtimeModule.buildGrokReviewArgs("grok-4.6", "/tmp/review-packet.txt", "high");
  const valueAfter = (flag) => args[args.indexOf(flag) + 1];
  assert.equal(valueAfter("--tools"), "");
  assert.equal(valueAfter("--max-turns"), "2");
  assert.equal(args.includes("--disable-web-search"), true);
  assert.equal(args.includes("--no-plan"), true);
  assert.equal(args.includes("--no-subagents"), true);
  assert.equal(args.includes("--json-schema"), true);
  assert.equal(args.includes("--system-prompt-override"), true);
  assert.equal(args.includes("--prompt-file"), true);
  assert.equal(args.includes("--output-format"), false);
  assert.equal(args.includes("--always-approve"), false);
  const system = valueAfter("--system-prompt-override");
  assert.match(system, /complete source packet/i);
  assert.match(system, /do not.*read.*repo|do not.*repository/i);
  assert.match(system, /first.*valid.*response.*verdict|verdict.*first.*valid.*response/i);
});

test("Grok Reviewer structured PASS and FAIL are validated independently from process exit", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-verdict=${Date.now()}`);
  assert.equal(typeof runtimeModule.validateGrokReviewResult, "function");
  const head = "a".repeat(40);
  const pass = runtimeModule.validateGrokReviewResult({
    reviewed_head: head, critical: 0, important: 0, minor: [], findings: [], verdict: "PASS",
  }, { candidateRevision: head });
  assert.equal(pass.reviewStatus, "REVIEW_PASS");
  assert.equal(pass.deliveryOutcome, "pass");
  assert.equal(pass.reviewVerdict.verdict, "PASS");

  const fail = runtimeModule.validateGrokReviewResult({
    reviewed_head: head,
    critical: 1,
    important: 1,
    minor: ["minor note"],
    findings: [
      { severity: "critical", message: "critical bug" },
      { severity: "important", message: "important bug" },
    ],
    verdict: "FAIL",
  }, { candidateRevision: head });
  assert.equal(fail.reviewStatus, "REVIEW_FAIL");
  assert.equal(fail.deliveryOutcome, "fail");
  assert.deepEqual(fail.reviewVerdict.critical, ["critical bug"]);
  assert.deepEqual(fail.reviewVerdict.important, ["important bug"]);
  assert.deepEqual(fail.reviewVerdict.minor, ["minor note"]);
  assert.equal(fail.reviewVerdict.verdict, "FINDINGS");
});

test("Grok Reviewer rejects malformed JSON and inconsistent PASS counts", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-invalid=${Date.now()}`);
  const head = "b".repeat(40);
  assert.throws(
    () => runtimeModule.parseGrokReviewOutput("not-json", { candidateRevision: head }),
    (error) => error?.failureClass === "review_output_invalid" && error?.reviewStatus === "REVIEW_OUTPUT_INVALID",
  );
  assert.throws(
    () => runtimeModule.validateGrokReviewResult({
      reviewed_head: head, critical: 1, important: 0, minor: [],
      findings: [{ severity: "critical", message: "bug" }], verdict: "PASS",
    }, { candidateRevision: head }),
    (error) => error?.failureClass === "review_output_invalid" && error?.reviewStatus === "REVIEW_OUTPUT_INVALID",
  );
});

test("Grok Reviewer max turns without verdict is REVIEW_MAX_TURNS, never PASS", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-max-turns=${Date.now()}`);
  const terminal = runtimeModule.classifyGrokReviewTerminal({
    exitCode: 1,
    stdout: "I'll review this first. First I'll load the candidate diff...\n",
    stderr: "Max turns reached\nError: max turns reached\n",
    timedOut: false,
    cleanupConfirmed: true,
    candidateRevision: "c".repeat(40),
  });
  assert.equal(terminal.reviewStatus, "REVIEW_MAX_TURNS");
  assert.equal(terminal.retrySafe, false);
  assert.equal(terminal.deliveryOutcome, "unresolved");
});

test("Grok Reviewer timeout distinguishes clean timeout from stuck residual process", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-timeout-class=${Date.now()}`);
  const clean = runtimeModule.classifyGrokReviewTerminal({
    exitCode: null, stdout: "", stderr: "", timedOut: true, cleanupConfirmed: true,
    candidateRevision: "d".repeat(40), hadModelOutput: false,
  });
  assert.equal(clean.reviewStatus, "REVIEW_TIMEOUT");
  assert.equal(clean.retrySafe, true);
  const stuck = runtimeModule.classifyGrokReviewTerminal({
    exitCode: null, stdout: "", stderr: "", timedOut: true, cleanupConfirmed: false,
    candidateRevision: "d".repeat(40), hadModelOutput: false,
  });
  assert.equal(stuck.reviewStatus, "REVIEW_PROCESS_STUCK");
  assert.equal(stuck.retrySafe, false);
});

test("Grok Reviewer retry policy requires explicit authorization and never retries after provider start", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-retry=${Date.now()}`);
  assert.equal(typeof runtimeModule.grokReviewRetryDecision, "function");
  for (const reviewStatus of ["REVIEW_FAIL", "REVIEW_MAX_TURNS", "REVIEW_OUTPUT_INVALID", "REVIEW_PROCESS_STUCK", "REVIEW_NO_VERDICT"]) {
    assert.deepEqual(runtimeModule.grokReviewRetryDecision({ reviewStatus, retrySafe: false }, { attempt: 1 }), { retry: false, reason: "terminal_review_outcome" });
  }
  assert.deepEqual(
    runtimeModule.grokReviewRetryDecision({ reviewStatus: "REVIEW_TIMEOUT", retrySafe: true, providerStarted: false, hadModelOutput: false }, { attempt: 1 }),
    { retry: false, reason: "explicit_authorization_required" },
  );
  assert.deepEqual(
    runtimeModule.grokReviewRetryDecision({ reviewStatus: "REVIEW_TIMEOUT", retrySafe: true, providerStarted: false, hadModelOutput: false }, { attempt: 1, authorizedSafeRetry: true }),
    { retry: true, reason: "authorized_safe_downgrade_before_provider" },
  );
  assert.deepEqual(
    runtimeModule.grokReviewRetryDecision({ reviewStatus: "REVIEW_TIMEOUT", retrySafe: true, providerStarted: true, hadModelOutput: false }, { attempt: 1, authorizedSafeRetry: true }),
    { retry: false, reason: "provider_boundary_crossed" },
  );
  assert.deepEqual(
    runtimeModule.grokReviewRetryDecision({ reviewStatus: "REVIEW_TIMEOUT", retrySafe: true, providerStarted: false, hadModelOutput: true }, { attempt: 1, authorizedSafeRetry: true }),
    { retry: false, reason: "model_output_observed" },
  );
  assert.deepEqual(
    runtimeModule.grokReviewRetryDecision({ reviewStatus: "REVIEW_TIMEOUT", retrySafe: true, hadModelOutput: false }, { attempt: 2 }),
    { retry: false, reason: "retry_budget_exhausted" },
  );
});

test("Grok Reviewer timeout performs bounded cleanup before REVIEW_TIMEOUT", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-monitor-timeout=${Date.now()}`);
  const { EventEmitter } = await import("node:events");
  const { PassThrough } = await import("node:stream");
  const child = new EventEmitter();
  child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.pid = 515151; child.exitCode = null; child.signalCode = null;
  let cleaned = 0;
  const old = { first: process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS, absolute: process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS, launch: process.env.AD_GROK_LAUNCH_TIMEOUT_MS, stall: process.env.AD_GROK_STALL_TIMEOUT_MS };
  process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "30"; process.env.AD_EXTERNAL_ATTEMPT_TIMEOUT_MS = "200"; process.env.AD_GROK_LAUNCH_TIMEOUT_MS = "100"; process.env.AD_GROK_STALL_TIMEOUT_MS = "100";
  try {
    const promise = runtimeModule.runMonitoredGrokReview("grok", [], {
      cwd: os.tmpdir(), env: process.env,
      spawnChild: () => { queueMicrotask(() => child.emit("spawn")); return child; },
      terminateGroup: async () => { cleaned += 1; child.exitCode = 1; return { confirmed: true, diagnostic: "TERM;KILL;reaped;group-gone" }; },
    });
    const terminal = await promise;
    assert.equal(terminal.reviewStatus, "REVIEW_TIMEOUT");
    assert.equal(terminal.retrySafe, false);
    assert.equal(cleaned, 1);
    assert.match(terminal.cleanupDiagnostic, /group-gone/);
  } finally {
    for (const [key,name] of [["first","AD_GROK_FIRST_OUTPUT_TIMEOUT_MS"],["absolute","AD_EXTERNAL_ATTEMPT_TIMEOUT_MS"],["launch","AD_GROK_LAUNCH_TIMEOUT_MS"],["stall","AD_GROK_STALL_TIMEOUT_MS"]]) old[key] === undefined ? delete process.env[name] : process.env[name] = old[key];
  }
});

test("Grok Reviewer timeout with residual process group is REVIEW_PROCESS_STUCK", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-monitor-stuck=${Date.now()}`);
  const { EventEmitter } = await import("node:events");
  const { PassThrough } = await import("node:stream");
  const child = new EventEmitter();
  child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.pid = 525252; child.exitCode = null; child.signalCode = null;
  const old = process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS; process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = "30";
  try {
    const terminal = await runtimeModule.runMonitoredGrokReview("grok", [], {
      cwd: os.tmpdir(), env: process.env,
      spawnChild: () => { queueMicrotask(() => child.emit("spawn")); return child; },
      terminateGroup: async () => ({ confirmed: false, diagnostic: "SIGTERM;SIGKILL; process group still alive" }),
    });
    assert.equal(terminal.reviewStatus, "REVIEW_PROCESS_STUCK");
    assert.equal(terminal.retrySafe, false);
    assert.match(terminal.cleanupDiagnostic, /still alive/);
  } finally { old === undefined ? delete process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS : process.env.AD_GROK_FIRST_OUTPUT_TIMEOUT_MS = old; }
});

test("Grok Reviewer parent SIGTERM cleans detached provider group before returning", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-parent-signal=${Date.now()}`);
  const { EventEmitter } = await import("node:events");
  const { PassThrough } = await import("node:stream");
  const child = new EventEmitter(); child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.pid = 535353; child.exitCode = null; child.signalCode = null;
  const parent = new EventEmitter();
  let cleaned = 0;
  const promise = runtimeModule.runMonitoredGrokReview("grok", [], {
    cwd: os.tmpdir(), env: process.env, parentProcess: parent,
    spawnChild: () => { queueMicrotask(() => { child.emit("spawn"); parent.emit("SIGTERM"); }); return child; },
    terminateGroup: async () => { cleaned += 1; child.exitCode = 1; return { confirmed: true, diagnostic: "parent-signal-group-gone" }; },
  });
  const terminal = await promise;
  assert.equal(cleaned, 1);
  assert.equal(terminal.reviewStatus, "REVIEW_TIMEOUT");
  assert.equal(terminal.parentSignal, "SIGTERM");
});

async function reviewerFakeRunner(bin, body) {
  const target = path.join(bin, "grok");
  await writeFile(target, `#!/usr/bin/env node\n${body}\n`);
  await chmod(target, 0o755);
  return target;
}

async function runPurePacketReview({ verdict, stderr = "", exitCode = 0, body = null, assignmentId = "pure-review" }) {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-pure-review-"));
  const repo = await makeAssignmentRepo(bin);
  const installed = await fakeInstalledSkill(bin);
  const manifestPath = path.join(installed.root, ".adaptive-delivery-install.json");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  manifest.impact = "test_only";
  await writeFile(manifestPath, JSON.stringify(manifest));
  const grokHome = path.join(bin, "grok-home");
  const runtimeReceipts = path.join(bin, "runtime-receipts.jsonl");
  const countFile = path.join(bin, "calls.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  const head = execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  const ack = await assignmentAckFile(bin, {
    assignment_id: assignmentId, task_id: assignmentId, agent_id: "reviewer", role: "reviewer",
    candidate_revision: head, reviewer_for_revision: head, review_phase: "full",
  }, repo);
  const script = body || `
import fs from "node:fs";
fs.appendFileSync(process.env.COUNT_FILE, "1\\n");
if (process.argv[2] === "version") { process.stdout.write("grok test\\n"); process.exit(0); }
const args = process.argv.slice(2);
if (!args.includes("--json-schema") || !args.includes("--no-plan") || !args.includes("--no-subagents") || !args.includes("--disable-web-search")) process.exit(91);
const tools = args.indexOf("--tools"); if (tools < 0 || args[tools + 1] !== "") process.exit(92);
const turns = args.indexOf("--max-turns"); if (turns < 0 || args[turns + 1] !== "2") process.exit(93);
const promptFile = args[args.indexOf("--prompt-file") + 1];
if ((fs.statSync(promptFile).mode & 0o777) !== 0o600) process.exit(94);
const packet = JSON.parse(fs.readFileSync(promptFile, "utf8"));
if (packet.schema_version !== 1 || packet.work_type !== "review" || packet.candidate_revision !== process.env.REVIEW_HEAD) process.exit(95);
${stderr ? `process.stderr.write(${JSON.stringify(stderr)});` : ""}
${verdict !== undefined ? `process.stdout.write(${JSON.stringify(typeof verdict === "string" ? verdict : JSON.stringify(verdict))});` : ""}
process.exit(${exitCode});`;
  await reviewerFakeRunner(bin, script);
  const inputVerdict = verdict && typeof verdict === "object" ? { ...verdict, reviewed_head: verdict.reviewed_head || head } : verdict;
  if (body === null && verdict && typeof verdict === "object") {
    const text = JSON.stringify(inputVerdict);
    await reviewerFakeRunner(bin, script.replace(JSON.stringify(JSON.stringify(verdict)), JSON.stringify(text)));
  }
  const result = spawnSync(process.execPath, [installed.adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "high", "--cwd", repo, "--work-type", "review",
    "--assignment-id", assignmentId, "--task-id", assignmentId, "--agent-id", "reviewer", "--session-id", `${assignmentId}-s1`,
    "--assignment-ack", ack, "--runtime-receipts", runtimeReceipts,
  ], { encoding: "utf8", input: JSON.stringify(reviewPacket({ candidateRevision: head })), env: {
    ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, COUNT_FILE: countFile, REVIEW_HEAD: head,
    // These cases verify Reviewer terminal semantics, not provider cold-start watchdog sensitivity.
    // Dedicated launch/first-output/stall tests exercise sub-second deadlines separately.
    AD_GROK_FIRST_OUTPUT_TIMEOUT_MS: "1500", AD_GROK_STALL_TIMEOUT_MS: "1500", AD_EXTERNAL_ATTEMPT_TIMEOUT_MS: "4000", AD_EXTERNAL_KILL_GRACE_MS: "30",
  } });
  const receipts = (await readFile(runtimeReceipts, "utf8")).trim().split("\n").filter(Boolean).map(JSON.parse);
  const callCount = (await readFile(countFile, "utf8")).trim().split("\n").filter(Boolean).length;
  return { result, receipts, terminal: receipts.at(-1), head, callCount };
}

test("Grok work_type=review accepts only structured PASS into canonical acceptance", async () => {
  const { result, terminal, head, callCount } = await runPurePacketReview({
    verdict: { critical: 0, important: 0, minor: [], findings: [], verdict: "PASS" },
    assignmentId: "review-pass",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(callCount, 1);
  assert.equal(terminal.terminal_state, "completed");
  assert.equal(terminal.transport_outcome, "completed");
  assert.equal(terminal.delivery_outcome, "pass");
  assert.equal(terminal.review_status, "REVIEW_PASS");
  assert.equal(terminal.review_verdict.reviewed_head, head);
  assert.equal(terminal.review_verdict.verdict, "PASS");
  assert.equal(terminal.outcome_code, "SUCCESS");
  assert.deepEqual(terminal.phase_history, ["PRECHECK", "PACKET_VALIDATED", "PROVIDER_STARTING", "PROVIDER_STARTED", "FIRST_PROGRESS", "ANALYZING", "FINAL_RESULT"]);
  assert.equal(terminal.advisory, true);
  assert.equal(terminal.critical_path, false);
});

test("Grok work_type=review valid FAIL is terminal findings and is never retried into PASS", async () => {
  const { result, terminal, callCount } = await runPurePacketReview({
    verdict: {
      critical: 0, important: 1, minor: [],
      findings: [{ severity: "important", message: "real defect" }], verdict: "FAIL",
    },
    assignmentId: "review-fail",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(callCount, 1);
  assert.equal(terminal.terminal_state, "completed");
  assert.equal(terminal.transport_outcome, "completed");
  assert.equal(terminal.delivery_outcome, "fail");
  assert.equal(terminal.review_status, "REVIEW_FAIL");
  assert.equal(terminal.review_verdict.verdict, "FINDINGS");
  assert.deepEqual(terminal.review_verdict.important, ["real defect"]);
});

test("Grok work_type=review max turns without verdict persists REVIEW_MAX_TURNS and no retry", async () => {
  const { result, terminal, callCount } = await runPurePacketReview({
    verdict: "I'll review first...",
    stderr: "Max turns reached\nError: max turns reached\n",
    exitCode: 1,
    assignmentId: "review-max-turns",
  });
  assert.equal(result.status, 1);
  assert.equal(callCount, 1);
  assert.equal(terminal.review_status, "REVIEW_MAX_TURNS");
  assert.equal(terminal.delivery_outcome, "unresolved");
  assert.equal(terminal.retry_safe, false);
});

test("Grok work_type=review malformed verdict persists REVIEW_OUTPUT_INVALID and no retry", async () => {
  const { result, terminal, callCount } = await runPurePacketReview({
    verdict: "{malformed",
    assignmentId: "review-invalid-output",
  });
  assert.equal(result.status, 1);
  assert.equal(callCount, 1);
  assert.equal(terminal.review_status, "REVIEW_OUTPUT_INVALID");
  assert.equal(terminal.delivery_outcome, "unresolved");
  assert.equal(terminal.retry_safe, false);
});

test("Grok Reviewer stall after stdout closes reaps TERM-resistant relay descendants", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-review-relay-cleanup-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const marker = path.join(bin, "relay-survived.txt");
  const relayReady = path.join(bin, "relay-ready.txt");
  const runtimeReceipts = path.join(bin, "runtime-receipts.jsonl");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  const head = execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  const ack = await assignmentAckFile(bin, {
    assignment_id: "review-relay-timeout", task_id: "review-relay-timeout", agent_id: "reviewer", role: "reviewer",
    candidate_revision: head, reviewer_for_revision: head, review_phase: "full",
  }, repo);
  await reviewerFakeRunner(bin, `
import { spawn } from "node:child_process";
import fs from "node:fs";
if (process.argv[2] === "version") { process.stdout.write("grok test\\n"); process.exit(0); }
process.on("SIGTERM", () => {});
spawn(process.execPath, ["-e", ${JSON.stringify(`require('fs').writeFileSync(process.env.RELAY_READY,'ready'); process.on('SIGTERM',()=>{}); setTimeout(()=>require('fs').writeFileSync(${JSON.stringify(marker)},'survived'),450); setInterval(()=>{},1000);`)}], { stdio: "ignore" });
const deadline = Date.now() + 500;
while (!fs.existsSync(process.env.RELAY_READY) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 10));
if (!fs.existsSync(process.env.RELAY_READY)) process.exit(96);
process.stdout.write("Reviewer started but no verdict yet\\n");
process.stdout.end();
setInterval(() => {}, 1000);
`);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "high", "--cwd", repo, "--work-type", "review",
    "--assignment-id", "review-relay-timeout", "--task-id", "review-relay-timeout", "--agent-id", "reviewer", "--session-id", "review-relay-timeout-s1",
    "--assignment-ack", ack, "--runtime-receipts", runtimeReceipts,
  ], { encoding: "utf8", input: JSON.stringify(reviewPacket({ candidateRevision: head })), env: {
    ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome, RELAY_READY: relayReady,
    AD_GROK_FIRST_OUTPUT_TIMEOUT_MS: "300", AD_GROK_STALL_TIMEOUT_MS: "60", AD_EXTERNAL_ATTEMPT_TIMEOUT_MS: "1000", AD_EXTERNAL_KILL_GRACE_MS: "30",
  } });
  assert.equal(result.status, 1);
  const receipts = (await readFile(runtimeReceipts, "utf8")).trim().split("\n").filter(Boolean).map(JSON.parse);
  const terminal = receipts.at(-1);
  assert.equal(terminal.review_status, "REVIEW_TIMEOUT");
  assert.equal(terminal.retry_safe, false); // provider boundary was crossed; never blind-retry
  assert.match(terminal.failure_details.cleanup_diagnostic, /SIGTERM/);
  await new Promise((resolve) => setTimeout(resolve, 550));
  await assert.rejects(readFile(marker, "utf8"));
});

test("ordinary Grok parent SIGTERM is parent cancellation and performs bounded process-group cleanup", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?ordinary-parent-signal=${Date.now()}`);
  const { EventEmitter } = await import("node:events");
  const { PassThrough } = await import("node:stream");
  const child = new EventEmitter(); child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.pid = 545454; child.exitCode = null; child.signalCode = null;
  const parent = new EventEmitter();
  let cleaned = 0;
  const promise = runtimeModule.runMonitoredGrok("grok", [], {
    cwd: os.tmpdir(), env: process.env, parentProcess: parent,
    spawnChild: () => { queueMicrotask(() => { child.emit("spawn"); parent.emit("SIGTERM"); }); return child; },
    terminateGroup: async () => { cleaned += 1; child.exitCode = 1; return { confirmed: true, diagnostic: "ordinary-parent-group-gone" }; },
  });
  await assert.rejects(promise, (error) => error?.failureClass === "parent_cancelled"
    && error?.outcomeCode === "PARENT_CANCELLED"
    && error?.cancellationSource === "parent_process"
    && error?.details?.parent_signal === "SIGTERM"
    && error?.details?.cleanup_confirmed === true);
  assert.equal(cleaned, 1);
});

test("Grok Reviewer distinguishes REVIEW_NO_VERDICT and REVIEW_PROVIDER_ERROR", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-other-status=${Date.now()}`);
  const head = "e".repeat(40);
  const noVerdict = runtimeModule.classifyGrokReviewTerminal({
    exitCode: 0, stdout: "", stderr: "", timedOut: false, cleanupConfirmed: true, candidateRevision: head,
  });
  assert.equal(noVerdict.reviewStatus, "REVIEW_NO_VERDICT");
  assert.equal(noVerdict.retrySafe, false);
  const provider = runtimeModule.classifyGrokReviewTerminal({
    exitCode: 1, stdout: "", stderr: "relay disconnected before response", timedOut: false, cleanupConfirmed: true, candidateRevision: head,
  });
  assert.equal(provider.reviewStatus, "REVIEW_PROVIDER_ERROR");
  assert.equal(provider.retrySafe, true);
});

test("Grok work_type=review does not implicitly retry a transient provider failure", async () => {
  const bin = await mkdtemp(path.join(os.tmpdir(), "adaptive-grok-review-transient-retry-"));
  const repo = await makeAssignmentRepo(bin);
  const grokHome = path.join(bin, "grok-home");
  const runtimeReceipts = path.join(bin, "runtime-receipts.jsonl");
  const countFile = path.join(bin, "calls.txt");
  await mkdir(grokHome, { recursive: true });
  await writeFile(path.join(grokHome, "auth.json"), "{}");
  const head = execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  const ack = await assignmentAckFile(bin, {
    assignment_id: "review-transient", task_id: "review-transient", agent_id: "reviewer", role: "reviewer",
    candidate_revision: head, reviewer_for_revision: head, review_phase: "full",
  }, repo);
  await reviewerFakeRunner(bin, `
import fs from "node:fs";
if (process.argv[2] === "version") { process.stdout.write("grok test\\n"); process.exit(0); }
let count = 0; try { count = Number(fs.readFileSync(process.env.COUNT_FILE, "utf8").trim() || 0); } catch {}
count += 1; fs.writeFileSync(process.env.COUNT_FILE, String(count));
if (count === 1) { process.stderr.write("relay disconnected before response\\n"); process.exit(1); }
process.stdout.write(JSON.stringify({ reviewed_head: process.env.REVIEW_HEAD, critical: 0, important: 0, minor: [], findings: [], verdict: "PASS" }));
`);
  const result = spawnSync(process.execPath, [adapter,
    "--execute", "--authorized-external-call", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "high", "--cwd", repo, "--work-type", "review",
    "--assignment-id", "review-transient", "--task-id", "review-transient", "--agent-id", "reviewer", "--session-id", "review-transient-s1",
    "--assignment-ack", ack, "--runtime-receipts", runtimeReceipts,
  ], { encoding: "utf8", input: JSON.stringify(reviewPacket({ candidateRevision: head })), env: {
    ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH || ""}`, GROK_HOME: grokHome,
    COUNT_FILE: countFile, REVIEW_HEAD: head,
    // This case verifies retry semantics, not cold-start watchdog sensitivity. Dedicated timeout tests cover 30-300ms deadlines.
    AD_GROK_FIRST_OUTPUT_TIMEOUT_MS: "1500", AD_GROK_STALL_TIMEOUT_MS: "1500", AD_EXTERNAL_ATTEMPT_TIMEOUT_MS: "4000", AD_EXTERNAL_KILL_GRACE_MS: "30",
  } });
  assert.equal(result.status, 1, result.stderr);
  assert.equal(Number((await readFile(countFile, "utf8")).trim()), 1);
  const receipts = (await readFile(runtimeReceipts, "utf8")).trim().split("\n").filter(Boolean).map(JSON.parse);
  const terminal = receipts.at(-1);
  assert.equal(terminal.review_status, "REVIEW_PROVIDER_ERROR");
  assert.equal(terminal.delivery_outcome, "unresolved");
});

test("Grok Reviewer waits for stdio close before classifying final verdict", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-close-drain=${Date.now()}`);
  const { EventEmitter } = await import("node:events");
  const { PassThrough } = await import("node:stream");
  const child = new EventEmitter();
  child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.pid = 565656; child.exitCode = null; child.signalCode = null;
  const head = "f".repeat(40);
  const promise = runtimeModule.runMonitoredGrokReview("grok", [], {
    cwd: os.tmpdir(), env: process.env, candidateRevision: head,
    spawnChild: () => {
      queueMicrotask(() => {
        child.emit("spawn");
        child.exitCode = 0;
        child.emit("exit", 0, null);
        setTimeout(() => {
          child.stdout.write(JSON.stringify({ reviewed_head: head, critical: 0, important: 0, minor: [], findings: [], verdict: "PASS" }));
          child.stdout.end(); child.stderr.end();
          child.emit("close", 0, null);
        }, 20);
      });
      return child;
    },
    terminateGroup: async () => ({ confirmed: true, diagnostic: "group-gone" }),
  });
  const terminal = await promise;
  assert.equal(terminal.reviewStatus, "REVIEW_PASS");
  assert.equal(terminal.reviewVerdict.verdict, "PASS");
});

test("Grok 1.0.13 json-schema envelope validates structuredOutput as Reviewer verdict", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-envelope=${Date.now()}`);
  const head = "1".repeat(40);
  const verdict = { reviewed_head: head, critical: 0, important: 0, minor: [], findings: [], verdict: "PASS" };
  const stdout = JSON.stringify({
    text: JSON.stringify(verdict), stopReason: "end_turn", sessionId: "s", requestId: "r",
    usage: { input_tokens: 1, output_tokens: 1 }, num_turns: 1,
    structuredOutput: verdict,
  });
  const terminal = runtimeModule.classifyGrokReviewTerminal({
    exitCode: 0, stdout, stderr: "", timedOut: false, cleanupConfirmed: true, candidateRevision: head,
  });
  assert.equal(terminal.reviewStatus, "REVIEW_PASS");
  assert.equal(terminal.reviewVerdict.verdict, "PASS");
});

test("Grok json-schema envelope rejects conflicting text and structuredOutput", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-envelope-conflict=${Date.now()}`);
  const head = "2".repeat(40);
  const structured = { reviewed_head: head, critical: 0, important: 0, minor: [], findings: [], verdict: "PASS" };
  const conflicting = { reviewed_head: head, critical: 0, important: 1, minor: [], findings: [{ severity: "important", message: "bug" }], verdict: "FAIL" };
  const terminal = runtimeModule.classifyGrokReviewTerminal({
    exitCode: 0,
    stdout: JSON.stringify({ text: JSON.stringify(conflicting), structuredOutput: structured, stopReason: "end_turn" }),
    stderr: "", timedOut: false, cleanupConfirmed: true, candidateRevision: head,
  });
  assert.equal(terminal.reviewStatus, "REVIEW_OUTPUT_INVALID");
  assert.match(terminal.validationError, /structuredOutput.*text|text.*structuredOutput/i);
});

test("Grok Reviewer returns validated verdict after bounded cleanup even if CLI does not exit", async () => {
  const runtimeModule = await import(`../scripts/run_external_agent.mjs?review-verdict-first=${Date.now()}`);
  const { EventEmitter } = await import("node:events");
  const { PassThrough } = await import("node:stream");
  const child = new EventEmitter();
  child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.pid = 575757; child.exitCode = null; child.signalCode = null;
  const head = "3".repeat(40);
  let cleaned = 0;
  const oldReview = process.env.AD_GROK_REVIEW_ATTEMPT_TIMEOUT_MS;
  const oldStall = process.env.AD_GROK_STALL_TIMEOUT_MS;
  process.env.AD_GROK_REVIEW_ATTEMPT_TIMEOUT_MS = "1000";
  process.env.AD_GROK_STALL_TIMEOUT_MS = "800";
  try {
    const promise = runtimeModule.runMonitoredGrokReview("grok", [], {
      cwd: os.tmpdir(), env: process.env, candidateRevision: head,
      spawnChild: () => {
        queueMicrotask(() => {
          child.emit("spawn");
          child.stdout.write(JSON.stringify({ reviewed_head: head, critical: 0, important: 0, minor: [], findings: [], verdict: "PASS" }));
          // Intentionally never emit exit/close. Runtime must stop the provider group after verdict.
        });
        return child;
      },
      terminateGroup: async () => { cleaned += 1; child.exitCode = 0; return { confirmed: true, diagnostic: "verdict-terminal-group-gone" }; },
    });
    const terminal = await promise;
    assert.equal(terminal.reviewStatus, "REVIEW_PASS");
    assert.equal(terminal.reviewVerdict.verdict, "PASS");
    assert.equal(cleaned, 1);
    assert.match(terminal.cleanupDiagnostic, /verdict-terminal-group-gone/);
  } finally {
    oldReview === undefined ? delete process.env.AD_GROK_REVIEW_ATTEMPT_TIMEOUT_MS : process.env.AD_GROK_REVIEW_ATTEMPT_TIMEOUT_MS = oldReview;
    oldStall === undefined ? delete process.env.AD_GROK_STALL_TIMEOUT_MS : process.env.AD_GROK_STALL_TIMEOUT_MS = oldStall;
  }
});

test("run_external_agent direct execution survives symlinked filesystem path", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "adaptive-runner-symlink-entry-"));
  const realDir = path.join(root, "real");
  const linkDir = path.join(root, "link");
  await mkdir(realDir, { recursive: true });
  const copied = path.join(realDir, "run_external_agent.mjs");
  await copyFile(path.join(skillRoot, "scripts", "external_agent_protocol.mjs"), path.join(realDir, "external_agent_protocol.mjs"));
  await copyFile(adapter, copied);
  const { symlink } = await import("node:fs/promises");
  await symlink(realDir, linkDir, "dir");
  const viaLink = path.join(linkDir, "run_external_agent.mjs");
  const result = spawnSync(process.execPath, [viaLink,
    "--render-status-card", "--engine", "grok-build", "--auth-mode", "oauth",
    "--model", "grok-4.6", "--reasoning-effort", "low",
    "--work-package", "SYMLINK-DIRECT", "--category", "backend", "--status", "running",
    "--detail", "direct entry executed",
  ], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /SYMLINK-DIRECT/);
  assert.match(result.stdout, /direct entry executed/);
});
