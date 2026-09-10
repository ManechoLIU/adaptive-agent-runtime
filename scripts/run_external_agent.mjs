#!/usr/bin/env node

import { spawn, spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const KIMI_KEYCHAIN_SERVICE = "adaptive-delivery-kimi-k3";
const XAI_KEYCHAIN_SERVICE = "adaptive-delivery-xai-grok";
const REASONING_EFFORTS = new Set(["low", "medium", "high", "xhigh", "max"]);
const CARD_CATEGORIES = new Set(["frontend", "backend", "general"]);
const REVIEW_PHASES = new Set(["full", "shard", "synthesis"]);
const CARD_STATUSES = {
  running: { icon: "🟢", label: "运行中" },
  returned: { icon: "🟡", label: "已返回" },
  accepted: { icon: "✅", label: "已验收" },
  blocked: { icon: "🟠", label: "阻塞" },
  unknown: { icon: "🔴", label: "结果未知" },
};

const SAFE_FALLBACK_FAILURES = new Set([
  "provider_unavailable",
  "cli_unavailable",
  "transport_failure_before_write",
  "no_valid_result",
]);
const CONTROLLER_HOSTS = new Set(["web", "desktop_codex"]);
const CURRENT_HOST_FALLBACK_FAILURES = new Set([
  "usage_limit_exceeded",
  "quota_exhausted",
  "model_unavailable",
  "service_unavailable",
  "auth_invalid",
  "runtime_unavailable",
]);

const DEFAULT_GROK_MAX_PROMPT_BYTES = 128 * 1024;
const DEFAULT_GROK_REVIEW_SHARD_TARGET_BYTES = 64 * 1024;
const DEFAULT_GROK_LAUNCH_TIMEOUT_MS = 10_000;
const DEFAULT_GROK_FIRST_OUTPUT_TIMEOUT_MS = 90_000;
const DEFAULT_GROK_STALL_TIMEOUT_MS = 180_000;
const DEFAULT_EXTERNAL_ATTEMPT_TIMEOUT_MS = 600_000;
const DEFAULT_GROK_REVIEW_ATTEMPT_TIMEOUT_MS = 90_000;
const DEFAULT_EXTERNAL_KILL_GRACE_MS = 5_000;
const GROK_REVIEW_MAX_TURNS = 2;
const GROK_REVIEW_SYSTEM_PROMPT = [
  "You are an independent pure-packet code Reviewer.",
  "The user prompt is the complete source packet and the only review source.",
  "Do not read or inspect the repository, do not call tools, do not browse, and do not enter planning mode.",
  "Your first valid model response must be the final structured verdict; do not preface it with planning or narration.",
  "PASS is allowed if and only if critical=0 and important=0. Otherwise verdict must be FAIL.",
].join(" ");
const GROK_REVIEW_JSON_SCHEMA = Object.freeze({
  type: "object",
  additionalProperties: false,
  required: ["reviewed_head", "critical", "important", "minor", "findings", "verdict"],
  properties: {
    reviewed_head: { type: "string", minLength: 1 },
    critical: { type: "integer", minimum: 0 },
    important: { type: "integer", minimum: 0 },
    minor: { type: "array", items: { type: "string", minLength: 1 } },
    findings: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["severity", "message"],
        properties: {
          severity: { type: "string", enum: ["critical", "important"] },
          message: { type: "string", minLength: 1 },
        },
      },
    },
    verdict: { type: "string", enum: ["PASS", "FAIL"] },
  },
});
const GROK_MODEL_PROGRESS_SESSION_UPDATES = new Set([
  "agent_message_chunk",
  "agent_thought_chunk",
  "tool_call",
  "tool_call_update",
]);

class ExternalAgentExecutionError extends Error {
  constructor(message, { failureClass = "transport_error", retrySafe = true, resultUnknown = false, details = {}, reviewStatus = null } = {}) {
    super(message);
    this.name = "ExternalAgentExecutionError";
    this.failureClass = failureClass;
    this.retrySafe = Boolean(retrySafe);
    this.resultUnknown = Boolean(resultUnknown);
    this.details = details && typeof details === "object" && !Array.isArray(details) ? details : {};
    this.reviewStatus = reviewStatus ? String(reviewStatus) : null;
  }
}

export function classifyExternalExecutionFailure(error, { sideEffect = false } = {}) {
  const providerFailureClass = String(error?.failureClass || "transport_error");
  const providerBoundaryCrossed = Boolean(error?.details?.provider_started);
  const providerResultUnknown = Boolean(error?.resultUnknown);
  const sideEffectBoundaryUnknown = Boolean(sideEffect) && providerBoundaryCrossed && !providerResultUnknown;
  const resultUnknown = providerResultUnknown || sideEffectBoundaryUnknown;
  const failureClass = sideEffectBoundaryUnknown ? "result_unknown" : providerFailureClass;
  const retrySafe = resultUnknown ? false : error?.retrySafe !== false;
  const failureDetails = {
    ...(error?.details && typeof error.details === "object" && !Array.isArray(error.details) ? error.details : {}),
    ...(sideEffectBoundaryUnknown && providerFailureClass !== "result_unknown"
      ? { underlying_failure_class: providerFailureClass, provider_failure_class: providerFailureClass }
      : {}),
  };
  return { failureClass, retrySafe, resultUnknown, failureDetails };
}

function boundedEnvInteger(name, fallback, { min = 1, max = Number.MAX_SAFE_INTEGER } = {}) {
  const raw = process.env[name];
  if (raw === undefined || raw === null || String(raw).trim() === "") return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new Error(`${name} must be an integer within ${min}..${max}`);
  }
  return value;
}

function grokMaxPromptBytes() {
  return boundedEnvInteger("AD_GROK_MAX_PROMPT_BYTES", DEFAULT_GROK_MAX_PROMPT_BYTES, { min: 64, max: 1024 * 1024 });
}

function grokReviewShardTargetBytes(maxBytes = grokMaxPromptBytes()) {
  const fallback = Math.min(DEFAULT_GROK_REVIEW_SHARD_TARGET_BYTES, maxBytes);
  return boundedEnvInteger("AD_GROK_REVIEW_SHARD_TARGET_BYTES", fallback, { min: 32, max: maxBytes });
}

function grokLaunchTimeoutMs() {
  return boundedEnvInteger("AD_GROK_LAUNCH_TIMEOUT_MS", DEFAULT_GROK_LAUNCH_TIMEOUT_MS, { min: 10, max: 60_000 });
}

function grokFirstOutputTimeoutMs() {
  return boundedEnvInteger("AD_GROK_FIRST_OUTPUT_TIMEOUT_MS", DEFAULT_GROK_FIRST_OUTPUT_TIMEOUT_MS, { min: 10, max: 30 * 60 * 1000 });
}

function grokStallTimeoutMs() {
  return boundedEnvInteger("AD_GROK_STALL_TIMEOUT_MS", DEFAULT_GROK_STALL_TIMEOUT_MS, { min: 10, max: 30 * 60 * 1000 });
}

function externalAttemptTimeoutMs(progressDeadlineMinutes) {
  const assignmentBound = Number.isInteger(progressDeadlineMinutes) && progressDeadlineMinutes > 0
    ? progressDeadlineMinutes * 60 * 1000
    : DEFAULT_EXTERNAL_ATTEMPT_TIMEOUT_MS;
  const configured = boundedEnvInteger(
    "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS", assignmentBound, { min: 10, max: 30 * 60 * 1000 },
  );
  return Math.min(configured, assignmentBound);
}

function externalKillGraceMs() {
  return boundedEnvInteger("AD_EXTERNAL_KILL_GRACE_MS", DEFAULT_EXTERNAL_KILL_GRACE_MS, { min: 10, max: 60_000 });
}
function grokReviewAttemptTimeoutMs(progressDeadlineMinutes) {
  const outerBound = externalAttemptTimeoutMs(progressDeadlineMinutes);
  const fallback = Math.min(DEFAULT_GROK_REVIEW_ATTEMPT_TIMEOUT_MS, outerBound);
  const configured = boundedEnvInteger(
    "AD_GROK_REVIEW_ATTEMPT_TIMEOUT_MS", fallback, { min: 10, max: 10 * 60 * 1000 },
  );
  return Math.min(configured, outerBound);
}


function meaningfulStreamValue(value) {
  if (value === null || value === undefined) return false;
  if (typeof value === "string") return Boolean(value.trim());
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return typeof value === "number" || typeof value === "boolean";
}

function grokModelProgressKind(event) {
  if (!event || typeof event !== "object" || Array.isArray(event)) return null;

  // Grok Build 1.0.13 streaming-json emits model/tool activity as top-level
  // typed records rather than only the older ACP session-update envelope.
  // Match the observed provider schema narrowly so metadata or a caller-crafted
  // bare type field cannot keep a stalled provider alive.
  const streamingType = typeof event.type === "string" ? event.type.trim() : "";
  if (streamingType === "text" || streamingType === "thought") {
    return typeof event.data === "string" && event.data.trim()
      ? streamingType
      : null;
  }
  if (streamingType === "tool_call") {
    const toolCallId = [event.toolCallId, event.tool_call_id].find(
      (value) => typeof value === "string" && value.trim(),
    );
    const toolName = [event.toolName, event.tool_name].find(
      (value) => typeof value === "string" && value.trim(),
    );
    const rawInput = event.rawInput ?? event.raw_input;
    if (toolCallId && toolName && rawInput && typeof rawInput === "object" && !Array.isArray(rawInput)) {
      return streamingType;
    }
    return null;
  }
  if (streamingType === "tool_call_update") {
    const toolCallId = [event.toolCallId, event.tool_call_id].find(
      (value) => typeof value === "string" && value.trim(),
    );
    if (!toolCallId) return null;
    const hasStatus = typeof event.status === "string" && event.status.trim();
    const hasContent = Array.isArray(event.content) && event.content.some((item) => meaningfulStreamValue(item));
    const rawOutput = event.rawOutput ?? event.raw_output;
    const hasRawOutput = meaningfulStreamValue(rawOutput);
    const hasLocations = Array.isArray(event.locations) && event.locations.some(
      (item) => item && typeof item === "object" && !Array.isArray(item)
        && typeof item.path === "string" && item.path.trim(),
    );
    return hasStatus || hasContent || hasRawOutput || hasLocations
      ? streamingType
      : null;
  }

  const sessionId = [event.sessionId, event.session_id].find(
    (value) => typeof value === "string" && value.trim(),
  );
  if (!sessionId) return null;
  const update = event.update;
  if (!update || typeof update !== "object" || Array.isArray(update)) return null;
  for (const key of ["sessionUpdate", "session_update"]) {
    const value = update[key];
    if (typeof value === "string" && GROK_MODEL_PROGRESS_SESSION_UPDATES.has(value.trim())) {
      return value.trim();
    }
  }
  return null;
}

function removeDirectoryConfirmed(directory) {
  rmSync(directory, { recursive: true, force: true });
  if (existsSync(directory)) {
    throw new Error(`resource still exists after cleanup: ${directory}`);
  }
}

export function runCleanupStack(cleanups, priorError = null) {
  const failures = [];
  for (const entry of [...cleanups].reverse()) {
    const label = typeof entry === "function" ? "external_resource" : String(entry?.label || "external_resource");
    const cleanup = typeof entry === "function" ? entry : entry?.cleanup;
    if (typeof cleanup !== "function") {
      failures.push({ label, error: "cleanup callback unavailable" });
      continue;
    }
    try {
      cleanup();
    } catch (error) {
      failures.push({ label, error: String(error?.message || error) });
    }
  }
  if (!failures.length) return;
  throw new ExternalAgentExecutionError(
    `cleanup_failed: ${failures.map((item) => `${item.label}: ${item.error}`).join("; ")}`,
    {
      failureClass: "cleanup_failed",
      retrySafe: false,
      resultUnknown: true,
      details: {
        failed_resources: failures.map((item) => item.label),
        cleanup_failures: failures,
        ...(priorError ? {
          prior_failure_class: String(priorError?.failureClass || "transport_error"),
          prior_error: String(priorError?.message || priorError),
          prior_retry_safe: priorError?.retrySafe !== false,
          prior_result_unknown: Boolean(priorError?.resultUnknown),
          ...(priorError?.details && typeof priorError.details === "object" && !Array.isArray(priorError.details)
            ? { prior_failure_details: { ...priorError.details } }
            : {}),
        } : {}),
      },
    },
  );
}

export function prepareGrokPrompt(prompt, {
  assignmentRole = null,
  createTempDir = mkdtempSync,
  writePrompt = writeFileSync,
  cleanupDirectory = removeDirectoryConfirmed,
} = {}) {
  const observedBytes = Buffer.byteLength(prompt, "utf8");
  const maxBytes = grokMaxPromptBytes();
  if (observedBytes > maxBytes) {
    const reviewer = String(assignmentRole || "").trim().toLowerCase() === "reviewer";
    const shardTargetBytes = grokReviewShardTargetBytes(maxBytes);
    const failureClass = reviewer ? "review_sharding_required" : "prompt_too_large";
    throw new ExternalAgentExecutionError(
      `${failureClass}: observed_bytes=${observedBytes} max_bytes=${maxBytes}`
        + (reviewer ? ` shard_target_bytes=${shardTargetBytes}; split the immutable review into bounded shards plus one final synthesis review` : ""),
      {
        failureClass,
        retrySafe: true,
        details: { observed_bytes: observedBytes, max_bytes: maxBytes, shard_target_bytes: reviewer ? shardTargetBytes : null },
      },
    );
  }
  let directory;
  try {
    directory = createTempDir(path.join(tmpdir(), "adaptive-delivery-grok-prompt-"));
  } catch (error) {
    throw new ExternalAgentExecutionError(`prompt_file_prepare_failed: ${error.message}`, {
      failureClass: "prompt_file_prepare_failed", retrySafe: true, resultUnknown: false,
      details: { prepare_error: String(error?.message || error) },
    });
  }
  const promptPath = path.join(directory, "prompt.txt");
  try {
    writePrompt(promptPath, prompt, { encoding: "utf8", mode: 0o600, flag: "wx" });
  } catch (error) {
    const writeError = new ExternalAgentExecutionError(`prompt_file_write_failed: ${error.message}`, {
      failureClass: "prompt_file_write_failed", retrySafe: true, resultUnknown: false,
      details: { write_error: String(error?.message || error) },
    });
    try {
      cleanupDirectory(directory);
    } catch (cleanupError) {
      throw new ExternalAgentExecutionError(
        `cleanup_failed: prompt_file: ${cleanupError.message}; prior=${writeError.message}`,
        {
          failureClass: "cleanup_failed", retrySafe: false, resultUnknown: true,
          details: {
            failed_resources: ["prompt_file"],
            cleanup_failures: [{ label: "prompt_file", error: String(cleanupError?.message || cleanupError) }],
            prior_failure_class: writeError.failureClass,
            prior_error: writeError.message,
          },
        },
      );
    }
    throw writeError;
  }
  return {
    path: promptPath,
    bytes: observedBytes,
    cleanup: () => cleanupDirectory(directory),
  };
}

function childExited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

function processGroupExists(pid) {
  if (!pid || process.platform === "win32") return false;
  try {
    process.kill(-pid, 0);
    return true;
  } catch (error) {
    if (error && error.code === "ESRCH") return false;
    if (error && error.code === "EPERM") return true;
    throw error;
  }
}

function signalProcessGroup(child, signal) {
  if (!child.pid) return;
  if (process.platform === "win32") {
    child.kill(signal);
    return;
  }
  try {
    process.kill(-child.pid, signal);
  } catch (error) {
    if (error && error.code === "ESRCH") return;
    throw error;
  }
}

function waitForChildExit(child, timeoutMs) {
  if (childExited(child)) return Promise.resolve(true);
  return new Promise((resolve) => {
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      child.off("exit", onExit);
      resolve(value);
    };
    const onExit = () => finish(true);
    const timer = setTimeout(() => finish(childExited(child)), Math.max(0, timeoutMs));
    child.once("exit", onExit);
  });
}

async function waitForProcessGroupGone(pid, timeoutMs) {
  if (!pid || process.platform === "win32") return true;
  const deadline = Date.now() + Math.max(0, timeoutMs);
  while (true) {
    if (!processGroupExists(pid)) return true;
    const remaining = deadline - Date.now();
    if (remaining <= 0) return false;
    await new Promise((resolve) => setTimeout(resolve, Math.min(25, remaining)));
  }
}

async function terminateProcessGroup(child, graceMs) {
  const notes = [];
  try {
    signalProcessGroup(child, "SIGTERM");
    notes.push("SIGTERM");
  } catch (error) {
    return { confirmed: false, diagnostic: `SIGTERM failed: ${error.message}` };
  }
  let groupGone = false;
  try {
    const [, observedGroupGone] = await Promise.all([
      waitForChildExit(child, graceMs),
      waitForProcessGroupGone(child.pid, graceMs),
    ]);
    groupGone = process.platform === "win32" ? childExited(child) : observedGroupGone;
  } catch (error) {
    return { confirmed: false, diagnostic: `process-group probe failed after SIGTERM: ${error.message}` };
  }
  if (groupGone && childExited(child)) return { confirmed: true, diagnostic: notes.join(";") };
  try {
    signalProcessGroup(child, "SIGKILL");
    notes.push("SIGKILL");
  } catch (error) {
    try {
      if (process.platform !== "win32" && !processGroupExists(child.pid) && childExited(child)) {
        return { confirmed: true, diagnostic: `${notes.join(";")}; group disappeared before SIGKILL` };
      }
    } catch {}
    return { confirmed: false, diagnostic: `${notes.join(";")}; SIGKILL failed: ${error.message}` };
  }
  try {
    const [, observedGroupGone] = await Promise.all([
      waitForChildExit(child, graceMs),
      waitForProcessGroupGone(child.pid, graceMs),
    ]);
    groupGone = process.platform === "win32" ? childExited(child) : observedGroupGone;
  } catch (error) {
    return { confirmed: false, diagnostic: `${notes.join(";")}; final process-group probe failed: ${error.message}` };
  }
  return {
    confirmed: groupGone && childExited(child),
    diagnostic: groupGone && childExited(child) ? notes.join(";") : `${notes.join(";")}; process group still alive`,
  };
}

function peerHost(controllerHost) {
  return controllerHost === "web" ? "desktop_codex" : "web";
}

function fallbackModel({ workType, complexity, highRisk }) {
  const normalizedType = String(workType || "").trim().toLowerCase();
  const normalizedComplexity = String(complexity || "").trim().toLowerCase();
  if (highRisk || normalizedComplexity === "high" || new Set(["architecture", "root-cause", "root_cause", "high-risk", "high_risk"]).has(normalizedType)) {
    return { model: "gpt-5.6-sol", reasoningEffort: "xhigh" };
  }
  if (normalizedComplexity === "low" && new Set(["mechanical", "narrow", "repetitive", "routine"]).has(normalizedType)) {
    return { model: "gpt-5.6-luna", reasoningEffort: "low" };
  }
  return { model: "gpt-5.6-terra", reasoningEffort: "medium" };
}

export function resolveDispatchRoute({
  preferredEngine, category, failureClass, workType = "implementation", complexity = "normal", highRisk = false,
  providerPinned = false, resultUnknown = false, partialWritePossible = false, billingBoundary = false, authorizationBoundary = false,
  controllerHost = null, currentHostFailureClass = null, peerHostAvailable = false,
}) {
  if (!routes[preferredEngine]) throw new Error(`Unsupported preferred engine: ${preferredEngine}`);
  if (!CARD_CATEGORIES.has(category)) throw new Error(`Unsupported category: ${category}`);
  for (const [flag, reason] of [
    [providerPinned, "provider_pinned"],
    [resultUnknown, "result_unknown"],
    [partialWritePossible, "partial_write_possible"],
    [billingBoundary, "billing_boundary"],
    [authorizationBoundary, "authorization_boundary"],
  ]) {
    if (flag) return { decision: "blocked", reason };
  }
  if (!SAFE_FALLBACK_FAILURES.has(String(failureClass || "").trim())) {
    return { decision: "blocked", reason: "failure_not_safe_for_fallback" };
  }
  if (controllerHost !== null && !CONTROLLER_HOSTS.has(controllerHost)) {
    return { decision: "blocked", reason: "unknown_controller_host" };
  }
  const tier = fallbackModel({ workType, complexity, highRisk });
  if (!currentHostFailureClass) {
    if (!controllerHost) {
      return { decision: "blocked", reason: "controller_host_required" };
    }
    return {
      decision: "fallback", executionRoute: "native-subagent", ...tier,
      controllerHost, executionHost: controllerHost, hostFallbackLevel: 1, reason: "safe_external_failure",
    };
  }
  if (!controllerHost) {
    return { decision: "blocked", reason: "controller_host_required" };
  }
  const normalizedHostFailure = String(currentHostFailureClass).trim().toLowerCase();
  if (!CURRENT_HOST_FALLBACK_FAILURES.has(normalizedHostFailure)) {
    return { decision: "blocked", reason: "current_host_failure_not_fallback_eligible" };
  }
  const requestedPeerHost = peerHost(controllerHost);
  if (!peerHostAvailable) {
    return { decision: "blocked", reason: "peer_host_unavailable", controllerHost, requestedPeerHost };
  }
  return {
    decision: "fallback", executionRoute: "native-subagent", ...tier,
    controllerHost, executionHost: requestedPeerHost, hostFallbackLevel: 2,
    reason: `${controllerHost}_internal_${normalizedHostFailure}`,
  };
}


const routes = {
  "kimi-code": {
    cardMarker: "🟣",
    displayName: "Kimi K3",
    executable: "kimi",
    fallbackPaths: [path.join(homedir(), ".kimi-code", "bin", "kimi")],
    modelsByAuthMode: {
      oauth: new Set(["kimi-code/k3"]),
      api: new Set(["kimi-k3"]),
    },
    versionArgs: ["--version"],
  },
  "grok-build": {
    cardMarker: "🟦",
    displayName: "Grok 4.6",
    executable: "grok",
    fallbackPaths: [
      path.join(homedir(), ".grok", "bin", "grok"),
      path.join(homedir(), ".local", "bin", "grok"),
    ],
    modelsByAuthMode: {
      oauth: new Set(["grok-4.6"]),
      api: new Set(["grok-4.6"]),
    },
    versionArgs: ["version"],
  },
};

function assertSingleLine(value, name, maxLength) {
  if (!value) throw new Error(`${name} is required`);
  if (value.length > maxLength) throw new Error(`${name} must be at most ${maxLength} characters`);
  if (/[\u0000-\u001f\u007f]/u.test(value)) throw new Error(`${name} must be single-line text`);
}

function assertCardRoute({ engine, model, authMode, reasoningEffort }) {
  const route = routes[engine];
  if (!route) throw new Error(`Unsupported engine: ${engine}`);
  const models = route.modelsByAuthMode[authMode];
  if (!models) throw new Error(`Unsupported auth mode: ${authMode}`);
  if (!models.has(model)) {
    throw new Error(`Model ${model} is not allowed for engine ${engine} with auth mode ${authMode}`);
  }
  if (!reasoningEffort) throw new Error("--reasoning-effort is required");
  if (!REASONING_EFFORTS.has(reasoningEffort)) {
    throw new Error(`Unsupported reasoning effort: ${reasoningEffort}`);
  }
  return route;
}

export function renderExternalAgentCard({
  engine, model, authMode, reasoningEffort, workPackage, category, status, detail,
}) {
  const route = assertCardRoute({ engine, model, authMode, reasoningEffort });
  assertSingleLine(workPackage, "--work-package", 120);
  assertSingleLine(detail, "--detail", 180);
  if (!CARD_CATEGORIES.has(category)) throw new Error(`Unsupported card category: ${category}`);
  const statusDisplay = CARD_STATUSES[status];
  if (!statusDisplay) throw new Error(`Unsupported card status: ${status}`);
  return [
    `╭─ ${route.cardMarker} ${route.displayName} (${model}) · ${statusDisplay.icon} ${statusDisplay.label}`,
    `│ ${workPackage} · ${category} · ${authMode} · ${reasoningEffort}`,
    `╰─ ${detail}`,
  ].join("\n");
}

function kimiKeychainService() {
  return process.env.KIMI_K3_KEYCHAIN_SERVICE || KIMI_KEYCHAIN_SERVICE;
}

function xaiKeychainService() {
  return process.env.XAI_GROK_KEYCHAIN_SERVICE || XAI_KEYCHAIN_SERVICE;
}

function resolveExecutable(route) {
  for (const directory of (process.env.PATH || "").split(path.delimiter)) {
    if (!directory) continue;
    const candidate = path.join(directory, route.executable);
    if (existsSync(candidate)) return candidate;
  }
  return route.fallbackPaths.find((candidate) => existsSync(candidate)) || route.executable;
}

export function parseArgs(argv) {
  const options = {
    check: false,
    execute: false,
    login: false,
    renderStatusCard: false,
    resolveRoute: false,
    authorizedExternalCall: false,
    authorizedLogin: false,
    deviceAuth: false,
    engine: null,
    model: null,
    reasoningEffort: null,
    authMode: null,
    region: null,
    cwd: null,
    runtimeRepo: null,
    workPackage: null, category: null, status: null, detail: null,
    assignmentId: null, taskId: null, agentId: null, sessionId: null,
    attempt: 1, leaseId: null, runtimeReceipts: null, assignmentAck: null, deliveryReceipt: null, terminalReceipt: null, resultPath: null,
    workType: null, complexity: null, failureClass: null, controllerHost: null, currentHostFailureClass: null, peerHostAvailable: false, highRisk: false, providerPinned: false, resultUnknown: false, partialWritePossible: false, billingBoundary: false, authorizationBoundary: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--check") options.check = true;
    else if (argument === "--execute") options.execute = true;
    else if (argument === "--login") options.login = true;
    else if (argument === "--render-status-card") options.renderStatusCard = true;
    else if (argument === "--resolve-route") options.resolveRoute = true;
    else if (argument === "--authorized-external-call") options.authorizedExternalCall = true;
    else if (argument === "--authorized-login") options.authorizedLogin = true;
    else if (argument === "--device-auth") options.deviceAuth = true;
    else if (argument === "--high-risk") options.highRisk = true;
    else if (argument === "--provider-pinned") options.providerPinned = true;
    else if (argument === "--result-unknown") options.resultUnknown = true;
    else if (argument === "--partial-write-possible") options.partialWritePossible = true;
    else if (argument === "--billing-boundary") options.billingBoundary = true;
    else if (argument === "--authorization-boundary") options.authorizationBoundary = true;
    else if (argument === "--peer-host-available") options.peerHostAvailable = true;
    else if (["--engine", "--model", "--reasoning-effort", "--auth-mode", "--region", "--cwd", "--runtime-repo", "--work-package", "--category", "--status", "--detail", "--assignment-id", "--task-id", "--agent-id", "--session-id", "--attempt", "--lease-id", "--runtime-receipts", "--assignment-ack", "--delivery-receipt", "--terminal-receipt", "--result-path", "--work-type", "--complexity", "--failure-class", "--controller-host", "--current-host-failure-class"].includes(argument)) {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) throw new Error(`Missing value for ${argument}`);
      if (argument === "--auth-mode") options.authMode = value;
      else if (argument === "--reasoning-effort") options.reasoningEffort = value;
      else if (argument === "--assignment-id") options.assignmentId = value;
      else if (argument === "--task-id") options.taskId = value;
      else if (argument === "--agent-id") options.agentId = value;
      else if (argument === "--session-id") options.sessionId = value;
      else if (argument === "--attempt") options.attempt = Number.parseInt(value, 10);
      else if (argument === "--lease-id") options.leaseId = value;
      else if (argument === "--runtime-receipts") options.runtimeReceipts = value;
      else if (argument === "--runtime-repo") options.runtimeRepo = value;
      else if (argument === "--work-package") options.workPackage = value;
      else if (argument === "--assignment-ack") options.assignmentAck = value;
      else if (argument === "--delivery-receipt") options.deliveryReceipt = value;
      else if (argument === "--terminal-receipt") options.terminalReceipt = value;
      else if (argument === "--result-path") options.resultPath = value;
      else if (argument === "--work-type") options.workType = value;
      else if (argument === "--complexity") options.complexity = value;
      else if (argument === "--failure-class") options.failureClass = value;
      else if (argument === "--controller-host") options.controllerHost = value;
      else if (argument === "--current-host-failure-class") options.currentHostFailureClass = value;
      else options[argument.slice(2)] = value;
      index += 1;
    } else {
      throw new Error(`Unknown argument: ${argument}`);
    }
  }

  if ([options.check, options.execute, options.login, options.renderStatusCard, options.resolveRoute].filter(Boolean).length !== 1) {
    throw new Error("Choose exactly one of --check, --execute, --login, --render-status-card, or --resolve-route");
  }
  if (options.engine === "kimi-code-api") {
    options.engine = "kimi-code";
    options.authMode ||= "api";
  }
  if (options.resolveRoute) {
    const decision = resolveDispatchRoute({
      preferredEngine: options.engine, category: options.category, failureClass: options.failureClass,
      workType: options.workType, complexity: options.complexity, highRisk: options.highRisk,
      providerPinned: options.providerPinned, resultUnknown: options.resultUnknown, partialWritePossible: options.partialWritePossible,
      billingBoundary: options.billingBoundary, authorizationBoundary: options.authorizationBoundary,
      controllerHost: options.controllerHost, currentHostFailureClass: options.currentHostFailureClass, peerHostAvailable: options.peerHostAvailable,
    });
    options.routeDecision = decision;
    return options;
  }
  const route = routes[options.engine];
  if (!route) throw new Error(`Unsupported engine: ${options.engine}`);
  if (!options.authMode) throw new Error("--auth-mode oauth|api is required");
  const models = route.modelsByAuthMode[options.authMode];
  if (!models) throw new Error(`Unsupported auth mode: ${options.authMode}`);

  if (options.renderStatusCard) {
    renderExternalAgentCard(options);
    return options;
  }

  if (!options.cwd) throw new Error("--cwd is required");

  if (options.login) {
    if (options.authMode !== "oauth") throw new Error("--login supports only --auth-mode oauth");
    if (!options.authorizedLogin) throw new Error("--login requires --authorized-login");
    if (options.region && !["mainland-cn", "global"].includes(options.region)) {
      throw new Error("--region must be mainland-cn or global");
    }
    if (options.region && options.engine !== "kimi-code") {
      throw new Error("--region applies only to Kimi Code login");
    }
    if (options.deviceAuth && options.engine !== "grok-build") {
      throw new Error("--device-auth applies only to Grok Build login");
    }
    return options;
  }

  if (!models.has(options.model)) {
    throw new Error(
      `Model ${options.model} is not allowed for engine ${options.engine} with auth mode ${options.authMode}`,
    );
  }
  if (!options.reasoningEffort) throw new Error("--reasoning-effort is required");
  if (!REASONING_EFFORTS.has(options.reasoningEffort)) {
    throw new Error(`Unsupported reasoning effort: ${options.reasoningEffort}`);
  }
  if (options.execute && !options.authorizedExternalCall) {
    throw new Error("--execute requires --authorized-external-call after current user authorization");
  }
  if (options.execute) {
    const identity = [options.assignmentId, options.taskId, options.agentId, options.sessionId];
    const bound = identity.some(Boolean);
    if (bound && identity.some((value) => !value)) {
      throw new Error("Assignment-bound --execute requires assignment/task/agent/session identity");
    }
    if (bound && !options.assignmentAck) {
      throw new Error("Assignment-bound --execute requires --assignment-ack");
    }
    if (!bound && options.assignmentAck) {
      throw new Error("--assignment-ack requires Assignment-bound identity");
    }
  }
  return options;
}

function gitFact(cwd, args, label) {
  const result = spawnSync("git", ["-C", cwd, ...args], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(`cannot resolve ${label}: ${(result.stderr || result.stdout).trim()}`);
  return result.stdout.trim();
}

function runtimeRepository(options) {
  return options.runtimeRepo || options.cwd;
}

function validateRuntimeBinding(options) {
  if (!options.assignmentId) return;
  const executionCommon = gitFact(options.cwd, ["rev-parse", "--git-common-dir"], "execution git common-dir");
  const executionCommonBase = path.basename(path.resolve(options.cwd, executionCommon));
  if (!options.runtimeRepo && executionCommonBase !== ".git") {
    throw new Error("canonical runtime repo is required for assignment-bound execution from a nonstandard Git common-dir; pass --runtime-repo");
  }
  const runtimeRepo = runtimeRepository(options);
  const runtimeRoot = gitFact(runtimeRepo, ["rev-parse", "--show-toplevel"], "runtime repository");
  options.runtimeRepo = runtimeRoot;
}

function validateAssignmentLaunch(options) {
  if (!options.assignmentId) return;
  let assignment;
  try {
    assignment = JSON.parse(readFileSync(options.assignmentAck, "utf8"));
  } catch (error) {
    throw new Error(`assignment-ack is unreadable: ${error.message}`);
  }
  if (!assignment || typeof assignment !== "object" || Array.isArray(assignment)) {
    throw new Error("assignment-ack must contain one Assignment object");
  }
  const state = String(assignment.state || "").toUpperCase();
  if (!new Set(["ACKED", "ACTIVE"]).has(state)) {
    throw new Error("assignment-ack launch state must be ACKED or ACTIVE");
  }
  const contractVersion = assignment.assignment_contract_version === undefined ? 1 : Number(assignment.assignment_contract_version);
  if (!Number.isInteger(contractVersion) || contractVersion < 1) {
    throw new Error("assignment-ack assignment_contract_version must be a positive integer");
  }
  if (contractVersion >= 2 && typeof assignment.side_effect !== "boolean") {
    throw new Error("assignment-ack requires explicit side_effect contract");
  }
  if (assignment.side_effect !== undefined && typeof assignment.side_effect !== "boolean") {
    throw new Error("assignment-ack side_effect must be boolean when provided");
  }
  if (assignment.idempotency_key !== null && assignment.idempotency_key !== undefined && typeof assignment.idempotency_key !== "string") {
    throw new Error("assignment-ack idempotency_key must be a string or null");
  }
  if (typeof assignment.idempotency_key === "string" && assignment.idempotency_key.trim().length === 0) {
    throw new Error("assignment-ack idempotency_key cannot be blank");
  }
  if (contractVersion < 2 && assignment.idempotency_key !== null && assignment.idempotency_key !== undefined) {
    throw new Error("legacy assignment-ack cannot declare idempotency_key without v2 side-effect contract");
  }
  assignment.assignment_contract_version = contractVersion;
  assignment.progress_deadline_minutes = assignmentProgressDeadlineMinutes(assignment);
  const assignmentRole = String(assignment.role || "").trim().toLowerCase();
  const candidateRevision = String(assignment.candidate_revision || "").trim();
  if (assignmentRole === "reviewer") {
    if (!candidateRevision) {
      throw new Error("reviewer assignment-ack requires immutable candidate_revision");
    }
    let resolvedCandidate;
    try {
      resolvedCandidate = gitFact(options.cwd, ["rev-parse", "--verify", `${candidateRevision}^{commit}`], "reviewer candidate revision");
    } catch (error) {
      throw new Error(`reviewer candidate_revision must resolve to an immutable commit: ${error.message}`);
    }
    if (resolvedCandidate !== candidateRevision) {
      throw new Error("reviewer candidate_revision must be the exact immutable commit, not a branch, tag, or abbreviation");
    }
    const reviewPhase = String(assignment.review_phase || "").trim().toLowerCase();
    if (!REVIEW_PHASES.has(reviewPhase)) {
      throw new Error("reviewer assignment-ack review_phase must be explicit full|shard|synthesis");
    }
    const rawShardReceipts = assignment.review_shard_receipts;
    let reviewShardReceipts = [];
    if (reviewPhase === "synthesis") {
      if (!Array.isArray(rawShardReceipts) || rawShardReceipts.length === 0) {
        throw new Error("reviewer synthesis requires non-empty review_shard_receipts");
      }
      reviewShardReceipts = rawShardReceipts.map((item) => String(item || "").trim());
      if (reviewShardReceipts.some((item) => !item.startsWith("receipt:") || item.slice(8).trim().length === 0)) {
        throw new Error("review_shard_receipts must contain receipt: locators only");
      }
      if (new Set(reviewShardReceipts).size !== reviewShardReceipts.length) {
        throw new Error("review_shard_receipts must be unique");
      }
    } else if (rawShardReceipts !== undefined && rawShardReceipts !== null && !(Array.isArray(rawShardReceipts) && rawShardReceipts.length === 0)) {
      throw new Error("review_shard_receipts are valid only for synthesis reviewer assignments");
    }
    assignment.review_phase = reviewPhase;
    assignment.review_shard_receipts = reviewShardReceipts;
  } else {
    if (assignment.review_phase !== undefined && assignment.review_phase !== null) {
      throw new Error("assignment-ack review_phase is valid only for reviewer assignments");
    }
    if (assignment.review_shard_receipts !== undefined && assignment.review_shard_receipts !== null) {
      throw new Error("assignment-ack review_shard_receipts are valid only for reviewer assignments");
    }
    assignment.review_phase = null;
    assignment.review_shard_receipts = [];
  }
  const repositoryRoot = gitFact(options.cwd, ["rev-parse", "--show-toplevel"], "launch repository");
  const branch = gitFact(options.cwd, ["branch", "--show-current"], "launch branch");
  const head = gitFact(options.cwd, ["rev-parse", "HEAD"], "launch revision");
  const guard = fileURLToPath(new URL("./assignment_lease_guard.py", import.meta.url));
  const python = process.env.AD_PYTHON || "python3";
  const result = spawnSync(python, [
    guard, options.assignmentAck,
    "--expected-assignment-id", options.assignmentId,
    "--expected-task-id", options.taskId,
    "--expected-agent-id", options.agentId,
    "--expected-repository-root", repositoryRoot,
    "--expected-branch", branch,
    "--expected-head", head,
    "--expected-provider", options.engine,
    "--expected-model", options.model,
    "--expected-auth-mode", options.authMode,
    "--runtime-repo", runtimeRepository(options),
  ], { encoding: "utf8" });
  if (result.error) throw new Error(`assignment-ack validation failed: ${result.error.message}`);
  if (result.status !== 0) {
    throw new Error(`assignment-ack validation failed: ${(result.stdout || result.stderr).trim()}`);
  }
  return assignment;
}

const LINEAGE_WHITESPACE_RE = /[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+/gu;

function normalizeLineageText(value) {
  return String(value).replace(LINEAGE_WHITESPACE_RE, " ").replace(/^ +| +$/gu, "");
}

function compareCodePoints(left, right) {
  const leftPoints = Array.from(left);
  const rightPoints = Array.from(right);
  for (let index = 0; index < Math.min(leftPoints.length, rightPoints.length); index += 1) {
    const difference = leftPoints[index].codePointAt(0) - rightPoints[index].codePointAt(0);
    if (difference !== 0) return difference;
  }
  return leftPoints.length - rightPoints.length;
}

function deriveExecutionLineage(options, assignment) {
  const discriminator = assignment.strategy_discriminator === undefined ? "" : normalizeLineageText(assignment.strategy_discriminator);
  const strategy = [
    `engine=${options.engine}`,
    `model=${options.model}`,
    `auth_mode=${options.authMode}`,
    `reasoning_effort=${options.reasoningEffort}`,
    discriminator && `discriminator=${discriminator}`,
  ].filter(Boolean).join(";");
  const contract = {
    owned_scope: assignment.owned_scope.map(normalizeLineageText).sort(compareCodePoints),
    primary_goal: normalizeLineageText(assignment.primary_goal),
    strategy: normalizeLineageText(strategy),
    success_criteria: assignment.success_criteria.map(normalizeLineageText).sort(compareCodePoints),
    task_id: normalizeLineageText(options.taskId),
  };
  const canonical = JSON.stringify(contract);
  return {
    primary_goal: contract.primary_goal,
    success_criteria: contract.success_criteria,
    owned_scope: contract.owned_scope,
    strategy: contract.strategy,
    execution_lineage_id: createHash("sha256").update(canonical).digest("hex"),
  };
}

const PASS_EVIDENCE_SCHEMES = new Set(["test-log", "green-test", "receipt", "git", "file", "artifact"]);
const PASS_ARTIFACT_SCHEMES = new Set(["git", "file", "artifact"]);
const IMPLEMENTATION_PROGRESS_DEADLINE_MINUTES = 10;
const MAX_ASSIGNMENT_PROGRESS_DEADLINE_MINUTES = 30;

function assignmentProgressDeadlineMinutes(assignment) {
  const raw = assignment.progress_deadline_minutes;
  if (raw === undefined || raw === null) return IMPLEMENTATION_PROGRESS_DEADLINE_MINUTES;
  if (!Number.isInteger(raw) || raw < 1 || raw > MAX_ASSIGNMENT_PROGRESS_DEADLINE_MINUTES) {
    throw new Error("assignment-ack progress_deadline_minutes must be a positive integer within 1..30");
  }
  return raw;
}

function isTraceableLocator(value, schemes) {
  if (typeof value !== "string") return false;
  const token = value.trim();
  const separator = token.indexOf(":");
  if (separator <= 0) return false;
  return schemes.has(token.slice(0, separator)) && token.slice(separator + 1).trim().length > 0;
}

export function readDeliveryReceipt(pathname, {
  assignmentRole = null, candidateRevision = null, reviewPhase = null, reviewShardReceipts = [],
} = {}) {
  if (!pathname) return null;
  let receipt;
  try {
    receipt = JSON.parse(readFileSync(pathname, "utf8"));
  } catch (error) {
    throw new Error(`delivery-receipt is unreadable: ${error.message}`);
  }
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) {
    throw new Error("delivery-receipt must contain one delivery object");
  }
  const deliveryOutcome = String(receipt.delivery_outcome || "").toLowerCase();
  if (!new Set(["pass", "fail", "blocked", "unresolved"]).has(deliveryOutcome)) {
    throw new Error("delivery-receipt requires delivery_outcome pass|fail|blocked|unresolved");
  }
  const summary = String(receipt.summary || "").trim();
  if (!summary || !Array.isArray(receipt.evidence) || !Array.isArray(receipt.artifacts)) {
    throw new Error("delivery-receipt requires summary, evidence[], and artifacts[]");
  }
  if (!String(receipt.next_action || "").trim() || !String(receipt.retry_class || "").trim()) {
    throw new Error("delivery-receipt requires next_action and retry_class");
  }
  if (deliveryOutcome === "pass" && (receipt.evidence.length === 0 || receipt.artifacts.length === 0)) {
    throw new Error("delivery PASS requires evidence and artifact");
  }
  if (deliveryOutcome === "pass" && (!receipt.evidence.every((item) => isTraceableLocator(item, PASS_EVIDENCE_SCHEMES)) || !receipt.artifacts.every((item) => isTraceableLocator(item, PASS_ARTIFACT_SCHEMES)))) {
    throw new Error("delivery PASS requires traceable evidence and artifact");
  }
  if (receipt.reconciliation_evidence !== undefined && !Array.isArray(receipt.reconciliation_evidence)) {
    throw new Error("delivery-receipt reconciliation_evidence must be an array when provided");
  }
  const normalizedRole = String(assignmentRole || "").trim().toLowerCase();
  const normalizedPhase = String(reviewPhase || "").trim().toLowerCase();
  if (normalizedRole === "reviewer" && !REVIEW_PHASES.has(normalizedPhase)) {
    throw new Error("reviewer delivery validation requires explicit review_phase=full|shard|synthesis");
  }
  const expectedShardReceipts = Array.isArray(reviewShardReceipts)
    ? reviewShardReceipts.map((item) => String(item || "").trim())
    : [];
  if (normalizedRole === "reviewer" && normalizedPhase === "synthesis") {
    if (expectedShardReceipts.length === 0
        || expectedShardReceipts.some((item) => !item.startsWith("receipt:") || item.slice(8).trim().length === 0)
        || new Set(expectedShardReceipts).size !== expectedShardReceipts.length) {
      throw new Error("reviewer synthesis delivery requires exact assigned review_shard_receipts");
    }
  }
  let reviewVerdict;
  if (receipt.review_verdict !== undefined) {
    const verdict = receipt.review_verdict;
    const keys = verdict && typeof verdict === "object" && !Array.isArray(verdict) ? Object.keys(verdict).sort() : [];
    const expectedKeys = ["critical", "important", "minor", "reviewed_head", "verdict"].sort();
    if (JSON.stringify(keys) !== JSON.stringify(expectedKeys)
        || !["PASS", "FINDINGS"].includes(verdict.verdict)
        || typeof verdict.reviewed_head !== "string" || !verdict.reviewed_head.trim()
        || !["critical", "important", "minor"].every((key) => Array.isArray(verdict[key]) && verdict[key].every((item) => typeof item === "string" && item.trim()))) {
      throw new Error("delivery-receipt review_verdict is invalid");
    }
    const findings = verdict.critical.length + verdict.important.length + verdict.minor.length;
    if ((verdict.verdict === "PASS" && findings !== 0) || (verdict.verdict === "FINDINGS" && findings === 0)) {
      throw new Error("delivery-receipt review_verdict conflicts with findings");
    }
    const expectedOutcome = verdict.verdict === "PASS" ? "pass" : "fail";
    if (deliveryOutcome !== expectedOutcome) throw new Error("delivery-receipt review_verdict conflicts with delivery_outcome");
    const expectedHead = String(candidateRevision || "").trim();
    if (normalizedRole !== "reviewer") {
      throw new Error("delivery-receipt review_verdict is valid only for reviewer assignments");
    }
    if (!expectedHead) {
      throw new Error("delivery-receipt review_verdict requires immutable candidate_revision");
    }
    if (verdict.reviewed_head !== expectedHead) {
      throw new Error("delivery-receipt review_verdict reviewed_head must equal assignment candidate_revision");
    }
    if (normalizedPhase === "shard") {
      throw new Error("reviewer shard cannot publish a final review_verdict; one final synthesis review is required");
    }
    if (normalizedPhase === "synthesis") {
      const evidenceReceipts = receipt.evidence
        .filter((item) => typeof item === "string" && item.trim().startsWith("receipt:"))
        .map((item) => item.trim());
      if (JSON.stringify([...evidenceReceipts].sort()) !== JSON.stringify([...expectedShardReceipts].sort())) {
        throw new Error("reviewer synthesis evidence must exactly match assigned review_shard_receipts");
      }
    }
    reviewVerdict = verdict;
  }
  if (normalizedRole === "reviewer" && normalizedPhase !== "shard"
      && new Set(["pass", "fail"]).has(deliveryOutcome) && !reviewVerdict) {
    throw new Error("full or synthesis reviewer delivery requires structured review_verdict");
  }
  return {
    delivery_outcome: deliveryOutcome, summary, evidence: receipt.evidence, artifacts: receipt.artifacts,
    next_action: receipt.next_action, retry_class: receipt.retry_class,
    reconciliation_evidence: receipt.reconciliation_evidence || [],
    ...(reviewVerdict ? { review_verdict: reviewVerdict } : {}),
  };
}

function validateRuleHandshake(options) {
  if (!options.assignmentId) return;
  const guard = fileURLToPath(new URL("./rule_handshake.py", import.meta.url));
  const python = process.env.AD_PYTHON || "python3";
  const result = spawnSync(python, [guard, "launch-guard", "--repo", runtimeRepository(options)], { encoding: "utf8" });
  if (result.error) throw new Error(`rule handshake validation failed: ${result.error.message}`);
  if (result.status !== 0) throw new Error(`rule handshake validation failed: ${(result.stdout || result.stderr).trim()}`);
}

function buildRuntimeReceipt(options, eventType, eventSeq, extra = {}) {
  if (!options.assignmentId) return null;
  const required = [options.assignmentId, options.taskId, options.agentId, options.sessionId];
  if (required.some((value) => !value)) throw new Error("runtime receipts require assignment/task/agent/session identity");
  return {
    event_type: eventType, assignment_id: options.assignmentId, task_id: options.taskId,
    agent_id: options.agentId, provider: options.engine, session_id: options.sessionId,
    worktree: options.cwd, issued_at: new Date().toISOString(), attempt: options.attempt,
    lease_id: options.leaseId || `${options.assignmentId}:attempt:${options.attempt}`, event_seq: eventSeq,
    receipt_id: `${options.assignmentId}:${options.attempt}:${eventSeq}`,
    assignment_contract_version: options.assignmentContractVersion || 1,
    ...(Number(options.assignmentContractVersion || 1) >= 2 ? {
      side_effect: Boolean(options.sideEffect),
      idempotency_key: options.idempotencyKey || null,
    } : {}),
    ...(eventType === "assignment_started" ? {
      ...options.executionLineage,
      execution_transport: "external_process",
      execution_role: options.assignmentRole || null,
      candidate_revision: options.candidateRevision || null,
      review_phase: options.reviewPhase || null,
      review_shard_receipts: options.reviewShardReceipts || [],
      model: options.model,
      agent_type: options.agentType || `external-${options.engine}`,
      auth_mode: options.authMode,
      policy_class: options.assignmentRoute?.policy_class || null,
      route_decision: options.assignmentRoute?.decision || null,
      route_contract: options.assignmentRoute || null,
      exclusive_execution_key: `task:${options.taskId}`,
    } : {}), ...extra,
  };
}

function recordRuntimeReceipt(options, eventType, eventSeq, extra = {}) {
  const receipt = buildRuntimeReceipt(options, eventType, eventSeq, extra);
  if (!receipt) return;
  const runtime = fileURLToPath(new URL("./assignment_runtime.py", import.meta.url));
  const python = process.env.AD_PYTHON || "python3";
  const result = spawnSync(python, [runtime, "apply", "--repo", runtimeRepository(options)], { encoding: "utf8", input: JSON.stringify(receipt) });
  if (result.error) throw new Error(`runtime receipt apply failed: ${result.error.message}`);
  if (result.status !== 0) throw new Error(`runtime receipt apply failed: ${(result.stdout || result.stderr).trim()}`);
  if (options.runtimeReceipts) appendFileSync(options.runtimeReceipts, `${JSON.stringify(receipt)}\n`, "utf8");
}


function atomicWriteJson(pathname, payload) {
  const target = path.resolve(pathname);
  mkdirSync(path.dirname(target), { recursive: true });
  const temporary = `${target}.tmp-${process.pid}`;
  writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  renameSync(temporary, target);
  return target;
}

function persistExternalTerminalReceipt(options, {
  exitCode, summary, deliveryOutcome = "unresolved", failureClass = null, retryClass = null,
  retrySafe = null, resultUnknown = null, failureDetails = null, reviewStatus = null, reviewVerdict = null,
}) {
  if (!options.terminalReceipt) return null;
  const target = atomicWriteJson(options.terminalReceipt, {
    schema_version: 1,
    event_type: "external_agent_terminal",
    engine: options.engine,
    model: options.model,
    cwd: path.resolve(options.cwd),
    repo: path.resolve(runtimeRepository(options)),
    exit_code: exitCode,
    summary: String(summary || "external agent finished"),
    delivery_outcome: deliveryOutcome,
    result_path: options.resultPath ? path.resolve(options.resultPath) : null,
    assignment_id: options.assignmentId || null,
    task_id: options.taskId || null,
    agent_id: options.agentId || null,
    session_id: options.sessionId || null,
    attempt: options.assignmentId ? options.attempt : null,
    lease_id: options.assignmentId ? options.leaseId : null,
    ...(failureClass ? { failure_class: failureClass } : {}),
    ...(retryClass ? { retry_class: retryClass } : {}),
    ...(options.assignmentRole ? { execution_role: options.assignmentRole } : {}),
    ...(options.candidateRevision ? { candidate_revision: options.candidateRevision } : {}),
    ...(options.reviewPhase ? { review_phase: options.reviewPhase } : {}),
    ...(options.reviewShardReceipts?.length ? { review_shard_receipts: options.reviewShardReceipts } : {}),
    ...(typeof retrySafe === "boolean" ? { retry_safe: retrySafe } : {}),
    ...(typeof resultUnknown === "boolean" ? { result_unknown: resultUnknown } : {}),
    ...(failureDetails && typeof failureDetails === "object" && !Array.isArray(failureDetails) ? { failure_details: failureDetails } : {}),
    ...(reviewStatus ? { review_status: reviewStatus } : {}),
    ...(reviewVerdict && typeof reviewVerdict === "object" && !Array.isArray(reviewVerdict) ? { review_verdict: reviewVerdict } : {}),
    completed_at: new Date().toISOString(),
  });
  const helper = process.env.AD_TERMINAL_CONTINUATION_HELPER || fileURLToPath(new URL("./terminal_continuation.py", import.meta.url));
  const python = process.env.AD_PYTHON || "python3";
  const result = spawnSync(python, [helper, "consume", "--repo", runtimeRepository(options), "--receipt", target], { encoding: "utf8", env: process.env });
  if (result.error) throw new Error(`terminal continuation helper failed: ${result.error.message}`);
  if (result.status !== 0) throw new Error(`terminal continuation helper failed: ${(result.stderr || result.stdout || `exit ${result.status}`).trim()}`);
  return target;
}

function runtimeGitSnapshot(cwd) {
  const head = gitFact(cwd, ["rev-parse", "HEAD"], "runtime HEAD");
  const status = gitFact(cwd, ["status", "--porcelain=v1", "--untracked-files=all"], "runtime status");
  return {
    head,
    statusSha256: createHash("sha256").update(status).digest("hex"),
  };
}

function boundedProgressEvidence(previous, snapshot) {
  const changedFields = [];
  if (snapshot.head !== previous.head) changedFields.push("last_observed_head");
  if (snapshot.statusSha256 !== previous.statusSha256) changedFields.push("last_observed_status_sha256");
  const evidence = { changed_fields: changedFields };
  if (changedFields.includes("last_observed_head")) evidence.last_observed_head = snapshot.head;
  if (changedFields.includes("last_observed_status_sha256")) evidence.last_observed_status_sha256 = snapshot.statusSha256;
  return evidence;
}

function runtimeHeartbeatIntervalMs() {
  const configured = Number(process.env.AD_RUNTIME_HEARTBEAT_MS || 300000);
  if (!Number.isFinite(configured) || configured < 10) throw new Error("AD_RUNTIME_HEARTBEAT_MS must be at least 10ms");
  return configured;
}

function assertDirectory(cwd) {
  let stats;
  try {
    stats = statSync(cwd);
  } catch {
    throw new Error(`Working directory does not exist: ${cwd}`);
  }
  if (!stats.isDirectory()) throw new Error(`Working directory is not a directory: ${cwd}`);
}

function keychainHas(service) {
  if (process.platform !== "darwin") return false;
  const result = spawnSync("security", [
    "find-generic-password", "-s", service, "-a", process.env.USER || "",
  ], { encoding: "utf8", env: process.env });
  return result.status === 0;
}

function readKeychain(service) {
  if (process.platform !== "darwin") return null;
  const result = spawnSync("security", [
    "find-generic-password", "-s", service, "-a", process.env.USER || "", "-w",
  ], { encoding: "utf8", env: process.env });
  if (result.status !== 0) return null;
  return result.stdout.trim() || null;
}

function kimiHome() {
  return process.env.KIMI_CODE_HOME || path.join(homedir(), ".kimi-code");
}

function grokHome() {
  return process.env.GROK_HOME || path.join(homedir(), ".grok");
}

function credentialState(engine, authMode) {
  if (engine === "kimi-code" && authMode === "oauth") {
    return {
      configured: existsSync(path.join(kimiHome(), "credentials", "kimi-code.json")),
      source: "cli-session",
    };
  }
  if (engine === "kimi-code") {
    if (process.env.KIMI_MODEL_API_KEY || process.env.MOONSHOT_API_KEY) {
      return { configured: true, source: "environment" };
    }
    return { configured: keychainHas(kimiKeychainService()), source: "os-keychain" };
  }
  if (authMode === "oauth") {
    return {
      configured: existsSync(path.join(grokHome(), "auth.json")),
      source: "cli-session",
    };
  }
  if (process.env.XAI_API_KEY) return { configured: true, source: "environment" };
  return { configured: keychainHas(xaiKeychainService()), source: "os-keychain" };
}

function readApiKey(engine) {
  if (engine === "kimi-code") {
    return process.env.KIMI_MODEL_API_KEY
      || process.env.MOONSHOT_API_KEY
      || readKeychain(kimiKeychainService());
  }
  return process.env.XAI_API_KEY || readKeychain(xaiKeychainService());
}

function sanitizedEnvironment(prefixes = [], exactNames = []) {
  const env = { ...process.env };
  for (const name of Object.keys(env)) {
    if (exactNames.includes(name) || prefixes.some((prefix) => name.startsWith(prefix))) delete env[name];
  }
  return env;
}

function kimiApiBaseUrl() {
  if (process.env.KIMI_K3_BASE_URL) return process.env.KIMI_K3_BASE_URL;
  try {
    if (readFileSync(path.join(kimiHome(), "region"), "utf8").trim() === "mainland-cn") {
      return "https://api.moonshot.cn/v1";
    }
  } catch {}
  return "https://api.moonshot.ai/v1";
}

export function buildGrokReviewArgs(model, promptFile, reasoningEffort) {
  return [
    "--no-auto-update",
    "--no-subagents",
    "--no-memory",
    "--sandbox", "workspace",
    "--no-plan",
    "--disable-web-search",
    "--tools", "",
    "-m", model,
    "--prompt-file", promptFile,
    "--json-schema", JSON.stringify(GROK_REVIEW_JSON_SCHEMA),
    "--max-turns", String(GROK_REVIEW_MAX_TURNS),
    "--system-prompt-override", GROK_REVIEW_SYSTEM_PROMPT,
    "--verbatim",
    "--reasoning-effort", reasoningEffort,
  ];
}

function reviewOutputError(message, details = {}) {
  return new ExternalAgentExecutionError(`review_output_invalid: ${message}`, {
    failureClass: "review_output_invalid",
    retrySafe: false,
    resultUnknown: false,
    reviewStatus: "REVIEW_OUTPUT_INVALID",
    details,
  });
}

export function validateGrokReviewResult(value, { candidateRevision = null } = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw reviewOutputError("review verdict must be one JSON object");
  }
  const expectedKeys = ["critical", "findings", "important", "minor", "reviewed_head", "verdict"].sort();
  if (JSON.stringify(Object.keys(value).sort()) !== JSON.stringify(expectedKeys)) {
    throw reviewOutputError("review verdict keys are invalid");
  }
  const reviewedHead = String(value.reviewed_head || "").trim();
  if (!reviewedHead) throw reviewOutputError("reviewed_head is required");
  const expectedHead = String(candidateRevision || "").trim();
  if (expectedHead && reviewedHead !== expectedHead) {
    throw reviewOutputError("reviewed_head must equal immutable candidate_revision", { reviewed_head: reviewedHead, candidate_revision: expectedHead });
  }
  for (const key of ["critical", "important"]) {
    if (!Number.isSafeInteger(value[key]) || value[key] < 0) {
      throw reviewOutputError(`${key} must be a non-negative integer`);
    }
  }
  if (!Array.isArray(value.minor) || !value.minor.every((item) => typeof item === "string" && item.trim())) {
    throw reviewOutputError("minor must be an array of non-empty strings");
  }
  if (!Array.isArray(value.findings)) throw reviewOutputError("findings must be an array");
  const critical = [];
  const important = [];
  for (const finding of value.findings) {
    if (!finding || typeof finding !== "object" || Array.isArray(finding)) {
      throw reviewOutputError("each finding must be an object");
    }
    if (JSON.stringify(Object.keys(finding).sort()) !== JSON.stringify(["message", "severity"])) {
      throw reviewOutputError("finding keys are invalid");
    }
    const severity = String(finding.severity || "").trim().toLowerCase();
    const message = String(finding.message || "").trim();
    if (!message || !new Set(["critical", "important"]).has(severity)) {
      throw reviewOutputError("finding requires severity critical|important and non-empty message");
    }
    (severity === "critical" ? critical : important).push(message);
  }
  if (critical.length !== value.critical || important.length !== value.important) {
    throw reviewOutputError("critical/important counts must match findings");
  }
  if (!new Set(["PASS", "FAIL"]).has(value.verdict)) {
    throw reviewOutputError("verdict must be PASS or FAIL");
  }
  const shouldPass = value.critical === 0 && value.important === 0;
  if ((value.verdict === "PASS") !== shouldPass) {
    throw reviewOutputError("PASS iff critical=0 and important=0");
  }
  const reviewStatus = shouldPass ? "REVIEW_PASS" : "REVIEW_FAIL";
  return {
    reviewStatus,
    deliveryOutcome: shouldPass ? "pass" : "fail",
    retrySafe: false,
    rawVerdict: {
      reviewed_head: reviewedHead,
      critical: value.critical,
      important: value.important,
      minor: value.minor.map((item) => item.trim()),
      findings: value.findings.map((item) => ({ severity: item.severity, message: item.message.trim() })),
      verdict: value.verdict,
    },
    reviewVerdict: {
      reviewed_head: reviewedHead,
      verdict: shouldPass ? "PASS" : "FINDINGS",
      critical,
      important,
      minor: value.minor.map((item) => item.trim()),
    },
  };
}

function canonicalReviewJson(value) {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalReviewJson(item)).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalReviewJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function parseGrokReviewOutput(stdout, { candidateRevision = null } = {}) {
  const text = String(stdout || "").trim();
  if (!text) {
    throw new ExternalAgentExecutionError("review_no_verdict: Reviewer produced no verdict", {
      failureClass: "review_no_verdict", retrySafe: false, reviewStatus: "REVIEW_NO_VERDICT",
    });
  }
  let value;
  try {
    value = JSON.parse(text);
  } catch (error) {
    throw reviewOutputError(`malformed JSON verdict: ${error.message}`);
  }
  if (value && typeof value === "object" && !Array.isArray(value) && Object.prototype.hasOwnProperty.call(value, "structuredOutput")) {
    const structured = value.structuredOutput;
    if (!structured || typeof structured !== "object" || Array.isArray(structured)) {
      throw reviewOutputError("Grok json-schema envelope structuredOutput must be one object");
    }
    if (typeof value.text === "string" && value.text.trim()) {
      let textVerdict;
      try {
        textVerdict = JSON.parse(value.text);
      } catch (error) {
        throw reviewOutputError(`Grok json-schema envelope text is not matching verdict JSON: ${error.message}`);
      }
      if (canonicalReviewJson(textVerdict) !== canonicalReviewJson(structured)) {
        throw reviewOutputError("Grok json-schema envelope structuredOutput conflicts with text verdict");
      }
    }
    value = structured;
  }
  return validateGrokReviewResult(value, { candidateRevision });
}

export function classifyGrokReviewTerminal({
  exitCode = null, stdout = "", stderr = "", timedOut = false, cleanupConfirmed = true,
  candidateRevision = null, hadModelOutput = null,
} = {}) {
  const stdoutText = String(stdout || "");
  const stderrText = String(stderr || "");
  const observedModelOutput = typeof hadModelOutput === "boolean" ? hadModelOutput : Boolean(stdoutText.trim());
  if (!cleanupConfirmed) {
    return { reviewStatus: "REVIEW_PROCESS_STUCK", deliveryOutcome: "unresolved", retrySafe: false, hadModelOutput: observedModelOutput };
  }
  if (timedOut) {
    return { reviewStatus: "REVIEW_TIMEOUT", deliveryOutcome: "unresolved", retrySafe: !observedModelOutput, hadModelOutput: observedModelOutput };
  }
  if (/max\s+turns\s+reached/i.test(`${stdoutText}\n${stderrText}`)) {
    return { reviewStatus: "REVIEW_MAX_TURNS", deliveryOutcome: "unresolved", retrySafe: false, hadModelOutput: observedModelOutput };
  }
  if (exitCode !== 0) {
    const transient = !observedModelOutput && /(relay|websocket|transport|disconnect|connection|startup|spawn|temporar|unavailable)/i.test(stderrText);
    return { reviewStatus: "REVIEW_PROVIDER_ERROR", deliveryOutcome: "unresolved", retrySafe: transient, hadModelOutput: observedModelOutput };
  }
  if (!stdoutText.trim()) {
    return { reviewStatus: "REVIEW_NO_VERDICT", deliveryOutcome: "unresolved", retrySafe: false, hadModelOutput: false };
  }
  try {
    return { ...parseGrokReviewOutput(stdoutText, { candidateRevision }), hadModelOutput: true };
  } catch (error) {
    if (error?.reviewStatus === "REVIEW_NO_VERDICT") {
      return { reviewStatus: "REVIEW_NO_VERDICT", deliveryOutcome: "unresolved", retrySafe: false, hadModelOutput: observedModelOutput };
    }
    return { reviewStatus: "REVIEW_OUTPUT_INVALID", deliveryOutcome: "unresolved", retrySafe: false, hadModelOutput: observedModelOutput, validationError: String(error?.message || error) };
  }
}

export function grokReviewRetryDecision(terminal, { attempt = 1 } = {}) {
  const status = String(terminal?.reviewStatus || "");
  if (attempt >= 2) return { retry: false, reason: "retry_budget_exhausted" };
  if (new Set(["REVIEW_FAIL", "REVIEW_MAX_TURNS", "REVIEW_OUTPUT_INVALID", "REVIEW_PROCESS_STUCK", "REVIEW_NO_VERDICT", "REVIEW_PASS"]).has(status)) {
    return { retry: false, reason: "terminal_review_outcome" };
  }
  if (terminal?.hadModelOutput) return { retry: false, reason: "model_output_observed" };
  if (terminal?.retrySafe === true && new Set(["REVIEW_TIMEOUT", "REVIEW_PROVIDER_ERROR"]).has(status)) {
    return { retry: true, reason: "transient_before_model_output" };
  }
  return { retry: false, reason: "not_retry_safe" };
}

function commonGrokArgs(model, promptFile, reasoningEffort) {
  return [
    "--no-auto-update", "--no-subagents", "--no-memory", "--sandbox", "workspace",
    "--always-approve", "-m", model, "--prompt-file", promptFile, "--output-format", "streaming-json",
    "--reasoning-effort", reasoningEffort,
  ];
}

function runAttached(executable, args, { cwd, env }) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { cwd, env, stdio: ["ignore", "inherit", "inherit"] });
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      if (signal) reject(new Error(`${executable} terminated by signal ${signal}`));
      else resolve(code ?? 1);
    });
  });
}

export function runMonitoredGrok(executable, args, {
  cwd, env, progressDeadlineMinutes = null, onStructuredProgress = null,
  terminateGroup = terminateProcessGroup, spawnChild = spawn, parentProcess = process,
}) {
  const launchTimeoutMs = grokLaunchTimeoutMs();
  const absoluteTimeoutMs = externalAttemptTimeoutMs(progressDeadlineMinutes);
  const firstOutputTimeoutMs = grokFirstOutputTimeoutMs();
  const stallTimeoutMs = grokStallTimeoutMs();
  const killGraceMs = externalKillGraceMs();
  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    let child;
    try {
      child = spawnChild(executable, args, {
        cwd, env,
        stdio: ["ignore", "pipe", "pipe"],
        detached: process.platform !== "win32",
      });
    } catch (error) {
      reject(new ExternalAgentExecutionError(`cli_launch_failed: ${error.message}`, { failureClass: "cli_launch_failed", retrySafe: true, details: { provider_started: false } }));
      return;
    }
    let launchConfirmed = false;
    let launchedAt = null;
    let firstStructuredOutputAt = null;
    let lastStructuredOutputAt = startedAt;
    let stdoutBuffer = "";
    let terminating = false;
    let settled = false;
    const parentSignalHandlers = [];

    const removeParentSignalHandlers = () => {
      for (const [signal, handler] of parentSignalHandlers) parentProcess.off(signal, handler);
      parentSignalHandlers.length = 0;
    };
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearInterval(watchdog);
      removeParentSignalHandlers();
      fn(value);
    };

    const observeStructuredLines = (text) => {
      stdoutBuffer += text;
      while (true) {
        const newline = stdoutBuffer.indexOf("\n");
        if (newline < 0) break;
        const line = stdoutBuffer.slice(0, newline).trim();
        stdoutBuffer = stdoutBuffer.slice(newline + 1);
        if (!line) continue;
        try {
          const event = JSON.parse(line);
          if (event && typeof event === "object" && !Array.isArray(event)) {
            const progressKind = grokModelProgressKind(event);
            if (progressKind) {
              const now = Date.now();
              if (firstStructuredOutputAt === null) firstStructuredOutputAt = now;
              lastStructuredOutputAt = now;
              if (typeof onStructuredProgress === "function") {
                onStructuredProgress(event, progressKind);
              }
            }
          }
        } catch {}
      }
    };

    child.once("spawn", () => {
      const now = Date.now();
      launchConfirmed = true;
      launchedAt = now;
      lastStructuredOutputAt = now;
    });

    child.stdout?.on("data", (chunk) => {
      process.stdout.write(chunk);
      observeStructuredLines(chunk.toString("utf8"));
    });
    child.stderr?.on("data", (chunk) => {
      process.stderr.write(chunk);
    });

    const terminateFor = async (failureClass, message) => {
      if (terminating || settled) return;
      terminating = true;
      clearInterval(watchdog);
      const cleanup = await terminateGroup(child, killGraceMs);
      const finalClass = cleanup.confirmed ? failureClass : "process_group_cleanup_failed";
      const diagnostic = `${message}; cleanup=${cleanup.diagnostic}`;
      finish(reject, new ExternalAgentExecutionError(`${finalClass}: ${diagnostic}`, {
        failureClass: finalClass,
        retrySafe: cleanup.confirmed,
        resultUnknown: !cleanup.confirmed,
        details: { cleanup_confirmed: cleanup.confirmed, cleanup_diagnostic: cleanup.diagnostic, provider_started: launchConfirmed },
      }));
    };

    const smallestDeadline = Math.max(10, Math.min(launchTimeoutMs, firstOutputTimeoutMs, stallTimeoutMs, absoluteTimeoutMs));
    const watchdogIntervalMs = Math.max(10, Math.min(250, Math.floor(smallestDeadline / 4)));
    const watchdog = setInterval(() => {
      if (settled || terminating) return;
      const now = Date.now();
      if (!launchConfirmed && now - startedAt >= launchTimeoutMs) {
        void terminateFor("cli_launch_timeout", `Grok CLI launch was not confirmed within ${launchTimeoutMs}ms`);
        return;
      }
      if (now - startedAt >= absoluteTimeoutMs) {
        void terminateFor("provider_timeout", `absolute provider deadline ${absoluteTimeoutMs}ms exceeded`);
        return;
      }
      if (launchConfirmed && firstStructuredOutputAt === null && now - (launchedAt ?? startedAt) >= firstOutputTimeoutMs) {
        void terminateFor("first_output_timeout", `no structured Grok stdout within ${firstOutputTimeoutMs}ms`);
        return;
      }
      if (firstStructuredOutputAt !== null && now - lastStructuredOutputAt >= stallTimeoutMs) {
        void terminateFor("generation_stalled", `no structured Grok stdout progress within ${stallTimeoutMs}ms`);
      }
    }, watchdogIntervalMs);
    for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) {
      const handler = () => {
        void terminateFor("provider_timeout", `parent process received ${signal}; terminating Grok process group`);
      };
      parentSignalHandlers.push([signal, handler]);
      parentProcess.once(signal, handler);
    }
    child.once("error", (error) => {
      if (terminating || settled) return;
      finish(reject, new ExternalAgentExecutionError(`cli_launch_failed: ${error.message}`, { failureClass: "cli_launch_failed", retrySafe: true, details: { provider_started: launchConfirmed } }));
    });
    child.once("exit", (code, signal) => {
      if (terminating || settled) return;
      terminating = true;
      clearInterval(watchdog);
      void (async () => {
        let groupAlive = false;
        try {
          groupAlive = process.platform === "win32" ? false : processGroupExists(child.pid);
        } catch (error) {
          finish(reject, new ExternalAgentExecutionError(`process_group_cleanup_failed: terminal group probe failed: ${error.message}`, {
            failureClass: "process_group_cleanup_failed", retrySafe: false, resultUnknown: true,
            details: { cleanup_confirmed: false, cleanup_diagnostic: `terminal group probe failed: ${error.message}`, provider_started: launchConfirmed },
          }));
          return;
        }
        let cleanup = { confirmed: true, diagnostic: "process group already gone" };
        if (groupAlive) cleanup = await terminateGroup(child, killGraceMs);
        if (!cleanup.confirmed) {
          finish(reject, new ExternalAgentExecutionError(`process_group_cleanup_failed: provider leader exited but descendants survived cleanup; cleanup=${cleanup.diagnostic}`, {
            failureClass: "process_group_cleanup_failed", retrySafe: false, resultUnknown: true,
            details: { cleanup_confirmed: false, cleanup_diagnostic: cleanup.diagnostic, provider_started: launchConfirmed },
          }));
          return;
        }
        if (signal) {
          finish(reject, new ExternalAgentExecutionError(`provider_terminated_by_signal: ${signal}`, {
            failureClass: "provider_terminated_by_signal", retrySafe: true,
            details: { provider_started: launchConfirmed, cleanup_confirmed: true, cleanup_diagnostic: cleanup.diagnostic },
          }));
        } else {
          finish(resolve, code ?? 1);
        }
      })();
    });
  });
}

export function runMonitoredGrokReview(executable, args, {
  cwd, env, progressDeadlineMinutes = null, candidateRevision = null,
  terminateGroup = terminateProcessGroup, spawnChild = spawn, parentProcess = process,
} = {}) {
  const launchTimeoutMs = grokLaunchTimeoutMs();
  const absoluteTimeoutMs = grokReviewAttemptTimeoutMs(progressDeadlineMinutes);
  const firstOutputTimeoutMs = grokFirstOutputTimeoutMs();
  const stallTimeoutMs = grokStallTimeoutMs();
  const killGraceMs = externalKillGraceMs();
  const maxCaptureBytes = 2 * 1024 * 1024;
  return new Promise((resolve) => {
    const startedAt = Date.now();
    let child = null;
    let launchConfirmed = false;
    let launchedAt = null;
    let firstOutputAt = null;
    let lastOutputAt = null;
    let stdout = "";
    let stderr = "";
    let terminating = false;
    let settled = false;
    let verdictCleanupStarted = false;
    let watchdog = null;
    let closeDrainTimer = null;
    let verdictCleanupTimer = null;
    let observedExitCode = null;
    let observedExitSignal = null;
    let exitObserved = false;
    const parentSignalHandlers = [];

    const appendBounded = (current, chunk) => {
      const next = current + chunk;
      if (Buffer.byteLength(next, "utf8") <= maxCaptureBytes) return next;
      return Buffer.from(next, "utf8").subarray(0, maxCaptureBytes).toString("utf8");
    };
    const removeSignalHandlers = () => {
      for (const [signal, handler] of parentSignalHandlers) parentProcess.off(signal, handler);
      parentSignalHandlers.length = 0;
    };
    const finish = (terminal) => {
      if (settled) return;
      settled = true;
      if (watchdog) clearInterval(watchdog);
      if (closeDrainTimer) clearTimeout(closeDrainTimer);
      if (verdictCleanupTimer) clearTimeout(verdictCleanupTimer);
      removeSignalHandlers();
      resolve(terminal);
    };
    const classify = (extra = {}) => classifyGrokReviewTerminal({
      exitCode: child?.exitCode ?? null,
      stdout, stderr,
      candidateRevision: extra.candidateRevision || candidateRevision || null,
      hadModelOutput: firstOutputAt !== null,
      ...extra,
    });
    const terminateForTimeout = async (reason, { parentSignal = null } = {}) => {
      if (terminating || settled) return;
      terminating = true;
      if (watchdog) clearInterval(watchdog);
      let cleanup = { confirmed: true, diagnostic: "provider process was not launched" };
      if (child) cleanup = await terminateGroup(child, killGraceMs);
      const terminal = classify({
        timedOut: true,
        cleanupConfirmed: cleanup.confirmed,
      });
      terminal.cleanupDiagnostic = cleanup.diagnostic;
      terminal.timeoutReason = reason;
      if (parentSignal) terminal.parentSignal = parentSignal;
      finish(terminal);
    };

    try {
      child = spawnChild(executable, args, {
        cwd, env,
        stdio: ["ignore", "pipe", "pipe"],
        detached: process.platform !== "win32",
      });
    } catch (error) {
      finish({
        reviewStatus: "REVIEW_PROVIDER_ERROR", deliveryOutcome: "unresolved", retrySafe: true,
        hadModelOutput: false, providerError: `cli_launch_failed: ${error.message}`,
      });
      return;
    }

    child.once("spawn", () => {
      launchConfirmed = true;
      launchedAt = Date.now();
    });
    const maybeFinishFromValidatedVerdict = () => {
      if (settled || terminating || verdictCleanupStarted || !stdout.trim()) return;
      let validated;
      try {
        validated = parseGrokReviewOutput(stdout, { candidateRevision });
      } catch {
        return;
      }
      verdictCleanupStarted = true;
      if (watchdog) clearInterval(watchdog);
      if (closeDrainTimer) clearTimeout(closeDrainTimer);
      const naturalExitGraceMs = Math.max(25, Math.min(100, killGraceMs));
      verdictCleanupTimer = setTimeout(() => {
        if (settled || terminating) return;
        terminating = true;
        void (async () => {
          let cleanup = { confirmed: true, diagnostic: "process group already gone" };
          try {
            const groupAlive = child?.pid && process.platform !== "win32" ? processGroupExists(child.pid) : !childExited(child);
            if (groupAlive || !childExited(child)) cleanup = await terminateGroup(child, killGraceMs);
          } catch (error) {
            cleanup = { confirmed: false, diagnostic: `verdict-terminal cleanup failed: ${error.message}` };
          }
          if (!cleanup.confirmed) {
            finish({
              reviewStatus: "REVIEW_PROCESS_STUCK", deliveryOutcome: "unresolved", retrySafe: false, hadModelOutput: true,
              cleanupDiagnostic: cleanup.diagnostic, validatedReviewVerdict: validated.reviewVerdict,
            });
            return;
          }
          finish({
            ...validated,
            hadModelOutput: true,
            cleanupDiagnostic: cleanup.diagnostic,
            providerTerminatedAfterVerdict: true,
          });
        })();
      }, naturalExitGraceMs);
    };

    child.stdout?.on("data", (chunk) => {
      const text = chunk.toString("utf8");
      process.stdout.write(chunk);
      stdout = appendBounded(stdout, text);
      if (text.trim()) {
        const now = Date.now();
        if (firstOutputAt === null) firstOutputAt = now;
        lastOutputAt = now;
      }
      maybeFinishFromValidatedVerdict();
    });
    child.stderr?.on("data", (chunk) => {
      process.stderr.write(chunk);
      stderr = appendBounded(stderr, chunk.toString("utf8"));
    });

    const smallestDeadline = Math.max(10, Math.min(launchTimeoutMs, firstOutputTimeoutMs, stallTimeoutMs, absoluteTimeoutMs));
    watchdog = setInterval(() => {
      if (settled || terminating) return;
      const now = Date.now();
      if (!launchConfirmed && now - startedAt >= launchTimeoutMs) {
        void terminateForTimeout(`review launch timeout after ${launchTimeoutMs}ms`);
        return;
      }
      if (now - startedAt >= absoluteTimeoutMs) {
        void terminateForTimeout(`review absolute timeout after ${absoluteTimeoutMs}ms`);
        return;
      }
      if (launchConfirmed && firstOutputAt === null && now - (launchedAt ?? startedAt) >= firstOutputTimeoutMs) {
        void terminateForTimeout(`review first-output timeout after ${firstOutputTimeoutMs}ms`);
        return;
      }
      if (firstOutputAt !== null && lastOutputAt !== null && now - lastOutputAt >= stallTimeoutMs) {
        void terminateForTimeout(`review output stalled after ${stallTimeoutMs}ms`);
      }
    }, Math.max(10, Math.min(250, Math.floor(smallestDeadline / 4))));

    for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) {
      const handler = () => { void terminateForTimeout(`parent process received ${signal}`, { parentSignal: signal }); };
      parentSignalHandlers.push([signal, handler]);
      parentProcess.once(signal, handler);
    }

    child.once("error", async (error) => {
      if (settled || terminating) return;
      terminating = true;
      if (watchdog) clearInterval(watchdog);
      let cleanup = { confirmed: true, diagnostic: "provider process never became active" };
      if (child?.pid) cleanup = await terminateGroup(child, killGraceMs);
      if (!cleanup.confirmed) {
        finish({ reviewStatus: "REVIEW_PROCESS_STUCK", deliveryOutcome: "unresolved", retrySafe: false, hadModelOutput: firstOutputAt !== null, cleanupDiagnostic: cleanup.diagnostic });
        return;
      }
      finish({ reviewStatus: "REVIEW_PROVIDER_ERROR", deliveryOutcome: "unresolved", retrySafe: firstOutputAt === null, hadModelOutput: firstOutputAt !== null, providerError: `cli_launch_failed: ${error.message}`, cleanupDiagnostic: cleanup.diagnostic });
    });

    child.once("exit", (code, signal) => {
      if (settled || terminating) return;
      exitObserved = true;
      observedExitCode = code;
      observedExitSignal = signal;
      if (watchdog) clearInterval(watchdog);
      closeDrainTimer = setTimeout(() => {
        if (settled || terminating) return;
        terminating = true;
        void (async () => {
          let cleanup = { confirmed: true, diagnostic: "process group already gone" };
          try {
            const groupAlive = process.platform === "win32" ? false : processGroupExists(child.pid);
            if (groupAlive) cleanup = await terminateGroup(child, killGraceMs);
          } catch (error) {
            cleanup = { confirmed: false, diagnostic: `stdio-close process-group probe failed: ${error.message}` };
          }
          if (!cleanup.confirmed) {
            finish({ reviewStatus: "REVIEW_PROCESS_STUCK", deliveryOutcome: "unresolved", retrySafe: false, hadModelOutput: firstOutputAt !== null, cleanupDiagnostic: cleanup.diagnostic });
            return;
          }
          finish({
            reviewStatus: "REVIEW_PROVIDER_ERROR", deliveryOutcome: "unresolved", retrySafe: false,
            hadModelOutput: firstOutputAt !== null,
            providerError: "stdio_close_timeout: provider exited but stdout/stderr did not close within bounded drain window",
            cleanupDiagnostic: cleanup.diagnostic,
          });
        })();
      }, Math.max(100, Math.min(2_000, killGraceMs)));
    });

    child.once("close", (code, signal) => {
      if (settled || terminating) return;
      terminating = true;
      if (watchdog) clearInterval(watchdog);
      if (closeDrainTimer) clearTimeout(closeDrainTimer);
      void (async () => {
        let cleanup = { confirmed: true, diagnostic: "process group already gone" };
        try {
          const groupAlive = process.platform === "win32" ? false : processGroupExists(child.pid);
          if (groupAlive) cleanup = await terminateGroup(child, killGraceMs);
        } catch (error) {
          cleanup = { confirmed: false, diagnostic: `terminal process-group probe failed: ${error.message}` };
        }
        if (!cleanup.confirmed) {
          finish({ reviewStatus: "REVIEW_PROCESS_STUCK", deliveryOutcome: "unresolved", retrySafe: false, hadModelOutput: firstOutputAt !== null, cleanupDiagnostic: cleanup.diagnostic });
          return;
        }
        const finalSignal = signal || observedExitSignal;
        const finalCode = code ?? observedExitCode;
        const terminal = classifyGrokReviewTerminal({
          exitCode: finalSignal ? 1 : (finalCode ?? 1),
          stdout, stderr,
          timedOut: false,
          cleanupConfirmed: true,
          candidateRevision,
          hadModelOutput: firstOutputAt !== null,
        });
        terminal.cleanupDiagnostic = cleanup.diagnostic;
        terminal.exitObservedBeforeClose = exitObserved;
        if (finalSignal) terminal.providerSignal = finalSignal;
        finish(terminal);
      })();
    });
  });
}

export function checkExternalAgent({ cwd, engine, model, reasoningEffort, authMode }) {
  assertDirectory(cwd);
  const route = routes[engine];
  const executable = resolveExecutable(route);
  const result = spawnSync(executable, route.versionArgs, { cwd, encoding: "utf8", env: process.env });
  if (result.error?.code === "ENOENT") {
    return { available: false, engine, model, reasoningEffort, authMode, reason: `${route.executable} executable not found` };
  }
  if (result.error) throw result.error;
  if (result.status !== 0) {
    return {
      available: false,
      engine,
      model,
      reasoningEffort,
      authMode,
      reason: (result.stderr || `${route.executable} version check exited ${result.status}`).trim(),
    };
  }
  const credential = credentialState(engine, authMode);
  return {
    available: true,
    engine,
    model,
    reasoningEffort,
    authMode,
    version: (result.stdout || result.stderr).trim(),
    credentialConfigured: credential.configured,
    credentialSource: credential.source,
    provesLiveModelAccess: false,
  };
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8").trim();
}

async function loginExternalAgent({ cwd, engine, region, deviceAuth }) {
  assertDirectory(cwd);
  const executable = resolveExecutable(routes[engine]);
  const args = ["login"];
  if (engine === "kimi-code" && region) args.push("--region", region);
  if (engine === "grok-build" && deviceAuth) args.push("--device-auth");
  return await runAttached(executable, args, { cwd, env: process.env });
}

async function executeExternalAgent({
  cwd, engine, model, reasoningEffort, authMode, sideEffect, idempotencyKey,
  assignmentRole = null, progressDeadlineMinutes = null, onStructuredProgress = null,
  workType = null, candidateRevision = null, reviewPhase = null,
}) {
  assertDirectory(cwd);
  const rawPrompt = await readStdin();
  if (!rawPrompt) throw new Error("A bounded routing contract prompt is required on stdin");
  const prompt = sideEffect && idempotencyKey
    ? `[Adaptive Agent Runtime side-effect contract] Any external side effect in this execution MUST use the exact idempotency key: ${idempotencyKey}. Do not perform the side effect without applying this key through the provider/API mechanism.\n\n${rawPrompt}`
    : rawPrompt;
  const normalizedWorkType = String(workType || "").trim().toLowerCase();
  const normalizedRole = String(assignmentRole || "").trim().toLowerCase();
  const normalizedReviewPhase = String(reviewPhase || "").trim().toLowerCase();
  const purePacketReview = engine === "grok-build" && normalizedWorkType === "review";
  if (purePacketReview && (normalizedRole !== "reviewer" || !new Set(["full", "synthesis"]).has(normalizedReviewPhase))) {
    throw new ExternalAgentExecutionError("review_policy_invalid: work_type=review requires reviewer role and full|synthesis review_phase", {
      failureClass: "review_policy_invalid", retrySafe: false, reviewStatus: "REVIEW_OUTPUT_INVALID",
    });
  }

  const executable = resolveExecutable(routes[engine]);
  let args;
  let env;
  const cleanups = [];
  let executionError = null;
  try {
    if (engine === "kimi-code" && authMode === "api") {
      const apiKey = readApiKey(engine);
      if (!apiKey) {
        throw new Error(
          `Kimi K3 API key not found; set KIMI_MODEL_API_KEY or store it in macOS Keychain service ${kimiKeychainService()}`,
        );
      }
      args = ["-p", prompt, "--output-format", "stream-json"];
      env = {
        ...process.env,
        KIMI_MODEL_NAME: model,
        KIMI_MODEL_API_KEY: apiKey,
        // Kimi K3's Platform API uses the standard Chat Completions
        // `reasoning_effort` field. Kimi Code's `kimi` provider emits the
        // legacy `thinking` object, which the K3 route rejects behind a
        // Kimi-Api-Version compatibility gate. The `openai` provider keeps the
        // same official Moonshot endpoint while encoding K3's wire contract.
        KIMI_MODEL_PROVIDER_TYPE: "openai",
        KIMI_MODEL_BASE_URL: kimiApiBaseUrl(),
        KIMI_MODEL_MAX_CONTEXT_SIZE: "1048576",
        KIMI_MODEL_CAPABILITIES: "image_in,video_in,thinking,always_thinking,tool_use",
        KIMI_MODEL_DISPLAY_NAME: "Kimi K3 API",
        KIMI_MODEL_THINKING_EFFORT: reasoningEffort,
      };
    } else if (engine === "kimi-code") {
      if (!credentialState(engine, authMode).configured) {
        throw new Error("Kimi Code OAuth session not found; run the Adaptive Agent Runtime login command first");
      }
      args = ["-m", model, "-p", prompt, "--output-format", "stream-json"];
      env = {
        ...sanitizedEnvironment(["KIMI_MODEL_"], ["MOONSHOT_API_KEY"]),
        KIMI_MODEL_THINKING_EFFORT: reasoningEffort,
      };
    } else if (authMode === "api") {
      const apiKey = readApiKey(engine);
      if (!apiKey) {
        throw new Error(
          `xAI API key not found; set XAI_API_KEY or store it in macOS Keychain service ${xaiKeychainService()}`,
        );
      }
      const grokPrompt = prepareGrokPrompt(prompt, { assignmentRole });
      cleanups.push({ label: "prompt_file", cleanup: grokPrompt.cleanup });
      const isolatedHome = mkdtempSync(path.join(tmpdir(), "adaptive-delivery-grok-api-"));
      cleanups.push({ label: "grok_home", cleanup: () => removeDirectoryConfirmed(isolatedHome) });
      args = purePacketReview
        ? buildGrokReviewArgs(model, grokPrompt.path, reasoningEffort)
        : commonGrokArgs(model, grokPrompt.path, reasoningEffort);
      env = { ...process.env, GROK_HOME: isolatedHome, XAI_API_KEY: apiKey };
    } else {
      if (!credentialState(engine, authMode).configured) {
        throw new Error("Grok OAuth session not found; run the Adaptive Agent Runtime login command first");
      }
      const grokPrompt = prepareGrokPrompt(prompt, { assignmentRole });
      cleanups.push({ label: "prompt_file", cleanup: grokPrompt.cleanup });
      args = purePacketReview
        ? buildGrokReviewArgs(model, grokPrompt.path, reasoningEffort)
        : commonGrokArgs(model, grokPrompt.path, reasoningEffort);
      env = sanitizedEnvironment([], ["XAI_API_KEY"]);
    }

    if (sideEffect && idempotencyKey) {
      env.ADAPTIVE_AGENT_IDEMPOTENCY_KEY = idempotencyKey;
    }
    if (engine === "grok-build") {
      if (purePacketReview) {
        let reviewTerminal = null;
        let reviewAttempt = 1;
        while (reviewAttempt <= 2) {
          reviewTerminal = await runMonitoredGrokReview(executable, args, {
            cwd, env, progressDeadlineMinutes, candidateRevision,
          });
          const retry = grokReviewRetryDecision(reviewTerminal, { attempt: reviewAttempt });
          if (!retry.retry) {
            if (reviewAttempt >= 2 && reviewTerminal.retrySafe === true) reviewTerminal.retrySafe = false;
            break;
          }
          reviewAttempt += 1;
        }
        reviewTerminal.attempts = reviewAttempt;
        const validVerdict = new Set(["REVIEW_PASS", "REVIEW_FAIL"]).has(reviewTerminal.reviewStatus);
        return { code: validVerdict ? 0 : 1, reviewTerminal };
      }
      const code = await runMonitoredGrok(executable, args, {
        cwd, env, progressDeadlineMinutes, onStructuredProgress,
      });
      if (code !== 0) {
        executionError = new ExternalAgentExecutionError(`provider_exit: external agent exited ${code}`, {
          failureClass: "provider_exit",
          retrySafe: true,
          resultUnknown: Boolean(sideEffect),
          details: { provider_exit_code: code },
        });
      }
      return code;
    }
    return await runAttached(executable, args, { cwd, env });
  } catch (error) {
    executionError = error;
    throw error;
  } finally {
    runCleanupStack(cleanups, executionError);
  }
}


async function main() {
  try {
    const options = parseArgs(process.argv.slice(2));
    if (options.renderStatusCard) {
      process.stdout.write(`${renderExternalAgentCard(options)}\n`);
      return;
    }
    if (options.resolveRoute) {
      process.stdout.write(`${JSON.stringify(options.routeDecision)}\n`);
      return;
    }
    if (options.check) {
      const result = checkExternalAgent(options);
      process.stdout.write(`${JSON.stringify(result)}\n`);
      if (!result.available) process.exitCode = 2;
      return;
    }
    if (options.login) {
      process.exitCode = await loginExternalAgent(options);
      return;
    }
    validateRuntimeBinding(options);
    const assignment = validateAssignmentLaunch(options);
    if (assignment) {
      options.assignmentContractVersion = assignment.assignment_contract_version || 1;
      options.sideEffect = typeof assignment.side_effect === "boolean" ? assignment.side_effect : null;
      options.idempotencyKey = typeof assignment.idempotency_key === "string" ? assignment.idempotency_key.trim() : null;
      options.progressDeadlineMinutes = assignment.progress_deadline_minutes;
      options.assignmentRoute = assignment.route && typeof assignment.route === "object" ? assignment.route : null;
      options.agentType = typeof assignment.agent_type === "string" && assignment.agent_type.trim()
        ? assignment.agent_type.trim()
        : `external-${options.engine}`;
      options.assignmentRole = typeof assignment.role === "string" && assignment.role.trim()
        ? assignment.role.trim().toLowerCase()
        : null;
      options.candidateRevision = typeof assignment.candidate_revision === "string" && assignment.candidate_revision.trim()
        ? assignment.candidate_revision.trim()
        : null;
      options.reviewPhase = typeof assignment.review_phase === "string" && assignment.review_phase.trim()
        ? assignment.review_phase.trim().toLowerCase()
        : null;
      options.reviewShardReceipts = Array.isArray(assignment.review_shard_receipts)
        ? assignment.review_shard_receipts.slice()
        : [];
      options.executionLineage = deriveExecutionLineage(options, assignment);
    }
    validateRuleHandshake(options);
    let eventSeq = 1;
    let previousSnapshot = runtimeGitSnapshot(options.cwd);
    recordRuntimeReceipt(options, "assignment_started", eventSeq, {
      baseline_head: previousSnapshot.head,
      last_observed_head: previousSnapshot.head,
      last_observed_status_sha256: previousSnapshot.statusSha256,
      ...(options.progressDeadlineMinutes ? { progress_deadline_minutes: options.progressDeadlineMinutes } : {}),
    });
    const heartbeat = options.assignmentId ? setInterval(() => {
      try {
        const snapshot = runtimeGitSnapshot(options.cwd);
        eventSeq += 1;
        if (snapshot.head !== previousSnapshot.head || snapshot.statusSha256 !== previousSnapshot.statusSha256) {
          recordRuntimeReceipt(options, "assignment_progress", eventSeq, {
            last_observed_head: snapshot.head,
            last_observed_status_sha256: snapshot.statusSha256,
            progress_evidence: boundedProgressEvidence(previousSnapshot, snapshot),
          });
          previousSnapshot = snapshot;
        } else {
          recordRuntimeReceipt(options, "assignment_heartbeat", eventSeq);
        }
      } catch (error) {
        process.stderr.write(`adaptive-delivery-runtime-heartbeat: ${error.message}\n`);
      }
    }, runtimeHeartbeatIntervalMs()) : null;
    heartbeat?.unref();
    let code;
    let reviewTerminal = null;
    try {
      const executionResult = await executeExternalAgent(options);
      if (executionResult && typeof executionResult === "object" && !Array.isArray(executionResult) && "reviewTerminal" in executionResult) {
        code = executionResult.code;
        reviewTerminal = executionResult.reviewTerminal;
      } else {
        code = executionResult;
      }
    } catch (error) {
      if (heartbeat) clearInterval(heartbeat);
      const { failureClass, retrySafe, resultUnknown, failureDetails } = classifyExternalExecutionFailure(error, {
        sideEffect: Boolean(options.sideEffect),
      });
      const nextAction = failureClass === "review_sharding_required"
        ? "split the immutable Reviewer contract into bounded shards and one final synthesis review"
        : failureClass === "prompt_too_large"
          ? "reduce the external Agent prompt scope before retrying"
          : retrySafe
            ? "inspect bounded external agent failure before any retry"
            : "do not retry automatically; reconcile provider/process state first";
      eventSeq += 1;
      recordRuntimeReceipt(options, "assignment_terminal", eventSeq, {
        terminal_state: "failed", transport_outcome: "failed", delivery_outcome: "unresolved",
        summary: error.message, evidence: [], artifacts: [], next_action: nextAction,
        retry_class: failureClass, failure_class: failureClass, retry_safe: retrySafe,
        failure_details: failureDetails, result_unknown: resultUnknown,
      });
      persistExternalTerminalReceipt(options, {
        exitCode: 1, summary: error.message, deliveryOutcome: "unresolved",
        failureClass, retryClass: failureClass, retrySafe, resultUnknown, failureDetails,
      });
      throw error;
    }
    if (heartbeat) clearInterval(heartbeat);
    if (options.assignmentId) {
      const finalSnapshot = runtimeGitSnapshot(options.cwd);
      if (
        finalSnapshot.head !== previousSnapshot.head
        || finalSnapshot.statusSha256 !== previousSnapshot.statusSha256
      ) {
        eventSeq += 1;
        recordRuntimeReceipt(options, "assignment_progress", eventSeq, {
          last_observed_head: finalSnapshot.head,
          last_observed_status_sha256: finalSnapshot.statusSha256,
          progress_evidence: boundedProgressEvidence(previousSnapshot, finalSnapshot),
        });
        previousSnapshot = finalSnapshot;
      }
    }
    if (reviewTerminal) {
      eventSeq += 1;
      const reviewStatus = reviewTerminal.reviewStatus;
      const validVerdict = new Set(["REVIEW_PASS", "REVIEW_FAIL"]).has(reviewStatus);
      const deliveryOutcome = validVerdict ? reviewTerminal.deliveryOutcome : "unresolved";
      const transportCompleted = new Set(["REVIEW_PASS", "REVIEW_FAIL", "REVIEW_OUTPUT_INVALID", "REVIEW_NO_VERDICT"]).has(reviewStatus);
      const terminalState = transportCompleted ? "completed" : "failed";
      const transportOutcome = transportCompleted ? "completed" : "failed";
      const failureClassByStatus = {
        REVIEW_NO_VERDICT: "review_no_verdict",
        REVIEW_MAX_TURNS: "review_max_turns",
        REVIEW_TIMEOUT: "review_timeout",
        REVIEW_PROCESS_STUCK: "review_process_stuck",
        REVIEW_OUTPUT_INVALID: "review_output_invalid",
        REVIEW_PROVIDER_ERROR: "review_provider_error",
      };
      const failureClass = validVerdict ? null : (failureClassByStatus[reviewStatus] || "review_provider_error");
      const resultUnknown = reviewStatus === "REVIEW_PROCESS_STUCK";
      const retrySafe = validVerdict ? false : Boolean(reviewTerminal.retrySafe) && !resultUnknown;
      const evidence = validVerdict
        ? (options.reviewPhase === "synthesis" ? [...options.reviewShardReceipts] : [`git:${options.candidateRevision}`])
        : [];
      const artifacts = validVerdict ? [`git:${options.candidateRevision}`] : [];
      const summary = reviewStatus === "REVIEW_PASS"
        ? "Grok Reviewer returned a validated PASS verdict"
        : reviewStatus === "REVIEW_FAIL"
          ? "Grok Reviewer returned validated Critical/Important findings"
          : `Grok Reviewer terminal outcome: ${reviewStatus}`;
      const nextAction = reviewStatus === "REVIEW_PASS"
        ? "continue candidate acceptance"
        : reviewStatus === "REVIEW_FAIL"
          ? "return exact candidate to rework without Reviewer retry"
          : reviewStatus === "REVIEW_MAX_TURNS"
            ? "repair Reviewer invocation policy; do not repeat the same max-turns command"
            : retrySafe
              ? "retry the same pure-packet review at most once after verified process cleanup"
              : "do not retry automatically; inspect Reviewer terminal evidence";
      const failureDetails = validVerdict ? null : {
        review_status: reviewStatus,
        attempts: reviewTerminal.attempts || 1,
        had_model_output: Boolean(reviewTerminal.hadModelOutput),
        ...(reviewTerminal.cleanupDiagnostic ? { cleanup_diagnostic: reviewTerminal.cleanupDiagnostic } : {}),
        ...(reviewTerminal.timeoutReason ? { timeout_reason: reviewTerminal.timeoutReason } : {}),
        ...(reviewTerminal.validationError ? { validation_error: reviewTerminal.validationError } : {}),
        ...(reviewTerminal.providerError ? { provider_error: reviewTerminal.providerError } : {}),
      };
      recordRuntimeReceipt(options, "assignment_terminal", eventSeq, {
        terminal_state: terminalState,
        transport_outcome: transportOutcome,
        delivery_outcome: deliveryOutcome,
        summary,
        evidence,
        artifacts,
        next_action: nextAction,
        retry_class: failureClass || "none",
        ...(failureClass ? { failure_class: failureClass } : {}),
        retry_safe: retrySafe,
        ...(failureDetails ? { failure_details: failureDetails } : {}),
        ...(reviewTerminal.reviewVerdict ? { review_verdict: reviewTerminal.reviewVerdict } : {}),
        review_status: reviewStatus,
        result_unknown: resultUnknown,
      });
      persistExternalTerminalReceipt(options, {
        exitCode: code,
        summary,
        deliveryOutcome,
        failureClass,
        retryClass: failureClass || "none",
        retrySafe,
        resultUnknown,
        failureDetails,
        reviewStatus,
        reviewVerdict: reviewTerminal.reviewVerdict || null,
      });
      process.exitCode = validVerdict ? 0 : 1;
      return;
    }
    eventSeq += 1;
    let delivery = null;
    let deliveryError = null;
    if (code === 0) {
      try {
        delivery = readDeliveryReceipt(options.deliveryReceipt, {
          assignmentRole: options.assignmentRole,
          candidateRevision: options.candidateRevision,
          reviewPhase: options.reviewPhase,
          reviewShardReceipts: options.reviewShardReceipts,
        });
      } catch (error) {
        deliveryError = error;
      }
    }
    const underlyingFinalFailureClass = deliveryError
      ? "delivery_receipt_invalid"
      : code === 0
        ? null
        : "provider_exit";
    const finalResultUnknown = Boolean(options.sideEffect)
      && (code !== 0 || deliveryError !== null || delivery?.delivery_outcome === "unresolved" || delivery === null);
    const finalFailureClass = finalResultUnknown ? "result_unknown" : underlyingFinalFailureClass;
    const finalRetryClass = finalResultUnknown
      ? "result_unknown"
      : delivery?.retry_class || finalFailureClass || "none";
    const finalRetrySafe = finalResultUnknown
      ? false
      : finalFailureClass === "delivery_receipt_invalid"
        ? false
        : finalFailureClass === "provider_exit"
          ? true
          : null;
    const finalFailureDetails = deliveryError
      ? {
          validation_error: deliveryError.message,
          ...(finalResultUnknown ? { underlying_failure_class: "delivery_receipt_invalid" } : {}),
        }
      : code !== 0
        ? {
            provider_exit_code: code,
            ...(finalResultUnknown ? { underlying_failure_class: "provider_exit", provider_failure_class: "provider_exit" } : {}),
          }
        : finalResultUnknown
          ? { underlying_failure_class: "provider_result_unreconciled" }
          : null;
    const finalSummary = delivery?.summary || deliveryError?.message
      || (code === 0 ? "external agent process completed" : `external agent exited ${code}`);
    recordRuntimeReceipt(options, "assignment_terminal", eventSeq, {
      terminal_state: code === 0 ? "completed" : "failed",
      transport_outcome: code === 0 ? "completed" : "failed",
      delivery_outcome: delivery?.delivery_outcome || "unresolved",
      summary: finalSummary,
      evidence: delivery?.evidence || [], artifacts: delivery?.artifacts || [],
      next_action: delivery?.next_action || (deliveryError ? "repair delivery receipt" : code === 0 ? "inspect delivery" : "inspect external agent output"),
      retry_class: finalRetryClass,
      ...(finalFailureClass ? { failure_class: finalFailureClass } : {}),
      ...(typeof finalRetrySafe === "boolean" ? { retry_safe: finalRetrySafe } : {}),
      ...(finalFailureDetails ? { failure_details: finalFailureDetails } : {}),
      reconciliation_evidence: delivery?.reconciliation_evidence || [],
      ...(delivery?.review_verdict ? { review_verdict: delivery.review_verdict } : {}),
      result_unknown: finalResultUnknown,
    });
    persistExternalTerminalReceipt(options, {
      exitCode: code,
      summary: finalSummary,
      deliveryOutcome: delivery?.delivery_outcome || "unresolved",
      failureClass: finalFailureClass,
      retryClass: finalRetryClass,
      retrySafe: finalRetrySafe,
      resultUnknown: finalResultUnknown,
      failureDetails: finalFailureDetails,
    });
    if (deliveryError) throw deliveryError;
    process.exitCode = code;
  } catch (error) {
    process.stderr.write(`adaptive-delivery-external-agent: ${error.message}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === new URL(import.meta.url).pathname) {
  await main();
}
