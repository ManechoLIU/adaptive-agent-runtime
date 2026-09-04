#!/usr/bin/env python3
"""Generic project-context / Fact-First lifecycle gate for Adaptive Agent Runtime."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

STATE_ROOT = Path(
    os.environ.get(
        "AD_PROJECT_CONTEXT_STATE_DIR",
        str(Path.home() / ".codex" / "state" / "adaptive-agent-runtime-project-context"),
    )
).expanduser()

PROJECT_FACT_REQUEST = re.compile(
    r"(?:"
    r"(?:当前|现有|这个|本|该).{0,8}(?:项目|治理|controller|runtime|规则|机制|模型|状态|进度|HEAD|分支|工作树|reviewer)"
    r"|(?:治理体系里的|治理体系中的|现有模型|项目规定的|当前规则|评分模型|项目规则)"
    r"|(?:TASK_LEDGER|worktree|branch|reviewer|controller|runtime).{0,20}(?:当前|状态|规则|进度|结果|评分)"
    r")",
    re.IGNORECASE,
)
EXISTING_MECHANISM_REQUEST = re.compile(
    r"(?:治理体系里的|治理体系中的|现有(?:的)?(?:模型|机制|规则|标准|合同)|项目规定的|当前(?:的)?(?:规则|模型|机制)|评分模型|existing[ 	]+(?:model|mechanism|rule)|current[ 	]+(?:rule|model|mechanism)|project[- ]defined)",
    re.IGNORECASE,
)
PROJECT_FACT_OUTPUT = re.compile(
    r"(?:"
    r"(?:当前|现在|本项目|这个项目|该项目).{0,30}(?:是|为|有|没有|处于|使用|采用|规则|状态|进度)"
    r"|(?:Controller|Runtime|HEAD|分支|工作树|Reviewer|TASK_LEDGER|评分模型).{0,30}(?:是|为|已|未|当前|状态|结果|PASS|FAIL|分)"
    r")",
    re.IGNORECASE,
)
UNKNOWN_MARKER = re.compile(
    r"(?:UNKNOWN|NOT[ 	]+FOUND|未找到|找不到|无法读取|未知)",
    re.IGNORECASE,
)
UNRESOLVED_MECHANISM_RESPONSE = re.compile(
    r"^\s*(?:UNKNOWN(?:\s*/\s*NOT[ \t]+FOUND)?|NOT[ \t]+FOUND|未找到|未知)"
    r"\s*(?:[:：]\s*(?:"
    r"(?:当前|现有|本项目|该项目|权威|事实源|项目事实源|当前权威事实源中|当前事实源中)"
    r".{0,80}(?:未找到|没有找到|找不到|无法读取|无法确认|缺少|不存在)"
    r".{0,80}"
    r"|(?:当前权威事实源中)?(?:未找到|没有找到|找不到|无法读取|无法确认|缺少|不存在)"
    r".{0,120}"
    r"))?[。.!！]?\s*$",
    re.IGNORECASE,
)

_GENERIC_MECHANISM_PREFIXES = (
    "治理体系里的", "治理体系中的", "治理体系现有", "治理体系当前",
    "项目规定的", "项目现有", "当前", "现有", "既有", "使用", "按照", "根据",
)
_MECHANISM_SUFFIXES = (
    "评分模型", "评分矩阵", "模型", "矩阵", "机制", "规则", "合同", "标准",
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _repo_root(cwd: str | Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(Path(cwd).expanduser().resolve()), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(completed.stdout.strip()).resolve()


def _git_common_dir(repo: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    )
    value = Path(completed.stdout.strip())
    return (repo / value).resolve() if not value.is_absolute() else value.resolve()


def _source(path: Path, *, required: bool = False, max_bytes: int = 256 * 1024) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=False)
    try:
        payload = resolved.read_bytes()
    except FileNotFoundError:
        return {
            "status": "missing_required" if required else "not_found",
            "path": str(resolved),
        }
    except OSError as error:
        return {
            "status": "unreadable",
            "path": str(resolved),
            "error": str(error),
        }
    if len(payload) > max_bytes:
        return {
            "status": "too_large",
            "path": str(resolved),
            "sha256": _sha256_bytes(payload),
            "bytes": len(payload),
        }
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "status": "unreadable",
            "path": str(resolved),
            "sha256": _sha256_bytes(payload),
            "error": "not valid UTF-8",
        }
    return {
        "status": "verified",
        "path": str(resolved),
        "sha256": _sha256_bytes(payload),
        "bytes": len(payload),
        "content": content,
    }


def _applicable_agents_source(root: Path, cwd: Path) -> dict[str, Any]:
    """Resolve the full AGENTS.md scope chain from repo root through current cwd."""
    root = root.resolve()
    cwd = cwd.resolve()
    try:
        relative = cwd.relative_to(root)
    except ValueError:
        relative = Path(".")
        cwd = root

    directories = [root]
    current = root
    for part in relative.parts:
        if part in {"", "."}:
            continue
        current = current / part
        directories.append(current)

    chain: list[dict[str, Any]] = []
    for directory in directories:
        item = _source(directory / "AGENTS.md", required=False)
        if item.get("status") == "verified":
            chain.append(item)

    if not chain:
        return {
            "status": "missing_required",
            "path": str((root / "AGENTS.md").resolve(strict=False)),
            "scope_chain": [],
        }

    identity = [
        {"path": item["path"], "sha256": item["sha256"]}
        for item in chain
    ]
    content = (chr(10) * 2).join(
        f"## scoped instructions: {item['path']}" + chr(10) + str(item.get("content", ""))
        for item in chain
    )
    return {
        "status": "verified",
        "path": chain[-1]["path"],
        "sha256": _sha256_bytes(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ),
        "bytes": sum(int(item.get("bytes", 0)) for item in chain),
        "content": content,
        "scope_chain": chain,
    }


def _git_facts(repo: Path) -> dict[str, Any]:
    def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            capture_output=True,
            text=True,
        )

    head_result = run("rev-parse", "--verify", "HEAD", check=False)
    head = head_result.stdout.strip() if head_result.returncode == 0 else "UNBORN"
    branch = run("branch", "--show-current").stdout.strip()
    status = run("status", "--porcelain=v1", "--untracked-files=all").stdout
    return {
        "status": "verified",
        "head": head,
        "branch": branch,
        "worktree_status": status,
        "worktree_status_sha256": _sha256_bytes(status.encode("utf-8")),
    }


def _runtime_state_source(repo: Path) -> dict[str, Any]:
    path = _git_common_dir(repo) / "adaptive-delivery" / "runtime-assignments.json"
    result = _source(path, required=False, max_bytes=512 * 1024)
    return result


def initialize_project_context(
    repo: str | Path,
    *,
    skill_root: str | Path,
) -> dict[str, Any]:
    working_directory = Path(repo).expanduser().resolve()
    root = _repo_root(working_directory)
    skill = Path(skill_root).expanduser().resolve()
    ledger = root / ("TASK_LEDGER.md" if (root / "TASK_LEDGER.md").is_file() else "PROJECT_STATUS.md")
    sources = {
        "agents": _applicable_agents_source(root, working_directory),
        "project_skill": _source(root / "SKILL.md", required=False),
        "runtime_skill": _source(skill / "SKILL.md", required=True),
        "ledger": _source(ledger, required=False),
        "runtime_state": _runtime_state_source(root),
        "git": _git_facts(root),
    }
    required_bad = [
        name for name in ("agents", "runtime_skill")
        if sources[name].get("status") != "verified"
    ]
    state = "initialized" if not required_bad else "incomplete"
    verified = [
        name for name, value in sources.items()
        if value.get("status") == "verified"
    ]
    unknown = [
        name for name, value in sources.items()
        if value.get("status") != "verified"
    ]
    return {
        "schema_version": 1,
        "state": state,
        "project_root": str(root),
        "working_directory": str(working_directory),
        "runtime_skill_root": str(skill),
        "sources": sources,
        "verified_facts": verified,
        "unknown_facts": unknown,
        "required_failures": required_bad,
    }


def _receipt_source_changed(receipt: dict[str, Any]) -> bool:
    if receipt.get("state") != "initialized":
        return True
    sources = receipt.get("sources")
    if not isinstance(sources, dict):
        return True
    try:
        root = Path(str(receipt.get("project_root") or "")).resolve()
        cwd = Path(str(receipt.get("working_directory") or root)).resolve()
    except (OSError, ValueError):
        return True

    for name in ("agents", "project_skill", "runtime_skill", "ledger", "runtime_state"):
        prior = sources.get(name)
        if not isinstance(prior, dict):
            return True
        try:
            if name == "agents":
                current = _applicable_agents_source(root, cwd)
            elif name == "runtime_state":
                current = _runtime_state_source(root)
            else:
                raw_path = str(prior.get("path") or "").strip()
                if not raw_path:
                    return True
                current = _source(
                    Path(raw_path),
                    required=(name == "runtime_skill"),
                    max_bytes=(512 * 1024 if name == "runtime_state" else 256 * 1024),
                )
        except (OSError, ValueError, subprocess.CalledProcessError):
            return True

        prior_status = str(prior.get("status") or "")
        current_status = str(current.get("status") or "")
        if current_status != prior_status:
            return True
        if current_status == "verified":
            if str(current.get("sha256") or "") != str(prior.get("sha256") or ""):
                return True
        elif current_status == "too_large":
            if str(current.get("sha256") or "") != str(prior.get("sha256") or ""):
                return True
        elif current_status == "unreadable":
            if str(current.get("error") or "") != str(prior.get("error") or ""):
                return True

    try:
        current_git = _git_facts(root)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return True
    prior_git = sources.get("git")
    if not isinstance(prior_git, dict):
        return True
    return any(
        str(current_git.get(field, "")) != str(prior_git.get(field, ""))
        for field in ("head", "branch", "worktree_status_sha256")
    )


def _bounded(content: str, limit: int = 96 * 1024) -> str:
    if len(content) <= limit:
        return content
    return content[:limit] + chr(10) + "[TRUNCATED_BY_PROJECT_CONTEXT_GUARD]"


def _context_text(receipt: dict[str, Any], *, mechanism: dict[str, Any] | None = None) -> str:
    lines = [
        "Adaptive Agent Runtime project-context Fact-First gate is active.",
        "Current project/runtime facts below are authoritative for this turn; chat history, compact summaries, memory, and prior-session impressions are non-authoritative when they conflict.",
        f"project_context_state={receipt.get('state')}",
        f"project_root={receipt.get('project_root')}",
        "verified_facts=" + json.dumps(receipt.get("verified_facts", []), ensure_ascii=False),
        "unknown_facts=" + json.dumps(receipt.get("unknown_facts", []), ensure_ascii=False),
    ]
    sources = receipt.get("sources", {})
    if isinstance(sources, dict):
        for key in ("agents", "project_skill", "runtime_skill", "ledger"):
            source = sources.get(key)
            if not isinstance(source, dict):
                continue
            lines.append(
                f"source[{key}] status={source.get('status')} path={source.get('path', '')} sha256={source.get('sha256', '')}"
            )
            if source.get("status") == "verified":
                lines.append(f"--- {key} ---")
                lines.append(_bounded(str(source.get("content", ""))))
        git = sources.get("git")
        if isinstance(git, dict) and git.get("status") == "verified":
            lines.append(
                "git_facts="
                + json.dumps(
                    {
                        "head": git.get("head"),
                        "branch": git.get("branch"),
                        "worktree_status": git.get("worktree_status"),
                        "worktree_status_sha256": git.get("worktree_status_sha256"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        runtime = sources.get("runtime_state")
        if isinstance(runtime, dict):
            lines.append(
                f"runtime_state status={runtime.get('status')} path={runtime.get('path', '')} sha256={runtime.get('sha256', '')}"
            )
    if isinstance(mechanism, dict):
        lines.append("mechanism_resolution=" + json.dumps(
            {key: value for key, value in mechanism.items() if key != "content"},
            ensure_ascii=False,
            sort_keys=True,
        ))
        if mechanism.get("state") == "found":
            lines.append("--- resolved existing mechanism ---")
            lines.append(_bounded(str(mechanism.get("content", ""))))
        elif mechanism.get("state") == "not_found":
            lines.append(
                "Existing-mechanism request could not be resolved from current authoritative sources. "
                "Output must explicitly say UNKNOWN / NOT FOUND and must not synthesize an approximate mechanism."
            )
    return chr(10).join(lines)


def _linked_markdown_documents(receipt: dict[str, Any]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    sources = receipt.get("sources")
    if not isinstance(sources, dict):
        return result
    for key in ("agents", "project_skill", "runtime_skill"):
        source = sources.get(key)
        if not isinstance(source, dict) or source.get("status") != "verified":
            continue
        source_path = Path(str(source.get("path", ""))).resolve()
        text = str(source.get("content", ""))
        cursor = 0
        while True:
            marker = text.find("](", cursor)
            if marker < 0:
                break
            close = text.find(")", marker + 2)
            if close < 0:
                break
            raw = text[marker + 2 : close].split("#", 1)[0].strip()
            cursor = close + 1
            if not raw.lower().endswith(".md"):
                continue
            candidate = (source_path.parent / raw).resolve(strict=False)
            if candidate in seen or not candidate.is_file():
                continue
            seen.add(candidate)
            result.append(candidate)
    return result


def _mechanism_phrase(prompt: str) -> str:
    text = "".join(str(prompt or "").split())
    candidates: list[tuple[int, int, str]] = []
    delimiters = "，。！？；：,:;!?()（）[]【】<>《》"
    for suffix in _MECHANISM_SUFFIXES:
        search_from = 0
        while True:
            position = text.find(suffix, search_from)
            if position < 0:
                break
            left = max(0, position - 48)
            clause = text[left : position + len(suffix)]
            cut = -1
            for index, character in enumerate(clause):
                if character in delimiters:
                    cut = index
            if cut >= 0:
                clause = clause[cut + 1:]
            candidate = clause
            matched_prefix = ""
            matched_at = -1
            for prefix in _GENERIC_MECHANISM_PREFIXES:
                marker = candidate.rfind(prefix)
                if marker > matched_at:
                    matched_at = marker
                    matched_prefix = prefix
            priority = 0
            if matched_prefix:
                candidate = candidate[matched_at + len(matched_prefix):]
                priority = 100
            candidate = candidate.strip()
            if candidate:
                candidates.append((priority, position, candidate))
            search_from = position + len(suffix)
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _distinctive_mechanism_stem(phrase: str) -> str:
    value = phrase
    for suffix in _MECHANISM_SUFFIXES:
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    value = re.sub(r"^(?:现有|当前|治理体系|项目|正式|既有)+", "", value)
    return value.strip()


def resolve_existing_mechanism(
    prompt: str,
    *,
    receipt: dict[str, Any],
    skill_root: str | Path,
) -> dict[str, Any]:
    if not EXISTING_MECHANISM_REQUEST.search(str(prompt or "")):
        return {"state": "not_requested"}

    phrase = _mechanism_phrase(prompt)
    stem = _distinctive_mechanism_stem(phrase)
    scoring = bool(re.search(r"(?:评分|score|scoring)", str(prompt or ""), re.IGNORECASE))

    candidates = _linked_markdown_documents(receipt)
    runtime_scoring = Path(skill_root).resolve() / "references" / "controller-performance-scoring.md"
    if scoring and runtime_scoring.is_file() and runtime_scoring not in candidates:
        candidates.append(runtime_scoring)

    ranked: list[tuple[int, Path, bytes]] = []
    project_root = Path(str(receipt.get("project_root", ""))).resolve()
    for path in candidates:
        try:
            payload = path.read_bytes()
            text = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        normalized = (path.name + chr(10) + text).casefold()
        score = 0
        try:
            path.relative_to(project_root)
            score += 50
        except ValueError:
            pass
        if stem:
            if stem.casefold() not in normalized:
                continue
            score += 100
        elif scoring:
            if not (
                ("评分" in normalized or "score" in normalized or "scoring" in normalized)
                and ("模型" in normalized or "model" in normalized)
            ):
                continue
            score += 40
        elif phrase:
            if phrase.casefold() not in normalized:
                continue
            score += 60
        else:
            continue
        ranked.append((score, path, payload))

    if not ranked:
        return {
            "state": "not_found",
            "query": phrase or str(prompt or "")[:160],
            "searched_paths": [str(path) for path in candidates],
        }
    ranked.sort(key=lambda item: (-item[0], str(item[1])))
    top_score = ranked[0][0]
    top = [item for item in ranked if item[0] == top_score]
    if len(top) != 1:
        return {
            "state": "not_found",
            "query": phrase or str(prompt or "")[:160],
            "reason": "ambiguous_current_definition",
            "searched_paths": [str(item[1]) for item in top],
        }
    _, path, payload = top[0]
    return {
        "state": "found",
        "query": phrase or str(prompt or "")[:160],
        "path": str(path.resolve()),
        "sha256": _sha256_bytes(payload),
        "content": payload.decode("utf-8"),
    }


def _mechanism_still_current(mechanism: dict[str, Any]) -> bool:
    if mechanism.get("state") != "found":
        return True
    try:
        current = _sha256_bytes(Path(str(mechanism["path"])).read_bytes())
    except OSError:
        return False
    return current == str(mechanism.get("sha256", ""))


def is_project_fact_request(prompt: str) -> bool:
    return bool(PROJECT_FACT_REQUEST.search(str(prompt or "")) or EXISTING_MECHANISM_REQUEST.search(str(prompt or "")))


def unresolved_mechanism_response_is_safe(message: str) -> bool:
    """Allow only an uncertainty-only answer when current mechanism resolution is not_found."""
    text = str(message or "").strip()
    if not text or not UNKNOWN_MARKER.search(text):
        return False
    if re.search(r"\d", text):
        return False
    if re.search(r"\b(?:provider|model|auth_mode)\s*=", text, re.IGNORECASE):
        return False
    return bool(UNRESOLVED_MECHANISM_RESPONSE.fullmatch(text))


def looks_like_project_fact_output(message: str) -> bool:
    text = str(message or "")
    return bool(PROJECT_FACT_OUTPUT.search(text) and not UNKNOWN_MARKER.search(text))


def _refresh_for_correction(
    event: dict[str, Any],
    *,
    skill_root: str | Path,
    state: dict[str, Any],
    reason: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    working_directory = Path(
        str(event.get("cwd", "") or Path.cwd())
    ).expanduser().resolve()
    receipt = initialize_project_context(working_directory, skill_root=skill_root)
    prompt = str(state.get("prompt", ""))
    mechanism = resolve_existing_mechanism(prompt, receipt=receipt, skill_root=skill_root)
    state.update(
        {
            "project_context_receipt": receipt,
            "mechanism_resolution": mechanism,
            "correction_required": True,
            "pending_project_fact_turn": True,
        }
    )
    correction = (
        "项目事实门禁检测到未经验证或已过期的确定性结论；必须在当前回合自动纠偏："
        "撤销未经验证结论 → 以以下当前事实重新判断 → 再回答。"
        + chr(10)
        + "correction_reason="
        + reason
        + chr(10)
        + _context_text(receipt, mechanism=mechanism)
    )
    return {"decision": "block", "reason": correction}, state


def evaluate_event(
    event: dict[str, Any],
    *,
    skill_root: str | Path,
    prior_state: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = dict(prior_state or {})
    event_name = str(event.get("hook_event_name", ""))
    current_turn = str(event.get("turn_id", ""))

    if event_name == "SessionStart":
        try:
            receipt = initialize_project_context(
                str(event.get("cwd", "") or Path.cwd()),
                skill_root=skill_root,
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            receipt = {
                "schema_version": 1,
                "state": "not_project_or_unavailable",
                "project_root": str(event.get("cwd", "") or Path.cwd()),
                "sources": {},
                "verified_facts": [],
                "unknown_facts": ["project_root", "AGENTS.md", "SKILL.md"],
                "error": str(error),
            }
        state.update(
            {
                "project_context_receipt": receipt,
                "pending_project_fact_turn": False,
                "correction_required": False,
                "mechanism_resolution": {"state": "not_requested"},
                "turn_id": current_turn,
                "session_source": str(event.get("source", "")),
            }
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _context_text(receipt),
            }
        }, state

    if event_name == "UserPromptSubmit":
        prompt = str(event.get("prompt", ""))
        if not is_project_fact_request(prompt):
            if state.get("pending_project_fact_turn") and state.get("turn_id") != current_turn:
                state.update(
                    {
                        "pending_project_fact_turn": False,
                        "correction_required": False,
                        "mechanism_resolution": {"state": "not_requested"},
                    }
                )
            state["turn_id"] = current_turn
            return {}, state
        try:
            receipt = initialize_project_context(
                str(event.get("cwd", "") or Path.cwd()),
                skill_root=skill_root,
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            return {
                "decision": "block",
                "reason": (
                    "project-context gate blocked: current project root/rules could not be initialized; "
                    f"project-level deterministic claims are forbidden until this is resolved: {error}"
                ),
            }, state
        mechanism = resolve_existing_mechanism(prompt, receipt=receipt, skill_root=skill_root)
        state.update(
            {
                "project_context_receipt": receipt,
                "pending_project_fact_turn": True,
                "correction_required": False,
                "mechanism_resolution": mechanism,
                "prompt": prompt,
                "turn_id": current_turn,
                "controller_identity_required": False,
            }
        )
        if receipt.get("state") != "initialized":
            return {
                "decision": "block",
                "reason": (
                    "project-context gate blocked: required current project rules are incomplete. "
                    "Return UNKNOWN for unresolved facts; do not infer from history or memory."
                    + chr(10)
                    + _context_text(receipt, mechanism=mechanism)
                ),
            }, state
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _context_text(receipt, mechanism=mechanism),
            }
        }, state

    if event_name == "Stop":
        message = str(event.get("last_assistant_message", ""))
        receipt = state.get("project_context_receipt")
        if looks_like_project_fact_output(message) and not isinstance(receipt, dict):
            try:
                return _refresh_for_correction(
                    event,
                    skill_root=skill_root,
                    state=state,
                    reason="no_active_project_context_receipt",
                )
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                return {
                    "decision": "block",
                    "reason": (
                        "项目事实门禁阻止未经验证的确定性结论；无法初始化当前事实源，"
                        f"只能回答 UNKNOWN / NOT FOUND：{error}"
                    ),
                }, state

        if not state.get("pending_project_fact_turn"):
            return {}, state

        if not isinstance(receipt, dict):
            return _refresh_for_correction(
                event,
                skill_root=skill_root,
                state=state,
                reason="missing_project_context_receipt",
            )

        if _receipt_source_changed(receipt):
            return _refresh_for_correction(
                event,
                skill_root=skill_root,
                state=state,
                reason="authoritative_project_source_changed_before_stop",
            )

        mechanism = state.get("mechanism_resolution")
        if not isinstance(mechanism, dict):
            mechanism = {"state": "not_requested"}
        if not _mechanism_still_current(mechanism):
            return _refresh_for_correction(
                event,
                skill_root=skill_root,
                state=state,
                reason="resolved_mechanism_changed_before_stop",
            )
        if mechanism.get("state") == "not_found" and not unresolved_mechanism_response_is_safe(message):
            state["correction_required"] = True
            return {
                "decision": "block",
                "reason": (
                    "Existing project mechanism is UNKNOWN / NOT FOUND in current authoritative sources. "
                    "The final answer must be uncertainty-only: start with UNKNOWN / NOT FOUND (or 未找到/未知) "
                    "and state only that the current authoritative sources do not establish the mechanism. "
                    "Do not append scores, dimensions, replacement rules, provider/model declarations, or other definitive claims."
                    + chr(10)
                    + _context_text(receipt, mechanism=mechanism)
                ),
            }, state

        state["pending_project_fact_turn"] = False
        state["correction_required"] = False
        return {}, state

    return {}, state


def _state_path(session_id: str) -> Path:
    safe = "".join(character for character in str(session_id) if character.isalnum() or character in "-_")
    return STATE_ROOT / f"{safe or 'unknown'}.json"


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write(chr(10))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
    specs = {
        "SessionStart": ("startup|resume|clear|compact", True),
        "UserPromptSubmit": (None, True),
        "Stop": (None, False),
    }
    for event_name, (matcher, inject_context) in specs.items():
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            raise ValueError(f"{event_name} hooks must be a list")
        kept: list[Any] = []
        for entry in entries:
            if "project_context_guard.py" not in str(entry):
                kept.append(entry)
        entries[:] = kept
        handler: dict[str, Any] = {
            "type": "command",
            "command": command,
            "timeout": 5,
            "statusMessage": "Loading Adaptive Agent Runtime current project facts",
        }
        if inject_context:
            handler["additionalContextLimit"] = 0
        group: dict[str, Any] = {"hooks": [handler]}
        if matcher is not None:
            group["matcher"] = matcher
        entries.append(group)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + chr(10),
        encoding="utf-8",
    )
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
    prior = _read_state(path)
    output, state = evaluate_event(
        event,
        skill_root=Path(__file__).resolve().parents[1],
        prior_state=prior,
    )
    try:
        _write_state(path, state)
    except OSError as error:
        relevant = (
            is_project_fact_request(str(event.get("prompt", "")))
            or looks_like_project_fact_output(str(event.get("last_assistant_message", "")))
            or bool(prior.get("pending_project_fact_turn"))
            or bool(state.get("pending_project_fact_turn"))
        )
        if relevant:
            print(json.dumps({
                "decision": "block",
                "reason": f"project-context gate state could not persist safely; project claims are blocked: {error}",
            }, ensure_ascii=False))
        return 0
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--install-hooks":
        install_hooks(Path.home() / ".codex" / "hooks.json")
        print("project-context hooks installed: SessionStart + UserPromptSubmit + Stop")
        return 0
    return run_hook()


if __name__ == "__main__":
    raise SystemExit(main())
