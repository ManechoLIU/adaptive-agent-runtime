import { Buffer } from "node:buffer";

export const EXTERNAL_EXECUTION_PHASES = Object.freeze([
  "PRECHECK",
  "PACKET_VALIDATED",
  "PROVIDER_STARTING",
  "PROVIDER_STARTED",
  "FIRST_PROGRESS",
  "ANALYZING",
  "FINAL_RESULT",
]);

export const EXTERNAL_OUTCOME_CODES = Object.freeze([
  "LOCAL_PRECHECK_FAILED",
  "PROVIDER_START_TIMEOUT",
  "FIRST_TOKEN_TIMEOUT",
  "HEARTBEAT_TIMEOUT",
  "PROVIDER_TIMEOUT",
  "PARENT_CANCELLED",
  "RESULT_PARSE_FAILED",
  "MODEL_CONTRADICTED_BY_LOCAL_EVIDENCE",
  "SUCCESS",
]);

const OUTCOME_SET = new Set(EXTERNAL_OUTCOME_CODES);
const HYPOTHESIS_VERDICTS = new Set(["supported", "contradicted", "insufficient"]);

export class ExternalAgentProtocolError extends Error {
  constructor(message, {
    outcomeCode = "LOCAL_PRECHECK_FAILED",
    failureClass = "local_precheck_failed",
    retrySafe = false,
    resultUnknown = false,
    phaseHistory = ["PRECHECK"],
    details = {},
  } = {}) {
    super(message);
    this.name = "ExternalAgentProtocolError";
    this.outcomeCode = outcomeCode;
    this.failureClass = failureClass;
    this.retrySafe = Boolean(retrySafe);
    this.resultUnknown = Boolean(resultUnknown);
    this.phaseHistory = [...phaseHistory];
    this.details = details && typeof details === "object" && !Array.isArray(details) ? { ...details } : {};
  }
}

export function createExternalExecutionProtocol({ advisory = true, criticalPath = false } = {}) {
  let phaseIndex = 0;
  const phaseHistory = [EXTERNAL_EXECUTION_PHASES[0]];
  const metadata = { advisory: Boolean(advisory), critical_path: Boolean(criticalPath) };
  return {
    get currentPhase() {
      return EXTERNAL_EXECUTION_PHASES[phaseIndex];
    },
    get phaseHistory() {
      return [...phaseHistory];
    },
    advance(nextPhase) {
      const expected = EXTERNAL_EXECUTION_PHASES[phaseIndex + 1];
      if (nextPhase !== expected) {
        throw new ExternalAgentProtocolError(
          `illegal execution phase transition: ${EXTERNAL_EXECUTION_PHASES[phaseIndex]} -> ${nextPhase}; expected ${expected || "terminal"}`,
          { failureClass: "execution_phase_invalid", phaseHistory },
        );
      }
      phaseIndex += 1;
      phaseHistory.push(nextPhase);
      return this.snapshot();
    },
    snapshot(outcomeCode = null) {
      if (outcomeCode !== null && !OUTCOME_SET.has(outcomeCode)) {
        throw new ExternalAgentProtocolError(`unsupported external outcome code: ${outcomeCode}`, {
          failureClass: "external_outcome_invalid", phaseHistory,
        });
      }
      return {
        ...(outcomeCode ? { outcome_code: outcomeCode } : {}),
        phase_history: [...phaseHistory],
        ...metadata,
      };
    },
    succeed() {
      if (this.currentPhase !== "FINAL_RESULT") {
        throw new ExternalAgentProtocolError("SUCCESS requires FINAL_RESULT", {
          failureClass: "execution_phase_invalid", phaseHistory,
        });
      }
      return this.snapshot("SUCCESS");
    },
  };
}

export function protocolFailure(error, {
  outcomeCode,
  failureClass,
  protocol,
  retrySafe = false,
  resultUnknown = false,
  details = {},
} = {}) {
  return new ExternalAgentProtocolError(String(error?.message || error), {
    outcomeCode,
    failureClass,
    retrySafe,
    resultUnknown,
    phaseHistory: protocol?.phaseHistory || ["PRECHECK"],
    details,
  });
}

export function readBoundedExternalInput(stream, { timeoutMs, maxBytes }) {
  if (!stream || typeof stream.on !== "function") {
    return Promise.reject(new ExternalAgentProtocolError("input stream is unavailable", {
      failureClass: "input_unavailable",
    }));
  }
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1) {
    return Promise.reject(new ExternalAgentProtocolError("input timeout must be a positive integer", {
      failureClass: "input_bound_invalid",
    }));
  }
  if (!Number.isInteger(maxBytes) || maxBytes < 1) {
    return Promise.reject(new ExternalAgentProtocolError("input byte bound must be a positive integer", {
      failureClass: "input_bound_invalid",
    }));
  }
  return new Promise((resolve, reject) => {
    const chunks = [];
    let observedBytes = 0;
    let settled = false;
    const cleanup = () => {
      clearTimeout(timer);
      stream.off("data", onData);
      stream.off("end", onEnd);
      stream.off("error", onError);
      stream.pause?.();
    };
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      cleanup();
      fn(value);
    };
    const fail = (message, failureClass, details = {}) => finish(reject, new ExternalAgentProtocolError(message, {
      failureClass,
      retrySafe: true,
      details: { provider_started: false, observed_bytes: observedBytes, max_bytes: maxBytes, ...details },
    }));
    const onData = (chunk) => {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      observedBytes += bytes.length;
      if (observedBytes > maxBytes) {
        fail(`external input exceeds ${maxBytes} bytes`, "input_too_large");
        return;
      }
      chunks.push(bytes);
    };
    const onEnd = () => finish(resolve, Buffer.concat(chunks).toString("utf8").trim());
    const onError = (error) => fail(`external input failed: ${error.message}`, "input_error", {
      input_error: String(error?.message || error),
    });
    const timer = setTimeout(() => fail(
      `external input did not close within ${timeoutMs}ms`,
      "input_timeout",
      { input_timeout_ms: timeoutMs },
    ), timeoutMs);
    stream.on("data", onData);
    stream.once("end", onEnd);
    stream.once("error", onError);
    stream.resume?.();
  });
}

function boundedString(value, field, { max = 2_000 } = {}) {
  if (typeof value !== "string" || !value.trim()) {
    throw new ExternalAgentProtocolError(`advisory hypothesis ${field} must be a non-empty string`, {
      failureClass: "advisory_packet_invalid",
    });
  }
  const normalized = value.trim();
  if (normalized.length > max) {
    throw new ExternalAgentProtocolError(`advisory hypothesis ${field} exceeds ${max} characters`, {
      failureClass: "advisory_packet_invalid",
    });
  }
  return normalized;
}

export function parseAdvisoryHypothesisPacket(rawPacket) {
  let packet;
  try {
    packet = typeof rawPacket === "string" ? JSON.parse(rawPacket) : rawPacket;
  } catch (error) {
    throw new ExternalAgentProtocolError(`advisory hypothesis packet must be valid JSON: ${error.message}`, {
      failureClass: "advisory_packet_invalid",
    });
  }
  if (!packet || typeof packet !== "object" || Array.isArray(packet)) {
    throw new ExternalAgentProtocolError("advisory hypothesis packet must be one object", {
      failureClass: "advisory_packet_invalid",
    });
  }
  if (packet.schema_version !== 1 || packet.mode !== "advisory_hypothesis_review") {
    throw new ExternalAgentProtocolError("advisory hypothesis packet requires schema_version=1 and mode=advisory_hypothesis_review", {
      failureClass: "advisory_packet_invalid",
    });
  }
  const question = boundedString(packet.question, "question", { max: 500 });
  if (/\b(find|discover|investigate)\b.{0,30}\b(root[ -]?cause|cause)\b/i.test(question)
      || /根因|原因是什么|自由分析/u.test(question)) {
    throw new ExternalAgentProtocolError("open-ended root-cause prompts are forbidden; provide bounded hypotheses and evidence", {
      failureClass: "advisory_packet_invalid",
    });
  }
  if (!Array.isArray(packet.hypotheses) || packet.hypotheses.length < 1 || packet.hypotheses.length > 8) {
    throw new ExternalAgentProtocolError("advisory mode requires 1..8 bounded hypotheses", {
      failureClass: "advisory_packet_invalid",
    });
  }
  const ids = new Set();
  const hypotheses = packet.hypotheses.map((hypothesis) => {
    if (!hypothesis || typeof hypothesis !== "object" || Array.isArray(hypothesis)) {
      throw new ExternalAgentProtocolError("each advisory hypothesis must be one object", {
        failureClass: "advisory_packet_invalid",
      });
    }
    const id = boundedString(hypothesis.id, "id", { max: 80 });
    if (ids.has(id)) {
      throw new ExternalAgentProtocolError(`duplicate advisory hypothesis id: ${id}`, {
        failureClass: "advisory_packet_invalid",
      });
    }
    ids.add(id);
    const statement = boundedString(hypothesis.statement, "statement", { max: 500 });
    if (!Array.isArray(hypothesis.evidence) || hypothesis.evidence.length < 1 || hypothesis.evidence.length > 12) {
      throw new ExternalAgentProtocolError(`hypothesis ${id} requires 1..12 bounded evidence items`, {
        failureClass: "advisory_packet_invalid",
      });
    }
    const evidence = hypothesis.evidence.map((item) => boundedString(item, `evidence for ${id}`));
    return { id, statement, evidence };
  });
  const localEvidenceContradictions = packet.local_evidence_contradictions ?? [];
  if (!Array.isArray(localEvidenceContradictions)
      || localEvidenceContradictions.some((id) => typeof id !== "string" || !ids.has(id.trim()))
      || new Set(localEvidenceContradictions.map((id) => id.trim())).size !== localEvidenceContradictions.length) {
    throw new ExternalAgentProtocolError("local_evidence_contradictions must contain unique bounded hypothesis ids", {
      failureClass: "advisory_packet_invalid",
    });
  }
  return {
    schema_version: 1,
    mode: packet.mode,
    question,
    hypotheses,
    local_evidence_contradictions: localEvidenceContradictions.map((id) => id.trim()),
  };
}

export function validateAdvisoryHypothesisResult(value, { packet, localEvidenceContradictions = null } = {}) {
  const normalizedPacket = parseAdvisoryHypothesisPacket(packet);
  if (!value || typeof value !== "object" || Array.isArray(value) || !Array.isArray(value.hypotheses)) {
    throw new ExternalAgentProtocolError("advisory hypothesis result requires a hypotheses array", {
      outcomeCode: "RESULT_PARSE_FAILED", failureClass: "result_parse_failed",
    });
  }
  const expectedIds = normalizedPacket.hypotheses.map(({ id }) => id);
  const results = value.hypotheses.map((result) => {
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      throw new ExternalAgentProtocolError("each advisory result must be one object", {
        outcomeCode: "RESULT_PARSE_FAILED", failureClass: "result_parse_failed",
      });
    }
    const id = boundedString(result.id, "result id", { max: 80 });
    const verdict = String(result.verdict || "").trim().toLowerCase();
    if (!HYPOTHESIS_VERDICTS.has(verdict)) {
      throw new ExternalAgentProtocolError(`hypothesis ${id} verdict must be supported|contradicted|insufficient`, {
        outcomeCode: "RESULT_PARSE_FAILED", failureClass: "result_parse_failed",
      });
    }
    return { id, verdict, rationale: boundedString(result.rationale, `rationale for ${id}`) };
  });
  if (JSON.stringify(results.map(({ id }) => id)) !== JSON.stringify(expectedIds)) {
    throw new ExternalAgentProtocolError("advisory result ids/order must exactly match the bounded packet", {
      outcomeCode: "RESULT_PARSE_FAILED", failureClass: "result_parse_failed",
    });
  }
  const contradictionIds = new Set((localEvidenceContradictions ?? normalizedPacket.local_evidence_contradictions)
    .map((id) => String(id).trim()).filter(Boolean));
  const contradictedByLocalEvidence = results.some(({ id, verdict }) => contradictionIds.has(id) && verdict !== "contradicted");
  return {
    outcome_code: contradictedByLocalEvidence ? "MODEL_CONTRADICTED_BY_LOCAL_EVIDENCE" : "SUCCESS",
    advisory: true,
    critical_path: false,
    hypotheses: results,
    local_evidence_contradictions: [...contradictionIds],
  };
}

export function externalRetryDecision(terminal, { attempt = 1, authorizedSafeRetry = false } = {}) {
  if (attempt >= 2) return { retry: false, reason: "retry_budget_exhausted" };
  if (terminal?.resultUnknown) return { retry: false, reason: "result_unknown" };
  if (terminal?.hadModelOutput) return { retry: false, reason: "model_output_observed" };
  if (terminal?.providerStarted) return { retry: false, reason: "provider_boundary_crossed" };
  if (terminal?.retrySafe !== true) return { retry: false, reason: "not_retry_safe" };
  void authorizedSafeRetry;
  return { retry: false, reason: "controller_route_reassignment_required" };
}
