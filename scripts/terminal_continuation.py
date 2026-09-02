#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
_added_skill_root = False
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))
    _added_skill_root = True
try:
    from scripts import lifecycle_hook as lifecycle
    from scripts import web_lifecycle_bridge as web_bridge
finally:
    if _added_skill_root:
        try:
            sys.path.remove(str(SKILL_ROOT))
        except ValueError:
            pass


def _load_terminal_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"terminal receipt is unreadable: {exc}") from exc
    if not isinstance(value, dict) or value.get("event_type") != "external_agent_terminal":
        raise ValueError("terminal receipt must be an external_agent_terminal object")
    return value


def consume_terminal_receipt(
    *,
    repo: Path,
    receipt_path: Path,
    registry_path: Path = web_bridge.DEFAULT_REGISTRY,
    wake_dispatcher: Callable[..., dict[str, Any] | None] | None = None,
    codex: str = "/opt/homebrew/bin/codex",
    dispatch_wake: bool = True,
) -> dict[str, Any]:
    repo = Path(repo).expanduser().resolve()
    receipt_path = Path(receipt_path).expanduser().resolve()
    registry_path = Path(registry_path).expanduser()
    receipt = _load_terminal_receipt(receipt_path)

    receipt_repo = str(receipt.get("repo") or "").strip()
    if not receipt_repo:
        raise ValueError("terminal receipt repository identity is required")
    try:
        if web_bridge._git_common_dir(Path(receipt_repo).expanduser().resolve()) != web_bridge._git_common_dir(repo):
            raise PermissionError("terminal receipt repository does not match continuation repository")
    except Exception as exc:
        if isinstance(exc, PermissionError):
            raise
        raise ValueError(f"cannot verify terminal receipt repository: {exc}") from exc

    controller_id = web_bridge._registered_controller_for_common_dir(repo, registry_path)
    if not controller_id:
        raise PermissionError("terminal continuation requires exactly one registered Controller")
    registry = web_bridge.load_json(registry_path)
    registered_repo = registry.get(controller_id)
    if not isinstance(registered_repo, str) or not registered_repo.strip():
        raise PermissionError("registered Controller repository is missing")
    controller_repo = Path(registered_repo).expanduser().resolve()

    snapshot = lifecycle.project_snapshot(controller_repo)
    if snapshot is None:
        raise RuntimeError("cannot snapshot registered Controller repository")
    state_path = lifecycle.state_path(controller_id)
    prior_state = lifecycle.load_json(state_path)
    controller_host = str(prior_state.get("controller_host") or "desktop_codex").strip()
    if controller_host not in {"web", "desktop_codex"}:
        controller_host = "desktop_codex"
    agent_id = str(receipt.get("agent_id") or "").strip() or f"external:{receipt_path.stem}"
    event = {
        "hook_event_name": "SubagentStop",
        "session_id": controller_id,
        "controller_host": controller_host,
        "event_source": "external_agent_terminal",
        "agent_id": agent_id,
        "cwd": str(controller_repo),
        "terminal_receipt": str(receipt_path),
    }
    _, lifecycle_state = lifecycle.evaluate_event(event, snapshot=snapshot, prior_state=prior_state)
    lifecycle.write_json(state_path, lifecycle_state)

    wake_receipt = None
    if dispatch_wake:
        dispatcher = wake_dispatcher or web_bridge.dispatch_pending_lifecycle_wake
        wake_receipt = dispatcher(
            lifecycle_state=lifecycle_state,
            session_id=controller_id,
            repo=controller_repo,
            registry=registry_path,
            codex=codex,
        )
    return {
        "controller_id": controller_id,
        "terminal_receipt": str(receipt_path),
        "lifecycle_state_path": str(state_path),
        "wake_result": wake_receipt,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Continue the existing Controller from a durable external-agent terminal receipt")
    subparsers = parser.add_subparsers(dest="command", required=True)
    consume = subparsers.add_parser("consume")
    consume.add_argument("--repo", required=True)
    consume.add_argument("--receipt", required=True)
    consume.add_argument("--registry", default=str(web_bridge.DEFAULT_REGISTRY))
    consume.add_argument("--codex", default="/opt/homebrew/bin/codex")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "consume":
        return 2
    wake_child = os.environ.get("AD_TERMINAL_CONTINUATION_WAKE_CHILD") == "1"
    try:
        result = consume_terminal_receipt(
            repo=Path(args.repo),
            receipt_path=Path(args.receipt),
            registry_path=Path(args.registry),
            codex=args.codex,
            dispatch_wake=wake_child,
        )
    except (OSError, ValueError, PermissionError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 78
    print(json.dumps(result, ensure_ascii=False))
    if wake_child:
        # The durable receipt + lifecycle pending state already exist. A legitimate
        # DEFERRED/NOOP wake must not rewrite the external Agent's completed outcome.
        return 0
    env = dict(os.environ)
    env["AD_TERMINAL_CONTINUATION_WAKE_CHILD"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "consume", "--repo", args.repo,
             "--receipt", args.receipt, "--registry", args.registry, "--codex", args.codex],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        # The pending lifecycle state remains durable for the existing Supervisor's next
        # observation; do not turn a completed Agent into a failed Assignment attempt.
        print(f"terminal wake launch deferred: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
