#!/usr/bin/env python3
"""Parse structured local collaboration lifecycle events for Runtime-managed Web agents."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("payload")
    return value if isinstance(value, dict) else {}


def structured_subagent_events(
    paths: Iterable[str | Path], *, max_tail_bytes: int = 4 * 1024 * 1024
) -> list[dict[str, Any]]:
    """Return bounded machine lifecycle observations; never parse UI/DOM text or whole unbounded logs."""
    if max_tail_bytes < 4096:
        raise ValueError("structured event tail must be at least 4096 bytes")
    calls: dict[str, dict[str, Any]] = {}
    output: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                start = max(0, size - max_tail_bytes)
                handle.seek(start)
                raw = handle.read(max_tail_bytes)
            if start:
                newline = raw.find(b"\n")
                raw = raw[newline + 1:] if newline >= 0 else b""
            lines = raw.decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            payload = _payload(record)
            if (
                payload.get("type") == "function_call"
                and payload.get("namespace") == "collaboration"
                and payload.get("name") == "spawn_agent"
            ):
                call_id = str(payload.get("call_id") or "").strip()
                if not call_id:
                    continue
                raw_args = payload.get("arguments")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
                except (json.JSONDecodeError, TypeError, ValueError):
                    args = {}
                calls[call_id] = {
                    "call_id": call_id,
                    "task_name": str(args.get("task_name") or "").strip(),
                    "agent_type": str(args.get("agent_type") or "").strip(),
                    "model": str(args.get("model") or "").strip(),
                    "timestamp": str(record.get("timestamp") or ""),
                    "source_path": str(path.resolve()),
                    "line_number": line_number,
                }
                continue
            if payload.get("type") != "item_completed":
                continue
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") != "SubAgentActivity":
                continue
            kind = str(item.get("kind") or "").strip().lower()
            conversation_id = str(item.get("agent_thread_id") or "").strip()
            if not conversation_id or kind not in {"started", "completed", "failed", "interrupted", "cancelled", "disconnected"}:
                continue
            item_id = str(item.get("id") or "").strip()
            spawn = calls.get(item_id, {}) if kind == "started" else {}
            output.append({
                "source": "collaboration_session_event",
                "kind": kind,
                "conversation_id": conversation_id,
                "observation_id": item_id or f"{path.name}:{line_number}:{kind}:{conversation_id}",
                "call_id": str(spawn.get("call_id") or ""),
                "task_name": str(spawn.get("task_name") or ""),
                "agent_type": str(spawn.get("agent_type") or ""),
                "model": str(spawn.get("model") or ""),
                "agent_path": str(item.get("agent_path") or "").strip(),
                "timestamp": str(record.get("timestamp") or ""),
                "source_path": str(path.resolve()),
                "line_number": line_number,
            })
    return output


def discover_recent_session_paths(
    *,
    since_values: Iterable[str],
    now: Any | None = None,
    roots: Iterable[str | Path] | None = None,
    max_paths: int = 256,
) -> list[Path]:
    """Find recent structured session logs by mtime; never infer lifecycle from UI text."""
    from datetime import datetime, timedelta, timezone
    UTC = timezone.utc
    now = now or datetime.now(UTC)
    candidates = []
    parsed = []
    for value in since_values:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        parsed.append(dt.astimezone(UTC))
    floor = min(parsed) - timedelta(minutes=5) if parsed else now.astimezone(UTC) - timedelta(hours=24)
    session_roots = tuple(roots) if roots is not None else (
        Path.home() / ".codex" / "sessions",
        Path.home() / ".codex" / "archived_sessions",
    )
    floor_ts = floor.timestamp()
    for raw_root in session_roots:
        root = Path(raw_root).expanduser()
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= floor_ts:
                candidates.append((stat.st_mtime, path.resolve()))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates[:max_paths]]
