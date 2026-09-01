#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    from controller_health import decide_controller_wake, derive_controller_health
except ModuleNotFoundError:
    from scripts.controller_health import decide_controller_wake, derive_controller_health


DEFAULT_REGISTRY = Path.home() / ".codex" / "adaptive-delivery-controllers.json"
DEFAULT_AUDIT_LOG = (
    Path.home()
    / "Library"
    / "Application Support"
    / "ai.originone.gpt-bridge"
    / "audit"
    / "activity.redacted.jsonl"
)
AI_BRIDGE_EXECUTABLE = "/Applications/AI-Bridge.app/Contents/MacOS/ai-bridge"
DEFAULT_RUNTIME_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
STDERR_TAIL_LIMIT = 8192
LAUNCHER_LOG_LIMIT = 262144
RESTORE_STATIC_DOCUMENT_NAMES = ("AGENTS.md", "MEMORY.md", "WIKI_INDEX.md")
AUTHORITATIVE_DOCUMENT_NAMES = ("SKILL.md", "SPEC.md", "DESIGN.md", "TECHNICAL.md", "EVOLUTION.md")
RESTORE_DOCUMENT_LIMIT = 32768


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def canonical_root(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    completed = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip()).resolve()
    return candidate


def registered_controller_for_repo(repo: Path, registry_path: Path) -> str | None:
    repo = repo.resolve()
    registry = load_json(registry_path)
    matches: set[str] = set()
    for session_id, value in registry.items():
        if session_id == "__controller_surfaces__" or not isinstance(session_id, str):
            continue
        if isinstance(value, str) and Path(value).expanduser().resolve() == repo:
            matches.add(session_id)
    surfaces = registry.get("__controller_surfaces__")
    if isinstance(surfaces, dict):
        for session_id, value in surfaces.items():
            if isinstance(session_id, str) and isinstance(value, str) and Path(value).expanduser().resolve() == repo:
                matches.add(session_id)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(f"expected exactly one registered controller for {repo}, found {len(matches)}")
    return next(iter(matches))


def registered_controller_session(
    *, controller_id: str, session_id: str, host: str, registry_path: Path
) -> bool:
    registry = load_json(registry_path)
    sessions = registry.get("__controller_sessions__")
    if not isinstance(sessions, dict):
        return False
    owners: set[str] = set()
    for candidate_controller, controller_sessions in sessions.items():
        if not isinstance(candidate_controller, str) or not isinstance(controller_sessions, dict):
            continue
        host_sessions = controller_sessions.get(host)
        if isinstance(host_sessions, str):
            host_sessions = [host_sessions]
        if not isinstance(host_sessions, list):
            continue
        if session_id in {value for value in host_sessions if isinstance(value, str)}:
            owners.add(candidate_controller)
    return owners == {controller_id}


def bind_web_session_to_controller(
    *, repo: Path, controller_id: str, web_session_id: str, registry_path: Path
) -> dict[str, Any]:
    controller_id = controller_id.strip()
    web_session_id = web_session_id.strip()
    if not controller_id or not web_session_id:
        raise ValueError("controller-id and web-session-id are required")
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry_path.with_suffix(registry_path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registered = registered_controller_for_repo(repo, registry_path)
            if registered != controller_id:
                raise PermissionError("Web session binding requires the registered Controller for this repository")
            registry = load_json(registry_path)
            sessions = registry.get("__controller_sessions__")
            if not isinstance(sessions, dict):
                sessions = {}
            for candidate_controller, controller_sessions in sessions.items():
                if candidate_controller == controller_id or not isinstance(controller_sessions, dict):
                    continue
                host_sessions = controller_sessions.get("web")
                if isinstance(host_sessions, str):
                    host_sessions = [host_sessions]
                if isinstance(host_sessions, list) and web_session_id in host_sessions:
                    raise PermissionError("Web Controller Session is already bound to another Controller")
            controller_sessions = sessions.get(controller_id)
            if not isinstance(controller_sessions, dict):
                controller_sessions = {}
            web_sessions = controller_sessions.get("web")
            if isinstance(web_sessions, str):
                web_sessions = [web_sessions]
            if not isinstance(web_sessions, list):
                web_sessions = []
            normalized = [value for value in web_sessions if isinstance(value, str) and value.strip()]
            if web_session_id not in normalized:
                normalized.append(web_session_id)
            controller_sessions["web"] = normalized
            sessions[controller_id] = controller_sessions
            registry["__controller_sessions__"] = sessions
            _write_json_atomic_file(registry_path, registry)
            return registry
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def require_web_controller_session(
    *, controller_id: str, web_session_id: str | None, registry_path: Path
) -> str:
    value = str(web_session_id or "").strip()
    if not value or not registered_controller_session(
        controller_id=controller_id, session_id=value, host="web", registry_path=registry_path
    ):
        raise PermissionError(
            "verified Web Controller Session identity required; refusing repository-only Controller attribution"
        )
    return value


def post_tool_event(
    *,
    session_id: str,
    repo: Path,
    command: str,
    exit_code: int | None = None,
    output: str = "",
    turn_id: str = "web-ai-bridge",
    web_session_id: str | None = None,
    execution_host: str = "web",
) -> dict[str, Any]:
    response: dict[str, Any] = {"output": output}
    if exit_code is not None:
        response["exit_code"] = exit_code
    return {
        "hook_event_name": "PostToolUse",
        "controller_host": execution_host,
        "execution_host": execution_host,
        "event_source": "web",
        "controller_id": session_id,
        "controller_session_id": session_id,
        "web_session_id": web_session_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": str(repo),
        "tool_name": "AI-Bridge.shell_command",
        "tool_input": {"command": command},
        "tool_response": response,
    }


def extract_command(receipt: dict[str, Any]) -> str:
    detail = receipt.get("detail")
    if isinstance(detail, str) and detail.startswith("命令："):
        body = detail.removeprefix("命令：")
        for marker in (" · 工作目录：", "\n\n命令输出："):
            if marker in body:
                body = body.split(marker, 1)[0]
        if body.strip():
            return body.strip()
    target = receipt.get("targetLabel")
    return target.strip() if isinstance(target, str) else ""


def translate_receipt(
    receipt: dict[str, Any], *, session_id: str, repo: Path, web_session_id: str | None = None
) -> dict[str, Any] | None:
    if receipt.get("childTool") != "shell_command":
        return None
    if receipt.get("state") not in {"succeeded", "failed"}:
        return None
    root_label = receipt.get("rootLabel")
    if not isinstance(root_label, str) or not root_label.strip():
        return None
    if Path(root_label).expanduser().resolve() != repo:
        return None
    command = extract_command(receipt)
    if not command:
        return None
    detail = receipt.get("detail")
    output = detail if isinstance(detail, str) else ""
    receipt_id = str(receipt.get("receiptId") or "web-audit")
    return post_tool_event(
        session_id=session_id,
        repo=repo,
        command=command,
        output=output,
        turn_id=f"web-audit:{receipt_id}",
        web_session_id=web_session_id,
        execution_host="web",
    )



def _git_common_dir(repo: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    )
    value = Path(completed.stdout.strip())
    return (repo / value).resolve() if not value.is_absolute() else value.resolve()


def _restore_document(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8")
    truncated = len(content.encode("utf-8")) > RESTORE_DOCUMENT_LIMIT
    if truncated:
        encoded = content.encode("utf-8")[:RESTORE_DOCUMENT_LIMIT]
        content = encoded.decode("utf-8", errors="ignore")
    return {"name": path.name, "path": str(path.resolve()), "content": content, "truncated": truncated}


def _bounded_text(value: str, limit: int = RESTORE_DOCUMENT_LIMIT) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _bounded_runtime_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False, "path": str(path), "content": "", "truncated": False}
    content, truncated = _bounded_text(path.read_text(encoding="utf-8"))
    return {
        "present": True,
        "path": str(path),
        "content": content,
        "truncated": truncated,
        "sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
    }


def web_session_restore_payload(repo: Path, registry_path: Path) -> dict[str, Any]:
    root = canonical_root(repo)
    controller = registered_controller_for_repo(root, registry_path)
    if controller is None:
        raise ValueError(f"no registered controller for {root}")
    ledger_name = "TASK_LEDGER.md" if (root / "TASK_LEDGER.md").is_file() else ("PROJECT_STATUS.md" if (root / "PROJECT_STATUS.md").is_file() else "TASK_LEDGER.md")
    restore_names = ("AGENTS.md", ledger_name, "MEMORY.md", "WIKI_INDEX.md")
    documents = [item for name in restore_names if (item := _restore_document(root / name)) is not None]
    authoritative_documents = [
        item for name in AUTHORITATIVE_DOCUMENT_NAMES
        if (item := _restore_document(root / name)) is not None
    ]
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True, capture_output=True, text=True,
    ).stdout
    status_content, status_truncated = _bounded_text(status)
    branch = subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"], check=True, capture_output=True, text=True
    ).stdout.strip()
    runtime_state = _git_common_dir(root) / "adaptive-delivery" / "runtime-assignments.json"
    runtime = _bounded_runtime_state(runtime_state)
    return {
        "product": "Adaptive Agent Runtime",
        "project_root": str(root),
        "controller_id": controller,
        "restore_order": ["AGENTS.md", ledger_name, "MEMORY.md", "WIKI_INDEX.md", "git_runtime"],
        "documents": documents,
        "authoritative_documents": authoritative_documents,
        "git": {
            "head": head,
            "branch": branch,
            "status": status_content,
            "status_truncated": status_truncated,
            "status_sha256": __import__("hashlib").sha256(status.encode("utf-8")).hexdigest(),
        },
        "runtime": runtime,
        "runtime_state_path": str(runtime_state),
        "compact": "not restored unless an explicit handoff/compact is available",
    }


def classify_native_resume_failure(returncode: int, stdout: str, stderr: str) -> dict[str, Any]:
    combined = f"{stdout}\n{stderr}".casefold()
    if "already has an active writer" in combined:
        return {
            "state": "RESUME_DEFERRED_ACTIVE_WRITER",
            "pending_control_event": True,
            "failure_class": "active_writer_present",
            "fallback_eligible": False,
            "error_code": "WEB_LIFECYCLE_ACTIVE_WRITER",
        }
    failure_patterns = (
        ("usage_limit_exceeded", ("usage limit", "usage_limit_exceeded")),
        ("quota_exhausted", ("quota exhausted", "quota_exhausted")),
        ("model_unavailable", ("model unavailable", "model_unavailable")),
        ("service_unavailable", ("service unavailable", "service_unavailable")),
        ("auth_invalid", ("authentication", "auth_invalid", "unauthorized")),
        ("runtime_unavailable", ("runtime unavailable", "runtime_unavailable")),
    )
    failure_class = next((name for name, patterns in failure_patterns if any(pattern in combined for pattern in patterns)), "resume_failed")
    return {
        "state": "RESUME_FAILED",
        "pending_control_event": True,
        "failure_class": failure_class,
        "fallback_eligible": failure_class != "resume_failed",
        "error_code": "WEB_LIFECYCLE_RESUME_FAILED",
        "returncode": returncode,
    }

def _write_json_atomic_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def audit_records_from_cursor(audit_log: Path, cursor_path: Path) -> tuple[list[tuple[dict[str, Any], int]], int]:
    state = load_json(cursor_path)
    try:
        stat = audit_log.stat()
    except OSError:
        return [], 0
    inode = int(state.get("inode", 0) or 0)
    offset = int(state.get("offset", 0) or 0)
    if inode != stat.st_ino or offset < 0 or offset > stat.st_size:
        offset = 0
    records: list[tuple[dict[str, Any], int]] = []
    with audit_log.open("rb") as handle:
        handle.seek(offset)
        for raw_line in handle:
            next_offset = handle.tell()
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                records.append((value, next_offset))
    return records, stat.st_ino


def audit_receipts_from_cursor(audit_log: Path, cursor_path: Path) -> list[dict[str, Any]]:
    records, _ = audit_records_from_cursor(audit_log, cursor_path)
    return [receipt for receipt, _ in records]


def _audit_receipt_state_path(cursor_path: Path) -> Path:
    return cursor_path.with_suffix(cursor_path.suffix + ".receipts.json")


def _audit_receipt_key(receipt: dict[str, Any]) -> str:
    receipt_id = str(receipt.get("receiptId") or "").strip()
    if receipt_id:
        return receipt_id
    import hashlib
    canonical = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit_receipt_status(cursor_path: Path, receipt: dict[str, Any]) -> str | None:
    data = load_json(_audit_receipt_state_path(cursor_path))
    receipts = data.get("receipts", {}) if isinstance(data, dict) else {}
    return receipts.get(_audit_receipt_key(receipt)) if isinstance(receipts, dict) else None


def _audit_receipt_wake_fingerprint(cursor_path: Path, receipt: dict[str, Any]) -> str | None:
    data = load_json(_audit_receipt_state_path(cursor_path))
    fingerprints = data.get("wake_fingerprints", {}) if isinstance(data, dict) else {}
    value = fingerprints.get(_audit_receipt_key(receipt)) if isinstance(fingerprints, dict) else None
    return value if isinstance(value, str) and value else None


def _set_audit_receipt_status(
    cursor_path: Path, receipt: dict[str, Any], status: str, *, wake_fingerprint: str | None = None
) -> None:
    path = _audit_receipt_state_path(cursor_path)
    data = load_json(path)
    receipts = data.get("receipts", {}) if isinstance(data.get("receipts"), dict) else {}
    fingerprints = data.get("wake_fingerprints", {}) if isinstance(data.get("wake_fingerprints"), dict) else {}
    key = _audit_receipt_key(receipt)
    receipts[key] = status
    if status == "wake_pending" and wake_fingerprint:
        fingerprints[key] = wake_fingerprint
    elif status != "wake_pending":
        fingerprints.pop(key, None)
    if len(receipts) > 256:
        keep = set(list(receipts.keys())[-256:])
        receipts = {key: value for key, value in receipts.items() if key in keep}
        fingerprints = {key: value for key, value in fingerprints.items() if key in keep}
    _write_json_atomic_file(path, {"receipts": receipts, "wake_fingerprints": fingerprints})


def _advance_audit_cursor(cursor_path: Path, inode: int, offset: int) -> None:
    _write_json_atomic_file(cursor_path, {"inode": inode, "offset": offset})


def computer_event_from_receipt(
    receipt: dict[str, Any],
    *,
    session_id: str,
    repo: Path,
    lease_path: Path,
    web_session_id: str | None = None,
    now_unix_ms: int | None = None,
) -> dict[str, Any] | None:
    if receipt.get("childTool") != "computer":
        return None
    if receipt.get("state") not in {"succeeded", "failed"}:
        return None
    lease = load_json(lease_path)
    if not lease:
        return None
    if lease.get("session_id") != session_id:
        return None
    if str(lease.get("repo") or "") != str(repo.resolve()):
        return None
    if web_session_id is not None and lease.get("web_session_id") != web_session_id:
        return None
    now_ms = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    issued_ms = int(lease.get("issued_at_unix_ms", 0) or 0)
    expires_ms = int(lease.get("expires_at_unix_ms", 0) or 0)
    remaining = int(lease.get("remaining_uses", 0) or 0)
    receipt_ms = int(receipt.get("occurredAtUnixMs", 0) or 0)
    if remaining <= 0 or expires_ms <= now_ms:
        lease_path.unlink(missing_ok=True)
        return None
    if receipt_ms and issued_ms and receipt_ms < issued_ms:
        return None

    detail = receipt.get("detail")
    target = receipt.get("targetLabel")
    event = {
        "hook_event_name": "PostToolUse",
        "controller_host": "web",
        "execution_host": "web",
        "event_source": "web",
        "controller_id": session_id,
        "controller_session_id": session_id,
        "web_session_id": web_session_id or lease.get("web_session_id"),
        "session_id": session_id,
        "turn_id": f"web-audit:{receipt.get('receiptId') or 'computer'}",
        "cwd": str(repo.resolve()),
        "tool_name": "AI-Bridge.computer",
        "tool_input": {
            "detail": detail if isinstance(detail, str) else "",
            "target": target if isinstance(target, str) else "",
        },
        "tool_response": {"state": receipt.get("state")},
    }
    remaining -= 1
    if remaining <= 0:
        lease_path.unlink(missing_ok=True)
    else:
        lease["remaining_uses"] = remaining
        lease_path.write_text(json.dumps(lease, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return event


def write_computer_lease(
    *, lease_path: Path, session_id: str, web_session_id: str, repo: Path, ttl_seconds: int, uses: int
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    value = {
        "session_id": session_id,
        "web_session_id": web_session_id,
        "repo": str(repo.resolve()),
        "issued_at_unix_ms": now_ms,
        "expires_at_unix_ms": now_ms + ttl_seconds * 1000,
        "remaining_uses": uses,
    }
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lease_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return value


def default_computer_lease_path(session_id: str) -> Path:
    return (
        Path.home()
        / ".codex"
        / "state"
        / "adaptive-delivery-web-lifecycle"
        / f"{session_id}.computer-lease.json"
    )


def bounded_tail(value: str, limit: int = STDERR_TAIL_LIMIT) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    marker = "\n...[diagnostic truncated]...\n"
    head_size = min(1024, max(1, limit // 4))
    tail_size = max(1, limit - head_size - len(marker))
    return text[:head_size] + marker + text[-tail_size:]


def native_runtime_env(runtime_path: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = runtime_path or DEFAULT_RUNTIME_PATH
    return env


def codex_requires_node(codex: Path) -> bool:
    try:
        with codex.open("r", encoding="utf-8", errors="ignore") as handle:
            first = handle.readline(512)
    except OSError:
        return False
    return "/usr/bin/env node" in first


def write_auto_stop_state(state_path: Path, value: dict[str, Any]) -> None:
    _write_json_atomic_file(state_path, value)


def preflight_native_resume(
    *, session_id: str, repo: Path, registry: Path, codex: str, runtime_path: str | None = None
) -> tuple[bool, str, dict[str, str]]:
    env = native_runtime_env(runtime_path)
    if not repo.is_dir():
        return False, f"repository does not exist: {repo}", env
    try:
        registered = _registered_controller_for_common_dir(repo, registry)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"cannot resolve registered controller common-dir: {exc}", env
    if registered != session_id:
        return False, f"session {session_id} is not the registered controller for {repo}", env
    codex_path = Path(codex).expanduser()
    if not codex_path.is_file() or not os.access(codex_path, os.X_OK):
        return False, f"codex executable unavailable: {codex_path}", env
    if codex_requires_node(codex_path) and shutil.which("node", path=env["PATH"]) is None:
        return False, f"missing node runtime in PATH for {codex_path}: {env['PATH']}", env
    completed = subprocess.run(
        [str(codex_path), "--version"], check=False, capture_output=True, text=True, env=env, timeout=15
    )
    if completed.returncode != 0:
        detail = bounded_tail(completed.stderr or completed.stdout)
        return False, f"codex preflight failed ({completed.returncode}): {detail}", env
    return True, "", env


def rotate_launcher_log(path: Path, max_bytes: int = LAUNCHER_LOG_LIMIT) -> None:
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        previous = path.with_suffix(path.suffix + ".1")
        previous.unlink(missing_ok=True)
        path.replace(previous)
    except OSError:
        return


def native_resume_command(*, codex: str, session_id: str, repo: Path) -> list[str]:
    prompt = (
        "Adaptive Delivery Web Stop checkpoint. Continue this existing registered controller "
        "thread only; do not create or fork another controller. Reconcile any pending lifecycle "
        "control event against the real main, ledger, live tasks, READY queue and candidates. "
        "Even when pending_control_event is false, perform one project-wide Goal rollover check: "
        "if the just-closed Goal has completed and the project still has executable open work, "
        "recompute readiness and roll to the next Goal before yielding; if everything is blocked, "
        "require the project-wide blocking proof. Obey the installed lifecycle hooks and "
        "control_event_guard; if no pending control action or Goal rollover remains, stop without "
        "starting unrelated work."
    )
    return [codex, "exec", "-C", str(repo.resolve()), "resume", session_id, prompt]


def execute_native_resume(
    *,
    session_id: str,
    repo: Path,
    registry: Path,
    codex: str,
    runtime_path: str | None = None,
) -> dict[str, Any]:
    """Run one bounded, preflighted same-thread native resume attempt."""
    command = native_resume_command(codex=codex, session_id=session_id, repo=repo)
    try:
        ok, preflight_error, env = preflight_native_resume(
            session_id=session_id,
            repo=repo,
            registry=registry,
            codex=codex,
            runtime_path=runtime_path,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        ok, preflight_error, env = (
            False,
            f"native resume preflight error: {exc}",
            native_runtime_env(runtime_path),
        )
    if not ok:
        return {
            "operation": "native_resume",
            "command": command,
            "result": "FAILED",
            "state": "RESUME_FAILED",
            "pending_control_event": True,
            "returncode": 78,
            "stderr_tail": bounded_tail(preflight_error),
            "error_code": "WEB_LIFECYCLE_RESUME_FAILED",
        }

    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, env=env
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "operation": "native_resume",
            "command": command,
            "result": "FAILED",
            "state": "RESUME_FAILED",
            "pending_control_event": True,
            "returncode": 78,
            "stderr_tail": bounded_tail(f"native resume execution error: {exc}"),
            "error_code": "WEB_LIFECYCLE_RESUME_FAILED",
        }

    attempt: dict[str, Any] = {
        "operation": "native_resume",
        "command": command,
        "pending_control_event": True,
        "returncode": completed.returncode,
        "stdout_tail": bounded_tail(completed.stdout),
        "stderr_tail": bounded_tail(completed.stderr),
    }
    if completed.returncode == 0:
        attempt.update({"result": "CONFIRMED", "state": "RESUME_SUCCEEDED"})
        return attempt
    attempt.update(classify_native_resume_failure(
        completed.returncode, completed.stdout, completed.stderr
    ))
    attempt["result"] = "DEFERRED" if attempt["state"] == "RESUME_DEFERRED_ACTIVE_WRITER" else "FAILED"
    return attempt


def controller_wake_lock_path(repo: Path) -> Path:
    return _git_common_dir(repo) / "adaptive-delivery" / "controller-wake.lock"


def _registered_controller_for_common_dir(repo: Path, registry_path: Path) -> str | None:
    common_dir = _git_common_dir(repo)
    registry = load_json(registry_path)
    matches: list[str] = []
    for session_id, registered_repo in registry.items():
        if not isinstance(session_id, str) or not isinstance(registered_repo, str):
            continue
        try:
            if _git_common_dir(Path(registered_repo).expanduser().resolve()) == common_dir:
                matches.append(session_id)
        except (OSError, subprocess.SubprocessError):
            continue
    if len(matches) != 1:
        return None
    return matches[0]


def _wake_event_fingerprint(lifecycle_state: dict[str, Any]) -> str:
    snapshot = lifecycle_state.get("snapshot")
    generation: dict[str, Any] = {}
    if isinstance(snapshot, dict):
        generation = {
            "head": snapshot.get("head"),
            "ledger_sha256": snapshot.get("ledger_sha256"),
            "worktree_status_sha256": snapshot.get("worktree_status_sha256"),
            "ready_ids": snapshot.get("ready_ids", []),
            "runnable_ids": snapshot.get("runnable_ids", []),
            "candidate_revisions": snapshot.get("candidate_revisions", []),
            "rule_handshake": snapshot.get("rule_handshake", {}),
        }
    value = {
        "pending_control_event": lifecycle_state.get("pending_control_event") is True,
        "triggers": lifecycle_state.get("triggers", []),
        "wake_generation": int(lifecycle_state.get("wake_generation", 0) or 0),
        "event_generation": generation,
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return __import__("hashlib").sha256(encoded).hexdigest()


def _bounded_adapter_operation(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return f"<non-text adapter operation: {type(value).__name__}>"
    return _bounded_text(value, 512)[0]


def _bounded_adapter_command(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    remaining = STDERR_TAIL_LIMIT
    command: list[str] = []
    for argument in value[:64]:
        if not isinstance(argument, str):
            return None
        if remaining <= 0:
            break
        normalized, _ = _bounded_text(argument, min(1024, remaining))
        command.append(normalized)
        remaining -= len(normalized.encode("utf-8"))
    return command


def _bounded_adapter_diagnostics(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return f"<non-text adapter diagnostics: {type(value).__name__}>"
    return bounded_tail(value)


def _wake_receipt(
    *,
    common_dir: Path,
    session_id: str,
    event_fingerprint: str,
    health: dict[str, Any],
    decision: str,
    selected_host: str | None,
    reason: str,
    operation: Any,
    result: str,
    command: Any = None,
    diagnostics: Any = None,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "canonical_common_dir": str(common_dir),
        "controller_id": session_id,
        "event_fingerprint": event_fingerprint,
        "health": health.get("state"),
        "preferred_host": health.get("controller_host"),
        "selected_host": selected_host,
        "decision": decision,
        "reason": bounded_tail(reason, 512),
        "started_at_unix_ms": now,
        "completed_at_unix_ms": now,
        "operation": _bounded_adapter_operation(operation),
        "result": result,
        # A host confirmation proves only the same-thread launch.  Lifecycle closure
        # remains the control-event guard's responsibility.
        "pending_control_event": True,
    }
    normalized_command = _bounded_adapter_command(command)
    if normalized_command is not None:
        receipt["command"] = normalized_command
    normalized_diagnostics = _bounded_adapter_diagnostics(diagnostics)
    if normalized_diagnostics:
        receipt["diagnostics"] = normalized_diagnostics
    return receipt


def wake_existing_controller(
    *,
    lifecycle_state: dict[str, Any],
    session_id: str,
    repo: Path,
    registry: Path,
    codex: str,
    receipt_path: Path,
    host_facts: dict[str, Any],
    resume_adapters: dict[str, Callable[..., dict[str, Any]]] | None = None,
    runtime_path: str | None = None,
) -> dict[str, Any]:
    """Wake only the registered controller, using Task 1's pure decision policy."""
    repo = repo.resolve()
    try:
        common_dir = _git_common_dir(repo)
    except (OSError, subprocess.SubprocessError) as exc:
        health = derive_controller_health({})
        receipt = _wake_receipt(
            common_dir=repo,
            session_id=session_id,
            event_fingerprint=_wake_event_fingerprint(lifecycle_state),
            health=health,
            decision="DEAD_BLOCK",
            selected_host=None,
            reason=f"cannot resolve Git common-dir: {exc}",
            operation=None,
            result="BLOCKED",
        )
        _write_json_atomic_file(receipt_path, receipt)
        return receipt

    facts = dict(host_facts) if isinstance(host_facts, dict) else {}
    facts.update({
        "registered_controller": session_id,
        "canonical_common_dir": str(common_dir),
        "pending_control_event": lifecycle_state.get("pending_control_event") is True,
    })
    if _registered_controller_for_common_dir(repo, registry) != session_id:
        facts.pop("registered_controller", None)
    health = derive_controller_health(facts)
    wake = decide_controller_wake(health)
    decision = str(wake["decision"])
    selected_host = wake["selected_host"]
    fingerprint = _wake_event_fingerprint(lifecycle_state)
    reason = str(facts.get("failure_class") or health["state"])

    lock_path = common_dir / "adaptive-delivery" / "controller-wake.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            receipt = _wake_receipt(
                common_dir=common_dir,
                session_id=session_id,
                event_fingerprint=fingerprint,
                health=health,
                decision="DEFER",
                selected_host=None,
                reason="common_dir_wake_locked",
                operation=None,
                result="DEFERRED",
            )
            return receipt

        if decision == "NOOP_ACTIVE":
            receipt = _wake_receipt(
                common_dir=common_dir, session_id=session_id, event_fingerprint=fingerprint,
                health=health, decision=decision, selected_host=None, reason=reason,
                operation=None, result="CONFIRMED",
            )
        elif decision == "DEFER":
            receipt = _wake_receipt(
                common_dir=common_dir, session_id=session_id, event_fingerprint=fingerprint,
                health=health, decision=decision, selected_host=None, reason=reason,
                operation=None, result="DEFERRED",
            )
        elif decision == "DEAD_BLOCK":
            receipt = _wake_receipt(
                common_dir=common_dir, session_id=session_id, event_fingerprint=fingerprint,
                health=health, decision=decision, selected_host=None, reason=reason,
                operation=None, result="BLOCKED",
            )
        else:
            if selected_host == health.get("controller_host"):
                attempt = execute_native_resume(
                    session_id=session_id, repo=repo, registry=registry, codex=codex,
                    runtime_path=runtime_path,
                )
            else:
                adapter = (resume_adapters or {}).get(str(selected_host))
                if adapter is None:
                    attempt = {
                        "operation": None,
                        "result": "DEFERRED",
                        "state": "RESUME_DEFERRED",
                        "stderr_tail": f"no authorized adapter for peer host {selected_host}",
                    }
                else:
                    try:
                        attempt = adapter(
                            session_id=session_id,
                            repo=repo,
                            registry=registry,
                            codex=codex,
                            runtime_path=runtime_path,
                        )
                    except (OSError, subprocess.SubprocessError) as exc:
                        attempt = {
                            "operation": f"{selected_host}_resume",
                            "result": "FAILED",
                            "state": "RESUME_FAILED",
                            "stderr_tail": f"host adapter error: {exc}",
                        }
            result = str(attempt.get("result", "FAILED"))
            receipt = _wake_receipt(
                common_dir=common_dir, session_id=session_id, event_fingerprint=fingerprint,
                health=health, decision=decision, selected_host=str(selected_host), reason=reason,
                operation=attempt.get("operation"), result=result,
                command=attempt.get("command"), diagnostics=attempt.get("stderr_tail"),
            )
        _write_json_atomic_file(receipt_path, receipt)
        return receipt
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def default_wake_receipt_path(repo: Path) -> Path:
    return _git_common_dir(repo) / "adaptive-delivery" / "controller-wake-receipt.json"


def _lifecycle_module() -> Any:
    try:
        import lifecycle_hook as lifecycle
    except ModuleNotFoundError:
        from scripts import lifecycle_hook as lifecycle
    return lifecycle


def _load_lifecycle_state(session_id: str) -> dict[str, Any]:
    try:
        lifecycle = _lifecycle_module()
        loaded = lifecycle.load_json(lifecycle.state_path(session_id))
    except (OSError, ValueError, ModuleNotFoundError, ImportError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def dispatch_pending_lifecycle_wake(
    *,
    lifecycle_state: dict[str, Any],
    session_id: str,
    repo: Path,
    registry: Path,
    codex: str,
    receipt_path: Path | None = None,
    host_facts: dict[str, Any] | None = None,
    resume_adapters: dict[str, Callable[..., dict[str, Any]]] | None = None,
    runtime_path: str | None = None,
) -> dict[str, Any] | None:
    """Route any pending lifecycle event through the one Wake Supervisor path."""
    if not isinstance(lifecycle_state, dict) or lifecycle_state.get("pending_control_event") is not True:
        return None
    repo = repo.resolve()
    try:
        target_receipt = receipt_path or default_wake_receipt_path(repo)
    except (OSError, subprocess.SubprocessError):
        if receipt_path is None:
            return None
        target_receipt = receipt_path
    fingerprint = _wake_event_fingerprint(lifecycle_state)
    prior = load_json(target_receipt)
    try:
        current_common_dir = str(_git_common_dir(repo))
        current_registered = _registered_controller_for_common_dir(repo, registry)
    except (OSError, subprocess.SubprocessError):
        current_common_dir = None
        current_registered = None
    if (
        prior.get("event_fingerprint") == fingerprint
        and prior.get("result") == "CONFIRMED"
        and prior.get("controller_id") == session_id
        and current_registered == session_id
        and current_common_dir is not None
        and prior.get("canonical_common_dir") == current_common_dir
    ):
        debounced = dict(prior)
        debounced["debounced"] = True
        return debounced

    facts = dict(host_facts) if isinstance(host_facts, dict) else {}
    controller_host = str(lifecycle_state.get("controller_host") or facts.get("controller_host") or "web").strip()
    if controller_host not in {"web", "desktop_codex"}:
        controller_host = "web"
    facts.setdefault("controller_host", controller_host)
    if (
        "resume_actionable" not in facts
        and "resume_state" not in facts
        and facts.get("controller_execution_active") is not True
        and facts.get("active_writer") is not True
    ):
        facts["resume_actionable"] = True
    return wake_existing_controller(
        lifecycle_state=lifecycle_state,
        session_id=session_id,
        repo=repo,
        registry=registry,
        codex=codex,
        receipt_path=target_receipt,
        host_facts=facts,
        resume_adapters=resume_adapters,
        runtime_path=runtime_path,
    )



def wake_receipt_confirmed(receipt: dict[str, Any] | None) -> bool:
    return isinstance(receipt, dict) and receipt.get("result") == "CONFIRMED"

def successful_guard_event_from_receipt(
    receipt: dict[str, Any], *, session_id: str, repo: Path, web_session_id: str
) -> dict[str, Any] | None:
    event = translate_receipt(
        receipt, session_id=session_id, repo=repo, web_session_id=web_session_id
    )
    if event is None:
        return None
    command = str(event["tool_input"].get("command", ""))
    output = str(event["tool_response"].get("output", ""))
    if "control_event_guard.py" not in command:
        return None
    if "control-event: allowed" not in output:
        return None
    return event


def append_captured_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def dispatch_event(event: dict[str, Any]) -> int:
    hook = Path(__file__).resolve().with_name("lifecycle_hook.py")
    completed = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(event, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    return completed.returncode


def rule_wake_schedule_decision(lifecycle_state: dict[str, Any]) -> str:
    policy = str(lifecycle_state.get("rule_wake_policy", "")).strip()
    if policy == "immediate":
        return "schedule_now"
    if policy == "next_turn":
        return "natural_turn"
    if policy != "after_event":
        return "none"
    triggers = {str(item) for item in lifecycle_state.get("triggers", [])}
    non_rule = {
        item for item in triggers
        if not item.startswith(("rule_update_pending:", "rule_ledger_stale:", "rule_install_integrity_error:"))
    }
    return "wait_for_event" if non_rule else "schedule_now"


def _rule_revision_from_state(lifecycle_state: dict[str, Any]) -> str | None:
    snapshot = lifecycle_state.get("snapshot")
    if isinstance(snapshot, dict):
        handshake = snapshot.get("rule_handshake")
        if isinstance(handshake, dict):
            revision = str(handshake.get("installed_revision", "")).strip()
            if revision:
                return revision
    for trigger in lifecycle_state.get("triggers", []):
        text = str(trigger)
        if text.startswith("rule_update_pending:"):
            revision = text.split(":", 1)[1].strip()
            if revision:
                return revision
    return None


def maybe_schedule_rule_wake(
    *,
    lifecycle_state: dict[str, Any],
    session_id: str,
    repo: Path,
    registry: Path,
    codex: str,
    delay_seconds: float,
    state_path: Path,
    capture_path: Path | None = None,
    runtime_path: str | None = None,
) -> str:
    decision = rule_wake_schedule_decision(lifecycle_state)
    if decision != "schedule_now":
        return decision
    revision = _rule_revision_from_state(lifecycle_state)
    if not revision:
        return "none"
    receipt_id = f"rule-update:{revision}"
    existing = load_json(state_path)
    if existing.get("receipt_id") == receipt_id and existing.get("state") in {"RESUME_PENDING", "RESUME_CONFIRMED"}:
        return "already_scheduled"
    schedule_auto_native_stop(
        session_id=session_id, repo=repo, receipt_id=receipt_id, registry=registry, codex=codex,
        delay_seconds=delay_seconds, state_path=state_path, capture_path=capture_path, runtime_path=runtime_path,
    )
    return "scheduled"


def refresh_rule_wake_state(*, session_id: str, repo: Path) -> dict[str, Any]:
    try:
        import lifecycle_hook as lifecycle
    except ModuleNotFoundError:
        from scripts import lifecycle_hook as lifecycle
    snapshot = lifecycle.project_snapshot(repo)
    if snapshot is None:
        return {}
    path = lifecycle.state_path(session_id)
    prior = lifecycle.load_json(path)
    event = post_tool_event(
        session_id=session_id, repo=repo, command="adaptive-delivery rule wake check", exit_code=0
    )
    _, next_state = lifecycle.evaluate_event(event, snapshot=snapshot, prior_state=prior)
    lifecycle.write_json(path, next_state)
    return next_state


def default_auto_stop_state_path(session_id: str) -> Path:
    return (
        Path.home()
        / ".codex"
        / "state"
        / "adaptive-delivery-web-lifecycle"
        / f"{session_id}.auto-stop.json"
    )


def schedule_auto_native_stop(
    *,
    session_id: str,
    repo: Path,
    receipt_id: str,
    registry: Path,
    codex: str,
    delay_seconds: float,
    state_path: Path,
    capture_path: Path | None = None,
    runtime_path: str | None = None,
) -> None:
    value = {
        "receipt_id": receipt_id,
        "session_id": session_id,
        "repo": str(repo.resolve()),
        "scheduled_at_unix_ms": int(time.time() * 1000),
        "state": "RESUME_PENDING",
        "pending_control_event": True,
    }
    write_auto_stop_state(state_path, value)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "auto-native-stop",
        "--session-id",
        session_id,
        "--repo",
        str(repo.resolve()),
        "--receipt-id",
        receipt_id,
        "--registry",
        str(registry.expanduser()),
        "--codex",
        codex,
        "--delay-seconds",
        str(delay_seconds),
        "--state",
        str(state_path),
        "--runtime-path",
        runtime_path or DEFAULT_RUNTIME_PATH,
    ]
    if capture_path is not None:
        capture_path.write_text(json.dumps(command, ensure_ascii=False) + "\n", encoding="utf-8")
        return
    launcher_log = state_path.with_suffix(state_path.suffix + ".launcher.log")
    rotate_launcher_log(launcher_log)
    launcher_log.parent.mkdir(parents=True, exist_ok=True)
    log_handle = launcher_log.open("ab", buffering=0)
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            env=native_runtime_env(runtime_path),
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_handle.close()


def run_auto_native_stop(
    *,
    session_id: str,
    repo: Path,
    receipt_id: str,
    registry: Path,
    codex: str,
    delay_seconds: float,
    state_path: Path,
    runtime_path: str | None = None,
) -> int:
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    state = load_json(state_path)
    if state.get("receipt_id") != receipt_id:
        return 0
    command = native_resume_command(codex=codex, session_id=session_id, repo=repo)
    latest = dict(state)
    latest.update({
        "state": "RESUME_PENDING",
        "pending_control_event": True,
        "started_at_unix_ms": int(time.time() * 1000),
        "command": command,
        "runtime_path": runtime_path or DEFAULT_RUNTIME_PATH,
    })
    write_auto_stop_state(state_path, latest)
    attempt = execute_native_resume(
        session_id=session_id, repo=repo, registry=registry, codex=codex,
        runtime_path=runtime_path,
    )
    latest = load_json(state_path)
    if latest.get("receipt_id") == receipt_id:
        if attempt["result"] == "CONFIRMED":
            latest.update({
                "state": "RESUME_CONFIRMED",
                "pending_control_event": True,
                "completed_at_unix_ms": int(time.time() * 1000),
                "returncode": attempt["returncode"],
                "stdout_tail": attempt.get("stdout_tail", ""),
                "stderr_tail": attempt.get("stderr_tail", ""),
            })
            latest.pop("error_code", None)
            latest.pop("failure_class", None)
            latest.pop("fallback_eligible", None)
        else:
            latest.update({
                "state": attempt["state"],
                "pending_control_event": True,
                "completed_at_unix_ms": int(time.time() * 1000),
                "returncode": attempt["returncode"],
                "stdout_tail": attempt.get("stdout_tail", ""),
                "stderr_tail": attempt.get("stderr_tail", ""),
            })
            for key in ("error_code", "failure_class", "fallback_eligible"):
                if key in attempt:
                    latest[key] = attempt[key]
        write_auto_stop_state(state_path, latest)
    stderr_tail = str(attempt.get("stderr_tail", ""))
    if stderr_tail:
        print(stderr_tail, file=sys.stderr, end="" if stderr_tail.endswith("\n") else "\n")
    return int(attempt["returncode"])


def zshenv_block() -> str:
    script = Path(__file__).resolve()
    python = Path("/Library/Frameworks/Python.framework/Versions/3.11/bin/python3")
    return f'''# >>> adaptive-delivery web lifecycle bridge >>>
_ad_web_parent=$(/bin/ps -p "$PPID" -o command= 2>/dev/null)
_ad_web_session_id="${{ADAPTIVE_DELIVERY_WEB_SESSION_ID:-}}"
if [[ "$_ad_web_parent" == *"{AI_BRIDGE_EXECUTABLE}"* && -n "$_ad_web_session_id" ]]; then
  _ad_web_cwd="$PWD"
  _ad_web_command="$ZSH_EXECUTION_STRING"
  _ad_web_bridge_script="{script}"
  _ad_web_bridge_python="{python}"
  _ad_web_lifecycle_exit() {{
    local _ad_web_exit_code=$?
    trap - EXIT
    "$_ad_web_bridge_python" "$_ad_web_bridge_script" post-shell --cwd "$_ad_web_cwd" --command "$_ad_web_command" --exit-code "$_ad_web_exit_code" --web-session-id "$_ad_web_session_id"
    local _ad_web_bridge_exit_code=$?
    if [[ "$_ad_web_exit_code" -ne 0 ]]; then
      exit "$_ad_web_exit_code"
    fi
    exit "$_ad_web_bridge_exit_code"
  }}
  trap _ad_web_lifecycle_exit EXIT
fi
unset _ad_web_parent
# <<< adaptive-delivery web lifecycle bridge <<<'''


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bridge AI-Bridge Web tool events into Adaptive Delivery lifecycle hooks."
    )
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    translate = subparsers.add_parser("translate-receipt")
    translate.add_argument("--session-id", required=True)
    translate.add_argument("--repo", required=True)
    translate.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    translate.add_argument("--web-session-id")

    bind_web = subparsers.add_parser("bind-web-session")
    bind_web.add_argument("--repo", required=True)
    bind_web.add_argument("--controller-id", required=True)
    bind_web.add_argument("--web-session-id", required=True)
    bind_web.add_argument("--registry", default=str(DEFAULT_REGISTRY))

    session_start = subparsers.add_parser("session-start")
    session_start.add_argument("--repo", required=True)
    session_start.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    session_start.add_argument("--web-session-id")

    post_shell = subparsers.add_parser("post-shell")
    post_shell.add_argument("--cwd", required=True)
    post_shell.add_argument("--command", required=True)
    post_shell.add_argument("--exit-code", required=True, type=int)
    post_shell.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    post_shell.add_argument("--web-session-id")
    post_shell.add_argument("--capture-event")

    audit_once = subparsers.add_parser("audit-once")
    audit_once.add_argument("--session-id", required=True)
    audit_once.add_argument("--repo", required=True)
    audit_once.add_argument("--audit-log", default=str(DEFAULT_AUDIT_LOG))
    audit_once.add_argument("--cursor", required=True)
    audit_once.add_argument("--capture-events")
    audit_once.add_argument("--computer-lease")
    audit_once.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    audit_once.add_argument("--web-session-id")
    audit_once.add_argument("--auto-native-stop", action="store_true")
    audit_once.add_argument("--auto-stop-delay-seconds", type=float, default=5.0)
    audit_once.add_argument("--auto-stop-state")
    audit_once.add_argument("--capture-auto-stop")
    audit_once.add_argument("--codex", default="/opt/homebrew/bin/codex")
    audit_once.add_argument("--runtime-path", default=DEFAULT_RUNTIME_PATH)

    arm_computer = subparsers.add_parser("arm-computer")
    arm_computer.add_argument("--cwd", required=True)
    arm_computer.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    arm_computer.add_argument("--web-session-id")
    arm_computer.add_argument("--lease")
    arm_computer.add_argument("--ttl-seconds", type=int, default=90)
    arm_computer.add_argument("--uses", type=int, default=1)

    native_stop = subparsers.add_parser("native-stop")
    native_stop.add_argument("--session-id", required=True)
    native_stop.add_argument("--repo", required=True)
    native_stop.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    native_stop.add_argument("--codex", default="/opt/homebrew/bin/codex")
    native_stop.add_argument("--runtime-path", default=DEFAULT_RUNTIME_PATH)
    native_stop.add_argument("--dry-run", action="store_true")

    auto_stop = subparsers.add_parser("auto-native-stop")
    auto_stop.add_argument("--session-id", required=True)
    auto_stop.add_argument("--repo", required=True)
    auto_stop.add_argument("--receipt-id", required=True)
    auto_stop.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    auto_stop.add_argument("--codex", default="/opt/homebrew/bin/codex")
    auto_stop.add_argument("--runtime-path", default=DEFAULT_RUNTIME_PATH)
    auto_stop.add_argument("--delay-seconds", type=float, default=5.0)
    auto_stop.add_argument("--state")

    subparsers.add_parser("print-zshenv-block")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command_name == "translate-receipt":
        repo = Path(args.repo).expanduser().resolve()
        registry_path = Path(args.registry).expanduser()
        try:
            registered = registered_controller_for_repo(repo, registry_path)
            if registered != args.session_id:
                raise PermissionError("translate-receipt controller does not match registered Controller")
            web_session_id = require_web_controller_session(
                controller_id=registered, web_session_id=args.web_session_id, registry_path=registry_path
            )
            receipt = json.load(sys.stdin)
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        except json.JSONDecodeError as exc:
            print(f"invalid receipt JSON: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if not isinstance(receipt, dict):
            print("receipt must be a JSON object", file=sys.stderr)
            return 2
        event = translate_receipt(
            receipt, session_id=args.session_id, repo=repo, web_session_id=web_session_id
        )
        if event is not None:
            print(json.dumps(event, ensure_ascii=False))
        return 0

    if args.command_name == "bind-web-session":
        repo = canonical_root(args.repo)
        registry_path = Path(args.registry).expanduser()
        try:
            bind_web_session_to_controller(
                repo=repo, controller_id=args.controller_id, web_session_id=args.web_session_id, registry_path=registry_path
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({
            "controller_id": args.controller_id,
            "controller_session_id": args.controller_id,
            "web_session_id": args.web_session_id,
            "event_source": "web",
        }, ensure_ascii=False))
        return 0

    if args.command_name == "session-start":
        repo = canonical_root(args.repo)
        registry_path = Path(args.registry).expanduser()
        try:
            controller_id = registered_controller_for_repo(repo, registry_path)
            if controller_id is None:
                raise ValueError(f"no registered controller for {repo}")
            web_session_id = require_web_controller_session(
                controller_id=controller_id,
                web_session_id=args.web_session_id,
                registry_path=registry_path,
            )
            payload = web_session_restore_payload(repo, registry_path)
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        payload["controller_id"] = controller_id
        payload["controller_session_id"] = controller_id
        payload["web_session_id"] = web_session_id
        payload["event_source"] = "web"
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if args.command_name == "post-shell":
        repo = canonical_root(args.cwd)
        try:
            session_id = registered_controller_for_repo(
                repo, Path(args.registry).expanduser()
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if session_id is None:
            return 0
        registry_path = Path(args.registry).expanduser()
        try:
            web_session_id = require_web_controller_session(
                controller_id=session_id,
                web_session_id=args.web_session_id,
                registry_path=registry_path,
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        event = post_tool_event(
            session_id=session_id,
            repo=repo,
            command=args.command,
            exit_code=args.exit_code,
            web_session_id=web_session_id,
            execution_host="web",
        )
        if args.capture_event:
            Path(args.capture_event).write_text(
                json.dumps(event, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return 0
        dispatch_code = dispatch_event(event)
        if dispatch_code != 0:
            return dispatch_code
        lifecycle_state = _load_lifecycle_state(session_id)
        wake_receipt = dispatch_pending_lifecycle_wake(
            lifecycle_state=lifecycle_state,
            session_id=session_id,
            repo=repo,
            registry=Path(args.registry).expanduser(),
            codex="/opt/homebrew/bin/codex",
        )
        if lifecycle_state.get("pending_control_event") is True and not wake_receipt_confirmed(wake_receipt):
            return 78
        return 0

    if args.command_name == "audit-once":
        repo = Path(args.repo).expanduser().resolve()
        registry_path = Path(args.registry).expanduser()
        try:
            registered = registered_controller_for_repo(repo, registry_path)
            if registered is None or registered != args.session_id:
                raise PermissionError("audit-once controller does not match registered Controller")
            web_session_id = require_web_controller_session(
                controller_id=registered, web_session_id=args.web_session_id, registry_path=registry_path
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        cursor_path = Path(args.cursor).expanduser()
        consumer_lock_path = cursor_path.with_suffix(cursor_path.suffix + ".consumer.lock")
        consumer_lock_path.parent.mkdir(parents=True, exist_ok=True)
        consumer_lock = consumer_lock_path.open("a+")
        try:
            fcntl.flock(consumer_lock.fileno(), fcntl.LOCK_EX)
            records, audit_inode = audit_records_from_cursor(
                Path(args.audit_log).expanduser(), cursor_path
            )
            receipts = [receipt for receipt, _ in records]
            lease_path = (
                Path(args.computer_lease).expanduser()
                if args.computer_lease
                else default_computer_lease_path(args.session_id)
            )
            for receipt, next_offset in records:
                status = _audit_receipt_status(cursor_path, receipt)
                if status == "handled":
                    _advance_audit_cursor(cursor_path, audit_inode, next_offset)
                    continue
                if status == "pending":
                    print(
                        f"web lifecycle receipt outcome unknown; reconcile before replay: {_audit_receipt_key(receipt)}",
                        file=sys.stderr,
                    )
                    return 3
                if status == "wake_pending":
                    expected_wake_fingerprint = _audit_receipt_wake_fingerprint(cursor_path, receipt)
                    try:
                        lifecycle_state = _load_lifecycle_state(args.session_id)
                        if lifecycle_state.get("pending_control_event") is not True:
                            print(
                                f"web lifecycle wake retry state missing or no longer pending for {_audit_receipt_key(receipt)}",
                                file=sys.stderr,
                            )
                            return 78
                        current_fingerprint = _wake_event_fingerprint(lifecycle_state)
                        if not expected_wake_fingerprint or current_fingerprint != expected_wake_fingerprint:
                            print(
                                f"web lifecycle wake retry generation mismatch for {_audit_receipt_key(receipt)}",
                                file=sys.stderr,
                            )
                            return 78
                        wake_receipt = dispatch_pending_lifecycle_wake(
                            lifecycle_state=lifecycle_state,
                            session_id=args.session_id,
                            repo=repo,
                            registry=Path(args.registry).expanduser(),
                            codex=args.codex,
                            runtime_path=args.runtime_path,
                        )
                        if not wake_receipt_confirmed(wake_receipt):
                            result = wake_receipt.get("result") if isinstance(wake_receipt, dict) else "MISSING_RECEIPT"
                            print(
                                f"web lifecycle wake not confirmed for {_audit_receipt_key(receipt)}: {result}",
                                file=sys.stderr,
                            )
                            return 78
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        print(f"web lifecycle handling failed for {_audit_receipt_key(receipt)}: {exc}", file=sys.stderr)
                        return 3
                    _set_audit_receipt_status(cursor_path, receipt, "handled")
                    _advance_audit_cursor(cursor_path, audit_inode, next_offset)
                    continue

                guard_event = successful_guard_event_from_receipt(
                    receipt, session_id=args.session_id, repo=repo, web_session_id=web_session_id
                )
                event = guard_event
                if event is None:
                    event = computer_event_from_receipt(
                        receipt, session_id=args.session_id, repo=repo, lease_path=lease_path,
                        web_session_id=web_session_id
                    )
                if event is None:
                    _advance_audit_cursor(cursor_path, audit_inode, next_offset)
                    continue
                _set_audit_receipt_status(cursor_path, receipt, "pending")
                try:
                    if args.capture_events:
                        append_captured_event(Path(args.capture_events), event)
                    else:
                        dispatch_code = dispatch_event(event)
                        if dispatch_code != 0:
                            print(
                                f"web lifecycle dispatch failed for {_audit_receipt_key(receipt)}: exit {dispatch_code}",
                                file=sys.stderr,
                            )
                            return dispatch_code
                        lifecycle_state = _load_lifecycle_state(args.session_id)
                        if lifecycle_state.get("pending_control_event") is True:
                            _set_audit_receipt_status(
                                cursor_path,
                                receipt,
                                "wake_pending",
                                wake_fingerprint=_wake_event_fingerprint(lifecycle_state),
                            )
                            wake_receipt = dispatch_pending_lifecycle_wake(
                                lifecycle_state=lifecycle_state,
                                session_id=args.session_id,
                                repo=repo,
                                registry=Path(args.registry).expanduser(),
                                codex=args.codex,
                                runtime_path=args.runtime_path,
                            )
                            if not wake_receipt_confirmed(wake_receipt):
                                result = wake_receipt.get("result") if isinstance(wake_receipt, dict) else "MISSING_RECEIPT"
                                print(
                                    f"web lifecycle wake not confirmed for {_audit_receipt_key(receipt)}: {result}",
                                    file=sys.stderr,
                                )
                                return 78
                    if args.auto_native_stop and guard_event is not None:
                        receipt_id = str(receipt.get("receiptId") or "web-guard")
                        state_path = (
                            Path(args.auto_stop_state).expanduser()
                            if args.auto_stop_state
                            else default_auto_stop_state_path(args.session_id)
                        )
                        schedule_auto_native_stop(
                            session_id=args.session_id,
                            repo=repo,
                            receipt_id=receipt_id,
                            registry=Path(args.registry).expanduser(),
                            codex=args.codex,
                            delay_seconds=max(0.0, args.auto_stop_delay_seconds),
                            state_path=state_path,
                            capture_path=Path(args.capture_auto_stop).expanduser() if args.capture_auto_stop else None,
                            runtime_path=args.runtime_path,
                        )
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    print(f"web lifecycle handling failed for {_audit_receipt_key(receipt)}: {exc}", file=sys.stderr)
                    return 3
                _set_audit_receipt_status(cursor_path, receipt, "handled")
                _advance_audit_cursor(cursor_path, audit_inode, next_offset)
            if receipts and args.auto_native_stop:
                lifecycle_state = refresh_rule_wake_state(session_id=args.session_id, repo=repo)
                state_path = (
                    Path(args.auto_stop_state).expanduser()
                    if args.auto_stop_state
                    else default_auto_stop_state_path(args.session_id)
                )
                maybe_schedule_rule_wake(
                    lifecycle_state=lifecycle_state,
                    session_id=args.session_id, repo=repo, registry=Path(args.registry).expanduser(),
                    codex=args.codex, delay_seconds=max(0.0, args.auto_stop_delay_seconds),
                    state_path=state_path,
                    capture_path=Path(args.capture_auto_stop).expanduser() if args.capture_auto_stop else None,
                    runtime_path=args.runtime_path,
                )
            return 0

        finally:
            try:
                fcntl.flock(consumer_lock.fileno(), fcntl.LOCK_UN)
            finally:
                consumer_lock.close()

    if args.command_name == "arm-computer":
        repo = canonical_root(args.cwd)
        registry_path = Path(args.registry).expanduser()
        try:
            session_id = registered_controller_for_repo(repo, registry_path)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if session_id is None:
            print(f"no registered controller for {repo}", file=sys.stderr)
            return 2
        try:
            web_session_id = require_web_controller_session(
                controller_id=session_id, web_session_id=args.web_session_id, registry_path=registry_path
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        if not (5 <= args.ttl_seconds <= 300):
            print("ttl-seconds must be between 5 and 300", file=sys.stderr)
            return 2
        if not (1 <= args.uses <= 8):
            print("uses must be between 1 and 8", file=sys.stderr)
            return 2
        lease_path = (
            Path(args.lease).expanduser()
            if args.lease
            else default_computer_lease_path(session_id)
        )
        value = write_computer_lease(
            lease_path=lease_path,
            session_id=session_id,
            web_session_id=web_session_id,
            repo=repo,
            ttl_seconds=args.ttl_seconds,
            uses=args.uses,
        )
        print(json.dumps(value, ensure_ascii=False))
        return 0

    if args.command_name == "native-stop":
        repo = canonical_root(args.repo)
        try:
            registered = registered_controller_for_repo(
                repo, Path(args.registry).expanduser()
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if registered != args.session_id:
            print(
                f"session {args.session_id} is not the registered controller for {repo}",
                file=sys.stderr,
            )
            return 2
        command = native_resume_command(
            codex=args.codex, session_id=args.session_id, repo=repo
        )
        if args.dry_run:
            print(json.dumps(command, ensure_ascii=False))
            return 0
        lifecycle_state = _load_lifecycle_state(args.session_id)
        wake_receipt = dispatch_pending_lifecycle_wake(
            lifecycle_state=lifecycle_state,
            session_id=args.session_id,
            repo=repo,
            registry=Path(args.registry).expanduser(),
            codex=args.codex,
            runtime_path=args.runtime_path,
        )
        if wake_receipt is None:
            print("web lifecycle wake supervisor produced no receipt", file=sys.stderr)
            return 78
        return 0 if wake_receipt_confirmed(wake_receipt) else 78

    if args.command_name == "auto-native-stop":
        repo = canonical_root(args.repo)
        state_path = (
            Path(args.state).expanduser()
            if args.state
            else default_auto_stop_state_path(args.session_id)
        )
        return run_auto_native_stop(
            session_id=args.session_id,
            repo=repo,
            receipt_id=args.receipt_id,
            registry=Path(args.registry).expanduser(),
            codex=args.codex,
            delay_seconds=max(0.0, args.delay_seconds),
            state_path=state_path,
            runtime_path=args.runtime_path,
        )

    if args.command_name == "print-zshenv-block":
        print(zshenv_block())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
