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
    from controller_scoring_guard import consume_score_guard, cycle_score_extremes, finalize_cycle_score, finalize_score, latest_score_history, read_and_record_model, receipt_path
except ModuleNotFoundError:
    from scripts.controller_scoring_guard import consume_score_guard, cycle_score_extremes, finalize_cycle_score, finalize_score, latest_score_history, read_and_record_model, receipt_path

MODEL_RELATIVE_PATH = Path("references/controller-performance-scoring.md")
STATE_ROOT = Path(
    os.environ.get(
        "AD_SCORING_STATE_DIR",
        str(Path.home() / ".codex" / "state" / "adaptive-delivery-scoring"),
    )
).expanduser()

_CONTROLLER_TERMS = re.compile(r"(?:总控|项目总控|controller|orchestrator)", re.IGNORECASE)
_SCORE_VALUE = r"(?:100|[1-9]?\d)(?:\.\d+)?(?:\s*/\s*100|\s*分)"
_CYCLE_REQUEST = re.compile(r"(?:单回合|闭环回合|最佳闭环|最差闭环|single[ -]?cycle)", re.IGNORECASE)
_CYCLE_OUTPUT_SCORE = re.compile(r"(?:单回合诊断评分|单回合评分|single[ -]?cycle(?: diagnostic)? score).{0,20}?(?:100|[1-9]?\d)(?:\.\d+)?(?:\s*/\s*100|\s*分)", re.IGNORECASE | re.DOTALL)
_OUTPUT_SCORE = re.compile(
    rf"(?:总控|项目总控|controller|orchestrator).{{0,80}}?(?:评分|得分|score|performance).{{0,40}}?{_SCORE_VALUE}",
    re.IGNORECASE | re.DOTALL,
)
_FORMAL_SCORE_LABEL = re.compile(
    rf"(?:正式(?:总控)?(?:履职)?评分|履职评分|formal(?: controller)? score|controller performance score).{{0,20}}?{_SCORE_VALUE}",
    re.IGNORECASE | re.DOTALL,
)

_SCORE_TERMS = re.compile(
    r"(?:评分|打分|分数|多少分|履职评估|履职评分|performance\s+(?:score|scoring|evaluation)|score\s+(?:the\s+)?(?:controller|orchestrator)|rate\s+(?:the\s+)?(?:controller|orchestrator))",
    re.IGNORECASE,
)
_PERFORMANCE_WORKFLOW = re.compile(
    r"(?:审计.{0,16}(?:项目总控|总控).{0,16}(?:履职|表现)|(?:项目总控|总控).{0,16}(?:履职|表现).{0,16}审计|(?:比较|评估|评价).{0,16}(?:项目总控|总控).{0,16}(?:履职|表现)|(?:项目总控|总控).{0,16}(?:履职|表现).{0,16}(?:比较|评估|评价)|(?:检查|核对).{0,16}(?:项目总控|总控).{0,16}假繁荣|(?:audit|evaluate|assess|review|compare).{0,30}(?:controller|orchestrator).{0,30}(?:performance|duty|execution)|(?:controller|orchestrator).{0,30}(?:performance|duty|execution).{0,30}(?:audit|evaluate|assess|review|compare))",
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
    )


def looks_like_controller_score_output(message: str) -> bool:
    text = str(message or "")
    return bool(_OUTPUT_SCORE.search(text) or _FORMAL_SCORE_LABEL.search(text) or _CYCLE_OUTPUT_SCORE.search(text))


def _extract_score_value(message: str) -> float | None:
    text = str(message or "")
    match = _OUTPUT_SCORE.search(text) or _FORMAL_SCORE_LABEL.search(text)
    if not match:
        return None
    value = re.search(r"(?:100|[1-9]?\d)(?:\.\d+)?", match.group(0))
    return float(value.group(0)) if value else None


def _extract_cycle_score_value(message: str) -> float | None:
    match = _CYCLE_OUTPUT_SCORE.search(str(message or ""))
    if not match:
        return None
    value = re.search(r"(?:100|[1-9]?\d)(?:\.\d+)?", match.group(0))
    return float(value.group(0)) if value else None


def _has_distinct_formal_score_output(message: str) -> bool:
    text = str(message or "")
    cycle_spans = [match.span() for match in _CYCLE_OUTPUT_SCORE.finditer(text)]
    formal_matches = list(_OUTPUT_SCORE.finditer(text)) + list(_FORMAL_SCORE_LABEL.finditer(text))
    for formal in formal_matches:
        formal_start, formal_end = formal.span()
        if not any(max(formal_start, cycle_start) < min(formal_end, cycle_end) for cycle_start, cycle_end in cycle_spans):
            return True
    return False


def _extract_labeled_value(message: str, labels: tuple[str, ...]) -> str | None:
    for raw_line in str(message or "").splitlines():
        line = raw_line.strip()
        for label in labels:
            match = re.match(rf"{label}\s*[：:]\s*(.+)$", line, re.IGNORECASE)
            if match:
                return match.group(1).strip()[:500] or None
    return None


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
                state.update({"pending_scoring": False, "reinject_required": False, "turn_id": current_turn})
            return {}, state
        try:
            repo = _repo_root(str(event.get("cwd", "") or Path.cwd()))
            model = scoring_model_path(skill_root)
            content, receipt = read_and_record_model(repo, skill_root=skill_root)
            digest = str(receipt["model_sha256"])
            context = (
                "Adaptive Agent Runtime controller-scoring machine gate is active. "
                "The following is the exact installed scoring model and is authoritative for this scoring turn. "
                "Do not substitute another rubric. The Stop gate will fail closed if this exact model changes before the response completes.\n"
                f"installed_scoring_model_sha256={digest}\n"
                f"installed_scoring_model_path={model}\n\n"
                + content.decode("utf-8")
            )
        except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as error:
            return {
                "decision": "block",
                "reason": f"controller scoring blocked: score-guard could not load and record the exact installed scoring model read: {error}",
            }, state
        state.update(
            {
                "pending_scoring": True,
                "model_path": str(model),
                "model_sha256": digest,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "repo_root": str(repo),
                "receipt_path": str(receipt_path(repo)),
                "receipt_sha256": str(receipt.get("model_sha256", "")),
                "reinject_required": False,
                "turn_id": str(event.get("turn_id", "")),
                "scoring_mode": "cycle" if _CYCLE_REQUEST.search(prompt) else "formal",
            }
        )
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
            state.update({"pending_scoring": False, "reinject_required": False, "turn_id": current_turn})
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
        scoring_mode = str(state.get("scoring_mode", "formal"))
        cycle_score = _extract_cycle_score_value(message)
        formal_score = _extract_score_value(message)
        if scoring_mode == "cycle" and (
            (cycle_score is None and formal_score is not None)
            or _has_distinct_formal_score_output(message)
        ):
            return {
                "decision": "block",
                "reason": "controller scoring blocked: score output mode mismatch; cycle diagnostic requested",
            }, state
        if scoring_mode == "formal" and cycle_score is not None:
            return {
                "decision": "block",
                "reason": "controller scoring blocked: score output mode mismatch; formal scoring requested",
            }, state
        score = cycle_score if scoring_mode == "cycle" else formal_score
        try:
            if score is None:
                errors = consume_score_guard(Path(repo_text), skill_root=skill_root)
                if errors:
                    return {
                        "decision": "block",
                        "reason": "controller scoring blocked: score-guard failed: " + "; ".join(errors),
                    }, state
            elif scoring_mode == "cycle":
                cycle_id = _extract_labeled_value(message, (r"控制回合", r"cycle(?: id)?"))
                terminal_status = _extract_labeled_value(message, (r"回合终态", r"terminal status"))
                evidence_summary = _extract_labeled_value(message, (r"证据摘要", r"evidence summary"))
                finalize_cycle_score(
                    Path(repo_text), skill_root=skill_root,
                    controller_session_id=str(event.get("session_id", "")).strip(),
                    turn_id=current_turn, cycle_id=cycle_id or "", terminal_status=terminal_status or "",
                    score=score, evidence_summary=evidence_summary,
                    message_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
                )
            else:
                finalize_score(
                    Path(repo_text), skill_root=skill_root,
                    controller_session_id=str(event.get("session_id", "")).strip(),
                    turn_id=current_turn, score=score, window_summary=_extract_window_summary(message),
                    message_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
                )
        except ValueError as error:
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
