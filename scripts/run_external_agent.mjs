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

function readDeliveryReceipt(pathname) {
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
  return {
    delivery_outcome: deliveryOutcome, summary, evidence: receipt.evidence, artifacts: receipt.artifacts,
    next_action: receipt.next_action, retry_class: receipt.retry_class,
    reconciliation_evidence: receipt.reconciliation_evidence || [],
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
    ...(eventType === "assignment_started" ? options.executionLineage : {}), ...extra,
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

function persistExternalTerminalReceipt(options, { exitCode, summary, deliveryOutcome = "unresolved" }) {
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

function commonGrokArgs(model, prompt, reasoningEffort) {
  return [
    "--no-auto-update", "--no-subagents", "--no-memory", "--sandbox", "workspace",
    "--always-approve", "-m", model, "-p", prompt, "--output-format", "streaming-json",
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

async function executeExternalAgent({ cwd, engine, model, reasoningEffort, authMode, sideEffect, idempotencyKey }) {
  assertDirectory(cwd);
  const rawPrompt = await readStdin();
  if (!rawPrompt) throw new Error("A bounded routing contract prompt is required on stdin");
  const prompt = sideEffect && idempotencyKey
    ? `[Adaptive Agent Runtime side-effect contract] Any external side effect in this execution MUST use the exact idempotency key: ${idempotencyKey}. Do not perform the side effect without applying this key through the provider/API mechanism.\n\n${rawPrompt}`
    : rawPrompt;

  const executable = resolveExecutable(routes[engine]);
  let args;
  let env;
  let cleanup = () => {};

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
      KIMI_MODEL_PROVIDER_TYPE: "kimi",
      KIMI_MODEL_BASE_URL: kimiApiBaseUrl(),
      KIMI_MODEL_MAX_CONTEXT_SIZE: "1048576",
      KIMI_MODEL_CAPABILITIES: "image_in,video_in,thinking,always_thinking,tool_use",
      KIMI_MODEL_DISPLAY_NAME: "Kimi K3 API",
      KIMI_MODEL_THINKING_EFFORT: reasoningEffort,
    };
  } else if (engine === "kimi-code") {
    if (!credentialState(engine, authMode).configured) {
      throw new Error("Kimi Code OAuth session not found; run the Adaptive Delivery login command first");
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
    const isolatedHome = mkdtempSync(path.join(tmpdir(), "adaptive-delivery-grok-api-"));
    cleanup = () => rmSync(isolatedHome, { recursive: true, force: true });
    args = commonGrokArgs(model, prompt, reasoningEffort);
    env = { ...process.env, GROK_HOME: isolatedHome, XAI_API_KEY: apiKey };
  } else {
    if (!credentialState(engine, authMode).configured) {
      throw new Error("Grok OAuth session not found; run the Adaptive Delivery login command first");
    }
    args = commonGrokArgs(model, prompt, reasoningEffort);
    env = sanitizedEnvironment([], ["XAI_API_KEY"]);
  }

  if (sideEffect && idempotencyKey) {
    env.ADAPTIVE_AGENT_IDEMPOTENCY_KEY = idempotencyKey;
  }
  try {
    return await runAttached(executable, args, { cwd, env });
  } finally {
    cleanup();
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
    const assignment = validateAssignmentLaunch(options);
    validateRuntimeBinding(options);
    if (assignment) {
      options.assignmentContractVersion = assignment.assignment_contract_version || 1;
      options.sideEffect = typeof assignment.side_effect === "boolean" ? assignment.side_effect : null;
      options.idempotencyKey = typeof assignment.idempotency_key === "string" ? assignment.idempotency_key.trim() : null;
      options.progressDeadlineMinutes = assignment.progress_deadline_minutes;
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
    try {
      code = await executeExternalAgent(options);
    } catch (error) {
      if (heartbeat) clearInterval(heartbeat);
      eventSeq += 1;
      recordRuntimeReceipt(options, "assignment_terminal", eventSeq, { terminal_state: "failed", transport_outcome: "failed", delivery_outcome: "unresolved", summary: error.message, evidence: [], artifacts: [], next_action: "inspect external agent failure", retry_class: "transport_error", result_unknown: Boolean(options.sideEffect) });
      persistExternalTerminalReceipt(options, { exitCode: 1, summary: error.message, deliveryOutcome: "unresolved" });
      throw error;
    }
    if (heartbeat) clearInterval(heartbeat);
    eventSeq += 1;
    let delivery = null;
    let deliveryError = null;
    if (code === 0) {
      try {
        delivery = readDeliveryReceipt(options.deliveryReceipt);
      } catch (error) {
        deliveryError = error;
      }
    }
    recordRuntimeReceipt(options, "assignment_terminal", eventSeq, {
      terminal_state: code === 0 ? "completed" : "failed",
      transport_outcome: code === 0 ? "completed" : "failed",
      delivery_outcome: delivery?.delivery_outcome || "unresolved",
      summary: delivery?.summary || deliveryError?.message || (code === 0 ? "external agent process completed" : `external agent exited ${code}`),
      evidence: delivery?.evidence || [], artifacts: delivery?.artifacts || [],
      next_action: delivery?.next_action || (deliveryError ? "repair delivery receipt" : code === 0 ? "inspect delivery" : "inspect external agent output"),
      retry_class: delivery?.retry_class || (deliveryError ? "none" : code === 0 ? "none" : "provider_exit"),
      reconciliation_evidence: delivery?.reconciliation_evidence || [],
      result_unknown: Boolean(options.sideEffect) && (code !== 0 || deliveryError !== null || delivery?.delivery_outcome === "unresolved" || delivery === null),
    });
    persistExternalTerminalReceipt(options, {
      exitCode: code,
      summary: delivery?.summary || deliveryError?.message || (code === 0 ? "external agent process completed" : `external agent exited ${code}`),
      deliveryOutcome: delivery?.delivery_outcome || "unresolved",
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
