#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any

ROUTE_DECISIONS = {"default", "safe_fallback", "controller_exception"}
ROUTE_CLASS_MARKERS = {
    "backend": ("backend", "后端", "server"),
    "frontend": ("frontend", "前端", "web", "小程序", "miniapp"),
}


def _traceable_runtime_evidence(value: Any) -> bool:
    token = str(value or "").strip()
    if ":" not in token:
        return False
    scheme, locator = token.split(":", 1)
    return scheme in {"receipt", "artifact"} and bool(locator.strip())


def derived_route_classes(owned_files: Any) -> set[str]:
    if not isinstance(owned_files, list):
        return set()
    classes: set[str] = set()
    for raw in owned_files:
        path = str(raw or "").strip().replace(chr(92), "/").casefold().lstrip("./")
        if not path:
            continue
        parts = {part for part in path.split("/") if part}
        if (
            path.startswith("apps/web/")
            or path.startswith("apps/miniapp/")
            or "frontend" in parts
            or "miniapp" in parts
        ):
            classes.add("frontend")
        if (
            path.startswith("apps/server/")
            or "backend" in parts
            or "server" in parts
        ):
            classes.add("backend")
    return classes


def route_scope_errors(task_id: str, owned_files: Any, route: Any) -> list[str]:
    if not isinstance(route, dict):
        return []
    classes = derived_route_classes(owned_files)
    if len(classes) > 1:
        return [
            f"{task_id} owned_files span multiple route classes: "
            + ", ".join(sorted(classes))
            + "; split the Assignment before dispatch"
        ]
    if len(classes) == 1:
        derived = next(iter(classes))
        declared = str(route.get("policy_class", "")).strip().lower()
        if declared and declared != derived:
            return [
                f"{task_id} route policy_class {declared} conflicts with derived {derived}"
            ]
    return []


INACTIVE_POLICY_LINE_MARKERS = (
    "disallowed",
    "disabled",
    "deprecated",
    "forbidden",
    "must not",
    "mustn't",
    "should not",
    "shouldn't",
    "may not",
    "cannot",
    "can't",
    "never",
    "do not",
    "don't",
    "not allowed",
    "not authorized",
    "not permitted",
    "not recommended",
    "deny",
    "denied",
    "example",
    "e.g.",
    "eg:",
    "historical",
    "obsolete",
    "retired",
    "old route",
    "legacy",
    "禁止",
    "不允许",
    "不要",
    "勿用",
    "不可",
    "不得",
    "不能",
    "禁用",
    "废弃",
    "不推荐",
    "示例",
    "例如",
    "历史规则",
    "仅历史",
)


POLICY_INLINE_COMMENT_MARKERS = ("#", "//", "<!--")
POLICY_ROUTE_FIELDS = ("provider", "model", "auth_mode")
POLICY_FIELD_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<field>provider|model|auth_mode)"
    r"\s*=\s*(?P<value>[A-Za-z0-9_.:+/-]+)",
    re.IGNORECASE,
)


UNICODE_DASH_EQUIVALENTS = "‐‑‒–—―−﹘﹣－"


def _normalize_policy_symbols(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return normalized.translate(
        str.maketrans({character: "-" for character in UNICODE_DASH_EQUIVALENTS})
    )


def _normalized_policy_text(value: str) -> str:
    lowered = _normalize_policy_symbols(value).casefold()
    lowered = re.sub(r"[-_]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _active_policy_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if line.startswith(("    ", "	")):
        return False
    if stripped.startswith(("#", "//", "<!--", "-->", ">")):
        return False
    if chr(96) in stripped:
        return False
    if any(marker in stripped for marker in POLICY_INLINE_COMMENT_MARKERS):
        return False
    normalized = _normalized_policy_text(stripped)
    return not any(
        _normalized_policy_text(token) in normalized
        for token in INACTIVE_POLICY_LINE_MARKERS
    )


def _policy_field_assignments(line: str) -> tuple[dict[str, str], int | None]:
    normalized_line = _normalize_policy_symbols(line)
    values: dict[str, str] = {}
    first_start: int | None = None
    for match in POLICY_FIELD_PATTERN.finditer(normalized_line):
        field = match.group("field").casefold()
        value = match.group("value").strip().rstrip(".")
        if first_start is None:
            first_start = match.start()
        if field in values and values[field] != value:
            return {}, first_start
        values[field] = value
    return values, first_start


def _policy_class_marker_matches(prefix: str, markers: tuple[str, ...]) -> bool:
    if not markers:
        return True
    lowered = _normalize_policy_symbols(prefix).casefold()
    for marker in markers:
        token = marker.casefold()
        if token.isascii():
            if re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])",
                lowered,
            ):
                return True
        elif token in lowered:
            for match in re.finditer(re.escape(token), lowered):
                prefix = lowered[: match.start()].rstrip()
                if prefix.endswith(("非", "不", "无")):
                    continue
                return True
    return False


def _active_policy_route_declaration(
    line: str, markers: tuple[str, ...]
) -> dict[str, str] | None:
    if not _active_policy_line(line):
        return None
    values, first_start = _policy_field_assignments(line)
    if first_start is None or set(values) != set(POLICY_ROUTE_FIELDS):
        return None
    if not _policy_class_marker_matches(line[:first_start], markers):
        return None
    return values

def route_policy_errors(task_id: str, route: dict[str, Any]) -> list[str]:
    source = route.get("policy_source")
    if not isinstance(source, dict):
        return []
    raw_path = str(source.get("path", "")).strip()
    expected_sha = str(source.get("sha256", "")).strip().lower()
    if not raw_path or not expected_sha:
        return []
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return [f"{task_id} route policy_source.path must be absolute"]
    try:
        payload = path.read_bytes()
    except OSError as error:
        return [f"{task_id} route policy source is unreadable: {error}"]
    if hashlib.sha256(payload).hexdigest() != expected_sha:
        return [f"{task_id} route policy_source.sha256 does not match the policy file"]

    decision = str(route.get("decision", "")).strip().lower()
    if decision in {"default", "safe_fallback"}:
        selected: Any = route
    elif decision == "controller_exception":
        selected = route.get("default_route")
        if not isinstance(selected, dict):
            return [f"{task_id} controller_exception route requires default_route"]
    else:
        return []
    if not isinstance(selected, dict):
        return []
    route_values = {
        field: str(selected.get(field, "")).strip()
        for field in ("provider", "model", "auth_mode")
    }
    if any(not value for value in route_values.values()):
        return []
    policy_class = str(route.get("policy_class", "")).strip().lower()
    markers = ROUTE_CLASS_MARKERS.get(policy_class, (policy_class,)) if policy_class else ()
    policy_text = payload.decode("utf-8", errors="replace")
    active_fence: str | None = None
    in_html_comment = False
    fence_tokens = (chr(96) * 3, "~" * 3)
    for line in policy_text.splitlines():
        stripped = line.strip()
        if in_html_comment:
            if "-->" in stripped:
                in_html_comment = False
            continue
        if "<!--" in stripped:
            if "-->" not in stripped.split("<!--", 1)[1]:
                in_html_comment = True
            continue
        matched_fence = next(
            (token for token in fence_tokens if stripped.startswith(token)),
            None,
        )
        if matched_fence is not None:
            if active_fence is None:
                active_fence = matched_fence
            elif matched_fence == active_fence:
                active_fence = None
            continue
        if active_fence is not None:
            continue
        declaration = _active_policy_route_declaration(line, markers)
        if declaration is None:
            continue
        if all(
            declaration.get(field, "").casefold() == value.casefold()
            for field, value in route_values.items()
        ):
            return []
    return [f"{task_id} route is not declared by policy source"]

def canonical_safe_fallback_errors(
    task_id: str,
    route: dict[str, Any],
    *,
    runtime_repo: str | Path | None,
) -> list[str]:
    if str(route.get("decision", "")).strip().lower() != "safe_fallback":
        return []
    errors: list[str] = []
    prior_assignment_id = str(route.get("prior_assignment_id", "")).strip()
    failure_evidence = str(route.get("failure_evidence", "")).strip()
    if not prior_assignment_id:
        errors.append(f"{task_id} safe fallback requires prior_assignment_id")
    if not _traceable_runtime_evidence(failure_evidence):
        errors.append(f"{task_id} safe fallback requires traceable failure_evidence")
    if runtime_repo is None:
        errors.append(f"{task_id} safe fallback requires canonical Runtime proof")
        return errors

    try:
        from scripts.assignment_runtime import load_runtime_state
    except ModuleNotFoundError:
        from assignment_runtime import load_runtime_state

    runtime = load_runtime_state(Path(runtime_repo).expanduser().resolve())
    leases = runtime.get("leases", {}) if isinstance(runtime, dict) else {}
    lease = leases.get(prior_assignment_id) if isinstance(leases, dict) else None
    if not isinstance(lease, dict):
        errors.append(
            f"{task_id} safe fallback prior_assignment_id has no canonical Runtime lease"
        )
        return errors
    if str(lease.get("task_id", "")).strip() != task_id:
        errors.append(f"{task_id} safe fallback prior Assignment belongs to another task")

    fallback_from = route.get("fallback_from")
    if not isinstance(fallback_from, dict):
        errors.append(f"{task_id} safe fallback requires fallback_from")
    else:
        for field in ("provider", "model", "auth_mode"):
            declared = str(fallback_from.get(field, "")).strip()
            actual = str(lease.get(field, "")).strip()
            if not declared:
                errors.append(f"{task_id} safe fallback requires fallback_from.{field}")
            elif declared != actual:
                errors.append(
                    f"{task_id} safe fallback fallback_from.{field} does not match canonical prior Assignment"
                )

    terminal_state = str(lease.get("terminal_state", "")).strip().lower()
    delivery_outcome = str(lease.get("delivery_outcome", "")).strip().lower()
    failure_terminal = terminal_state in {"failed", "cancelled", "disconnected"} or (
        terminal_state == "completed"
        and delivery_outcome in {"fail", "blocked", "unresolved"}
    )
    if not failure_terminal:
        errors.append(
            f"{task_id} safe fallback prior Assignment is not a canonical failure terminal"
        )
    if lease.get("result_unknown") is not False:
        errors.append(f"{task_id} safe fallback canonical prior result is unknown")
    evidence = lease.get("evidence")
    if not isinstance(evidence, list) or failure_evidence not in evidence:
        errors.append(
            f"{task_id} safe fallback failure_evidence is not present in canonical prior terminal evidence"
        )
    return errors


def delegated_route_contract_errors(
    task_id: str,
    owned_files: Any,
    route: Any,
    *,
    runtime_repo: str | Path | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(route, dict):
        return [f"{task_id} delegated assignment requires route"]
    for field in ("decision", "policy_class", "provider", "model", "auth_mode"):
        if not str(route.get(field, "")).strip():
            errors.append(f"{task_id} route requires {field}")
    source = route.get("policy_source")
    if not isinstance(source, dict):
        errors.append(f"{task_id} route requires policy_source")
    else:
        for field in ("path", "sha256"):
            if not str(source.get(field, "")).strip():
                errors.append(f"{task_id} route policy_source requires {field}")

    errors.extend(route_scope_errors(task_id, owned_files, route))
    errors.extend(route_policy_errors(task_id, route))

    decision = str(route.get("decision", "")).strip().lower()
    if decision not in ROUTE_DECISIONS:
        errors.append(
            f"{task_id} route decision must be default, safe_fallback, or controller_exception"
        )
    elif decision == "controller_exception":
        errors.append(f"{task_id} delegated assignment cannot use controller_exception")
    elif decision == "safe_fallback":
        errors.extend(
            canonical_safe_fallback_errors(
                task_id,
                route,
                runtime_repo=runtime_repo,
            )
        )
    return errors
