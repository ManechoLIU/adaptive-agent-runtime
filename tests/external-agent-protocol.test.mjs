import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import test from "node:test";

const moduleUrl = new URL("../scripts/run_external_agent.mjs", import.meta.url);

async function runtimeModule(label) {
  return import(`${moduleUrl.href}?${label}=${Date.now()}-${Math.random()}`);
}

function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.pid = 424299;
  child.exitCode = null;
  child.signalCode = null;
  return child;
}

async function withMonitorTimeouts(values, run) {
  const names = {
    launch: "AD_GROK_LAUNCH_TIMEOUT_MS",
    first: "AD_GROK_FIRST_OUTPUT_TIMEOUT_MS",
    stall: "AD_GROK_STALL_TIMEOUT_MS",
    absolute: "AD_EXTERNAL_ATTEMPT_TIMEOUT_MS",
    grace: "AD_EXTERNAL_KILL_GRACE_MS",
  };
  const previous = Object.fromEntries(Object.entries(names).map(([key, name]) => [key, process.env[name]]));
  for (const [key, value] of Object.entries(values)) process.env[names[key]] = String(value);
  try {
    return await run();
  } finally {
    for (const [key, name] of Object.entries(names)) {
      if (previous[key] === undefined) delete process.env[name];
      else process.env[name] = previous[key];
    }
  }
}

test("execution protocol rejects illegal phase transitions and records the canonical ordered success path", async () => {
  const runtime = await runtimeModule("phase-machine");
  assert.equal(typeof runtime.createExternalExecutionProtocol, "function");
  const protocol = runtime.createExternalExecutionProtocol();
  assert.throws(() => protocol.advance("PROVIDER_STARTED"), /illegal execution phase transition/i);
  for (const phase of ["PACKET_VALIDATED", "PROVIDER_STARTING", "PROVIDER_STARTED", "FIRST_PROGRESS", "ANALYZING", "FINAL_RESULT"]) {
    protocol.advance(phase);
  }
  assert.deepEqual(protocol.succeed(), {
    outcome_code: "SUCCESS",
    phase_history: ["PRECHECK", "PACKET_VALIDATED", "PROVIDER_STARTING", "PROVIDER_STARTED", "FIRST_PROGRESS", "ANALYZING", "FINAL_RESULT"],
    advisory: true,
    critical_path: false,
  });
});
test("bounded input acquisition fails closed before provider spawn when the pipe never closes", async () => {
  const runtime = await runtimeModule("bounded-input");
  assert.equal(typeof runtime.readBoundedExternalInput, "function");
  const input = new PassThrough();
  let providerSpawned = false;
  await assert.rejects(
    runtime.readBoundedExternalInput(input, { timeoutMs: 30, maxBytes: 128 }),
    (error) => error?.outcomeCode === "LOCAL_PRECHECK_FAILED"
      && error?.failureClass === "input_timeout"
      && providerSpawned === false,
  );
});

test("provider start timeout publishes canonical outcome and stops before PROVIDER_STARTED", async () => {
  const runtime = await runtimeModule("start-timeout");
  const child = fakeChild();
  await withMonitorTimeouts({ launch: 30, first: 500, stall: 500, absolute: 1000, grace: 20 }, async () => {
    const spawnTimer = setTimeout(() => child.emit("spawn"), 5);
    await assert.rejects(
      runtime.runMonitoredGrok("fake-grok", [], {
        cwd: process.cwd(), env: process.env, spawnChild: () => child,
        terminateGroup: async () => ({ confirmed: true, diagnostic: "fake group gone" }),
      }),
      (error) => error?.failureClass === "cli_launch_timeout"
        && error?.outcomeCode === "PROVIDER_START_TIMEOUT"
        && !error?.phaseHistory?.includes("PROVIDER_STARTED"),
    );
    clearTimeout(spawnTimer);
  });
});

test("first-token timeout is anchored after provider start", async () => {
  const runtime = await runtimeModule("first-token-timeout");
  const child = fakeChild();
  await withMonitorTimeouts({ launch: 250, first: 70, stall: 500, absolute: 1000, grace: 20 }, async () => {
    const startedAt = Date.now();
    const timers = [
      setTimeout(() => child.emit("spawn"), 5),
      setTimeout(() => child.stdout.write(`${JSON.stringify({ type: "provider_started", provider: "grok-build", session_id: "s1" })}\n`), 90),
    ];
    try {
      await assert.rejects(
        runtime.runMonitoredGrok("fake-grok", [], {
          cwd: process.cwd(), env: process.env, spawnChild: () => child,
          terminateGroup: async () => ({ confirmed: true, diagnostic: "fake group gone" }),
        }),
        (error) => error?.failureClass === "first_output_timeout"
          && error?.outcomeCode === "FIRST_TOKEN_TIMEOUT"
          && error?.phaseHistory?.at(-1) === "PROVIDER_STARTED",
      );
      assert.ok(Date.now() - startedAt >= 135, "first-token clock must start after validated provider start");
    } finally {
      for (const timer of timers) clearTimeout(timer);
    }
  });
});

test("an OS child spawn is not PROVIDER_STARTED without a validated provider event", async () => {
  const runtime = await runtimeModule("spawn-is-not-provider-start");
  const child = fakeChild();
  await withMonitorTimeouts({ launch: 40, first: 500, stall: 500, absolute: 1000, grace: 20 }, async () => {
    const spawnedAt = Date.now();
    const spawnTimer = setTimeout(() => child.emit("spawn"), 5);
    try {
      await assert.rejects(
        runtime.runMonitoredGrok("fake-grok", [], {
          cwd: process.cwd(), env: process.env, spawnChild: () => child,
          terminateGroup: async () => ({ confirmed: true, diagnostic: "fake group gone" }),
        }),
        (error) => error?.outcomeCode === "PROVIDER_START_TIMEOUT"
          && error?.details?.provider_started === false
          && !error?.phaseHistory?.includes("PROVIDER_STARTED"),
      );
      assert.ok(Date.now() - spawnedAt >= 35);
    } finally {
      clearTimeout(spawnTimer);
    }
  });
});

test("ACP progress envelopes without type-specific payload do not count as progress", async () => {
  const runtime = await runtimeModule("payloadless-acp");
  const child = fakeChild();
  await withMonitorTimeouts({ launch: 100, first: 70, stall: 500, absolute: 1000, grace: 20 }, async () => {
    const timers = [
      setTimeout(() => child.emit("spawn"), 5),
      setTimeout(() => child.stdout.write(`${JSON.stringify({ type: "provider_started", provider: "grok-build", session_id: "s1" })}\n`), 10),
      setTimeout(() => child.stdout.write(`${JSON.stringify({ sessionId: "s1", update: { sessionUpdate: "agent_message_chunk" } })}\n`), 35),
    ];
    try {
      await assert.rejects(
        runtime.runMonitoredGrok("fake-grok", [], {
          cwd: process.cwd(), env: process.env, spawnChild: () => child,
          terminateGroup: async () => ({ confirmed: true, diagnostic: "fake group gone" }),
        }),
        (error) => error?.outcomeCode === "FIRST_TOKEN_TIMEOUT"
          && error?.phaseHistory?.at(-1) === "PROVIDER_STARTED",
      );
    } finally {
      for (const timer of timers) clearTimeout(timer);
    }
  });
});

test("Kimi uses the same bounded provider protocol and cleanup for every watchdog class", async () => {
  const runtime = await runtimeModule("kimi-bounded-protocol");
  assert.equal(typeof runtime.runMonitoredKimi, "function");
  const scenarios = [
    { name: "provider start", events: [], values: { launch: 35, first: 200, stall: 200, absolute: 500 }, outcome: "PROVIDER_START_TIMEOUT" },
    { name: "first progress", events: [[10, { type: "system", subtype: "init", session_id: "k1" }]], values: { launch: 100, first: 35, stall: 200, absolute: 500 }, outcome: "FIRST_TOKEN_TIMEOUT" },
    { name: "heartbeat", events: [[10, { type: "system", subtype: "init", session_id: "k1" }], [15, { type: "assistant", message: { role: "assistant", content: [{ type: "text", text: "working" }] } }]], values: { launch: 100, first: 100, stall: 35, absolute: 500 }, outcome: "HEARTBEAT_TIMEOUT" },
    { name: "absolute", events: [[10, { type: "system", subtype: "init", session_id: "k1" }], [15, { type: "assistant", message: { role: "assistant", content: [{ type: "text", text: "working" }] } }], [40, { type: "assistant", message: { role: "assistant", content: [{ type: "text", text: "still working" }] } }]], values: { launch: 100, first: 100, stall: 100, absolute: 55 }, outcome: "PROVIDER_TIMEOUT" },
  ];
  for (const scenario of scenarios) {
    const child = fakeChild();
    let cleanupCalls = 0;
    await withMonitorTimeouts({ ...scenario.values, grace: 20 }, async () => {
      const timers = [setTimeout(() => child.emit("spawn"), 5), ...scenario.events.map(([delay, event]) => setTimeout(() => child.stdout.write(`${JSON.stringify(event)}\n`), delay))];
      try {
        await assert.rejects(
          runtime.runMonitoredKimi("fake-kimi", [], {
            cwd: process.cwd(), env: process.env, spawnChild: () => child,
            terminateGroup: async () => { cleanupCalls += 1; return { confirmed: true, diagnostic: "fake group gone" }; },
          }),
          (error) => error?.outcomeCode === scenario.outcome,
          scenario.name,
        );
        assert.equal(cleanupCalls, 1, `${scenario.name} must clean the process group`);
      } finally {
        for (const timer of timers) clearTimeout(timer);
      }
    });
  }
});

test("only validated model progress refreshes the heartbeat clock", async () => {
  const runtime = await runtimeModule("heartbeat-timeout");
  const child = fakeChild();
  await withMonitorTimeouts({ launch: 200, first: 100, stall: 80, absolute: 1000, grace: 20 }, async () => {
    const startedAt = Date.now();
    const timers = [
      setTimeout(() => child.emit("spawn"), 5),
      setTimeout(() => child.stdout.write(`${JSON.stringify({ type: "text", data: "working" })}\n`), 15),
      setTimeout(() => child.stdout.write(`${JSON.stringify({ type: "usage", usage: { input_tokens: 1 } })}\n`), 55),
      setTimeout(() => child.stdout.write("not-json\n"), 75),
      setTimeout(() => child.stdout.write(`${JSON.stringify({ type: "tool_call_update", toolCallId: "x", content: [], rawOutput: null, locations: [] })}\n`), 90),
    ];
    try {
      await assert.rejects(
        runtime.runMonitoredGrok("fake-grok", [], {
          cwd: process.cwd(), env: process.env, spawnChild: () => child,
          terminateGroup: async () => ({ confirmed: true, diagnostic: "fake group gone" }),
        }),
        (error) => error?.failureClass === "generation_stalled"
          && error?.outcomeCode === "HEARTBEAT_TIMEOUT"
          && error?.phaseHistory?.includes("FIRST_PROGRESS"),
      );
      assert.ok(Date.now() - startedAt < 150, "noise must not refresh the heartbeat clock");
    } finally {
      for (const timer of timers) clearTimeout(timer);
    }
  });
});

test("final-result validation distinguishes parse failure from canonical success", async () => {
  const runtime = await runtimeModule("final-result");
  const validateFinalResult = (stdout) => {
    const records = stdout.trim().split("\n").map((line) => JSON.parse(line));
    const final = records.find((record) => record.type === "final_result");
    if (!final?.result?.ok) throw new Error("missing required final result");
    return final.result;
  };
  await withMonitorTimeouts({ launch: 200, first: 100, stall: 500, absolute: 1000, grace: 20 }, async () => {
    const invalidChild = fakeChild();
    const invalid = runtime.runMonitoredGrok("fake-grok", [], {
      cwd: process.cwd(), env: process.env, spawnChild: () => invalidChild,
      validateFinalResult, returnProtocol: true,
    });
    invalidChild.emit("spawn");
    invalidChild.stdout.write(`${JSON.stringify({ type: "text", data: "analysis" })}\n`);
    invalidChild.exitCode = 0;
    invalidChild.emit("exit", 0, null);
    invalidChild.stdout.end(); invalidChild.stderr.end();
    invalidChild.emit("close", 0, null);
    await assert.rejects(invalid, (error) => error?.outcomeCode === "RESULT_PARSE_FAILED");

    const validChild = fakeChild();
    const valid = runtime.runMonitoredGrok("fake-grok", [], {
      cwd: process.cwd(), env: process.env, spawnChild: () => validChild,
      validateFinalResult, returnProtocol: true,
    });
    validChild.emit("spawn");
    validChild.stdout.write(`${JSON.stringify({ type: "text", data: "analysis" })}\n`);
    validChild.stdout.write(`${JSON.stringify({ type: "final_result", result: { ok: true } })}\n`);
    validChild.exitCode = 0;
    validChild.emit("exit", 0, null);
    validChild.stdout.end(); validChild.stderr.end();
    validChild.emit("close", 0, null);
    assert.deepEqual(await valid, {
      code: 0,
      result: { ok: true },
      outcome_code: "SUCCESS",
      phase_history: ["PRECHECK", "PACKET_VALIDATED", "PROVIDER_STARTING", "PROVIDER_STARTED", "FIRST_PROGRESS", "ANALYZING", "FINAL_RESULT"],
      advisory: true,
      critical_path: false,
    });
  });
});

test("ordinary zero exit without a validated final result stays RESULT_PARSE_FAILED", async () => {
  const runtime = await runtimeModule("missing-final-result");
  const child = fakeChild();
  const completion = runtime.runMonitoredGrok("fake-grok", [], {
    cwd: process.cwd(), env: process.env, spawnChild: () => child, returnProtocol: true,
  });
  child.emit("spawn");
  child.stdout.write(`${JSON.stringify({ type: "provider_started", provider: "grok-build", session_id: "s1" })}\n`);
  child.stdout.write(`${JSON.stringify({ type: "text", data: "analysis only" })}\n`);
  child.exitCode = 0;
  child.emit("exit", 0, null);
  child.stdout.end(); child.stderr.end();
  child.emit("close", 0, null);
  const terminal = await completion;
  assert.equal(terminal.outcome_code, "RESULT_PARSE_FAILED");
  assert.equal(terminal.phase_history.includes("FINAL_RESULT"), false);
});

test("advisory hypothesis mode is bounded and preserves supported contradicted insufficient verdicts", async () => {
  const runtime = await runtimeModule("advisory-hypotheses");
  const packet = runtime.parseAdvisoryHypothesisPacket(JSON.stringify({
    schema_version: 1,
    mode: "advisory_hypothesis_review",
    question: "Evaluate only these bounded hypotheses.",
    hypotheses: [
      { id: "h1", statement: "Clock starts before provider spawn.", evidence: ["trace:a"] },
      { id: "h2", statement: "Metadata refreshes provider progress.", evidence: ["trace:b"] },
      { id: "h3", statement: "Cleanup leaves a child process.", evidence: ["trace:c"] },
    ],
  }));
  const result = runtime.validateAdvisoryHypothesisResult({
    hypotheses: [
      { id: "h1", verdict: "supported", rationale: "trace" },
      { id: "h2", verdict: "contradicted", rationale: "trace" },
      { id: "h3", verdict: "insufficient", rationale: "trace" },
    ],
  }, { packet, localEvidenceContradictions: ["h1"] });
  assert.equal(result.outcome_code, "MODEL_CONTRADICTED_BY_LOCAL_EVIDENCE");
  assert.equal(result.advisory, true);
  assert.equal(result.critical_path, false);
  assert.deepEqual(result.hypotheses.map(({ verdict }) => verdict), ["supported", "contradicted", "insufficient"]);
  assert.throws(() => runtime.parseAdvisoryHypothesisPacket(JSON.stringify({
    schema_version: 1, mode: "advisory_hypothesis_review", question: "Find the root cause", hypotheses: [],
  })), /bounded hypotheses|open-ended root-cause/i);
});

test("the runner never retries a provider route; Controller must issue a new route and assignment", async () => {
  const runtime = await runtimeModule("retry-gate");
  const preBoundary = { retrySafe: true, providerStarted: false, hadModelOutput: false, resultUnknown: false };
  assert.deepEqual(runtime.externalRetryDecision(preBoundary, { attempt: 1, authorizedSafeRetry: false }), {
    retry: false, reason: "controller_route_reassignment_required",
  });
  assert.deepEqual(runtime.externalRetryDecision(preBoundary, { attempt: 1, authorizedSafeRetry: true }), {
    retry: false, reason: "controller_route_reassignment_required",
  });
  assert.deepEqual(runtime.externalRetryDecision(preBoundary, { attempt: 2, authorizedSafeRetry: true }), {
    retry: false, reason: "retry_budget_exhausted",
  });
  for (const terminal of [
    { ...preBoundary, providerStarted: true },
    { ...preBoundary, hadModelOutput: true },
    { ...preBoundary, resultUnknown: true },
  ]) {
    assert.equal(runtime.externalRetryDecision(terminal, { attempt: 1, authorizedSafeRetry: true }).retry, false);
  }
});
