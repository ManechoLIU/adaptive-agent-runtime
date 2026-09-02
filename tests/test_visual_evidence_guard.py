from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "visual_evidence_guard.py"


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, width: int, height: int) -> None:
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    pixels = b"".join(b"\x00" + bytes(width) for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(pixels))
        + png_chunk(b"IEND", b"")
    )


class VisualEvidenceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.reference = self.root / "reference.png"
        self.capture = self.root / "normal-1440x1024.png"
        self.asset = self.root / "approved-scenery.png"
        write_png(self.reference, 1486, 1058)
        write_png(self.capture, 1440, 1024)
        write_png(self.asset, 320, 200)

    def receipt(self) -> dict[str, object]:
        return {
            "references": [
                {
                    "id": "reference-normal",
                    "path": self.reference.name,
                    "sha256": hashlib.sha256(self.reference.read_bytes()).hexdigest(),
                    "role": "binding",
                    "states": ["normal"],
                }
            ],
            "approved_components": [
                {
                    "id": "reference:search-field",
                    "reference": "reference-normal",
                }
            ],
            "required_runtime_assets": ["approved-scenery"],
            "runtime_assets": [
                {
                    "id": "approved-scenery",
                    "path": self.asset.name,
                    "sha256": hashlib.sha256(self.asset.read_bytes()).hexdigest(),
                    "approval": "approved",
                }
            ],
            "required_viewports": ["1440x1024"],
            "required_states": ["normal"],
            "captures": [
                {
                    "state": "normal",
                    "reference": self.reference.name,
                    "viewport": "1440x1024",
                    "screenshot": self.capture.name,
                    "browser_zoom": 1,
                    "device_scale_factor": 1,
                    "capture_source": "real-browser",
                    "source_revision": "working-tree:example",
                    "checks": {
                        "structure": True,
                        "proportion": True,
                        "hierarchy": True,
                        "style": True,
                        "responsive": True,
                    },
                    "verdict": "pass",
                }
            ],
            "uncovered_components": [
                {"name": "new-filter", "derived_from": "reference:search-field"}
            ],
        }

    def run_guard(self, receipt: dict[str, object]) -> subprocess.CompletedProcess[str]:
        receipt_path = self.root / "visual-receipt.json"
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False), encoding="utf-8"
        )
        return subprocess.run(
            [sys.executable, str(GUARD), str(receipt_path)],
            capture_output=True,
            text=True,
        )

    def test_accepts_complete_binding_visual_receipt(self) -> None:
        result = self.run_guard(self.receipt())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("visual evidence: passed", result.stdout)

    def test_rejects_capture_pixels_that_do_not_match_viewport_and_dpr(self) -> None:
        write_png(self.capture, 1600, 1138)

        result = self.run_guard(self.receipt())

        self.assertEqual(result.returncode, 1)
        self.assertIn("pixel dimensions", result.stderr)

    def test_rejects_truncated_png_that_only_has_dimensions(self) -> None:
        self.capture.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">II", 1440, 1024)
        )

        result = self.run_guard(self.receipt())

        self.assertEqual(result.returncode, 1)
        self.assertIn("readable PNG", result.stderr)

    def test_rejects_uncovered_component_without_reference_derivation(self) -> None:
        receipt = self.receipt()
        receipt["uncovered_components"] = [{"name": "new-filter"}]

        result = self.run_guard(receipt)

        self.assertEqual(result.returncode, 1)
        self.assertIn("derived_from", result.stderr)

    def test_rejects_failed_style_comparison(self) -> None:
        receipt = self.receipt()
        captures = receipt["captures"]
        assert isinstance(captures, list)
        capture = captures[0]
        assert isinstance(capture, dict)
        checks = capture["checks"]
        assert isinstance(checks, dict)
        checks["style"] = False

        result = self.run_guard(receipt)

        self.assertEqual(result.returncode, 1)
        self.assertIn("style", result.stderr)

    def test_rejects_reference_that_does_not_cover_capture_state(self) -> None:
        receipt = self.receipt()
        references = receipt["references"]
        assert isinstance(references, list)
        reference = references[0]
        assert isinstance(reference, dict)
        reference["states"] = ["loading"]

        result = self.run_guard(receipt)

        self.assertEqual(result.returncode, 1)
        self.assertIn("does not cover state", result.stderr)

    def test_rejects_unapproved_required_runtime_asset(self) -> None:
        receipt = self.receipt()
        assets = receipt["runtime_assets"]
        assert isinstance(assets, list)
        asset = assets[0]
        assert isinstance(asset, dict)
        asset["approval"] = "candidate"

        result = self.run_guard(receipt)

        self.assertEqual(result.returncode, 1)
        self.assertIn("approval must be approved", result.stderr)

    def test_rejects_derivation_from_undeclared_approved_component(self) -> None:
        receipt = self.receipt()
        uncovered = receipt["uncovered_components"]
        assert isinstance(uncovered, list)
        component = uncovered[0]
        assert isinstance(component, dict)
        component["derived_from"] = "made-up:generic-filter"

        result = self.run_guard(receipt)

        self.assertEqual(result.returncode, 1)
        self.assertIn("approved_components", result.stderr)

    def test_rejects_capture_without_real_runtime_provenance(self) -> None:
        receipt = self.receipt()
        captures = receipt["captures"]
        assert isinstance(captures, list)
        capture = captures[0]
        assert isinstance(capture, dict)
        capture["capture_source"] = "fixture"

        result = self.run_guard(receipt)

        self.assertEqual(result.returncode, 1)
        self.assertIn("capture_source", result.stderr)


if __name__ == "__main__":
    unittest.main()
