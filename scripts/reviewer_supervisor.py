#!/usr/bin/env python3
from pathlib import Path
import json
import os
import subprocess
import tempfile


def git_common_state_root(repo: Path) -> Path:
    repo = Path(repo).resolve()
    raw = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True
    ).strip()
    common = Path(raw)
    if not common.is_absolute():
        common = (repo / common).resolve()
    return common / "adaptive-delivery" / "reviewer-runs"


def validate_verdict(payload: dict, expected_head: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("verdict payload must be an object")
    if payload.get("reviewed_head") != expected_head:
        raise ValueError("reviewed_head does not match candidate HEAD")
    if payload.get("verdict") not in {"PASS", "FINDINGS"}:
        raise ValueError("verdict must be PASS or FINDINGS")
    for field in ("critical", "important", "minor"):
        if not isinstance(payload.get(field), list):
            raise ValueError(f"{field} must be a list")
    return payload


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class ReviewContract:
    repo: Path
    base: str
    head: str
    instructions: str
    event_path: Path
    final_path: Path


@dataclass(frozen=True)
class AttemptResult:
    state: str
    pid: int
    exit_code: int
    running_observed: bool
    session_id: Optional[str] = None
    diagnostic: str = ""


def _session_id_from_event(event: dict) -> Optional[str]:
    for key in ("thread_id", "session_id", "threadId", "sessionId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    payload = event.get("payload")
    if isinstance(payload, dict):
        return _session_id_from_event(payload)
    return None


def run_attempt(
    contract: ReviewContract,
    attempt: int,
    *,
    popen_factory: Callable = subprocess.Popen,
    codex_executable: str = "codex",
) -> AttemptResult:
    contract.event_path.parent.mkdir(parents=True, exist_ok=True)
    contract.final_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        codex_executable,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--json",
        "-o",
        str(contract.final_path),
        "review",
        "--base",
        contract.base,
        contract.instructions,
    ]
    process = popen_factory(
        argv,
        cwd=str(contract.repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    running_observed = False
    session_id = None
    diagnostics = []
    with contract.event_path.open("w", encoding="utf-8") as event_log:
        if process.stdout is not None:
            for line in process.stdout:
                event_log.write(line)
                event_log.flush()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    diagnostics.append(line.strip())
                    continue
                if isinstance(event, dict):
                    running_observed = True
                    session_id = session_id or _session_id_from_event(event)
    exit_code = process.wait()
    if not running_observed:
        state = "REVIEW_INFRA_FAILED"
    else:
        state = "RUNNING" if exit_code == 0 else "REVIEW_INFRA_FAILED"
    return AttemptResult(
        state=state,
        pid=process.pid,
        exit_code=exit_code,
        running_observed=running_observed,
        session_id=session_id,
        diagnostic="\n".join(diagnostics)[-4000:],
    )
