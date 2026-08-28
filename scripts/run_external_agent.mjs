#!/usr/bin/env node

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";

const KIMI_KEYCHAIN_SERVICE = "adaptive-delivery-kimi-k3";
const XAI_KEYCHAIN_SERVICE = "adaptive-delivery-xai-grok";

const routes = {
  "kimi-code": {
    executable: "kimi",
    fallbackPaths: [path.join(homedir(), ".kimi-code", "bin", "kimi")],
    modelsByAuthMode: {
      oauth: new Set(["kimi-code/k3"]),
      api: new Set(["k3"]),
    },
    versionArgs: ["--version"],
  },
  "grok-build": {
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
    authorizedExternalCall: false,
    authorizedLogin: false,
    deviceAuth: false,
    engine: null,
    model: null,
    authMode: null,
    region: null,
    cwd: null,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--check") options.check = true;
    else if (argument === "--execute") options.execute = true;
    else if (argument === "--login") options.login = true;
    else if (argument === "--authorized-external-call") options.authorizedExternalCall = true;
    else if (argument === "--authorized-login") options.authorizedLogin = true;
    else if (argument === "--device-auth") options.deviceAuth = true;
    else if (["--engine", "--model", "--auth-mode", "--region", "--cwd"].includes(argument)) {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) throw new Error(`Missing value for ${argument}`);
      if (argument === "--auth-mode") options.authMode = value;
      else options[argument.slice(2)] = value;
      index += 1;
    } else {
      throw new Error(`Unknown argument: ${argument}`);
    }
  }

  if ([options.check, options.execute, options.login].filter(Boolean).length !== 1) {
    throw new Error("Choose exactly one of --check, --execute, or --login");
  }
  if (options.engine === "kimi-code-api") {
    options.engine = "kimi-code";
    options.authMode ||= "api";
  }
  const route = routes[options.engine];
  if (!route) throw new Error(`Unsupported engine: ${options.engine}`);
  if (!options.authMode) throw new Error("--auth-mode oauth|api is required");
  const models = route.modelsByAuthMode[options.authMode];
  if (!models) throw new Error(`Unsupported auth mode: ${options.authMode}`);
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
  if (options.execute && !options.authorizedExternalCall) {
    throw new Error("--execute requires --authorized-external-call after current user authorization");
  }
  return options;
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
    if (process.env.KIMI_MODEL_API_KEY) {
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
  return "https://api.kimi.com/coding/v1";
}

function commonGrokArgs(model, prompt) {
  return [
    "--no-auto-update", "--no-subagents", "--no-memory", "--sandbox", "workspace",
    "--always-approve", "-m", model, "-p", prompt, "--output-format", "streaming-json",
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

export function checkExternalAgent({ cwd, engine, model, authMode }) {
  assertDirectory(cwd);
  const route = routes[engine];
  const executable = resolveExecutable(route);
  const result = spawnSync(executable, route.versionArgs, { cwd, encoding: "utf8", env: process.env });
  if (result.error?.code === "ENOENT") {
    return { available: false, engine, model, authMode, reason: `${route.executable} executable not found` };
  }
  if (result.error) throw result.error;
  if (result.status !== 0) {
    return {
      available: false,
      engine,
      model,
      authMode,
      reason: (result.stderr || `${route.executable} version check exited ${result.status}`).trim(),
    };
  }
  const credential = credentialState(engine, authMode);
  return {
    available: true,
    engine,
    model,
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

async function executeExternalAgent({ cwd, engine, model, authMode }) {
  assertDirectory(cwd);
  const prompt = await readStdin();
  if (!prompt) throw new Error("A bounded routing contract prompt is required on stdin");

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
    };
  } else if (engine === "kimi-code") {
    if (!credentialState(engine, authMode).configured) {
      throw new Error("Kimi Code OAuth session not found; run the Adaptive Delivery login command first");
    }
    args = ["-m", model, "-p", prompt, "--output-format", "stream-json"];
    env = sanitizedEnvironment(["KIMI_MODEL_"], ["MOONSHOT_API_KEY"]);
  } else if (authMode === "api") {
    const apiKey = readApiKey(engine);
    if (!apiKey) {
      throw new Error(
        `xAI API key not found; set XAI_API_KEY or store it in macOS Keychain service ${xaiKeychainService()}`,
      );
    }
    const isolatedHome = mkdtempSync(path.join(tmpdir(), "adaptive-delivery-grok-api-"));
    cleanup = () => rmSync(isolatedHome, { recursive: true, force: true });
    args = commonGrokArgs(model, prompt);
    env = { ...process.env, GROK_HOME: isolatedHome, XAI_API_KEY: apiKey };
  } else {
    if (!credentialState(engine, authMode).configured) {
      throw new Error("Grok OAuth session not found; run the Adaptive Delivery login command first");
    }
    args = commonGrokArgs(model, prompt);
    env = sanitizedEnvironment([], ["XAI_API_KEY"]);
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
    process.exitCode = await executeExternalAgent(options);
  } catch (error) {
    process.stderr.write(`adaptive-delivery-external-agent: ${error.message}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === new URL(import.meta.url).pathname) {
  await main();
}
