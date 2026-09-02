#!/usr/bin/env python3
"""Derive a compact non-numeric controller self-check from the formal scoring model."""
from __future__ import annotations

import hashlib
from pathlib import Path

MODEL_RELATIVE_PATH = Path("references/controller-performance-scoring.md")
EXPECTED_DIMENSIONS = (
    "有效成果与 Goal 推进",
    "任务拆解与边界设计",
    "关键路径与优先级",
    "调度与执行效率",
    "质量、验收与证据",
    "异常恢复与任务流转",
    "控制面一致性与可审计性",
)


def _dimension_judgments(text: str) -> dict[str, str]:
    section_marker = "## 2. 七维评分模型"
    if section_marker not in text:
        raise ValueError("controller self-check requires the formal seven scoring dimensions")
    section = text.split(section_marker, 1)[1].split("## 3.", 1)[0]
    judgments: dict[str, str] = {}
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        dimension, _weight, judgment = cells[:3]
        if dimension in EXPECTED_DIMENSIONS:
            judgments[dimension] = judgment
    if tuple(dimension for dimension in EXPECTED_DIMENSIONS if dimension in judgments) != EXPECTED_DIMENSIONS:
        raise ValueError("controller self-check requires the formal seven scoring dimensions")
    return judgments


def render_controller_self_check(skill_root: str | Path) -> str:
    model = Path(skill_root).resolve() / MODEL_RELATIVE_PATH
    content = model.read_bytes()
    text = content.decode("utf-8")
    judgments = _dimension_judgments(text)
    digest = hashlib.sha256(content).hexdigest()
    lines = [
        f"Controller Self-Check（derived from installed scoring model sha256:{digest}；不含实时分数）",
    ]
    for dimension in EXPECTED_DIMENSIONS:
        lines.append(f"- {dimension}：{judgments[dimension]}")
    if "人工唤醒" in text:
        lines.append("- 自主续作：没有真实外部阻塞时，不依赖用户提醒来恢复 dispatch / recovery / review / integration。")
    if "责任归因" in text and "governance-caused" in text and "external-caused" in text:
        lines.append("- 责任归因：先区分 controller / governance / external / mixed，再决定纠偏动作；困难环境本身不作为加分或免责。")
    lines.append("只用这些标准纠偏当前行为；不要计算、猜测或展示评分结果。")
    return "\n".join(lines)
