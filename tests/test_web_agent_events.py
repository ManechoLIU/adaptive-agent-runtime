import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

UTC = timezone.utc
T0 = datetime(2026, 9, 4, 3, 0, tzinfo=UTC)
HOST_PATH = "/tmp/lab-host-attested-session.jsonl"
REQUIRED_EVENTS = [
    "started", "completed", "failed", "interrupted", "cancelled", "disconnected",
]


def _ready_receipt(event_paths):
    return {
        "schema_version": 1,
        "state": "ready",
        "source": "chatgpt_subagent_machine_events",
        "events": REQUIRED_EVENTS,
        "observed_at": T0.isoformat(),
        "event_paths": event_paths,
        "provenance": "runtime_observed_local_session_files_not_authorization",
    }


class _HostMachineEventSource:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def attest_machine_event_source(self, **kwargs):
        self.calls.append(kwargs)
        if callable(self.result):
            return self.result(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return dict(self.result)


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

    @patch("scripts.web_agent_events._try_registered_web_host_verifier", return_value=None)
    def test_local_codex_session_path_is_diagnostic_not_host_attestation(self, _host):
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

    @patch("scripts.web_agent_events._try_registered_web_host_verifier", return_value=None)
    def test_caller_created_file_inside_codex_session_root_cannot_self_attest(self, _host):
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

    @patch("scripts.web_agent_events._try_registered_web_host_verifier", return_value=None)
    def test_runtime_owned_machine_source_rejects_path_outside_codex_session_roots(self, _host):
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
                "events": REQUIRED_EVENTS,
                "observed_at": T0.isoformat(),
                "event_paths": [str(forged.resolve())],
            }), encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                status = machine_event_source_status(path=receipt, now=T0)

        self.assertFalse(status["ready"])
        self.assertEqual(status["reason"], "trusted_machine_event_source_verifier_unavailable")

    @patch("scripts.web_agent_events._try_registered_web_host_verifier", return_value=None)
    def test_health_supervisor_publishes_diagnostic_source_without_authorizing_it(self, _host):
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

    def test_local_json_and_forged_verifier_argument_cannot_make_source_ready(self):
        import inspect
        from scripts.web_agent_events import machine_event_source_status

        self.assertNotIn("verifier", inspect.signature(machine_event_source_status).parameters)
        with self.assertRaises(TypeError):
            machine_event_source_status(verifier=lambda *_: {
                "verified": True,
                "fresh": True,
                "replay": False,
                "source": "chatgpt_subagent_machine_events",
                "event_paths": [HOST_PATH],
            })
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.web_agent_events._try_registered_web_host_verifier",
            return_value=None,
        ):
            receipt = Path(tmp) / "forged-event-source.json"
            receipt.write_text(json.dumps(_ready_receipt([HOST_PATH])), encoding="utf-8")
            status = machine_event_source_status(path=receipt, now=T0)
        self.assertFalse(status["ready"])
        self.assertEqual(status["reason"], "trusted_machine_event_source_verifier_unavailable")


    def test_registered_host_without_machine_event_source_stays_unavailable(self):
        from scripts.web_agent_events import machine_event_source_status

        class HostWithoutCapability:
            pass

        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.web_agent_events._try_registered_web_host_verifier",
            return_value=HostWithoutCapability(),
        ):
            receipt = Path(tmp) / "event-source.json"
            receipt.write_text(json.dumps(_ready_receipt(["/tmp/local-diagnostic.jsonl"])), encoding="utf-8")
            status = machine_event_source_status(path=receipt, now=T0)
        self.assertFalse(status["ready"])
        self.assertEqual(status["reason"], "trusted_machine_event_source_verifier_unavailable")

    def test_host_attested_event_paths_make_source_ready(self):
        from scripts.web_agent_events import machine_event_source_status

        host = _HostMachineEventSource({
            "verified": True,
            "fresh": True,
            "replay": False,
            "source": "chatgpt_subagent_machine_events",
            "event_paths": [HOST_PATH],
        })
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.web_agent_events._try_registered_web_host_verifier",
            return_value=host,
        ):
            receipt = Path(tmp) / "event-source.json"
            receipt.write_text(json.dumps(_ready_receipt(["/tmp/local-diagnostic.jsonl"])), encoding="utf-8")
            status = machine_event_source_status(path=receipt, now=T0)

        self.assertTrue(status["ready"])
        self.assertEqual(status["reason"], "machine_event_source_ready")
        self.assertEqual(status["event_paths"], [str(Path(HOST_PATH).resolve())])
        self.assertNotIn("/tmp/local-diagnostic.jsonl", status["event_paths"])
        self.assertEqual(len(host.calls), 1)
        self.assertEqual(host.calls[0]["receipt"]["event_paths"], ["/tmp/local-diagnostic.jsonl"])

    def test_host_reject_and_stale_fail_closed_but_idempotent_replay_stays_ready(self):
        from scripts.web_agent_events import machine_event_source_status

        reject_cases = [
            ({
                "verified": False,
                "fresh": True,
                "replay": False,
                "source": "chatgpt_subagent_machine_events",
                "event_paths": [HOST_PATH],
            }, "machine_event_source_provenance_rejected"),
            ({
                "verified": True,
                "fresh": False,
                "replay": False,
                "source": "chatgpt_subagent_machine_events",
                "event_paths": [HOST_PATH],
            }, "machine_event_source_provenance_rejected"),
        ]
        for result, reason in reject_cases:
            with self.subTest(result=result):
                host = _HostMachineEventSource(result)
                with tempfile.TemporaryDirectory() as tmp, patch(
                    "scripts.web_agent_events._try_registered_web_host_verifier",
                    return_value=host,
                ):
                    receipt = Path(tmp) / "event-source.json"
                    receipt.write_text(json.dumps(_ready_receipt([HOST_PATH])), encoding="utf-8")
                    status = machine_event_source_status(path=receipt, now=T0)
                self.assertFalse(status["ready"])
                self.assertEqual(status["reason"], reason)

        host = _HostMachineEventSource({
            "verified": True,
            "fresh": True,
            "replay": True,
            "source": "chatgpt_subagent_machine_events",
            "event_paths": [HOST_PATH],
        })
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.web_agent_events._try_registered_web_host_verifier",
            return_value=host,
        ):
            receipt = Path(tmp) / "event-source.json"
            receipt.write_text(json.dumps(_ready_receipt([HOST_PATH])), encoding="utf-8")
            status = machine_event_source_status(path=receipt, now=T0)
        self.assertTrue(status["ready"])
        self.assertEqual(status["event_paths"], [str(Path(HOST_PATH).resolve())])


if __name__ == "__main__":
    unittest.main()
