#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


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
    registry = load_json(registry_path)
    matches = [
        session_id
        for session_id, value in registry.items()
        if isinstance(value, str) and Path(value).expanduser().resolve() == repo
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one registered controller for {repo}, found {len(matches)}"
        )
    return matches[0]


def post_tool_event(
    *,
    session_id: str,
    repo: Path,
    command: str,
    exit_code: int | None = None,
    output: str = "",
    turn_id: str = "web-ai-bridge",
) -> dict[str, Any]:
    response: dict[str, Any] = {"output": output}
    if exit_code is not None:
        response["exit_code"] = exit_code
    return {
        "hook_event_name": "PostToolUse",
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
    receipt: dict[str, Any], *, session_id: str, repo: Path
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
    )


def audit_receipts_from_cursor(audit_log: Path, cursor_path: Path) -> list[dict[str, Any]]:
    state = load_json(cursor_path)
    try:
        stat = audit_log.stat()
    except OSError:
        return []
    inode = int(state.get("inode", 0) or 0)
    offset = int(state.get("offset", 0) or 0)
    if inode != stat.st_ino or offset < 0 or offset > stat.st_size:
        offset = 0
    receipts: list[dict[str, Any]] = []
    with audit_log.open("rb") as handle:
        handle.seek(offset)
        for raw_line in handle:
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                receipts.append(value)
        next_offset = handle.tell()
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(
        json.dumps({"inode": stat.st_ino, "offset": next_offset}, indent=2) + "\n",
        encoding="utf-8",
    )
    return receipts


def successful_guard_event_from_receipt(
    receipt: dict[str, Any], *, session_id: str, repo: Path
) -> dict[str, Any] | None:
    event = translate_receipt(receipt, session_id=session_id, repo=repo)
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


def zshenv_block() -> str:
    script = Path(__file__).resolve()
    python = Path("/Library/Frameworks/Python.framework/Versions/3.11/bin/python3")
    return f'''# >>> adaptive-delivery web lifecycle bridge >>>
_ad_web_parent=$(/bin/ps -p "$PPID" -o command= 2>/dev/null)
if [[ "$_ad_web_parent" == *"{AI_BRIDGE_EXECUTABLE}"* ]]; then
  _ad_web_cwd="$PWD"
  _ad_web_command="$ZSH_EXECUTION_STRING"
  _ad_web_bridge_script="{script}"
  _ad_web_bridge_python="{python}"
  _ad_web_lifecycle_exit() {{
    local _ad_web_exit_code=$?
    trap - EXIT
    "$_ad_web_bridge_python" "$_ad_web_bridge_script" post-shell \\
      --cwd "$_ad_web_cwd" \\
      --command "$_ad_web_command" \\
      --exit-code "$_ad_web_exit_code" || true
    exit "$_ad_web_exit_code"
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

    post_shell = subparsers.add_parser("post-shell")
    post_shell.add_argument("--cwd", required=True)
    post_shell.add_argument("--command", required=True)
    post_shell.add_argument("--exit-code", required=True, type=int)
    post_shell.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    post_shell.add_argument("--capture-event")

    audit_once = subparsers.add_parser("audit-once")
    audit_once.add_argument("--session-id", required=True)
    audit_once.add_argument("--repo", required=True)
    audit_once.add_argument("--audit-log", default=str(DEFAULT_AUDIT_LOG))
    audit_once.add_argument("--cursor", required=True)
    audit_once.add_argument("--capture-events")

    subparsers.add_parser("print-zshenv-block")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command_name == "translate-receipt":
        repo = Path(args.repo).expanduser().resolve()
        try:
            receipt = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(f"invalid receipt JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(receipt, dict):
            print("receipt must be a JSON object", file=sys.stderr)
            return 2
        event = translate_receipt(receipt, session_id=args.session_id, repo=repo)
        if event is not None:
            print(json.dumps(event, ensure_ascii=False))
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
        event = post_tool_event(
            session_id=session_id,
            repo=repo,
            command=args.command,
            exit_code=args.exit_code,
        )
        if args.capture_event:
            Path(args.capture_event).write_text(
                json.dumps(event, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return 0
        return dispatch_event(event)

    if args.command_name == "audit-once":
        repo = Path(args.repo).expanduser().resolve()
        receipts = audit_receipts_from_cursor(
            Path(args.audit_log).expanduser(), Path(args.cursor).expanduser()
        )
        for receipt in receipts:
            event = successful_guard_event_from_receipt(
                receipt, session_id=args.session_id, repo=repo
            )
            if event is None:
                continue
            if args.capture_events:
                append_captured_event(Path(args.capture_events), event)
            else:
                dispatch_event(event)
        return 0

    if args.command_name == "print-zshenv-block":
        print(zshenv_block())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
