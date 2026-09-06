#!/usr/bin/env python3
from __future__ import annotations

import json
import fcntl
import re
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Callable

try:
    import controller_target_guard as target_guard
except ModuleNotFoundError:
    from scripts import controller_target_guard as target_guard

DEFAULT_REGISTRY = Path.home() / ".codex" / "adaptive-delivery-controllers.json"
DEFAULT_WEB_LEASES = Path.home() / ".codex" / "adaptive-delivery-web-controller-leases.json"
MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_TIMEOUT_SECONDS = 8


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve_reentry_session(
    *, controller_id: str, repo: Path, registry_path: Path = DEFAULT_REGISTRY,
    lease_path: Path = DEFAULT_WEB_LEASES, now_unix: int | None = None,
) -> str:
    repo = Path(repo).expanduser().resolve()
    registry = _load_json(Path(registry_path).expanduser())
    if registry.get(controller_id) != str(repo):
        raise PermissionError("registered Controller repository does not match Web re-entry repository")
    sessions = registry.get("__controller_sessions__")
    controller_sessions = sessions.get(controller_id) if isinstance(sessions, dict) else None
    web_sessions = controller_sessions.get("web") if isinstance(controller_sessions, dict) else None
    if isinstance(web_sessions, str):
        web_sessions = [web_sessions]
    bound = {value for value in web_sessions or [] if isinstance(value, str) and value.strip()}

    payload = _load_json(Path(lease_path).expanduser())
    leases = payload.get("leases")
    record = leases.get(controller_id) if isinstance(leases, dict) else None
    if not isinstance(record, dict):
        raise PermissionError("current Web Controller resume lease is missing")
    if record.get("controller_id") not in (None, controller_id):
        raise PermissionError("Web re-entry lease belongs to another Controller")
    if record.get("repo") != str(repo):
        raise PermissionError("Web re-entry lease repository does not match registered Controller")
    if record.get("provenance") != "manual_user_authorized" or record.get("mode") != "resume_only":
        raise PermissionError("Web re-entry requires a manual resume_only Controller lease")
    expires_at = record.get("expires_at_unix")
    now = int(time.time()) if now_unix is None else int(now_unix)
    if not isinstance(expires_at, int) or expires_at <= now:
        raise PermissionError("Web re-entry lease is expired")
    session_id = record.get("web_session_id")
    if not isinstance(session_id, str) or not session_id.strip() or session_id not in bound:
        raise PermissionError("Web re-entry lease session is not bound to the registered Controller")
    session_id = session_id.strip()
    target = target_guard.target_record(
        registry, controller_id=controller_id, host="web"
    )
    if (
        isinstance(target, dict)
        and target.get("provenance") == "host_attested_same_controller_recovery"
        and target.get("identity_proof") != "host_attested_origin"
    ):
        raise PermissionError(
            "legacy browser-tab Web identity record is not a trusted Host origin attestation"
        )
    if target is None:
        raise PermissionError(
            "Web re-entry requires an explicit canonical current Web target"
        )
    elif (
        target_guard.active_source_controller_id(
            registry, source_session_id=session_id, host="web"
        )
        != controller_id
    ):
        raise PermissionError(
            "Web re-entry lease session is not the current verified Controller entry"
        )
    ownership = target_guard.execution_ownership_record(
        registry, controller_id=controller_id
    )
    if ownership is None:
        raise PermissionError("canonical Controller execution ownership is missing")
    active_host, ownership_target, _ownership_generation = (
        target_guard.validate_execution_ownership_record(ownership)
    )
    if active_host != "web" or ownership_target != session_id:
        raise PermissionError(
            "Web re-entry lease does not match canonical Controller execution ownership"
        )
    return session_id


def _loopback_mcp_endpoint_live(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
            return False
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.35):
            return True
    except (OSError, ValueError):
        return False


def discover_ai_bridge_mcp_url(
    *,
    ps_text: str | None = None,
    endpoint_probe: Callable[[str], bool] | None = None,
) -> str:
    live_process_scan = ps_text is None
    if ps_text is None:
        ps_text = subprocess.check_output(["/bin/ps", "aux"], text=True)
    matches = re.findall(
        r"--mcp\.server-url\s+(?:url=)?(http://127\.0\.0\.1:\d+/mcp/[^\s]+)",
        str(ps_text),
    )
    unique = list(dict.fromkeys(matches))
    probe = endpoint_probe or (_loopback_mcp_endpoint_live if live_process_scan else None)
    if probe is not None:
        unique = [url for url in unique if probe(url)]
        if len(unique) != 1:
            raise RuntimeError("exactly one live local AI-Bridge MCP endpoint is required")
    elif len(unique) != 1:
        raise RuntimeError("exactly one local AI-Bridge MCP endpoint is required")
    return unique[0]


def _decode_mcp_body(body: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            value = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    if events:
        return events[-1]
    value = json.loads(body)
    if not isinstance(value, dict):
        raise RuntimeError("AI-Bridge MCP returned a non-object response")
    return value


class _McpSession:
    def __init__(self, url: str) -> None:
        self.url = url
        self.session_id: str | None = None
        self._next_id = 1
        init = self._post({
            "jsonrpc": "2.0", "id": self._id(), "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "adaptive-agent-runtime-web-reentry", "version": "1"},
            },
        }, allow_session_header=True)
        if "error" in init:
            raise RuntimeError("AI-Bridge MCP initialize failed")
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except Exception:
            pass

    def _id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _post(self, payload: dict[str, Any], *, allow_session_header: bool = False) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=MCP_TIMEOUT_SECONDS) as response:
            if allow_session_header:
                self.session_id = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
            return _decode_mcp_body(response.read().decode("utf-8", "replace"))

    def browser(self, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self._post({
            "jsonrpc": "2.0", "id": self._id(), "method": "tools/call",
            "params": {"name": "browser", "arguments": arguments},
        })
        if "error" in response:
            raise RuntimeError("AI-Bridge browser tool call failed")
        result = response.get("result")
        if not isinstance(result, dict) or result.get("isError") is True:
            raise RuntimeError("AI-Bridge browser tool returned an error")
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise RuntimeError("AI-Bridge browser tool omitted structured result")
        payload: Any = structured
        # Native tool surface wraps one or two {ok,result} layers. Peel them safely.
        for _ in range(3):
            if isinstance(payload, dict) and payload.get("ok") is True and isinstance(payload.get("result"), dict):
                payload = payload["result"]
            else:
                break
        return payload if isinstance(payload, dict) else {}


def _default_browser_call(arguments: dict[str, Any]) -> dict[str, Any]:
    client = _McpSession(discover_ai_bridge_mcp_url())
    return client.browser(arguments)


def _tab_for_session(tabs: list[Any], web_session_id: str) -> dict[str, Any] | None:
    suffix = f"/c/{web_session_id}"
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        url = str(tab.get("url") or "")
        if url.startswith("https://chatgpt.com/") and (url.endswith(suffix) or f"{suffix}?" in url):
            return tab
    return None


def _active_response(nodes: list[Any]) -> bool:
    stop_names = ("stop generating", "stop streaming", "stop response", "停止生成", "停止响应")
    for node in nodes:
        if not isinstance(node, dict) or str(node.get("role") or "").lower() != "button":
            continue
        name = str(node.get("name") or "").strip().lower()
        if any(marker in name for marker in stop_names):
            return True
    return False


def _composer_node(nodes: list[Any]) -> str | None:
    candidates: list[dict[str, Any]] = []
    for node in nodes:
        if isinstance(node, dict) and str(node.get("role") or "").lower() == "textbox":
            candidates.append(node)
    for node in candidates:
        name = str(node.get("name") or "").lower()
        if "chatgpt" in name or "message" in name or "消息" in name:
            value = node.get("node_id")
            return value if isinstance(value, str) and value else None
    for node in candidates:
        value = node.get("node_id")
        if isinstance(value, str) and value:
            return value
    return None


def verify_ai_bridge_web_destination_tab_consistency(
    *,
    controller_id: str,
    host: str,
    expected_target_session_id: str,
    expected_target_generation: int,
    expected_target_mode: str | None,
    host_execution_receipt: Any,
    adapter_attempt: Any,
    browser_call: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> bool:
    """Check only that a destination tab still matches caller-observed browser data.

    This is not Host identity attestation and cannot satisfy the pre-delivery
    origin verifier contract. Browser tab, URL, and DOM state are mutable
    destination consistency evidence only.
    """
    del controller_id, expected_target_generation, adapter_attempt
    if host != "web":
        return False
    session_id = str(expected_target_session_id or "").strip()
    if not session_id:
        return False
    if str(expected_target_mode or "").strip() not in {
        "same_controller_session_recovery",
        "web_lease",
        "explicit_current",
        "canonical_host_ownership",
    }:
        return False
    if not isinstance(host_execution_receipt, dict):
        return False
    if str(host_execution_receipt.get("host") or "").strip() != "web":
        return False
    receipt_session = str(
        host_execution_receipt.get("web_session_id")
        or host_execution_receipt.get("session_id")
        or ""
    ).strip()
    if receipt_session != session_id:
        return False
    if str(host_execution_receipt.get("source") or "").strip() != "ai_bridge_browser":
        return False
    receipt_tab_id = str(host_execution_receipt.get("tab_id") or "").strip()
    if not receipt_tab_id:
        return False
    receipt_url = str(host_execution_receipt.get("url") or "").strip()

    call = browser_call or _default_browser_call
    try:
        listed = call({"action": "list_tabs"})
    except Exception:
        return False
    tabs = listed.get("tabs") if isinstance(listed, dict) else None
    tabs = tabs if isinstance(tabs, list) else []
    tab = _tab_for_session(tabs, session_id)
    if tab is None:
        return False
    if str(tab.get("tab_id") or "").strip() != receipt_tab_id:
        return False
    live_url = str(tab.get("url") or "").strip()
    if receipt_url and live_url != receipt_url:
        return False
    return True


def build_reentry_prompt(
    *, controller_id: str, lifecycle_state: dict[str, Any], terminal_receipts: list[Any] | None = None,
) -> str:
    generation = int(lifecycle_state.get("wake_generation", 0) or 0)
    next_action = str(lifecycle_state.get("next_action") or "").strip()
    prompt = (
        "Adaptive Agent Runtime Web re-entry checkpoint. Continue this existing registered Web Controller "
        f"only (controller_id={controller_id}); do not create or fork another controller and do not invoke "
        "desktop Codex merely to continue this Web-hosted controller. Read the current authoritative project "
        "state and lifecycle, reconcile the pending control event, and keep rolling the open Goal while "
        "requires_user=false. Before any new delegation, recompute the current DAG / READY / WIP projection "
        "from authoritative project facts and apply the canonical route policy again, including the selected "
        "provider/model; do not reuse the most recent executor merely because it is convenient. "
        "Do not create a new ChatGPT Web child as a shortcut. A Web child may be spawned only after a "
        "prepared canonical Web dispatch exists and the Runtime can verify its machine event source; otherwise "
        "use the canonical selected non-Web executor or remain blocked with explicit evidence. "
        "Stop only for a closed lifecycle, an explicit user decision, or verified blocking "
        f"evidence. Wake generation={generation}."
    )
    triggers = [
        str(value).strip()
        for value in lifecycle_state.get("triggers", [])
        if str(value).strip()
    ] if isinstance(lifecycle_state.get("triggers"), list) else []
    if triggers:
        prompt += " Current lifecycle triggers: " + "; ".join(triggers[:16]) + "."
    if next_action:
        prompt += f" Persisted next action: {next_action}."
    receipts = [str(value) for value in terminal_receipts or [] if str(value).strip()]
    if receipts:
        prompt += " Pending terminal receipts: " + "; ".join(receipts[:8]) + "."
    return prompt[:7800]


def _execute_web_reentry_under_registry_fence(
    *,
    controller_id: str,
    repo: Path,
    lifecycle_state: dict[str, Any],
    registry_path: Path = DEFAULT_REGISTRY,
    lease_path: Path = DEFAULT_WEB_LEASES,
    browser_call: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    approval_id: str | None = None,
) -> dict[str, Any]:
    if lifecycle_state.get("pending_control_event") is not True:
        return {"operation": "web_reentry", "result": "DEFERRED", "state": "WEB_REENTRY_CLOSED", "returncode": 0}
    if lifecycle_state.get("requires_user") is True:
        return {
            "operation": "web_reentry", "result": "DEFERRED", "state": "WAITING_USER", "returncode": 0,
            "failure_class": "user_decision_required",
        }
    try:
        web_session_id = resolve_reentry_session(
            controller_id=controller_id, repo=Path(repo), registry_path=Path(registry_path), lease_path=Path(lease_path)
        )
        registry = _load_json(Path(registry_path).expanduser())
        target = target_guard.target_record(
            registry, controller_id=controller_id, host="web"
        )
        if target is None:
            target_generation = 0
            target_mode = "legacy_canonical"
        else:
            _status, _target_session, target_generation = (
                target_guard.validate_target_record(target, host="web")
            )
            target_mode = "explicit_current"
        ownership = target_guard.execution_ownership_record(
            registry, controller_id=controller_id
        )
        if ownership is None:
            raise PermissionError("canonical Controller execution ownership is missing")
        _ownership_host, _ownership_target, ownership_generation = (
            target_guard.validate_execution_ownership_record(ownership)
        )
    except (OSError, ValueError, PermissionError) as exc:
        return {
            "operation": "web_reentry", "result": "DEFERRED", "state": "WEB_REENTRY_IDENTITY_UNAVAILABLE",
            "returncode": 78, "failure_class": "web_reentry_identity_unavailable",
            "error_code": "WEB_REENTRY_IDENTITY_UNAVAILABLE", "stderr_tail": str(exc)[:1024],
        }

    if browser_call is None:
        client = _McpSession(discover_ai_bridge_mcp_url())
        call = client.browser
    else:
        call = browser_call
    try:
        listed = call({"action": "list_tabs"})
        tabs = listed.get("tabs") if isinstance(listed, dict) else None
        tabs = tabs if isinstance(tabs, list) else []
        tab = _tab_for_session(tabs, web_session_id)
        if tab is None:
            open_args: dict[str, Any] = {"action": "new_tab", "url": f"https://chatgpt.com/c/{web_session_id}"}
            if isinstance(approval_id, str) and approval_id.strip():
                open_args["approval_id"] = approval_id.strip()
            opened = call(open_args)
            if isinstance(opened, dict) and opened.get("state") == "waiting_for_local_approval":
                waiting_id = opened.get("approval_id")
                expires_at = opened.get("expires_at_unix")
                return {
                    "operation": "web_reentry", "result": "DEFERRED",
                    "state": "WEB_REENTRY_WAITING_LOCAL_APPROVAL", "returncode": 0,
                    "failure_class": "local_approval_required",
                    "approval_id": waiting_id if isinstance(waiting_id, str) else None,
                    "approval_expires_at_unix": expires_at if isinstance(expires_at, int) else None,
                    "execution_target_session_id": web_session_id,
                    "target_generation": target_generation,
                    "ownership_generation": ownership_generation,
                    "target_mode": target_mode,
                }
            tab_id = str(opened.get("tab_id") or "").strip() if isinstance(opened, dict) else ""
            if not tab_id:
                raise RuntimeError("AI-Bridge did not return a tab id for the leased ChatGPT conversation")
        else:
            tab_id = str(tab.get("tab_id") or "").strip()
            if not tab_id:
                raise RuntimeError("leased ChatGPT tab has no tab id")
            call({"action": "focus_tab", "tab_id": tab_id})
        snapshot = call({"action": "snapshot", "tab_id": tab_id})
        nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else None
        nodes = nodes if isinstance(nodes, list) else []
        if _active_response(nodes):
            return {
                "operation": "web_reentry", "result": "DEFERRED", "state": "WEB_REENTRY_DEFERRED_ACTIVE",
                "returncode": 0, "failure_class": "web_host_active",
                "execution_target_session_id": web_session_id,
                "target_generation": target_generation,
                "ownership_generation": ownership_generation,
                "target_mode": target_mode,
            }
        composer = _composer_node(nodes)
        if not composer:
            raise RuntimeError("ChatGPT composer is unavailable in the leased Web Controller conversation")
        prompt = build_reentry_prompt(
            controller_id=controller_id,
            lifecycle_state=lifecycle_state,
            terminal_receipts=lifecycle_state.get("pending_terminal_receipts", []),
        )
        current_registry = _load_json(Path(registry_path).expanduser())
        current_target = target_guard.target_record(
            current_registry, controller_id=controller_id, host="web"
        )
        current_ownership = target_guard.execution_ownership_record(
            current_registry, controller_id=controller_id
        )
        if current_target is None or current_ownership is None:
            raise PermissionError(
                "canonical Web target or execution ownership disappeared before submit"
            )
        current_status, current_session, current_generation = (
            target_guard.validate_target_record(current_target, host="web")
        )
        current_host, current_ownership_target, current_ownership_generation = (
            target_guard.validate_execution_ownership_record(current_ownership)
        )
        if (
            current_status != "active"
            or current_session != web_session_id
            or current_generation != target_generation
            or current_host != "web"
            or current_ownership_target != web_session_id
            or current_ownership_generation != ownership_generation
        ):
            return {
                "operation": "web_reentry",
                "result": "DEFERRED",
                "state": "WEB_REENTRY_SUPERSEDED_TARGET",
                "returncode": 0,
                "failure_class": "controller_target_superseded",
                "error_code": "CONTROLLER_TARGET_SUPERSEDED",
                "execution_target_session_id": web_session_id,
                "target_generation": target_generation,
                "ownership_generation": ownership_generation,
                "target_mode": target_mode,
            }
        submitted = call({
            "action": "type", "tab_id": tab_id, "node_id": composer, "text": prompt, "submit": True,
        })
        if isinstance(submitted, dict) and submitted.get("ok") is False:
            raise RuntimeError("AI-Bridge browser did not confirm Web re-entry submission")
        host_receipt: dict[str, Any] = {
            "host": "web",
            "web_session_id": web_session_id,
            "tab_id": tab_id,
            "source": "ai_bridge_browser",
            "submitted": True,
        }
        observed_url = str((tab or {}).get("url") or "").strip() if tab is not None else ""
        if observed_url:
            host_receipt["url"] = observed_url
        return {
            "operation": "web_reentry", "result": "CONFIRMED", "state": "WEB_REENTRY_SUBMITTED",
            "returncode": 0, "controller_id": controller_id,
            "execution_target_session_id": web_session_id,
            "target_generation": target_generation,
            "ownership_generation": ownership_generation,
            "target_mode": target_mode, "pending_control_event": True,
            "host_execution_receipt": host_receipt,
        }
    except Exception as exc:
        return {
            "operation": "web_reentry", "result": "DEFERRED", "state": "WEB_REENTRY_PENDING",
            "returncode": 78, "failure_class": "web_reentry_unavailable",
            "error_code": "WEB_REENTRY_UNAVAILABLE", "stderr_tail": str(exc)[:1024],
            "execution_target_session_id": web_session_id,
            "target_generation": target_generation,
            "ownership_generation": ownership_generation,
            "target_mode": target_mode,
        }


def execute_web_reentry(
    *,
    controller_id: str,
    repo: Path,
    lifecycle_state: dict[str, Any],
    registry_path: Path = DEFAULT_REGISTRY,
    lease_path: Path = DEFAULT_WEB_LEASES,
    browser_call: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    approval_id: str | None = None,
) -> dict[str, Any]:
    registry_path = Path(registry_path).expanduser()
    lock_path = target_guard.registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            return _execute_web_reentry_under_registry_fence(
                controller_id=controller_id,
                repo=repo,
                lifecycle_state=lifecycle_state,
                registry_path=registry_path,
                lease_path=lease_path,
                browser_call=browser_call,
                approval_id=approval_id,
            )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
