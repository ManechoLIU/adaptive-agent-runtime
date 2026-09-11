#!/usr/bin/env python3
"""Codex lifecycle enforcement for controller-performance scoring."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from controller_scoring_guard import consume_score_guard, cycle_score_extremes, finalize_attested_cycle_score, finalize_cycle_candidate, finalize_score, governance_risk_projection, latest_score_history, read_and_record_model, receipt_path
except ModuleNotFoundError:
    from scripts.controller_scoring_guard import consume_score_guard, cycle_score_extremes, finalize_attested_cycle_score, finalize_cycle_candidate, finalize_score, governance_risk_projection, latest_score_history, read_and_record_model, receipt_path
try:
    from evaluation_transaction import COMPUTE, COMPUTED, DERIVED, READ, begin_evaluation, classify_evaluation_intent, correct_evaluation, evidence_snapshot_sha256, validate_completion
except ModuleNotFoundError:
    from scripts.evaluation_transaction import COMPUTE, COMPUTED, DERIVED, READ, begin_evaluation, classify_evaluation_intent, correct_evaluation, evidence_snapshot_sha256, validate_completion
try:
    from project_context_guard import initialize_project_context
except ModuleNotFoundError:
    from scripts.project_context_guard import initialize_project_context

MODEL_RELATIVE_PATH = Path("references/controller-performance-scoring.md")
CONTROLLER_REGISTRY_PATH = Path(
    os.environ.get(
        "AD_CONTROLLER_REGISTRY",
        str(Path.home() / ".codex" / "adaptive-delivery-controllers.json"),
    )
).expanduser()
STATE_ROOT = Path(
    os.environ.get(
        "AD_SCORING_STATE_DIR",
        str(Path.home() / ".codex" / "state" / "adaptive-delivery-scoring"),
    )
).expanduser()

_CONTROLLER_TERMS = re.compile(r"(?:总控|项目总控|controller|orchestrator)", re.IGNORECASE)
_SCORE_VALUE = r"(?:100|[1-9]?\d)(?:\.\d+)?(?:\s*/\s*100|\s*分)"
# A score assertion connects its label directly to a value, not through prose
# about a rule, a file name, or a different paragraph. Keep one-line wrapping.
_SCORE_SEPARATOR = r"(?:[ \t\r:*_：=为是\[\]-]|\n(?![ \t\r]*\n)){0,20}"
_CONTROLLER_OUTPUT_TERM = r"(?:项目总控|总控|(?<![\w/-])(?:controller|orchestrator)(?![\w/-]))"
_CYCLE_REQUEST = re.compile(r"(?:单回合|闭环回合|最佳(?:闭环)?回合|最差(?:闭环)?回合|最佳闭环|最差闭环|能力上限|能力下限|single[ -]?cycle|best[ -]?(?:closed[ -]?loop[ -]?)?cycle|worst[ -]?(?:closed[ -]?loop[ -]?)?cycle)", re.IGNORECASE)
_CYCLE_OUTPUT_SCORE = re.compile(rf"(?:单回合诊断评分|单回合评分|single[ -]?cycle(?: diagnostic)? score){_SCORE_SEPARATOR}{_SCORE_VALUE}", re.IGNORECASE)
_CYCLE_EXTREMA_OUTPUT = re.compile(
    rf"(?:最佳(?:闭环)?回合|最差(?:闭环)?回合|能力上限|能力下限|best[ -]?(?:closed[ -]?loop[ -]?)?cycle|worst[ -]?(?:closed[ -]?loop[ -]?)?cycle){_SCORE_SEPARATOR}{_SCORE_VALUE}",
    re.IGNORECASE,
)
_OUTPUT_SCORE = re.compile(
    rf"{_CONTROLLER_OUTPUT_TERM}[^\n。！？!?;；]{{0,80}}?(?:评分|得分|performance score|score|performance){_SCORE_SEPARATOR}{_SCORE_VALUE}",
    re.IGNORECASE,
)
_FORMAL_SCORE_LABEL = re.compile(
    rf"(?:正式(?:总控)?(?:履职)?评分|履职评分|formal(?: controller)? score|controller performance score){_SCORE_SEPARATOR}{_SCORE_VALUE}",
    re.IGNORECASE,
)
_PERFORMANCE_SCORE_LABEL = re.compile(
    rf"(?:近期履职能力|recent performance score){_SCORE_SEPARATOR}{_SCORE_VALUE}",
    re.IGNORECASE,
)
_RISK_CONSTRAINED_SCORE_LABEL = re.compile(
    rf"(?:风险约束分|risk-constrained score){_SCORE_SEPARATOR}{_SCORE_VALUE}",
    re.IGNORECASE,
)
_ANY_SCORE_SHAPE = re.compile(
    r"(?:100|[1-9]?\d)(?:\.\d+)?(?:\s*/\s*100|\s*分)",
    re.IGNORECASE,
)

_SCORE_TERMS = re.compile(
    r"(?:评分|打分|分数|多少分|评估|评价|履职评估|履职评分|performance\s+(?:score|scoring|evaluation)|score\s+(?:the\s+)?(?:controller|orchestrator)|rate\s+(?:the\s+)?(?:controller|orchestrator)|evaluate|assess)",
    re.IGNORECASE,
)
_SCORING_MODEL_REQUEST = re.compile(
    r"(?:(?:调用|使用|让|按|按照|基于|根据|call|use).{0,12})?(?:现有|当前|治理体系.{0,6})?(?:评分模型|scoring\s+model).{0,16}(?:重新)?(?:评分|打分|评估|评价|score|rate|evaluate|assess)",
    re.IGNORECASE,
)
_SCORING_FAILURE_RETRY = re.compile(
    r"(?:(?:怎么|为什么|为何|还是|又|仍然|依然|还).{0,16}(?:不能|不行|没法|无法|失败|unknown|阻断|拦截).{0,12}(?:评分|打分)|(?:评分|打分).{0,12}(?:不能|不行|没法|无法|失败|unknown|阻断|拦截))",
    re.IGNORECASE,
)
_PERFORMANCE_WORKFLOW = re.compile(
    r"(?:审计.{0,16}(?:项目总控|总控).{0,16}(?:履职|表现)|(?:项目总控|总控).{0,16}(?:履职|表现).{0,16}审计|(?:比较|评估|评价).{0,16}(?:项目总控|总控).{0,16}(?:履职|表现)|(?:项目总控|总控).{0,16}(?:履职|表现).{0,16}(?:比较|评估|评价)|(?:检查|核对).{0,16}(?:项目总控|总控).{0,16}假繁荣|(?:audit|evaluate|assess|review|compare).{0,30}(?:controller|orchestrator).{0,30}(?:performance|duty|execution)|(?:controller|orchestrator).{0,30}(?:performance|duty|execution).{0,30}(?:audit|evaluate|assess|review|compare))",
    re.IGNORECASE,
)
_FORMAL_REQUEST_CONTEXT = re.compile(
    r"(?:正式|履职评分|总控(?:履职)?评分|项目总控(?:履职)?评分|近期(?:履职|表现)|最近(?:履职|表现)|综合评分|formal(?: controller)? score|controller performance score)",
    re.IGNORECASE,
)


def scoring_model_path(skill_root: str | Path) -> Path:
    return (Path(skill_root).resolve() / MODEL_RELATIVE_PATH).resolve()


def scoring_model_sha256(skill_root: str | Path) -> str:
    return hashlib.sha256(scoring_model_path(skill_root).read_bytes()).hexdigest()


def is_controller_scoring_request(prompt: str) -> bool:
    text = str(prompt or "").strip()
    return bool(
        (_CONTROLLER_TERMS.search(text) and _SCORE_TERMS.search(text))
        or (_CONTROLLER_TERMS.search(text) and _CYCLE_REQUEST.search(text))
        or _PERFORMANCE_WORKFLOW.search(text)
        or _SCORING_MODEL_REQUEST.search(text)
        or _SCORING_FAILURE_RETRY.search(text)
    )


def _scoring_mode(prompt: str) -> str:
    text = str(prompt or "")
    if _CYCLE_REQUEST.search(text) and not _FORMAL_REQUEST_CONTEXT.search(text):
        return "cycle"
    return "formal"


def _score_output_text(message: str) -> str:
    # Link destinations are metadata; retain visible labels (including scores).
    # Do not discard quotes/code blocks: wrapping a claim must not bypass the gate.
    return re.sub(
        r"\[([^\]\n]*)\]\((?:<[^>\n]*>|[^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)",
        r"\1",
        str(message or ""),
    )


def looks_like_controller_score_output(message: str) -> bool:
    text = _score_output_text(message)
    return bool(
        _OUTPUT_SCORE.search(text)
        or _FORMAL_SCORE_LABEL.search(text)
        or _CYCLE_OUTPUT_SCORE.search(text)
        or _CYCLE_EXTREMA_OUTPUT.search(text)
        or _PERFORMANCE_SCORE_LABEL.search(text)
        or _RISK_CONSTRAINED_SCORE_LABEL.search(text)
    )


def _extract_score_value(message: str) -> float | None:
    text = _score_output_text(message)
    match = _RISK_CONSTRAINED_SCORE_LABEL.search(text) or _OUTPUT_SCORE.search(text) or _FORMAL_SCORE_LABEL.search(text)
    if not match:
        return None
    value = re.search(r"(?:100|[1-9]?\d)(?:\.\d+)?", match.group(0))
    return float(value.group(0)) if value else None


def _extract_cycle_score_value(message: str) -> float | None:
    match = _CYCLE_OUTPUT_SCORE.search(_score_output_text(message))
    if not match:
        return None
    value = re.search(r"(?:100|[1-9]?\d)(?:\.\d+)?", match.group(0))
    return float(value.group(0)) if value else None


def _has_distinct_formal_score_output(message: str) -> bool:
    text = _score_output_text(message)
    cycle_spans = [match.span() for match in _CYCLE_OUTPUT_SCORE.finditer(text)]
    formal_matches = (
        list(_OUTPUT_SCORE.finditer(text))
        + list(_FORMAL_SCORE_LABEL.finditer(text))
        + list(_PERFORMANCE_SCORE_LABEL.finditer(text))
        + list(_RISK_CONSTRAINED_SCORE_LABEL.finditer(text))
    )
    for formal in formal_matches:
        formal_start, formal_end = formal.span()
        if not any(max(formal_start, cycle_start) < min(formal_end, cycle_end) for cycle_start, cycle_end in cycle_spans):
            return True
    return False


def _extract_labeled_value(message: str, labels: tuple[str, ...]) -> str | None:
    for raw_line in str(message or "").splitlines():
        line = raw_line.strip()
        for label in labels:
            match = re.match(
                rf"(?:(?:[-*+] |\d+[.)] )|(?:#{{1,6}} ))?(?:\*\*|__)?{label}(?:\*\*|__)?\s*[：:]\s*(.+)$",
                line,
                re.IGNORECASE,
            )
            if match:
                return match.group(1).strip()[:500] or None
    return None


def _parse_score_text(value: str | None) -> float | None:
    match = re.search(r"(?:100|[1-9]?\d)(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else None


def _extract_labeled_score(message: str, labels: tuple[str, ...]) -> float | None:
    return _parse_score_text(_extract_labeled_value(message, labels))


def _is_unknown(value: str | None) -> bool:
    return str(value or "").strip().upper() == "UNKNOWN"


EVALUATION_DIMENSION_WEIGHTS = {
    "goal_progress": 0.25,
    "task_decomposition": 0.15,
    "critical_path_priority": 0.15,
    "scheduling_execution": 0.15,
    "quality_acceptance_evidence": 0.10,
    "recovery_flow": 0.10,
    "control_plane_auditability": 0.10,
}


def _extract_evaluation_dimension_scores(message: str) -> dict[str, float]:
    raw = _extract_labeled_value(
        message,
        (r"七维原始分", r"dimension raw scores"),
    )
    if raw is None:
        raise ValueError(
            "COMPUTE evaluation requires machine-checkable seven-dimension raw scores"
        )
    values: dict[str, float] = {}
    for item in re.split(r"\s*[,，;；]\s*", raw):
        if not item:
            continue
        if "=" not in item:
            raise ValueError("dimension raw score entry must use key=value")
        key, text = item.split("=", 1)
        key = key.strip()
        if key not in EVALUATION_DIMENSION_WEIGHTS or key in values:
            raise ValueError(f"unknown or duplicate evaluation dimension {key or 'EMPTY'}")
        try:
            value = float(text.strip())
        except ValueError as error:
            raise ValueError(f"invalid raw score for {key}") from error
        if not 0 <= value <= 100:
            raise ValueError(f"raw score for {key} must be within 0..100")
        values[key] = value
    if set(values) != set(EVALUATION_DIMENSION_WEIGHTS):
        missing = sorted(set(EVALUATION_DIMENSION_WEIGHTS) - set(values))
        extra = sorted(set(values) - set(EVALUATION_DIMENSION_WEIGHTS))
        raise ValueError(
            "seven-dimension raw score vector is incomplete"
            + (f"; missing={missing}" if missing else "")
            + (f"; extra={extra}" if extra else "")
        )
    return values


def _runtime_weighted_performance(dimension_scores: dict[str, float]) -> float:
    return round(
        sum(
            float(dimension_scores[key]) * weight
            for key, weight in EVALUATION_DIMENSION_WEIGHTS.items()
        ),
        6,
    )


def _evaluation_calculation_receipt(
    message: str,
    transaction: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    try:
        dimensions = _extract_evaluation_dimension_scores(message)
    except ValueError as error:
        return None, [str(error)]
    performance = _extract_labeled_score(
        message,
        (r"近期履职能力", r"recent performance score"),
    )
    if performance is None:
        return None, ["COMPUTE evaluation requires recent performance score"]
    runtime_performance = _runtime_weighted_performance(dimensions)
    if abs(float(performance) - runtime_performance) > 1e-6:
        errors.append(
            "recent performance score does not equal Runtime weighted seven-dimension calculation"
        )

    errors.extend(
        validate_completion(
            transaction,
            result={
                "dimension_scores": dimensions,
                "performance_score": runtime_performance,
            },
            provenance={
                "dimension_scores": COMPUTED,
                "performance_score": DERIVED,
            },
            core_fields=("dimension_scores", "performance_score"),
        )
    )
    material = {
        "schema_version": 1,
        "evaluation_id": transaction.get("evaluation_id"),
        "evidence_snapshot_sha256": transaction.get("evidence_snapshot_sha256"),
        "model_sha256": (
            transaction.get("model", {}).get("sha256")
            if isinstance(transaction.get("model"), dict)
            else None
        ),
        "dimension_scores": {
            key: dimensions[key] for key in EVALUATION_DIMENSION_WEIGHTS
        },
        "dimension_weights": dict(EVALUATION_DIMENSION_WEIGHTS),
        "performance_score": runtime_performance,
    }
    canonical = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receipt = {
        **material,
        "calculation_receipt_sha256": hashlib.sha256(canonical).hexdigest(),
    }
    return receipt, errors


def _validate_cycle_extrema_claim(
    message: str,
    *,
    expected: dict[str, dict[str, Any] | None],
) -> None:
    fields = {
        "best_score": _extract_labeled_value(message, (r"单回合最高分", r"single-cycle best score")),
        "best_id": _extract_labeled_value(message, (r"最高回合", r"best cycle")),
        "worst_score": _extract_labeled_value(message, (r"单回合最低分", r"single-cycle worst score")),
        "worst_id": _extract_labeled_value(message, (r"最低回合", r"worst cycle")),
    }
    if any(value is None for value in fields.values()):
        raise ValueError("cycle extrema require best/worst scores and cycle ids, or explicit UNKNOWN for all four fields")
    for side in ("best", "worst"):
        score_text = fields[f"{side}_score"]
        cycle_text = fields[f"{side}_id"]
        expected_record = expected[side]
        if expected_record is None:
            if not (_is_unknown(score_text) and _is_unknown(cycle_text)):
                raise ValueError("cycle extrema do not match eligible same-controller, same-model history; expected UNKNOWN")
            continue
        claimed_score = _parse_score_text(score_text)
        claimed_cycle = str(cycle_text or "").strip().strip("`")
        expected_score = float(expected_record["score"])
        expected_cycle = str(expected_record["cycle_id"]).strip()
        if claimed_score is None or abs(claimed_score - expected_score) > 1e-9 or claimed_cycle != expected_cycle:
            raise ValueError("cycle extrema do not match eligible same-controller, same-model history")


def _extract_three_layer_report(message: str) -> tuple[float, str, str, float]:
    performance = _extract_labeled_score(message, (r"近期履职能力", r"recent performance score"))
    risk_status = _extract_labeled_value(message, (r"治理风险状态", r"governance risk status"))
    risk_summary = _extract_labeled_value(message, (r"治理风险依据", r"governance risk basis"))
    constrained = _extract_labeled_score(message, (r"风险约束分", r"risk-constrained score"))
    if performance is None or risk_status is None or risk_summary is None or constrained is None:
        raise ValueError(
            "three-layer formal report requires performance score, governance risk status/basis, and risk-constrained score"
        )
    return performance, risk_status.upper().strip(), risk_summary, constrained


def _extract_window_summary(message: str) -> str | None:
    for raw_line in str(message or "").splitlines():
        line = raw_line.strip()
        if re.search(r"(?:评估窗口|评分窗口|evaluation\s+window)", line, re.IGNORECASE):
            if "：" in line:
                line = line.split("：", 1)[1].strip()
            elif ":" in line:
                line = line.split(":", 1)[1].strip()
            return line[:500] or None
    return None


def _repo_root(cwd: str | Path) -> Path:
    completed = subprocess.run(["git", "-C", str(Path(cwd).resolve()), "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True)
    return Path(completed.stdout.strip()).resolve()


def _git_common_dir(repo: str | Path) -> Path:
    root = Path(repo).resolve()
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    )
    common = Path(completed.stdout.strip())
    return (root / common).resolve() if not common.is_absolute() else common.resolve()


def _logical_controller_id(repo: Path, source_session_id: str) -> str:
    source = str(source_session_id or "").strip()
    if not source:
        raise ValueError("controller scoring requires a non-empty source session identity")
    try:
        registry = json.loads(CONTROLLER_REGISTRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return source
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"controller registry is unreadable: {error}") from error
    if not isinstance(registry, dict):
        raise ValueError("controller registry root must be an object")
    requested_common = _git_common_dir(repo)
    matches: list[str] = []
    for controller_id, registered_repo in registry.items():
        if not isinstance(controller_id, str) or controller_id.startswith("__") or not isinstance(registered_repo, str):
            continue
        try:
            if _git_common_dir(Path(registered_repo).expanduser()) == requested_common:
                matches.append(controller_id)
        except (OSError, subprocess.CalledProcessError):
            continue
    if not matches:
        return source
    if len(matches) != 1:
        raise ValueError("controller scoring requires exactly one registered logical Controller")
    # The request session is the evaluator, not necessarily the Controller being
    # evaluated. The Stop event remains bound to this source session, while the
    # receipt and score history bind to the repository's unique logical Controller.
    return matches[0]


def _state_path(session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    return STATE_ROOT / f"{safe or 'unknown'}.json"


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _model_context(skill_root: str | Path, digest: str) -> str:
    model = scoring_model_path(skill_root)
    content = model.read_text(encoding="utf-8")
    return (
        "Adaptive Agent Runtime controller-scoring machine gate is active. "
        "The following is the exact installed scoring model and is authoritative for this scoring turn. "
        "Do not substitute another rubric. The Stop gate will fail closed if this exact model changes before the response completes.\n"
        f"installed_scoring_model_sha256={digest}\n"
        f"installed_scoring_model_path={model}\n\n"
        + content
    )


def _scoring_evidence_snapshot(
    repo: Path,
    *,
    skill_root: str | Path,
    controller_id: str,
) -> dict[str, Any]:
    context = initialize_project_context(repo, skill_root=skill_root)
    compact_sources: dict[str, Any] = {}
    for name, value in context.get("sources", {}).items():
        if not isinstance(value, dict):
            continue
        if name == "git":
            compact_sources[name] = {
                key: value.get(key)
                for key in ("status", "head", "branch", "worktree_status_sha256")
            }
        else:
            compact_sources[name] = {
                key: value.get(key)
                for key in ("status", "path", "sha256", "bytes")
            }
    return {
        "project_root": context.get("project_root"),
        "context_state": context.get("state"),
        "verified_facts": context.get("verified_facts", []),
        "unknown_facts": context.get("unknown_facts", []),
        "sources": compact_sources,
        "controller_id": controller_id,
        "governance_risk_projection": governance_risk_projection(
            repo,
            controller_session_id=controller_id,
        ),
    }


def _evaluation_context(transaction: dict[str, Any]) -> str:
    return (
        "Adaptive Agent Runtime current-evaluation transaction is active."
        + chr(10)
        + "Existing Result != New Evaluation; READ history cannot satisfy this COMPUTE task."
        + chr(10)
        + "Do not use any historical total score as the basis for the new capability score before current evidence is evaluated."
        + chr(10)
        + "evaluation_id=" + str(transaction.get("evaluation_id", ""))
        + chr(10)
        + "evaluation_intent=" + str(transaction.get("intent", ""))
        + chr(10)
        + "evaluation_begun_at=" + str(transaction.get("evaluation_begun_at", ""))
        + chr(10)
        + "evidence_cutoff_at=" + str(transaction.get("evidence_cutoff_at", ""))
        + chr(10)
        + "evidence_snapshot_sha256=" + str(transaction.get("evidence_snapshot_sha256", ""))
        + chr(10)
        + "evaluation_model_sha256=" + str(transaction.get("model", {}).get("sha256", ""))
        + chr(10)
        + "For the core new result, output provenance must be COMPUTED. Risk/cap results remain separate DERIVED layers."
        + chr(10)
        + "For COMPUTE, also output exactly one 七维原始分 line with these keys: "
        + "goal_progress, task_decomposition, critical_path_priority, scheduling_execution, "
        + "quality_acceptance_evidence, recovery_flow, control_plane_auditability. "
        + "Use key=value pairs on 0..100. Runtime—not the response—recomputes the 25/15/15/15/10/10/10 weighted performance score and creates the calculation receipt."
    )


def _evaluation_metadata_errors(
    message: str,
    transaction: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    provenance = _extract_labeled_value(
        message,
        (r"本次评估来源", r"evaluation provenance", r"result provenance"),
    )
    cutoff = _extract_labeled_value(
        message,
        (r"事实截止时间", r"evidence cutoff"),
    )
    evidence_sha = _extract_labeled_value(
        message,
        (r"证据快照\s*SHA256", r"evidence snapshot\s*sha256"),
    )
    model_sha = _extract_labeled_value(
        message,
        (r"评分模型\s*SHA256", r"model\s*sha256"),
    )
    if str(provenance or "").strip().upper() != COMPUTED:
        errors.append("core result provenance must be COMPUTED")
    if str(cutoff or "").strip() != str(transaction.get("evidence_cutoff_at", "")):
        errors.append("evidence cutoff does not match the current evaluation transaction")
    if str(evidence_sha or "").strip().lower() != str(transaction.get("evidence_snapshot_sha256", "")).lower():
        errors.append("evidence snapshot sha256 does not match the current evaluation transaction")
    expected_model = str(transaction.get("model", {}).get("sha256", "")).lower()
    if str(model_sha or "").strip().lower() != expected_model:
        errors.append("model sha256 does not match the current evaluation transaction")
    _, calculation_errors = _evaluation_calculation_receipt(message, transaction)
    errors.extend(calculation_errors)
    return errors


def _correct_current_evaluation(
    state: dict[str, Any],
    *,
    repo: Path,
    skill_root: str | Path,
    controller_id: str,
    reason: str,
) -> dict[str, Any]:
    previous = state.get("evaluation_transaction")
    if not isinstance(previous, dict):
        return state
    corrected = correct_evaluation(
        previous,
        reason=reason,
        evidence_provider=lambda: _scoring_evidence_snapshot(
            repo,
            skill_root=skill_root,
            controller_id=controller_id,
        ),
    )
    state["evaluation_transaction"] = corrected
    state["evaluation_correction_required"] = True
    state["retry_after_block"] = True
    return state


def evaluate_event(
    event: dict[str, Any],
    *,
    skill_root: str | Path,
    prior_state: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = dict(prior_state or {})
    event_name = str(event.get("hook_event_name", ""))

    if event_name == "UserPromptSubmit":
        prompt = str(event.get("prompt", ""))
        if not is_controller_scoring_request(prompt):
            current_turn = str(event.get("turn_id", ""))
            if state.get("pending_scoring") and str(state.get("turn_id", "")) != current_turn:
                state.update({
                    "pending_scoring": False,
                    "reinject_required": False,
                    "turn_id": current_turn,
                })
            return {}, state

        try:
            repo = _repo_root(str(event.get("cwd", "") or Path.cwd()))
            source_session_id = str(event.get("session_id", "")).strip()
            controller_id = _logical_controller_id(repo, source_session_id)
            evaluation_intent = classify_evaluation_intent(prompt)

            if evaluation_intent == READ:
                latest = latest_score_history(
                    repo,
                    controller_session_id=controller_id,
                )
                read_context = (
                    "Adaptive Agent Runtime scoring query intent=READ."
                    + chr(10)
                    + "This request asks for an existing machine record, not a new evaluation."
                    + chr(10)
                    + "recorded_result_provenance=READ"
                    + chr(10)
                    + "latest_record="
                    + (
                        json.dumps(
                            latest,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if isinstance(latest, dict)
                        else "UNKNOWN"
                    )
                )
                state.update({
                    "evaluation_intent": READ,
                    "pending_scoring": False,
                    "pending_scoring_read": True,
                    "repo_root": str(repo),
                    "controller_id": controller_id,
                    "source_session_id": source_session_id,
                    "turn_id": str(event.get("turn_id", "")),
                    "retry_after_block": False,
                })
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": read_context,
                    }
                }, state

            prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            receipt_id = ":".join((
                source_session_id,
                str(event.get("turn_id", "")),
                prompt_sha256,
            ))
            model = scoring_model_path(skill_root)
            content, receipt = read_and_record_model(
                repo,
                skill_root=skill_root,
                controller_session_id=controller_id,
                receipt_id=receipt_id,
            )
            digest = str(receipt["model_sha256"])
            context = (
                "Adaptive Agent Runtime controller-scoring machine gate is active. "
                "The following is the exact installed scoring model and is authoritative for this scoring turn. "
                "Do not substitute another rubric. The Stop gate will fail closed if this exact model changes before the response completes."
                + chr(10)
                + f"installed_scoring_model_sha256={digest}"
                + chr(10)
                + f"installed_scoring_model_path={model}"
                + chr(10)
                + chr(10)
                + f"stable_logical_controller_id={controller_id}"
                + chr(10)
                + "machine_governance_risk_projection="
                + json.dumps(
                    receipt.get("governance_risk_projection", {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + chr(10)
                + chr(10)
                + content.decode("utf-8")
            )
            evaluation_transaction = None
            if evaluation_intent == COMPUTE:
                evaluation_transaction = begin_evaluation(
                    prompt=prompt,
                    subject={"kind": "controller", "id": controller_id},
                    model={
                        "state": "found",
                        "path": str(model),
                        "sha256": digest,
                    },
                    evidence_provider=lambda: _scoring_evidence_snapshot(
                        repo,
                        skill_root=skill_root,
                        controller_id=controller_id,
                    ),
                )
                context = (
                    _evaluation_context(evaluation_transaction)
                    + chr(10)
                    + chr(10)
                    + context
                )
        except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as error:
            return {
                "decision": "block",
                "reason": (
                    "controller scoring blocked: score-guard could not initialize "
                    f"the current scoring transaction: {error}"
                ),
            }, state

        state.update({
            "pending_scoring": True,
            "model_path": str(model),
            "model_sha256": digest,
            "prompt_sha256": prompt_sha256,
            "repo_root": str(repo),
            "controller_id": controller_id,
            "source_session_id": source_session_id,
            "receipt_id": receipt_id,
            "receipt_path": str(receipt_path(repo, receipt_id=receipt_id)),
            "receipt_sha256": str(receipt.get("model_sha256", "")),
            "reinject_required": False,
            "retry_after_block": False,
            "turn_id": str(event.get("turn_id", "")),
            "scoring_mode": _scoring_mode(prompt),
            "evaluation_intent": evaluation_intent,
            "evaluation_transaction": evaluation_transaction,
            "evaluation_correction_required": False,
            "pending_scoring_read": False,
        })
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }, state

    if event_name == "Stop":
        message = str(event.get("last_assistant_message", ""))
        current_turn = str(event.get("turn_id", ""))
        if state.get("pending_scoring") and str(state.get("turn_id", "")) != current_turn:
            if state.get("retry_after_block") and str(event.get("session_id", "")).strip() == str(state.get("source_session_id", "")).strip():
                state.update({"turn_id": current_turn, "retry_after_block": False})
            else:
                state.update({"pending_scoring": False, "reinject_required": False, "retry_after_block": False, "turn_id": current_turn})
        if looks_like_controller_score_output(message) and not state.get("pending_scoring"):
            return {
                "decision": "block",
                "reason": "controller scoring blocked: score-guard has no active exact-model read state; reload the installed scoring model before outputting a controller score.",
            }, state
        if not state.get("pending_scoring"):
            return {}, state
        try:
            installed_path = scoring_model_path(skill_root)
            installed_digest = scoring_model_sha256(skill_root)
        except OSError as error:
            return {
                "decision": "block",
                "reason": f"controller scoring blocked: installed scoring model is unavailable; 重新加载评分模型后再输出评分。{error}",
            }, state
        repo_text = str(state.get("repo_root", "")).strip()
        if not repo_text:
            return {"decision": "block", "reason": "controller scoring blocked: score-guard repo binding is missing."}, state
        if str(event.get("session_id", "")).strip() != str(state.get("source_session_id", "")).strip():
            return {"decision": "block", "reason": "controller scoring blocked: scoring turn source session changed."}, state
        controller_id = str(state.get("controller_id", "")).strip()
        if not controller_id:
            return {"decision": "block", "reason": "controller scoring blocked: stable logical Controller binding is missing."}, state
        if state.get("model_sha256") != installed_digest or Path(str(state.get("model_path", ""))).resolve() != installed_path:
            # Stop continuation text is size-limited by Codex. Do not pretend the new
            # rubric was fully injected here and do not advance the recorded digest.
            # Release this turn from the scoring state so the assistant can explain the
            # block without a Stop loop; any score-shaped output remains fail-closed.
            state["pending_scoring"] = False
            state["reinject_required"] = True
            return {
                "decision": "block",
                "reason": (
                    "controller scoring blocked: installed scoring model changed after prompt injection; "
                    "当前评分正文不再有效。本回合只能说明阻塞，不能输出分数；请用户重新提交总控评分/审计请求，让新的 UserPromptSubmit 完整注入当前安装评分模型。"
                ),
            }, state
        evaluation_transaction = state.get("evaluation_transaction")
        if (
            str(state.get("evaluation_intent", "")).upper() == COMPUTE
            and isinstance(evaluation_transaction, dict)
        ):
            try:
                current_evidence = _scoring_evidence_snapshot(
                    Path(repo_text),
                    skill_root=skill_root,
                    controller_id=controller_id,
                )
            except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as error:
                state["retry_after_block"] = True
                return {
                    "decision": "block",
                    "reason": (
                        "controller scoring blocked: current evaluation evidence "
                        f"could not be refreshed safely: {error}"
                    ),
                }, state
            current_evidence_sha = evidence_snapshot_sha256(current_evidence)
            if (
                current_evidence_sha
                != str(evaluation_transaction.get("evidence_snapshot_sha256", ""))
            ):
                state = _correct_current_evaluation(
                    state,
                    repo=Path(repo_text),
                    skill_root=skill_root,
                    controller_id=controller_id,
                    reason="current evidence changed before evaluation completion",
                )
                return {
                    "decision": "block",
                    "reason": (
                        "controller scoring blocked: current evidence changed during "
                        "the evaluation; TASK NOT SATISFIED. Recompute from the refreshed "
                        "evaluation transaction before finalizing a score."
                    ),
                }, state

            metadata_errors = _evaluation_metadata_errors(
                message,
                evaluation_transaction,
            )
            if metadata_errors:
                state = _correct_current_evaluation(
                    state,
                    repo=Path(repo_text),
                    skill_root=skill_root,
                    controller_id=controller_id,
                    reason="; ".join(metadata_errors),
                )
                return {
                    "decision": "block",
                    "reason": (
                        "controller scoring blocked: TASK NOT SATISFIED: "
                        + "; ".join(metadata_errors)
                    ),
                }, state

        scoring_mode = str(state.get("scoring_mode", "formal"))
        receipt_id = str(state.get("receipt_id", "")).strip() or None
        cycle_score = _extract_cycle_score_value(message)
        formal_score = _extract_score_value(message)
        if scoring_mode == "cycle" and (
            (cycle_score is None and formal_score is not None)
            or _has_distinct_formal_score_output(message)
        ):
            state["retry_after_block"] = True
            return {
                "decision": "block",
                "reason": "controller scoring blocked: score output mode mismatch; cycle diagnostic requested",
            }, state
        if scoring_mode == "formal" and cycle_score is not None:
            state["retry_after_block"] = True
            return {
                "decision": "block",
                "reason": "controller scoring blocked: score output mode mismatch; formal scoring requested",
            }, state
        try:
            if scoring_mode == "cycle":
                controller_session_id = controller_id
                if cycle_score is None and (_CYCLE_EXTREMA_OUTPUT.search(message) or "单回合最高分" in message or "单回合最低分" in message):
                    expected_extremes = cycle_score_extremes(
                        Path(repo_text),
                        controller_session_id=controller_session_id,
                        model_sha256=installed_digest,
                    )
                    _validate_cycle_extrema_claim(message, expected=expected_extremes)
                    errors = consume_score_guard(Path(repo_text), skill_root=skill_root, receipt_id=receipt_id)
                    if errors:
                        raise ValueError("score-guard failed: " + "; ".join(errors))
                elif cycle_score is not None:
                    cycle_id = _extract_labeled_value(message, (r"控制回合", r"cycle(?: id)?"))
                    terminal_status = _extract_labeled_value(message, (r"回合终态", r"terminal status"))
                    evidence_summary = _extract_labeled_value(message, (r"证据摘要", r"evidence summary"))
                    evidence_id = _extract_labeled_value(message, (r"证据收据", r"evidence receipt"))
                    finalizer = finalize_attested_cycle_score if evidence_id else finalize_cycle_candidate
                    finalizer_kwargs = {
                        "repo": Path(repo_text),
                        "skill_root": skill_root,
                        "controller_session_id": controller_session_id,
                        "turn_id": current_turn,
                        "cycle_id": cycle_id or "",
                        "terminal_status": terminal_status or "",
                        "score": cycle_score,
                        "evidence_summary": evidence_summary,
                        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                        "receipt_id": receipt_id,
                    }
                    if evidence_id:
                        finalizer_kwargs["evidence_id"] = evidence_id
                    finalizer(**finalizer_kwargs)
                else:
                    errors = consume_score_guard(Path(repo_text), skill_root=skill_root, receipt_id=receipt_id)
                    if errors:
                        raise ValueError("score-guard failed: " + "; ".join(errors))
            else:
                formal_claim_present = bool(
                    formal_score is not None
                    or _ANY_SCORE_SHAPE.search(message)
                    or _PERFORMANCE_SCORE_LABEL.search(message)
                    or _RISK_CONSTRAINED_SCORE_LABEL.search(message)
                    or _CYCLE_EXTREMA_OUTPUT.search(message)
                    or "治理风险状态" in message
                    or "治理风险依据" in message
                    or "单回合最高分" in message
                    or "单回合最低分" in message
                )
                if formal_claim_present:
                    performance_score, risk_status, risk_summary, constrained_score = _extract_three_layer_report(message)
                    expected_extremes = cycle_score_extremes(
                        Path(repo_text),
                        controller_session_id=controller_id,
                        model_sha256=installed_digest,
                    )
                    _validate_cycle_extrema_claim(message, expected=expected_extremes)
                    evaluation_for_history = evaluation_transaction
                    if (
                        isinstance(evaluation_transaction, dict)
                        and str(evaluation_transaction.get("intent", "")).upper() == COMPUTE
                    ):
                        calculation_receipt, calculation_errors = _evaluation_calculation_receipt(
                            message, evaluation_transaction
                        )
                        if calculation_errors or calculation_receipt is None:
                            raise ValueError(
                                "evaluation calculation receipt invalid: "
                                + "; ".join(calculation_errors)
                            )
                        performance_score = float(calculation_receipt["performance_score"])
                        evaluation_for_history = {
                            **evaluation_transaction,
                            "calculation_receipt": calculation_receipt,
                        }
                    finalized = finalize_score(
                        Path(repo_text), skill_root=skill_root,
                        controller_session_id=controller_id,
                        turn_id=current_turn, score=constrained_score, performance_score=performance_score,
                        governance_risk_status=risk_status, risk_summary=risk_summary,
                        window_summary=_extract_window_summary(message),
                        message_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
                        receipt_id=receipt_id,
                        evaluation_transaction=(
                            evaluation_for_history
                            if isinstance(evaluation_for_history, dict)
                            and str(evaluation_for_history.get("intent", "")).upper() == COMPUTE
                            else None
                        ),
                    )
                    if (
                        isinstance(evaluation_transaction, dict)
                        and str(evaluation_transaction.get("intent", "")).upper() == COMPUTE
                    ):
                        completed_evaluation = dict(evaluation_for_history)
                        completed_evaluation["state"] = "CLOSED"
                        completed_evaluation["result_provenance"] = COMPUTED
                        completed_evaluation["history_recorded_at"] = finalized.get("recorded_at")
                        state["completed_evaluation"] = completed_evaluation
                else:
                    errors = consume_score_guard(Path(repo_text), skill_root=skill_root, receipt_id=receipt_id)
                    if errors:
                        raise ValueError("score-guard failed: " + "; ".join(errors))
        except ValueError as error:
            state["retry_after_block"] = True
            return {
                "decision": "block",
                "reason": f"controller scoring blocked: score-guard validation failed safely: {error}",
            }, state
        except (OSError, subprocess.CalledProcessError) as error:
            state["pending_scoring"] = False
            state["reinject_required"] = True
            return {
                "decision": "block",
                "reason": f"controller scoring blocked: score finalization could not persist a valid history record; resubmit the scoring request. {error}",
            }, state
        state["pending_scoring"] = False
        state["retry_after_block"] = False
        return {}, state

    return {}, state


def install_hooks(
    config_path: str | Path,
    *,
    script_path: str | Path | None = None,
    python_executable: str | None = None,
) -> dict[str, Any]:
    path = Path(config_path).expanduser().resolve()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("hooks config root must be an object")
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    script = Path(script_path or __file__).resolve()
    python = python_executable or sys.executable
    command = f"{shlex.quote(str(python))} {shlex.quote(str(script))}"

    def group(*, inject_context: bool) -> dict[str, Any]:
        handler: dict[str, Any] = {
            "type": "command",
            "command": command,
            "timeout": 5,
            "statusMessage": "Enforcing Adaptive Agent Runtime controller scoring model",
        }
        if inject_context:
            # The scoring rubric is intentionally not spilled/truncated before model injection.
            handler["additionalContextLimit"] = 0
        return {"hooks": [handler]}

    for event_name, inject_context in (("UserPromptSubmit", True), ("Stop", False)):
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            raise ValueError(f"{event_name} hooks must be a list")
        entries[:] = [entry for entry in entries if "controller_scoring_hook.py" not in str(entry)]
        entries.append(group(inject_context=inject_context))

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return config


def run_hook() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(event, dict):
        return 0
    session_id = str(event.get("session_id", "")).strip()
    path = _state_path(session_id)
    prior_state = _read_state(path)
    output, state = evaluate_event(
        event,
        skill_root=Path(__file__).resolve().parents[1],
        prior_state=prior_state,
    )
    try:
        _write_state(path, state)
    except OSError as error:
        scoring_related = (
            bool(prior_state.get("pending_scoring") or prior_state.get("reinject_required"))
            or bool(state.get("pending_scoring") or state.get("reinject_required"))
            or is_controller_scoring_request(str(event.get("prompt", "")))
            or looks_like_controller_score_output(str(event.get("last_assistant_message", "")))
            or output.get("decision") == "block"
        )
        if scoring_related:
            print(json.dumps({
                "decision": "block",
                "reason": f"controller scoring blocked: scoring gate state could not persist safely: {error}",
            }, ensure_ascii=False))
        return 0
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--install-hooks":
        install_hooks(Path.home() / ".codex" / "hooks.json")
        print("controller scoring hooks installed: UserPromptSubmit + Stop")
        return 0
    return run_hook()


if __name__ == "__main__":
    raise SystemExit(main())
