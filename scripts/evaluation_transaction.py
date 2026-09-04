#!/usr/bin/env python3
"""Generic Model / Evidence / Evaluation transaction contract."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

UTC = timezone.utc
READ = "READ"
COMPUTE = "COMPUTE"
COMPUTED = "COMPUTED"
DERIVED = "DERIVED"
UNKNOWN = "UNKNOWN"
NONE = "NONE"

TERMINAL_CYCLE_STATES = {
    "CLOSED",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
    "ABSORBED",
    "PARKED",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evidence_snapshot_sha256(value: Any) -> str:
    return _canonical_sha256(value)


def classify_evaluation_intent(prompt: str) -> str:
    text = " ".join(str(prompt or "").strip().lower().split())
    compact = "".join(text.split())

    read_markers = (
        "系统最新记录",
        "最新记录",
        "系统记录",
        "记录是多少",
        "记录多少",
        "上次评分",
        "历史评分",
        "previous score",
        "latest recorded",
        "recorded score",
        "what is recorded",
    )
    compute_markers = (
        "重新评估",
        "重新评分",
        "重新计算",
        "重新算",
        "重算",
        "当前真实能力",
        "现在真实能力",
        "评估当前",
        "评估现在",
        "重新判断",
        "re-evaluate",
        "reevaluate",
        "recompute",
        "recalculate",
        "evaluate current",
        "assess current",
    )

    if any(marker.replace(" ", "") in compact for marker in read_markers):
        if not any(marker.replace(" ", "") in compact for marker in compute_markers):
            return READ
    if any(marker.replace(" ", "") in compact for marker in compute_markers):
        return COMPUTE

    evaluation_words = (
        "评估",
        "评分",
        "判断",
        "计算",
        "evaluate",
        "assess",
        "score",
        "calculate",
    )
    current_words = (
        "当前",
        "现在",
        "此刻",
        "最新",
        "current",
        "now",
        "latest",
    )
    if any(word in text for word in evaluation_words) and any(
        word in text for word in current_words
    ):
        return COMPUTE

    return NONE


def begin_evaluation(
    *,
    prompt: str,
    subject: dict[str, Any],
    model: dict[str, Any],
    evidence_provider: Callable[[], Any],
    historical_refs: list[dict[str, Any]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    clock = clock or _now
    intent = classify_evaluation_intent(prompt)
    started_at = clock()
    history = [
        {**dict(item), "provenance": READ}
        for item in (historical_refs or [])
    ]

    transaction: dict[str, Any] = {
        "schema_version": 1,
        "intent": intent,
        "subject": dict(subject),
        "prompt": str(prompt),
        "model": dict(model),
        "evaluation_begun_at": _iso(started_at),
        "historical_refs": history,
        "state": "OPEN",
        "provenance": {
            "model": READ,
            "historical_refs": READ,
        },
    }

    if intent == COMPUTE:
        if model.get("state") != "found":
            raise ValueError(
                "COMPUTE evaluation requires a resolved current formal model"
            )
        model_sha = str(model.get("sha256", "")).strip()
        model_path = str(model.get("path", "")).strip()
        if not model_sha or not model_path:
            raise ValueError(
                "COMPUTE evaluation requires model path and sha256"
            )
        evidence = evidence_provider()
        cutoff_at = clock()
        if cutoff_at < started_at:
            raise ValueError(
                "evidence cutoff cannot predate evaluation start"
            )
        transaction.update(
            {
                "evidence_snapshot": evidence,
                "evidence_snapshot_sha256": _canonical_sha256(evidence),
                "evidence_cutoff_at": _iso(cutoff_at),
            }
        )
        transaction["provenance"]["evidence"] = READ
    elif intent == READ:
        transaction["evidence_snapshot"] = None
        transaction["evidence_snapshot_sha256"] = None
        transaction["evidence_cutoff_at"] = None
    else:
        transaction["evidence_snapshot"] = None
        transaction["evidence_snapshot_sha256"] = None
        transaction["evidence_cutoff_at"] = None

    identity_material = {
        "intent": transaction["intent"],
        "subject": transaction["subject"],
        "model_sha256": transaction["model"].get("sha256"),
        "evaluation_begun_at": transaction["evaluation_begun_at"],
        "evidence_cutoff_at": transaction.get("evidence_cutoff_at"),
        "evidence_snapshot_sha256": transaction.get(
            "evidence_snapshot_sha256"
        ),
        "prompt": transaction["prompt"],
    }
    transaction["evaluation_id"] = _canonical_sha256(identity_material)
    return transaction


def validate_completion(
    transaction: dict[str, Any],
    *,
    result: dict[str, Any],
    provenance: dict[str, str],
    core_fields: tuple[str, ...] | list[str],
) -> list[str]:
    errors: list[str] = []
    intent = str(transaction.get("intent", NONE))
    for field in core_fields:
        if field not in result:
            errors.append(f"core result field {field} is missing")
            continue
        source = str(provenance.get(field, "")).upper()
        if intent == COMPUTE and source not in {COMPUTED, DERIVED, UNKNOWN}:
            errors.append(
                f"core result field {field} must be COMPUTED, DERIVED, or UNKNOWN for a COMPUTE evaluation"
            )
        if intent == READ and source not in {READ, DERIVED, UNKNOWN, COMPUTED}:
            errors.append(
                f"core result field {field} has invalid provenance {source or 'MISSING'}"
            )

    if intent == COMPUTE:
        if not transaction.get("evidence_snapshot_sha256"):
            errors.append(
                "COMPUTE evaluation has no fresh evidence snapshot"
            )
        begun_raw = str(transaction.get("evaluation_begun_at", ""))
        cutoff_raw = str(transaction.get("evidence_cutoff_at", ""))
        try:
            begun = datetime.fromisoformat(begun_raw)
            cutoff = datetime.fromisoformat(cutoff_raw)
        except ValueError:
            errors.append(
                "COMPUTE evaluation has invalid begin/cutoff timestamps"
            )
        else:
            if cutoff < begun:
                errors.append(
                    "COMPUTE evidence cutoff predates evaluation begin"
                )
        model = transaction.get("model")
        if (
            not isinstance(model, dict)
            or model.get("state") != "found"
            or not model.get("sha256")
        ):
            errors.append(
                "COMPUTE evaluation is not bound to a current formal model"
            )
    return errors


def correct_evaluation(
    transaction: dict[str, Any],
    *,
    reason: str,
    evidence_provider: Callable[[], Any],
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    corrected = begin_evaluation(
        prompt=str(transaction.get("prompt", "")),
        subject=dict(transaction.get("subject", {})),
        model=dict(transaction.get("model", {})),
        evidence_provider=evidence_provider,
        historical_refs=[
            {
                key: value
                for key, value in item.items()
                if key != "provenance"
            }
            for item in transaction.get("historical_refs", [])
            if isinstance(item, dict)
        ],
        clock=clock,
    )
    corrected["supersedes_evaluation_id"] = transaction.get(
        "evaluation_id"
    )
    corrected["correction_reason"] = str(reason)
    corrected["correction_level"] = "mandatory"
    return corrected


def validate_scoring_layers(
    *,
    performance_score: float,
    governance_risk_status: str,
    risk_constrained_score: float,
    active_cap: float | None = None,
) -> dict[str, Any]:
    performance = float(performance_score)
    constrained = float(risk_constrained_score)
    if not 0 <= performance <= 100:
        raise ValueError("performance score must be within 0..100")
    if not 0 <= constrained <= 100:
        raise ValueError(
            "risk-constrained score must be within 0..100"
        )
    if constrained > performance:
        raise ValueError(
            "risk-constrained score cannot exceed performance score"
        )
    if active_cap is not None and constrained > float(active_cap):
        raise ValueError(
            "risk-constrained score exceeds active governance cap"
        )
    risk_status = str(governance_risk_status).upper().strip()
    if risk_status not in {"GREEN", "AMBER", "RED"}:
        raise ValueError(
            "governance risk status must be GREEN, AMBER, or RED"
        )
    return {
        "capability": {
            "value": performance,
            "provenance": COMPUTED,
        },
        "governance_risk": {
            "status": risk_status,
            "provenance": DERIVED,
        },
        "risk_constrained": {
            "value": constrained,
            "active_cap": active_cap,
            "provenance": DERIVED,
        },
    }


def current_model_extrema(
    records: list[dict[str, Any]],
    *,
    model_sha256: str,
) -> dict[str, Any]:
    eligible: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("record_kind") != "cycle":
            continue
        if str(record.get("model_sha256", "")) != str(model_sha256):
            continue
        if (
            str(record.get("terminal_status", "")).upper().strip()
            not in TERMINAL_CYCLE_STATES
        ):
            continue
        try:
            score = float(record["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= score <= 100:
            continue
        eligible.append({**record, "score": score})

    if not eligible:
        return {"best": UNKNOWN, "worst": UNKNOWN}

    best = max(
        eligible,
        key=lambda item: (
            float(item["score"]),
            str(item.get("cycle_id", "")),
        ),
    )
    worst = min(
        eligible,
        key=lambda item: (
            float(item["score"]),
            str(item.get("cycle_id", "")),
        ),
    )
    return {"best": best, "worst": worst}
