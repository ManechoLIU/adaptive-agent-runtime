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
    from controller_scoring_guard import record_model_read, receipt_path, score_guard_errors
except ModuleNotFoundError:
    from scripts.controller_scoring_guard import record_model_read, receipt_path, score_guard_errors

MODEL_RELATIVE_PATH = Path("references/controller-performance-scoring.md")
STATE_ROOT = Path(
    os.environ.get(
        "AD_SCORING_STATE_DIR",
        str(Path.home() / ".codex" / "state" / "adaptive-delivery-scoring"),
    )
).expanduser()

_CONTROLLER_TERMS = re.compile(r"(?:总控|项目总控|controller|orchestrator)", re.IGNORECASE)
_OUTPUT_SCORE = re.compile(r"(?:总控|项目总控|controller|orchestrator).{0,40}?(?:\b(?:100|[1-9]?\d)(?:\.\d+)?\s*/\s*100\b|(?:100|[1-9]?\d)(?:\.\d+)?\s*分)", re.IGNORECASE | re.DOTALL)

_SCORE_TERMS = re.compile(
    r"(?:评分|打分|分数|多少分|履职评估|履职评分|performance\s+(?:score|scoring|evaluation)|score\s+(?:the\s+)?(?:controller|orchestrator)|rate\s+(?:the\s+)?(?:controller|orchestrator))",
    re.IGNORECASE,
)


def scoring_model_path(skill_root: str | Path) -> Path:
    return (Path(skill_root).resolve() / MODEL_RELATIVE_PATH).resolve()


def scoring_model_sha256(skill_root: str | Path) -> str:
    return hashlib.sha256(scoring_model_path(skill_root).read_bytes()).hexdigest()


def is_controller_scoring_request(prompt: str) -> bool:
    text = str(prompt or "").strip()
    return bool(_CONTROLLER_TERMS.search(text) and _SCORE_TERMS.search(text))


def looks_like_controller_score_output(message: str) -> bool:
    return bool(_OUTPUT_SCORE.search(str(message or "")))


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
        "Adaptive Delivery controller-scoring machine gate is active. "
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
            return {}, state
        try:
            model = scoring_model_path(skill_root)
            digest = scoring_model_sha256(skill_root)
            context = _model_context(skill_root, digest)
        except OSError as error:
            return {
                "decision": "block",
                "reason": f"controller scoring blocked: installed scoring model cannot be loaded: {error}",
            }, state
        try:
            repo = _repo_root(str(event.get("cwd", "") or Path.cwd()))
            receipt = record_model_read(repo, skill_root=skill_root)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            return {
                "decision": "block",
                "reason": f"controller scoring blocked: score-guard could not record the exact installed scoring model read: {error}",
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
            context = _model_context(skill_root, installed_digest)
            receipt = record_model_read(Path(repo_text), skill_root=skill_root)
            state.update({"model_sha256": installed_digest, "model_path": str(installed_path), "receipt_sha256": receipt.get("model_sha256", "")})
            return {
                "decision": "block",
                "reason": (
                    "controller scoring blocked: installed scoring model changed after prompt injection; "
                    "已自动重新加载当前评分模型。必须按下面的新模型重新计算后再输出评分。\n\n" + context
                ),
            }, state
        errors = score_guard_errors(Path(repo_text), skill_root=skill_root)
        if errors:
            return {
                "decision": "block",
                "reason": "controller scoring blocked: score-guard failed: " + "; ".join(errors),
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
            "statusMessage": "Enforcing Adaptive Delivery controller scoring model",
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
    output, state = evaluate_event(
        event,
        skill_root=Path(__file__).resolve().parents[1],
        prior_state=_read_state(path),
    )
    _write_state(path, state)
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
