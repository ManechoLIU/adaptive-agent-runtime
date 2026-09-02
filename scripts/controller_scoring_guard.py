#!/usr/bin/env python3
"""Fail-closed guard for controller performance scoring model reads."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODEL_RELATIVE_PATH = Path("references/controller-performance-scoring.md")
RECEIPT_DIRECTORY = "adaptive-delivery"
RECEIPT_FILE = "controller-scoring-model-read.json"
RECEIPT_ACTION_DIRECTORY = "controller-scoring-model-reads"
RECEIPT_MAX_AGE_SECONDS = 1800
SCORE_HISTORY_FILE = "controller-score-history.jsonl"
CYCLE_EVIDENCE_DIRECTORY = "controller-cycle-evidence"
TERMINAL_CYCLE_STATUSES = {"CLOSED", "FAILED", "BLOCKED", "CANCELLED", "ABSORBED", "PARKED"}
GOVERNANCE_RISK_STATUSES = {"GREEN", "AMBER", "RED"}
CONTROLLER_REGISTRY_PATH = Path(
    os.environ.get(
        "AD_CONTROLLER_REGISTRY",
        str(Path.home() / ".codex" / "adaptive-delivery-controllers.json"),
    )
).expanduser()


def _git_common_dir(repo: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    )
    path = Path(completed.stdout.strip())
    return (repo / path).resolve() if not path.is_absolute() else path.resolve()


def stable_logical_controller_id(repo: str | Path, declared_controller_id: str) -> str:
    declared = str(declared_controller_id or "").strip()
    if not declared:
        raise ValueError("stable logical controller id is required")
    try:
        registry = json.loads(CONTROLLER_REGISTRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return declared
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"controller registry is unreadable: {error}") from error
    if not isinstance(registry, dict):
        raise ValueError("controller registry root must be an object")
    requested_common = _git_common_dir(Path(repo).resolve())
    owners: list[str] = []
    for controller_id, registered_repo in registry.items():
        if (
            not isinstance(controller_id, str)
            or controller_id.startswith("__")
            or not isinstance(registered_repo, str)
        ):
            continue
        try:
            if _git_common_dir(Path(registered_repo).expanduser().resolve()) == requested_common:
                owners.append(controller_id)
        except (OSError, subprocess.CalledProcessError):
            continue
    if not owners:
        return declared
    if len(owners) != 1 or declared != owners[0]:
        raise ValueError("registered repository scoring requires its stable logical controller id")
    return owners[0]


def receipt_path(repo: str | Path, *, receipt_id: str | None = None) -> Path:
    root = _git_common_dir(Path(repo).resolve()) / RECEIPT_DIRECTORY
    identifier = str(receipt_id or "").strip()
    if not identifier:
        return root / RECEIPT_FILE
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    return root / RECEIPT_ACTION_DIRECTORY / f"{digest}.json"


def score_history_path(repo: str | Path) -> Path:
    return _git_common_dir(Path(repo).resolve()) / RECEIPT_DIRECTORY / SCORE_HISTORY_FILE


def cycle_evidence_directory(repo: str | Path) -> Path:
    return _git_common_dir(Path(repo).resolve()) / RECEIPT_DIRECTORY / CYCLE_EVIDENCE_DIRECTORY


def _cycle_evidence_path(repo: str | Path, evidence_id: str) -> Path:
    identifier = str(evidence_id or "").strip()
    if not identifier or len(identifier) > 256:
        raise ValueError("cycle evidence id is missing or too long")
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    return cycle_evidence_directory(repo) / f"{digest}.json"


def load_cycle_evidence(repo: str | Path, evidence_id: str) -> tuple[dict[str, Any], str]:
    target = _cycle_evidence_path(repo, evidence_id)
    try:
        content = target.read_bytes()
        value = json.loads(content)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cycle evidence receipt is missing or unreadable: {error}") from error
    if not isinstance(value, dict) or value.get("record_kind") != "controller_cycle_evidence":
        raise ValueError("cycle evidence receipt has an invalid schema")
    if str(value.get("evidence_id", "")).strip() != str(evidence_id).strip():
        raise ValueError("cycle evidence receipt id mismatch")
    return value, hashlib.sha256(content).hexdigest()


def append_score_history(repo: str | Path, record: dict[str, Any]) -> dict[str, Any]:
    target = score_history_path(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload.setdefault("schema_version", 1)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
    return payload


def _score_history_records(repo: str | Path) -> list[dict[str, Any]]:
    target = score_history_path(repo)
    if not target.is_file():
        return []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def latest_score_history(repo: str | Path, *, controller_session_id: str) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for value in _score_history_records(repo):
        if str(value.get("controller_session_id", "")) != str(controller_session_id):
            continue
        # Backward compatibility: records written before record_kind existed are formal scores.
        if str(value.get("record_kind", "formal")) != "formal":
            continue
        latest = value
    return latest


def _valid_cycle_history_record(
    repo: str | Path,
    value: dict[str, Any],
    *,
    controller_session_id: str,
    model_sha256: str,
) -> bool:
    controller = str(controller_session_id).strip()
    if not controller or str(value.get("controller_session_id", "")).strip() != controller:
        return False
    if value.get("record_kind") != "cycle":
        return False
    if str(value.get("model_sha256", "")) != str(model_sha256):
        return False
    if str(value.get("terminal_status", "")).upper().strip() not in TERMINAL_CYCLE_STATUSES:
        return False
    if not str(value.get("cycle_id", "")).strip():
        return False
    if not str(value.get("evidence_summary", "")).strip():
        return False
    message_sha256 = str(value.get("message_sha256", "")).strip()
    if len(message_sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in message_sha256):
        return False
    try:
        score = float(value["score"])
    except (KeyError, TypeError, ValueError):
        return False
    if not 0 <= score <= 100:
        return False
    evidence_id = str(value.get("evidence_id", "")).strip()
    evidence_sha256 = str(value.get("evidence_sha256", "")).strip()
    try:
        receipt, actual_sha256 = load_cycle_evidence(repo, evidence_id)
    except ValueError:
        return False
    return bool(
        evidence_sha256 == actual_sha256
        and str(receipt.get("controller_id", "")).strip() == controller
        and str(receipt.get("cycle_id", "")).strip() == str(value.get("cycle_id", "")).strip()
        and str(receipt.get("terminal_status", "")).upper().strip()
        == str(value.get("terminal_status", "")).upper().strip()
        and str(receipt.get("evidence_summary", "")).strip()
        == str(value.get("evidence_summary", "")).strip()
    )


def cycle_score_extremes(
    repo: str | Path, *, controller_session_id: str, model_sha256: str
) -> dict[str, dict[str, Any] | None]:
    eligible = [
        value for value in _score_history_records(repo)
        if _valid_cycle_history_record(
            repo, value, controller_session_id=controller_session_id, model_sha256=model_sha256
        )
    ]
    if not eligible:
        return {"best": None, "worst": None}
    return {
        "best": max(eligible, key=lambda record: float(record["score"])),
        "worst": min(eligible, key=lambda record: float(record["score"])),
    }


def _recorded_at(value: dict[str, Any]) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value.get("recorded_at", "")))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cycle_evidence_receipts(repo: str | Path, *, controller_session_id: str) -> list[dict[str, Any]]:
    directory = cycle_evidence_directory(repo)
    if not directory.is_dir():
        return []
    receipts: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("record_kind") != "controller_cycle_evidence":
            continue
        if str(value.get("controller_id", "")).strip() != str(controller_session_id).strip():
            continue
        receipts.append(value)
    return receipts


def governance_risk_projection(
    repo: str | Path, *, controller_session_id: str
) -> dict[str, Any]:
    controller = str(controller_session_id).strip()
    receipts = _cycle_evidence_receipts(repo, controller_session_id=controller)
    incidents_by_id: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        if str(receipt.get("terminal_status", "")).upper().strip() != "FAILED":
            continue
        if str(receipt.get("governance_incident_severity", "")).lower().strip() != "major":
            continue
        incident_id = str(receipt.get("cycle_id", "")).strip()
        if incident_id:
            incidents_by_id[incident_id] = receipt
    for value in _score_history_records(repo):
        if str(value.get("controller_session_id", "")).strip() != controller:
            continue
        if value.get("record_kind") != "cycle":
            continue
        if str(value.get("terminal_status", "")).upper().strip() != "FAILED":
            continue
        try:
            score = float(value["score"])
        except (KeyError, TypeError, ValueError):
            continue
        cycle_id = str(value.get("cycle_id", "")).strip()
        if score <= 49 and cycle_id:
            existing = incidents_by_id.get(cycle_id)
            if existing is None or _recorded_at(value) < _recorded_at(existing):
                incidents_by_id[cycle_id] = value
    incidents = sorted(incidents_by_id.values(), key=_recorded_at)
    incident_states: list[dict[str, Any]] = []
    for incident in incidents:
        incident_id = str(incident["cycle_id"]).strip()
        incident_time = _recorded_at(incident)
        corrections = [
            receipt for receipt in receipts
            if str(receipt.get("corrects_incident", "")).strip() == incident_id
            and str(receipt.get("terminal_status", "")).upper().strip() == "CLOSED"
            and _recorded_at(receipt) > incident_time
        ]
        if not corrections:
            incident_states.append({"incident_cycle_id": incident_id, "status": "RED"})
            continue
        strong_corrections = [
            receipt for receipt in corrections
            if receipt.get("risk_clearance_contract_version") == 2
        ]
        if not strong_corrections:
            incident_states.append({
                "incident_cycle_id": incident_id,
                "status": "AMBER",
                "legacy_correction_evidence_ids": [
                    receipt.get("evidence_id")
                    for receipt in sorted(corrections, key=_recorded_at)
                ],
            })
            continue
        complete_chain: tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None = None
        for candidate_correction in sorted(strong_corrections, key=_recorded_at):
            candidate_time = _recorded_at(candidate_correction)
            candidate_alignments = [
                receipt for receipt in receipts
                if str(receipt.get("alignment_for_incident", "")).strip() == incident_id
                and str(receipt.get("terminal_status", "")).upper().strip() == "CLOSED"
                and _recorded_at(receipt) >= candidate_time
            ]
            candidate_closures = [
                receipt for receipt in receipts
                if str(receipt.get("post_incident_closure_for", "")).strip() == incident_id
                and str(receipt.get("depends_on_correction_evidence_id", "")).strip()
                == str(candidate_correction.get("evidence_id", "")).strip()
                and str(receipt.get("terminal_status", "")).upper().strip() == "CLOSED"
                and str(receipt.get("outcome_level", "")).upper().strip() in {"L3", "L4"}
                and _recorded_at(receipt) > candidate_time
            ]
            if candidate_alignments and candidate_closures:
                complete_chain = (
                    candidate_correction,
                    candidate_alignments,
                    candidate_closures,
                )
                break
        if complete_chain is None:
            correction = max(strong_corrections, key=_recorded_at)
            correction_time = _recorded_at(correction)
            alignments = [
                receipt for receipt in receipts
                if str(receipt.get("alignment_for_incident", "")).strip() == incident_id
                and str(receipt.get("terminal_status", "")).upper().strip() == "CLOSED"
                and _recorded_at(receipt) >= correction_time
            ]
            closures: list[dict[str, Any]] = []
            status = "AMBER"
        else:
            correction, alignments, closures = complete_chain
            status = "GREEN"
        state = {
            "incident_cycle_id": incident_id,
            "status": status,
            "correction_evidence_id": correction.get("evidence_id"),
        }
        if alignments:
            state["alignment_evidence_id"] = min(alignments, key=_recorded_at).get("evidence_id")
        if closures:
            state["post_incident_closure_evidence_id"] = min(closures, key=_recorded_at).get("evidence_id")
        incident_states.append(state)
    status_rank = {"GREEN": 0, "AMBER": 1, "RED": 2}
    status = max((item["status"] for item in incident_states), key=status_rank.get, default="GREEN")
    basis = {
        "controller_id": controller,
        "status": status,
        "active_cap": 49 if status in {"AMBER", "RED"} else None,
        "incident_states": incident_states,
        "observed_cycle_records": sum(
            1 for value in _score_history_records(repo)
            if str(value.get("controller_session_id", "")).strip() == controller
            and value.get("record_kind") == "cycle"
        ),
        "observed_machine_receipts": len(receipts),
    }
    basis["projection_sha256"] = hashlib.sha256(
        json.dumps(basis, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return basis


def scoring_model_path(skill_root: str | Path) -> Path:
    return Path(skill_root).resolve() / MODEL_RELATIVE_PATH


def scoring_model_sha256(skill_root: str | Path) -> str:
    return hashlib.sha256(scoring_model_path(skill_root).read_bytes()).hexdigest()


def _write_receipt(
    repo: str | Path,
    *,
    model: Path,
    content: bytes,
    controller_session_id: str | None = None,
    receipt_id: str | None = None,
) -> dict[str, Any]:
    receipt = {
        "schema_version": 1,
        "model_path": str(model.resolve()),
        "model_sha256": hashlib.sha256(content).hexdigest(),
        "read_at": datetime.now(timezone.utc).isoformat(),
    }
    declared_controller = str(controller_session_id or "").strip()
    controller = (
        stable_logical_controller_id(repo, declared_controller)
        if declared_controller
        else ""
    )
    if controller:
        receipt["controller_session_id"] = controller
        receipt["governance_risk_projection"] = governance_risk_projection(
            repo, controller_session_id=controller
        )
    if receipt_id:
        receipt["receipt_id"] = str(receipt_id)
    target = receipt_path(repo, receipt_id=receipt_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def read_and_record_model(
    repo: str | Path,
    *,
    skill_root: str | Path,
    controller_session_id: str | None = None,
    receipt_id: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    model = scoring_model_path(skill_root)
    content = model.read_bytes()
    return content, _write_receipt(
        repo,
        model=model,
        content=content,
        controller_session_id=controller_session_id,
        receipt_id=receipt_id,
    )


def record_model_read(
    repo: str | Path,
    *,
    skill_root: str | Path,
    controller_session_id: str | None = None,
    receipt_id: str | None = None,
) -> dict[str, Any]:
    _, receipt = read_and_record_model(
        repo,
        skill_root=skill_root,
        controller_session_id=controller_session_id,
        receipt_id=receipt_id,
    )
    return receipt


def _load_receipt(repo: str | Path, *, receipt_id: str | None = None) -> dict[str, Any] | None:
    target = receipt_path(repo, receipt_id=receipt_id)
    if not target.is_file():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _receipt_age_seconds(receipt: dict[str, Any]) -> float | None:
    try:
        read_at = datetime.fromisoformat(str(receipt.get("read_at", "")))
    except ValueError:
        return None
    if read_at.tzinfo is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - read_at.astimezone(timezone.utc)).total_seconds())


def score_guard_errors(
    repo: str | Path,
    *,
    skill_root: str | Path,
    receipt_id: str | None = None,
) -> list[str]:
    receipt = _load_receipt(repo, receipt_id=receipt_id)
    if receipt is None:
        return ["controller scoring blocked: scoring model read receipt is missing or unreadable"]
    age = _receipt_age_seconds(receipt)
    if age is None or age > RECEIPT_MAX_AGE_SECONDS:
        return ["controller scoring blocked: scoring model read receipt is expired for the current scoring action"]
    expected = scoring_model_sha256(skill_root)
    if receipt.get("model_sha256") != expected:
        return ["controller scoring blocked: scoring model read receipt is stale for the installed model"]
    if Path(str(receipt.get("model_path", ""))).resolve() != scoring_model_path(skill_root):
        return ["controller scoring blocked: scoring model read receipt points to a different model"]
    return []


def finalize_score(
    repo: str | Path,
    *,
    skill_root: str | Path,
    controller_session_id: str,
    turn_id: str,
    score: float,
    performance_score: float,
    governance_risk_status: str,
    risk_summary: str,
    window_summary: str | None,
    message_sha256: str | None,
    receipt_id: str | None = None,
) -> dict[str, Any]:
    controller = stable_logical_controller_id(repo, controller_session_id)
    performance = float(performance_score)
    constrained = float(score)
    risk_status = str(governance_risk_status).upper().strip()
    risk_basis = str(risk_summary or "").strip()
    if not controller:
        raise ValueError("formal score requires a non-empty controller session identity")
    if not 0 <= performance <= 100:
        raise ValueError("performance score must be within 0..100")
    if not 0 <= constrained <= 100:
        raise ValueError("risk-constrained score must be within 0..100")
    if constrained > performance:
        raise ValueError("risk-constrained score cannot exceed performance score")
    if risk_status not in GOVERNANCE_RISK_STATUSES:
        raise ValueError("governance risk status must be GREEN, AMBER, or RED")
    if not risk_basis:
        raise ValueError("formal score requires a non-empty governance risk summary")
    receipt = _load_receipt(repo, receipt_id=receipt_id)
    if receipt is None or str(receipt.get("controller_session_id", "")).strip() != controller:
        raise ValueError(
            "score-guard failed: machine governance risk projection is missing or bound to another Controller"
        )
    stored_projection = receipt.get("governance_risk_projection")
    current_projection = governance_risk_projection(repo, controller_session_id=controller)
    if not isinstance(stored_projection, dict) or stored_projection != current_projection:
        raise ValueError("machine governance risk projection changed or is unreadable; reload the scoring model")
    expected_status = str(current_projection.get("status", "")).upper().strip()
    if risk_status != expected_status:
        raise ValueError(
            f"machine governance risk projection requires {expected_status}, not {risk_status}"
        )
    active_cap = current_projection.get("active_cap")
    if active_cap is not None and constrained > float(active_cap):
        raise ValueError(f"machine governance risk projection enforces the active {active_cap} cap")
    model_sha256 = scoring_model_sha256(skill_root)
    extremes = cycle_score_extremes(
        repo,
        controller_session_id=controller,
        model_sha256=model_sha256,
    )
    errors = consume_score_guard(repo, skill_root=skill_root, receipt_id=receipt_id)
    if errors:
        raise ValueError("score-guard failed: " + "; ".join(errors))
    record = {
        "schema_version": 1,
        "record_kind": "formal",
        "controller_session_id": controller,
        "turn_id": str(turn_id),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        # Keep score as a compatibility alias for existing latest-score readers.
        "score": constrained,
        "performance_score": performance,
        "risk_constrained_score": constrained,
        "governance_risk_status": risk_status,
        "risk_summary": risk_basis,
        "governance_risk_projection": current_projection,
        "cycle_extremes": extremes,
        "window_summary": window_summary,
        "model_sha256": model_sha256,
        "message_sha256": message_sha256,
    }
    return append_score_history(repo, record)


def finalize_cycle_candidate(
    repo: str | Path,
    *,
    skill_root: str | Path,
    controller_session_id: str,
    turn_id: str,
    cycle_id: str,
    terminal_status: str,
    score: float,
    evidence_summary: str | None,
    message_sha256: str | None,
    receipt_id: str | None = None,
) -> dict[str, Any]:
    controller = stable_logical_controller_id(repo, controller_session_id)
    status = str(terminal_status).upper().strip()
    cycle = str(cycle_id).strip()
    evidence = str(evidence_summary or "").strip()
    message_ref = str(message_sha256 or "").strip()
    if not controller:
        raise ValueError("cycle score requires a non-empty controller session identity")
    if status not in TERMINAL_CYCLE_STATUSES:
        raise ValueError("cycle score requires a terminal cycle status")
    if not cycle:
        raise ValueError("cycle score requires a non-empty cycle_id")
    if not evidence:
        raise ValueError("cycle score requires a non-empty evidence summary")
    if len(message_ref) != 64 or any(char not in "0123456789abcdefABCDEF" for char in message_ref):
        raise ValueError("cycle score requires a valid message sha256 reference")
    if not 0 <= float(score) <= 100:
        raise ValueError("cycle score must be within 0..100")
    errors = consume_score_guard(repo, skill_root=skill_root, receipt_id=receipt_id)
    if errors:
        raise ValueError("score-guard failed: " + "; ".join(errors))
    record = {
        "schema_version": 1,
        "record_kind": "cycle_candidate",
        "controller_session_id": controller,
        "turn_id": str(turn_id),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "cycle_id": cycle,
        "claimed_terminal_status": status,
        "score": float(score),
        "claimed_evidence_summary": evidence,
        "model_sha256": scoring_model_sha256(skill_root),
        "message_sha256": message_ref.lower(),
    }
    return append_score_history(repo, record)


def finalize_attested_cycle_score(
    repo: str | Path,
    *,
    skill_root: str | Path,
    controller_session_id: str,
    turn_id: str,
    cycle_id: str,
    terminal_status: str,
    score: float,
    evidence_summary: str | None,
    evidence_id: str,
    message_sha256: str | None,
    receipt_id: str | None = None,
) -> dict[str, Any]:
    controller = stable_logical_controller_id(repo, controller_session_id)
    cycle = str(cycle_id).strip()
    claimed_status = str(terminal_status).upper().strip()
    claimed_evidence = str(evidence_summary or "").strip()
    message_ref = str(message_sha256 or "").strip()
    if not controller or not cycle:
        raise ValueError("attested cycle score requires controller and cycle identities")
    if not 0 <= float(score) <= 100:
        raise ValueError("cycle score must be within 0..100")
    if len(message_ref) != 64 or any(char not in "0123456789abcdefABCDEF" for char in message_ref):
        raise ValueError("cycle score requires a valid message sha256 reference")
    receipt, evidence_sha256 = load_cycle_evidence(repo, evidence_id)
    receipt_controller = str(receipt.get("controller_id", "")).strip()
    receipt_cycle = str(receipt.get("cycle_id", "")).strip()
    receipt_status = str(receipt.get("terminal_status", "")).upper().strip()
    receipt_evidence = str(receipt.get("evidence_summary", "")).strip()
    if receipt_controller != controller or receipt_cycle != cycle:
        raise ValueError("cycle evidence receipt is bound to another Controller or cycle")
    if receipt_status not in TERMINAL_CYCLE_STATUSES:
        raise ValueError("cycle evidence receipt is not terminal")
    if claimed_status != receipt_status or claimed_evidence != receipt_evidence:
        raise ValueError("cycle terminal status or evidence summary does not match the machine receipt")
    model_read_receipt = _load_receipt(repo, receipt_id=receipt_id)
    current_projection = governance_risk_projection(repo, controller_session_id=controller)
    if (
        not isinstance(model_read_receipt, dict)
        or str(model_read_receipt.get("controller_session_id", "")).strip() != controller
        or model_read_receipt.get("governance_risk_projection") != current_projection
    ):
        raise ValueError(
            "score-guard failed: cycle score requires the current bound machine governance risk projection"
        )
    active_cap = current_projection.get("active_cap")
    if active_cap is not None and float(score) > float(active_cap):
        raise ValueError(
            f"machine governance risk projection enforces the active {active_cap} cap on cycle scores"
        )
    errors = consume_score_guard(repo, skill_root=skill_root, receipt_id=receipt_id)
    if errors:
        raise ValueError("score-guard failed: " + "; ".join(errors))
    record = {
        "schema_version": 2,
        "record_kind": "cycle",
        "controller_session_id": controller,
        "turn_id": str(turn_id),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "cycle_id": cycle,
        "terminal_status": receipt_status,
        "score": float(score),
        "evidence_summary": receipt_evidence,
        "evidence_id": str(evidence_id).strip(),
        "evidence_sha256": evidence_sha256,
        "outcome_level": receipt.get("outcome_level"),
        "model_sha256": scoring_model_sha256(skill_root),
        "message_sha256": message_ref.lower(),
    }
    return append_score_history(repo, record)


def consume_score_guard(
    repo: str | Path,
    *,
    skill_root: str | Path,
    receipt_id: str | None = None,
) -> list[str]:
    errors = score_guard_errors(repo, skill_root=skill_root, receipt_id=receipt_id)
    if errors:
        return errors
    receipt_path(repo, receipt_id=receipt_id).unlink()
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Record and verify the mandatory controller scoring model read.")
    sub = parser.add_subparsers(dest="command", required=True)
    record_read = sub.add_parser("record-read")
    record_read.add_argument("--repo", required=True)
    record_read.add_argument("--controller-session")
    score_guard = sub.add_parser("score-guard")
    score_guard.add_argument("--repo", required=True)
    latest = sub.add_parser("latest-score")
    latest.add_argument("--repo", required=True)
    latest.add_argument("--controller-session", required=True)
    extremes = sub.add_parser("cycle-extremes")
    extremes.add_argument("--repo", required=True)
    extremes.add_argument("--controller-session", required=True)
    finalize = sub.add_parser("finalize-score")
    finalize.add_argument("--repo", required=True)
    finalize.add_argument("--controller-session", required=True)
    finalize.add_argument("--turn-id", default="")
    finalize.add_argument("--score", required=True, type=float)
    finalize.add_argument("--performance-score", required=True, type=float)
    finalize.add_argument("--governance-risk-status", required=True, choices=sorted(GOVERNANCE_RISK_STATUSES))
    finalize.add_argument("--risk-summary", required=True)
    finalize.add_argument("--window-summary")
    finalize.add_argument("--message-sha256")
    args = parser.parse_args()
    installed_skill_root = Path(__file__).resolve().parents[1]
    if args.command == "record-read":
        content, receipt = read_and_record_model(
            args.repo,
            skill_root=installed_skill_root,
            controller_session_id=args.controller_session,
        )
        text = content.decode("utf-8")
        print(text, end="" if text.endswith("\n") else "\n")
        print("--- controller-scoring-model-read-receipt ---")
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "latest-score":
        record = latest_score_history(args.repo, controller_session_id=args.controller_session)
        print("UNKNOWN" if record is None else json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "cycle-extremes":
        errors = consume_score_guard(args.repo, skill_root=installed_skill_root)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 2
        payload = cycle_score_extremes(
            args.repo, controller_session_id=args.controller_session,
            model_sha256=scoring_model_sha256(installed_skill_root),
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "finalize-score":
        try:
            record = finalize_score(
                args.repo, skill_root=installed_skill_root, controller_session_id=args.controller_session,
                turn_id=args.turn_id, score=args.score, performance_score=args.performance_score,
                governance_risk_status=args.governance_risk_status, risk_summary=args.risk_summary,
                window_summary=args.window_summary,
                message_sha256=args.message_sha256,
            )
        except ValueError as error:
            print(f"controller scoring blocked: {error}")
            return 2
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0
    errors = consume_score_guard(args.repo, skill_root=installed_skill_root)
    if errors:
        for error in errors:
            print(error)
        return 2
    print("controller scoring: allowed once; exact installed scoring model read receipt was current and has been consumed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
