#!/usr/bin/env python3
from pathlib import Path
import json
import os
import subprocess
import tempfile
import signal
import threading


def git_common_state_root(repo: Path) -> Path:
    repo = Path(repo).resolve()
    raw = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True
    ).strip()
    common = Path(raw)
    if not common.is_absolute():
        common = (repo / common).resolve()
    return common / "adaptive-delivery" / "reviewer-runs"


def build_review_instructions(user_instructions: str, expected_head: str, base_revision: str) -> str:
    schema_example = {
        "reviewed_head": expected_head,
        "verdict": "PASS",
        "critical": [],
        "important": [],
        "minor": [],
    }
    return (
        f"Act as an independent read-only code reviewer. Review exactly the immutable Git range {base_revision}..{expected_head}. "
        f"Inspect that exact range with Git and do not substitute mutable branch names or uncommitted content.\n"
        f"Additional review focus: {user_instructions.strip() or 'none'}\n\n"
        "Your final response MUST be ONLY one JSON object with exactly these keys. "
        "The verdict value is restricted to PASS or FINDINGS. Example shape:\n"
        + json.dumps(schema_example, ensure_ascii=False, indent=2)
        + "\nUse empty arrays when there are no findings at that severity. "
        "Use PASS only when critical, important, and minor are all empty; otherwise use FINDINGS. "
        "reviewed_head MUST equal the exact revision stated above. "
        "Do not wrap the JSON in markdown fences and do not include prose before or after it."
    )


def validate_verdict(payload: dict, expected_head: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("verdict payload must be an object")
    expected_keys = {"reviewed_head", "verdict", "critical", "important", "minor"}
    if set(payload) != expected_keys:
        raise ValueError("verdict payload exact keys required")
    if payload.get("reviewed_head") != expected_head:
        raise ValueError("reviewed_head does not match candidate HEAD")
    if payload.get("verdict") not in {"PASS", "FINDINGS"}:
        raise ValueError("verdict must be PASS or FINDINGS")
    for field in ("critical", "important", "minor"):
        if not isinstance(payload.get(field), list):
            raise ValueError(f"{field} must be a list")
        if not all(isinstance(item, str) and item.strip() for item in payload[field]):
            raise ValueError(f"{field} findings must be non-empty strings")
    finding_count = sum(len(payload[field]) for field in ("critical", "important", "minor"))
    if payload["verdict"] == "PASS" and finding_count:
        raise ValueError("PASS requires critical, important, and minor to be empty")
    if payload["verdict"] == "FINDINGS" and finding_count == 0:
        raise ValueError("FINDINGS requires at least one finding")
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
    retry_safe: bool = True


def _session_id_from_event(event: dict) -> Optional[str]:
    for key in ("thread_id", "session_id", "threadId", "sessionId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    payload = event.get("payload")
    if isinstance(payload, dict):
        return _session_id_from_event(payload)
    return None


def _capture_process_group(pid: int) -> int:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        # start_new_session=True guarantees the child was its own group leader.
        return pid


def _signal_process_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_for_group_exit(process_group_id: int, group_exists: Callable[[int], bool], grace_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while True:
        try:
            if not group_exists(process_group_id):
                return True
        except (OSError, RuntimeError, ValueError):
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _terminate_process_group(
    process,
    process_group_id: int,
    process_group_signaler: Callable[[int, int], None],
    process_group_exists: Callable[[int], bool],
    grace_seconds: float,
    *,
    send_term: bool = True,
) -> tuple[int, bool, str]:
    notes: list[str] = []
    leader_reaped = False
    exit_code = -signal.SIGTERM
    if send_term:
        try:
            process_group_signaler(process_group_id, signal.SIGTERM)
            notes.append("SIGTERM")
        except (OSError, RuntimeError, ValueError) as exc:
            return -signal.SIGTERM, False, f"SIGTERM signal failed: {exc}"
    try:
        exit_code = process.wait(timeout=grace_seconds)
        leader_reaped = True
    except subprocess.TimeoutExpired:
        notes.append("TERM grace expired")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        notes.append(f"wait after TERM failed: {exc}")

    group_gone = _wait_for_group_exit(process_group_id, process_group_exists, 0.0)
    if leader_reaped and group_gone:
        return exit_code, True, "; ".join(notes) or "already exited"

    try:
        process_group_signaler(process_group_id, signal.SIGKILL)
        notes.append("SIGKILL")
    except (OSError, RuntimeError, ValueError) as exc:
        return -signal.SIGKILL, False, "; ".join(notes + [f"SIGKILL signal failed: {exc}"])
    if not leader_reaped:
        try:
            exit_code = process.wait(timeout=grace_seconds)
            leader_reaped = True
        except subprocess.TimeoutExpired:
            return -signal.SIGKILL, False, "; ".join(notes + ["process could not be reaped"])
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            return -signal.SIGKILL, False, "; ".join(notes + [f"final reap failed: {exc}"])
    group_gone = _wait_for_group_exit(process_group_id, process_group_exists, grace_seconds)
    if not group_gone:
        return exit_code, False, "; ".join(notes + ["process group still alive after SIGKILL"])
    return exit_code, leader_reaped, "; ".join(notes)


def _review_output_schema(expected_head: str) -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["reviewed_head", "verdict", "critical", "important", "minor"],
        "properties": {
            "reviewed_head": {"type": "string", "enum": [expected_head]},
            "verdict": {"type": "string", "enum": ["PASS", "FINDINGS"]},
            "critical": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "important": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "minor": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
    }


def run_attempt(
    contract: ReviewContract,
    attempt: int,
    *,
    popen_factory: Callable = subprocess.Popen,
    codex_executable: str = "codex",
    timeout_seconds: float = 600.0,
    process_group_killer: Callable[[int, int], None] = _signal_process_group,
    process_group_getter: Callable[[int], int] = _capture_process_group,
    process_group_exists: Callable[[int], bool] = _process_group_exists,
    termination_grace_seconds: float = 5.0,
) -> AttemptResult:
    contract.event_path.parent.mkdir(parents=True, exist_ok=True)
    contract.final_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path = contract.final_path.with_name(contract.final_path.name + ".schema.json")
    atomic_write_json(schema_path, _review_output_schema(contract.head))
    argv = [
        codex_executable,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(schema_path),
        "--json",
        "-o",
        str(contract.final_path),
        "-",
    ]
    try:
        process = popen_factory(
            argv,
            cwd=str(contract.repo),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return AttemptResult(
            state="REVIEW_INFRA_FAILED",
            pid=-1,
            exit_code=127,
            running_observed=False,
            diagnostic=f"reviewer launch failed: {exc}",
        )
    try:
        process_group_id = process_group_getter(process.pid)
    except (OSError, RuntimeError, ValueError) as exc:
        # start_new_session=True makes the launched child's PID the session/process-group id.
        # Even if PGID lookup itself fails, use that invariant to clean up the started child.
        process_group_id = process.pid
        exit_code, retry_safe, cleanup = _terminate_process_group(
            process, process_group_id, process_group_killer, process_group_exists,
            termination_grace_seconds, send_term=True,
        )
        for stream in (getattr(process, "stdin", None), getattr(process, "stdout", None)):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError, AttributeError):
                pass
        return AttemptResult(
            state="REVIEW_INFRA_FAILED", pid=process.pid, exit_code=exit_code, running_observed=False,
            diagnostic=f"reviewer process-group capture failed: {exc}; cleanup={cleanup}", retry_safe=retry_safe,
        )
    if process.stdin is None:
        exit_code, retry_safe, cleanup = _terminate_process_group(
            process, process_group_id, process_group_killer, process_group_exists, termination_grace_seconds, send_term=True
        )
        return AttemptResult(
            state="REVIEW_INFRA_FAILED",
            pid=process.pid,
            exit_code=exit_code,
            running_observed=False,
            diagnostic=f"reviewer stdin pipe unavailable; cleanup={cleanup}",
            retry_safe=retry_safe,
        )

    observed = {"running": False, "session_id": None}
    diagnostics: list[str] = []
    writer_state = {"done": False, "error": None}

    def deliver_stdin() -> None:
        try:
            process.stdin.write(contract.instructions)
            process.stdin.flush()
            process.stdin.close()
            writer_state["done"] = True
        except (BrokenPipeError, OSError, ValueError) as exc:
            writer_state["error"] = exc
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    def consume_stdout() -> None:
        try:
            with contract.event_path.open("w", encoding="utf-8") as event_log:
                if process.stdout is None:
                    return
                for line in process.stdout:
                    event_log.write(line)
                    event_log.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        diagnostics.append(line.strip())
                        continue
                    if isinstance(event, dict):
                        observed["running"] = True
                        observed["session_id"] = observed["session_id"] or _session_id_from_event(event)
        except (OSError, ValueError) as exc:
            diagnostics.append(f"event stream failure: {exc}")

    writer = threading.Thread(target=deliver_stdin, name=f"reviewer-stdin-{attempt}", daemon=True)
    reader = threading.Thread(target=consume_stdout, name=f"reviewer-events-{attempt}", daemon=True)
    deadline = time.monotonic() + timeout_seconds
    writer.start()
    reader.start()

    remaining = max(0.0, deadline - time.monotonic())
    try:
        exit_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        exit_code, retry_safe, cleanup = _terminate_process_group(
            process, process_group_id, process_group_killer, process_group_exists, termination_grace_seconds, send_term=True
        )
        writer.join(0.05)
        reader.join(0.2)
        phase = "stdin/process" if not writer_state["done"] else "process"
        return AttemptResult(
            state="REVIEW_INFRA_FAILED",
            pid=process.pid,
            exit_code=exit_code,
            running_observed=bool(observed["running"]),
            session_id=observed["session_id"],
            diagnostic=f"reviewer {phase} timeout after {timeout_seconds:g}s; cleanup={cleanup}",
            retry_safe=retry_safe,
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        exit_code, retry_safe, cleanup = _terminate_process_group(
            process, process_group_id, process_group_killer, process_group_exists, termination_grace_seconds, send_term=True
        )
        writer.join(0.05)
        reader.join(0.2)
        return AttemptResult(
            state="REVIEW_INFRA_FAILED",
            pid=process.pid,
            exit_code=exit_code,
            running_observed=bool(observed["running"]),
            session_id=observed["session_id"],
            diagnostic=f"reviewer wait failed: {exc}; cleanup={cleanup}",
            retry_safe=retry_safe,
        )

    writer.join(0.2)
    reader.join(1.0)

    def post_wait_infra_failure(reason: str) -> AttemptResult:
        cleanup_exit, retry_safe, cleanup = _terminate_process_group(
            process, process_group_id, process_group_killer, process_group_exists,
            termination_grace_seconds, send_term=True,
        )
        writer.join(0.05)
        reader.join(0.2)
        return AttemptResult(
            state="REVIEW_INFRA_FAILED",
            pid=process.pid,
            exit_code=cleanup_exit,
            running_observed=bool(observed["running"]),
            session_id=observed["session_id"],
            diagnostic=f"{reason}; cleanup={cleanup}",
            retry_safe=retry_safe,
        )

    if writer_state["error"] is not None:
        return post_wait_infra_failure(f"reviewer stdin delivery failed: {writer_state['error']}")
    if not writer_state["done"]:
        return post_wait_infra_failure("reviewer stdin delivery did not complete before child exit")
    if reader.is_alive():
        return post_wait_infra_failure("reviewer event stream did not close after child exit")

    running_observed = bool(observed["running"])
    if not running_observed or exit_code != 0:
        return post_wait_infra_failure(
            f"review process exit={exit_code} running_observed={running_observed}"
        )
    if not _wait_for_group_exit(process_group_id, process_group_exists, 0.0):
        return post_wait_infra_failure(
            "reviewer leader exited successfully but process group still has live descendants"
        )
    return AttemptResult(
        state="RUNNING",
        pid=process.pid,
        exit_code=exit_code,
        running_observed=running_observed,
        session_id=observed["session_id"],
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


def _git_revision(repo: Path, ref: str) -> str:
    return subprocess.check_output(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo, text=True).strip()


def _git_head(repo: Path) -> str:
    return _git_revision(repo, "HEAD")


def _git_status(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo, text=True
    )


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
    timeout_seconds: float = 600.0,
) -> ReviewRunResult:
    repo = Path(repo).resolve()
    head = _git_head(repo)
    base_revision = _git_revision(repo, base)
    run_id = uuid.uuid4().hex
    root = git_common_state_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / f"{run_id}.json"
    review_instructions = build_review_instructions(instructions, head, base_revision)
    instruction_hash = hashlib.sha256(review_instructions.encode("utf-8")).hexdigest()
    runner = attempt_runner
    codex = codex_executable or shutil.which("codex") or "codex"
    base_state = {
        "run_id": run_id,
        "repo": str(repo),
        "base_ref": base,
        "base_revision": base_revision,
        "candidate_head": head,
        "instructions_sha256": instruction_hash,
        "state": "STARTING",
        "started_at": time.time(),
        "retry_count": 0,
    }
    dirty = _git_status(repo)
    if dirty:
        blocked = dict(base_state)
        blocked.update({
            "state": "REVIEW_INFRA_FAILED",
            "diagnostic": "dirty worktree; exact candidate review requires a clean worktree",
            "completed_at": time.time(),
        })
        atomic_write_json(state_path, blocked)
        return ReviewRunResult(run_id, "REVIEW_INFRA_FAILED", head, None, 0, state_path)

    atomic_write_json(state_path, base_state)
    last_diag = ""
    for attempt in range(max_infra_retries + 1):
        event_path = root / f"{run_id}.attempt-{attempt}.events.jsonl"
        final_path = root / f"{run_id}.attempt-{attempt}.final.json"
        contract = ReviewContract(repo, base_revision, head, review_instructions, event_path, final_path)
        try:
            if runner is None:
                result = run_attempt(
                    contract, attempt, codex_executable=codex, timeout_seconds=timeout_seconds
                )
            else:
                result = runner(contract, attempt)
        except Exception as exc:
            result = AttemptResult(
                state="REVIEW_INFRA_FAILED",
                pid=-1,
                exit_code=70,
                running_observed=False,
                diagnostic=f"review attempt infrastructure exception: {type(exc).__name__}: {exc}",
                retry_safe=False,
            )
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
        current_head = _git_head(repo)
        current_status = _git_status(repo)
        if current_head != head:
            last_diag = f"HEAD changed during review: expected {head}, observed {current_head}"
            snapshot.update({"state": "REVIEW_INFRA_FAILED", "diagnostic": last_diag})
            atomic_write_json(state_path, snapshot)
            continue
        if current_status:
            last_diag = "worktree became dirty during review; exact candidate binding invalidated"
            snapshot.update({"state": "REVIEW_INFRA_FAILED", "diagnostic": last_diag})
            atomic_write_json(state_path, snapshot)
            continue
        if result.state == "REVIEW_INFRA_FAILED" or result.exit_code != 0 or not result.running_observed:
            last_diag = result.diagnostic or f"review process exit={result.exit_code} running_observed={result.running_observed}"
            snapshot.update({"state": "REVIEW_INFRA_FAILED", "diagnostic": last_diag[-4000:], "retry_safe": result.retry_safe})
            atomic_write_json(state_path, snapshot)
            if not result.retry_safe:
                return ReviewRunResult(run_id, "REVIEW_INFRA_FAILED", head, None, attempt + 1, state_path)
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
    run.add_argument("--instructions", default="Review correctness, migration safety, runtime safety, and test coverage.")
    run.add_argument("--timeout-seconds", type=float, default=600.0)
    args = parser.parse_args(argv)
    result = run_review(Path(args.repo), args.base, args.instructions, timeout_seconds=args.timeout_seconds)
    print(json.dumps({"run_id": result.run_id, "state": result.state, "reviewed_head": result.reviewed_head, "state_path": str(result.state_path)}, ensure_ascii=False))
    return 0 if result.state == "PASS" else 10 if result.state == "FINDINGS" else 20


if __name__ == "__main__":
    raise SystemExit(main())
