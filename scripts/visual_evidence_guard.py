#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Sequence


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REQUIRED_CHECKS = (
    "structure",
    "proportion",
    "hierarchy",
    "style",
    "responsive",
)
REAL_CAPTURE_SOURCES = ("real-browser", "real-device", "equivalent-runtime")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def png_dimensions(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    if len(payload) < 8 or payload[:8] != PNG_SIGNATURE:
        raise ValueError(f"screenshot is not a readable PNG: {path}")
    offset = 8
    dimensions: tuple[int, int] | None = None
    compressed_image = bytearray()
    saw_end = False
    while offset + 12 <= len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_end = offset + 12 + length
        if chunk_end > len(payload):
            break
        kind = payload[offset + 4 : offset + 8]
        data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length : chunk_end])[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != expected_crc:
            break
        if dimensions is None:
            if kind != b"IHDR" or length != 13:
                break
            dimensions = struct.unpack(">II", data[:8])
        elif kind == b"IDAT":
            compressed_image.extend(data)
        elif kind == b"IEND":
            if length != 0 or chunk_end != len(payload):
                break
            saw_end = True
            offset = chunk_end
            break
        offset = chunk_end
    if dimensions is None or not compressed_image or not saw_end or offset != len(payload):
        raise ValueError(f"screenshot is not a readable PNG: {path}")
    try:
        if not zlib.decompress(bytes(compressed_image)):
            raise ValueError
    except (ValueError, zlib.error) as error:
        raise ValueError(f"screenshot is not a readable PNG: {path}") from error
    return dimensions


def viewport(value: Any) -> tuple[int, int]:
    if not isinstance(value, str):
        raise ValueError(f"viewport must use WIDTHxHEIGHT: {value!r}")
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value)
    if match is None:
        raise ValueError(f"viewport must use WIDTHxHEIGHT: {value!r}")
    return int(match.group(1)), int(match.group(2))


def resolve(base: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def validate_receipt(receipt_path: Path) -> list[str]:
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("receipt root must be an object")
    base = receipt_path.parent.resolve()
    errors: list[str] = []

    raw_references = data.get("references")
    if not isinstance(raw_references, list) or not raw_references:
        raise ValueError("references must be a non-empty list")
    references: dict[Path, set[str]] = {}
    reference_ids: set[str] = set()
    for index, record in enumerate(raw_references):
        if not isinstance(record, dict):
            errors.append(f"references[{index}] must be an object")
            continue
        try:
            path = resolve(base, record.get("path"), f"references[{index}].path")
        except ValueError as error:
            errors.append(str(error))
            continue
        if not path.is_file():
            errors.append(f"reference does not exist: {path}")
            continue
        expected_hash = record.get("sha256")
        if not isinstance(expected_hash, str) or sha256(path) != expected_hash:
            errors.append(f"reference sha256 mismatch: {path}")
        if record.get("role") != "binding":
            errors.append(f"reference role must be binding: {path}")
        reference_id = record.get("id")
        if not isinstance(reference_id, str) or not reference_id.strip():
            errors.append(f"references[{index}].id must be non-empty")
        elif reference_id in reference_ids:
            errors.append(f"duplicate reference id: {reference_id}")
        else:
            reference_ids.add(reference_id)
        raw_reference_states = record.get("states")
        if not isinstance(raw_reference_states, list) or not raw_reference_states:
            errors.append(f"references[{index}].states must be a non-empty list")
            reference_states: set[str] = set()
        else:
            reference_states = {
                str(item) for item in raw_reference_states if str(item).strip()
            }
            if len(reference_states) != len(raw_reference_states):
                errors.append(f"references[{index}].states cannot contain empty values")
        references[path] = reference_states

    raw_approved_components = data.get("approved_components", [])
    if not isinstance(raw_approved_components, list):
        raise ValueError("approved_components must be a list")
    approved_components: set[str] = set()
    for index, component in enumerate(raw_approved_components):
        label = f"approved_components[{index}]"
        if not isinstance(component, dict):
            errors.append(f"{label} must be an object")
            continue
        component_id = component.get("id")
        if not isinstance(component_id, str) or not component_id.strip():
            errors.append(f"{label}.id must be non-empty")
        elif component_id in approved_components:
            errors.append(f"duplicate approved component id: {component_id}")
        else:
            approved_components.add(component_id)
        if component.get("reference") not in reference_ids:
            errors.append(f"{label}.reference must name a binding reference id")

    raw_required_assets = data.get("required_runtime_assets", [])
    raw_assets = data.get("runtime_assets", [])
    if not isinstance(raw_required_assets, list):
        raise ValueError("required_runtime_assets must be a list")
    if not isinstance(raw_assets, list):
        raise ValueError("runtime_assets must be a list")
    required_assets = {str(item) for item in raw_required_assets if str(item).strip()}
    if len(required_assets) != len(raw_required_assets):
        raise ValueError("required_runtime_assets cannot contain empty values")
    approved_assets: set[str] = set()
    for index, asset in enumerate(raw_assets):
        label = f"runtime_assets[{index}]"
        if not isinstance(asset, dict):
            errors.append(f"{label} must be an object")
            continue
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not asset_id.strip():
            errors.append(f"{label}.id must be non-empty")
            continue
        try:
            asset_path = resolve(base, asset.get("path"), f"{label}.path")
        except ValueError as error:
            errors.append(str(error))
            continue
        if not asset_path.is_file():
            errors.append(f"runtime asset does not exist: {asset_path}")
        else:
            expected_hash = asset.get("sha256")
            if not isinstance(expected_hash, str) or sha256(asset_path) != expected_hash:
                errors.append(f"runtime asset sha256 mismatch: {asset_path}")
        if asset.get("approval") != "approved":
            errors.append(f"{label}.approval must be approved")
        else:
            approved_assets.add(asset_id)
    for asset_id in sorted(required_assets - approved_assets):
        errors.append(f"required runtime asset is not approved: {asset_id}")

    raw_viewports = data.get("required_viewports")
    raw_states = data.get("required_states")
    if not isinstance(raw_viewports, list) or not raw_viewports:
        raise ValueError("required_viewports must be a non-empty list")
    if not isinstance(raw_states, list) or not raw_states:
        raise ValueError("required_states must be a non-empty list")
    required_viewports = [str(item) for item in raw_viewports]
    for item in required_viewports:
        viewport(item)
    required_states = [str(item) for item in raw_states if str(item).strip()]
    if len(required_states) != len(raw_states):
        raise ValueError("required_states cannot contain empty values")

    raw_captures = data.get("captures")
    if not isinstance(raw_captures, list):
        raise ValueError("captures must be a list")
    covered: set[tuple[str, str]] = set()
    for index, capture in enumerate(raw_captures):
        label = f"captures[{index}]"
        if not isinstance(capture, dict):
            errors.append(f"{label} must be an object")
            continue
        state = capture.get("state")
        raw_viewport = capture.get("viewport")
        if not isinstance(state, str) or not state.strip():
            errors.append(f"{label}.state must be non-empty")
            continue
        try:
            css_width, css_height = viewport(raw_viewport)
            reference_path = resolve(base, capture.get("reference"), f"{label}.reference")
            screenshot = resolve(base, capture.get("screenshot"), f"{label}.screenshot")
        except ValueError as error:
            errors.append(str(error))
            continue
        covered.add((state, str(raw_viewport)))
        if reference_path not in references:
            errors.append(f"{label}.reference is not declared as binding: {reference_path}")
        elif state not in references[reference_path]:
            errors.append(f"{label}.reference does not cover state={state}")
        if not screenshot.is_file():
            errors.append(f"screenshot does not exist: {screenshot}")
            continue
        try:
            actual_width, actual_height = png_dimensions(screenshot)
        except ValueError as error:
            errors.append(str(error))
            continue

        zoom = capture.get("browser_zoom")
        if not isinstance(zoom, (int, float)) or float(zoom) != 1.0:
            errors.append(f"{label}.browser_zoom must be 1")
        dpr = capture.get("device_scale_factor")
        if not isinstance(dpr, (int, float)) or float(dpr) <= 0:
            errors.append(f"{label}.device_scale_factor must be positive")
        else:
            expected_pixels = (
                round(css_width * float(dpr)),
                round(css_height * float(dpr)),
            )
            if (actual_width, actual_height) != expected_pixels:
                errors.append(
                    f"{label} pixel dimensions {(actual_width, actual_height)} "
                    f"do not match viewport {raw_viewport} and DPR {dpr}"
                )

        if capture.get("capture_source") not in REAL_CAPTURE_SOURCES:
            errors.append(
                f"{label}.capture_source must be one of {REAL_CAPTURE_SOURCES}"
            )
        source_revision = capture.get("source_revision")
        if not isinstance(source_revision, str) or not source_revision.strip():
            errors.append(f"{label}.source_revision must be non-empty")

        checks = capture.get("checks")
        if not isinstance(checks, dict):
            errors.append(f"{label}.checks must be an object")
        else:
            for check in REQUIRED_CHECKS:
                if checks.get(check) is not True:
                    errors.append(f"{label}.checks.{check} must be true")
        if capture.get("verdict") != "pass":
            errors.append(f"{label}.verdict must be pass")

    for state in required_states:
        for required_viewport in required_viewports:
            if (state, required_viewport) not in covered:
                errors.append(
                    f"missing capture for state={state} viewport={required_viewport}"
                )

    uncovered = data.get("uncovered_components", [])
    if not isinstance(uncovered, list):
        raise ValueError("uncovered_components must be a list")
    for index, component in enumerate(uncovered):
        label = f"uncovered_components[{index}]"
        if not isinstance(component, dict):
            errors.append(f"{label} must be an object")
            continue
        if not isinstance(component.get("name"), str) or not component["name"].strip():
            errors.append(f"{label}.name must be non-empty")
        if (
            not isinstance(component.get("derived_from"), str)
            or not component["derived_from"].strip()
        ):
            errors.append(f"{label}.derived_from must be non-empty")
        elif component["derived_from"] not in approved_components:
            errors.append(f"{label}.derived_from must name an approved_components id")

    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="验证绑定视觉参考的同状态截图、视口、缩放、摘要和组件派生收据。"
    )
    parser.add_argument("receipt", help="视觉验收 JSON 收据")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    receipt_path = Path(args.receipt).resolve()
    try:
        errors = validate_receipt(receipt_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"visual evidence: error: {error}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(f"visual evidence: {error}", file=sys.stderr)
        return 1
    print(f"visual evidence: passed receipt={receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
