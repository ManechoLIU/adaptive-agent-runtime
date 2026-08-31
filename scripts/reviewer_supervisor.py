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

import argparse
import hashlib
import shutil
import time
import uuid


@dataclass(frozen=True)
class ReviewRunResult:
    run_id: str
    state: str
    reviewed_head: str
    verdict: Optional[dict]
    attempts: int
    state_path: Path


def _git_head(repo: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def _sha256(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_review(
    repo: Path,
    base: str,
    instructions: str,
    *,
    max_infra_retries: int = 1,
    attempt_runner=None,
    codex_executable: Optional[str] = None,
) -> ReviewRunResult:
    repo = Path(repo).resolve()
    head = _git_head(repo)
    run_id = uuid.uuid4().hex
    root = git_common_state_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / f"{run_id}.json"
    instruction_hash = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
    runner = attempt_runner
    codex = codex_executable or shutil.which("codex") or "codex"
    base_state = {
        "run_id": run_id,
        "repo": str(repo),
        "base": base,
        "candidate_head": head,
        "instructions_sha256": instruction_hash,
        "state": "STARTING",
        "started_at": time.time(),
        "retry_count": 0,
    }
    atomic_write_json(state_path, base_state)
    last_diag = ""
    for attempt in range(max_infra_retries + 1):
        event_path = root / f"{run_id}.attempt-{attempt}.events.jsonl"
        final_path = root / f"{run_id}.attempt-{attempt}.final.json"
        contract = ReviewContract(repo, base, head, instructions, event_path, final_path)
        if runner is None:
            result = run_attempt(contract, attempt, codex_executable=codex)
        else:
            result = runner(contract, attempt)
        snapshot = dict(base_state)
        snapshot.update({
            "retry_count": attempt,
            "pid": result.pid,
            "exit_code": result.exit_code,
            "session_id": result.session_id,
            "running_observed": result.running_observed,
            "event_sha256": _sha256(event_path),
            "final_sha256": _sha256(final_path),
        })
        if result.state == "REVIEW_INFRA_FAILED" or result.exit_code != 0 or not result.running_observed:
            last_diag = result.diagnostic or f"review process exit={result.exit_code} running_observed={result.running_observed}"
            snapshot.update({"state": "REVIEW_INFRA_FAILED", "diagnostic": last_diag[-4000:]})
            atomic_write_json(state_path, snapshot)
            continue
        try:
            if not final_path.exists() or not final_path.read_text(encoding="utf-8").strip():
                raise ValueError("final reviewer output missing or empty")
            payload = json.loads(final_path.read_text(encoding="utf-8"))
            verdict = validate_verdict(payload, head)
        except (ValueError, json.JSONDecodeError) as exc:
            last_diag = str(exc)
            snapshot.update({"state": "REVIEW_INFRA_FAILED", "diagnostic": last_diag[-4000:]})
            atomic_write_json(state_path, snapshot)
            continue
        state = verdict["verdict"]
        snapshot.update({"state": state, "verdict": verdict, "completed_at": time.time()})
        atomic_write_json(state_path, snapshot)
        return ReviewRunResult(run_id, state, head, verdict, attempt + 1, state_path)
    terminal = dict(base_state)
    terminal.update({
        "state": "REVIEW_INFRA_FAILED",
        "retry_count": max_infra_retries,
        "diagnostic": last_diag[-4000:],
        "completed_at": time.time(),
    })
    atomic_write_json(state_path, terminal)
    return ReviewRunResult(run_id, "REVIEW_INFRA_FAILED", head, None, max_infra_retries + 1, state_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Supervise a revision-bound Codex code review")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--repo", required=True)
    run.add_argument("--base", default="main")
    run.add_argument("--instructions", default="Review the exact candidate revision. Return only the required structured verdict JSON.")
    args = parser.parse_args(argv)
    result = run_review(Path(args.repo), args.base, args.instructions)
    print(json.dumps({"run_id": result.run_id, "state": result.state, "reviewed_head": result.reviewed_head, "state_path": str(result.state_path)}, ensure_ascii=False))
    return 0 if result.state == "PASS" else 10 if result.state == "FINDINGS" else 20


if __name__ == "__main__":
    raise SystemExit(main())
