import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

UTC = timezone.utc
T0 = datetime(2026, 9, 4, 3, 0, tzinfo=UTC)


class WebAgentMachineEventSourceTests(unittest.TestCase):
    def _session_log(self, codex_home: Path, *, session_id: str = "session-1") -> Path:
        path = codex_home / "sessions" / "2026" / "09" / "04" / f"rollout-{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "timestamp": T0.isoformat(),
                "type": "session_meta",
                "payload": {"id": session_id},
            },
            {
                "timestamp": T0.isoformat(),
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-1"},
            },
        ]
        path.write_text(
            chr(10).join(json.dumps(row) for row in rows) + chr(10),
            encoding="utf-8",
        )
        return path

    def test_local_codex_session_path_is_diagnostic_not_host_attestation(self):
        from scripts.web_agent_events import (
            machine_event_source_status,
            write_machine_event_source_receipt,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            session = self._session_log(codex_home)
            receipt = root / "event-source.json"
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                written = write_machine_event_source_receipt(path=receipt, now=T0)
                status = machine_event_source_status(path=receipt, now=T0)

        self.assertEqual(written["state"], "observed_unverified")
        self.assertIn(str(session.resolve()), written["event_paths"])
        self.assertFalse(status["ready"])
        self.assertEqual(status["reason"], "trusted_machine_event_source_verifier_unavailable")

    def test_caller_created_file_inside_codex_session_root_cannot_self_attest(self):
        from scripts.web_agent_events import (
            machine_event_source_status,
            write_machine_event_source_receipt,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            forged = self._session_log(codex_home, session_id="caller-forged")
            receipt = root / "event-source.json"
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                written = write_machine_event_source_receipt(path=receipt, now=T0)
                status = machine_event_source_status(path=receipt, now=T0)

        self.assertIn(str(forged.resolve()), written["event_paths"])
        self.assertFalse(status["ready"])
        self.assertEqual(status["reason"], "trusted_machine_event_source_verifier_unavailable")

    def test_runtime_owned_machine_source_rejects_path_outside_codex_session_roots(self):
        from scripts.web_agent_events import machine_event_source_status

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            (codex_home / "sessions").mkdir(parents=True)
            forged = root / "forged.jsonl"
            forged.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "forged"}}) + chr(10),
                encoding="utf-8",
            )
            receipt = root / "event-source.json"
            receipt.write_text(json.dumps({
                "schema_version": 1,
                "state": "ready",
                "source": "chatgpt_subagent_machine_events",
                "events": ["started", "completed", "failed", "interrupted", "cancelled", "disconnected"],
                "observed_at": T0.isoformat(),
                "event_paths": [str(forged.resolve())],
            }), encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                status = machine_event_source_status(path=receipt, now=T0)

        self.assertFalse(status["ready"])
        self.assertEqual(status["reason"], "trusted_machine_event_source_verifier_unavailable")

    def test_health_supervisor_publishes_diagnostic_source_without_authorizing_it(self):
        from scripts import web_agent_health_supervisor as supervisor
        from scripts.web_agent_events import machine_event_source_status

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            session = self._session_log(codex_home)
            receipt = root / "event-source.json"
            heartbeat = root / "heartbeat.json"
            registry = root / "controllers.json"
            registry.write_text("{}", encoding="utf-8")

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False), patch(
                "scripts.web_agent_health_supervisor.reconcile_all_web_agent_health_once",
                return_value=[],
            ):
                rc = supervisor.main([
                    "--once",
                    "--registry", str(registry),
                    "--heartbeat", str(heartbeat),
                    "--event-source", str(receipt),
                ])
                payload = json.loads(receipt.read_text(encoding="utf-8"))
                status = machine_event_source_status(path=receipt, now=None)
                heartbeat_exists = heartbeat.is_file()

        self.assertEqual(rc, 0)
        self.assertTrue(heartbeat_exists)
        self.assertEqual(payload["state"], "observed_unverified")
        self.assertIn(str(session.resolve()), payload["event_paths"])
        self.assertFalse(status["ready"])
        self.assertEqual(status["reason"], "trusted_machine_event_source_verifier_unavailable")


if __name__ == "__main__":
    unittest.main()
