"""Official app-server queue fallback regression tests for Desktop reentry.

The executable used here is a local JSON-RPC fixture.  It never connects to a
real Desktop Host or provider.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import web_lifecycle_bridge as bridge


FAKE_APP_SERVER = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

scenario = os.environ["FAKE_QUEUE_SCENARIO"]
log_path = Path(os.environ["FAKE_QUEUE_LOG"])
sentinel_path = Path(os.environ["FAKE_QUEUE_JOURNAL_SENTINEL"])

def emit(message):
    print(json.dumps(message, separators=(",", ":")), flush=True)

def log(message):
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(message, separators=(",", ":")) + "\n")

for line in sys.stdin:
    message = json.loads(line)
    log(message)
    method = message.get("method")
    if method == "initialize":
        emit({"jsonrpc":"2.0","id":message["id"],"result":{}})
    elif method == "thread/resume":
        error = {
            "code": -32603,
            "message": "thread-store conflict: thread desktop-current already has an active writer",
        }
        emit({"jsonrpc":"2.0","id":message["id"],"error":error})
    elif method == "thread/queue/add":
        if not sentinel_path.exists():
            error = {"code":-32603,"message":"durable submitting journal was not written first"}
            emit({"jsonrpc":"2.0","id":message["id"],"error":error})
            continue
        if scenario == "queue_response_lost":
            os._exit(0)
        emit({
            "jsonrpc":"2.0",
            "id":message["id"],
            "result":{"queuedSubmission":{
                "id":"queue-submission-1",
                "clientUserMessageId":"delivery-current",
            }},
        })
    elif method == "thread/turns/list":
        client_id = "delivery-current"
        if scenario == "malformed_data":
            emit({"jsonrpc":"2.0","id":message["id"],"result":{"data":None}})
            continue
        if scenario == "malformed_items":
            turns = [{"id":"turn-malformed","status":"completed","items":None}]
        elif scenario == "completed":
            turns = [
                {
                    "id":"turn-unrelated",
                    "status":"completed",
                    "completedAt":1792200000000,
                    "items":[{"type":"userMessage","clientId":"delivery-other"}],
                },
                {
                    "id":"turn-queued-1",
                    "status":"completed",
                    "completedAt":1792200000001,
                    "items":[{"type":"userMessage","clientId":client_id}],
                },
            ]
        elif scenario == "failed":
            turns = [{
                "id":"turn-queued-failed",
                "status":"failed",
                "completedAt":1792200000002,
                "error":{"message":"fixture terminal failure"},
                "items":[{"type":"userMessage","clientId":client_id}],
            }]
        elif scenario in {
            "false_terminal_interrupted",
            "false_terminal_failed",
            "invalid_completed_at_bool",
            "invalid_completed_at_zero",
            "invalid_completed_at_string",
        }:
            completed_at = {
                "invalid_completed_at_bool": True,
                "invalid_completed_at_zero": 0,
                "invalid_completed_at_string": "1792200000003",
            }.get(scenario)
            turns = [{
                "id":"turn-still-running",
                "status":"interrupted" if scenario == "false_terminal_interrupted" else "failed",
                "completedAt":completed_at,
                "items":[{"type":"userMessage","clientId":client_id}],
            }]
        else:
            turns = [
                {
                    "id":"turn-unrelated",
                    "status":"completed",
                    "completedAt":1792200000000,
                    "items":[{"type":"userMessage","clientId":"delivery-other"}],
                },
                {
                    "id":"turn-wrong-client",
                    "status":"completed",
                    "completedAt":1792200000001,
                    "items":[{"type":"userMessage","clientId":"delivery-near-match"}],
                },
            ]
        emit({"jsonrpc":"2.0","id":message["id"],"result":{"data":turns}})
        if scenario == "client_mismatch":
            raise SystemExit(0)
    elif method == "turn/start":
        emit({
            "jsonrpc":"2.0",
            "id":message["id"],
            "error":{"code":-32603,"message":"turn/start must not follow active-writer fallback"},
        })
'''


class DesktopQueueReentryTests(unittest.TestCase):
    delivery_id = "delivery-current"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake_codex = self.root / "fake-codex"
        self.fake_codex.write_text(FAKE_APP_SERVER, encoding="utf-8")
        self.fake_codex.chmod(0o700)
        self.log_path = self.root / "requests.jsonl"
        self.sentinel_path = self.root / "journal-before-submit"
        self.journal: list[dict] = []
        self.fence_entries = 0

    def env(self, scenario: str) -> dict[str, str]:
        return {
            **os.environ,
            "FAKE_QUEUE_SCENARIO": scenario,
            "FAKE_QUEUE_LOG": str(self.log_path),
            "FAKE_QUEUE_JOURNAL_SENTINEL": str(self.sentinel_path),
        }

    @contextmanager
    def submit_fence(self):
        self.fence_entries += 1
        yield {
            "controller_id": "controller-1",
            "execution_target_session_id": "desktop-current",
            "generation": 4,
            "ownership_generation": 6,
        }

    @contextmanager
    def superseded_fence(self):
        self.fence_entries += 1
        yield None

    def queue_journal(self, event: dict) -> None:
        copied = json.loads(json.dumps(event))
        self.journal.append(copied)
        if copied.get("phase") == "submitting":
            self.sentinel_path.write_text("durable", encoding="utf-8")

    def execute(self, scenario: str, *, superseded: bool = False):
        signature = inspect.signature(bridge.execute_codex_app_server_turn)
        self.assertIn("queue_delivery_id", signature.parameters)
        self.assertIn("queue_journal", signature.parameters)
        return bridge.execute_codex_app_server_turn(
            codex=str(self.fake_codex),
            execution_target="desktop-current",
            repo=self.root,
            prompt="continue the existing Controller",
            submit_fence=self.superseded_fence if superseded else self.submit_fence,
            env=self.env(scenario),
            max_runtime_seconds=0.25,
            queue_delivery_id=self.delivery_id,
            queue_journal=self.queue_journal,
        )

    def requests(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def methods(self) -> list[str]:
        return [
            str(message.get("method"))
            for message in self.requests()
            if message.get("method") != "initialized"
        ]

    def test_active_writer_queues_once_and_confirms_only_matching_completed_turn(self) -> None:
        result = self.execute("completed")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["turn_id"], "turn-queued-1")
        self.assertEqual(result["client_user_message_id"], self.delivery_id)
        self.assertEqual(result["queued_submission_id"], "queue-submission-1")
        self.assertEqual(self.fence_entries, 1)
        self.assertEqual(
            self.methods(),
            ["initialize", "thread/resume", "thread/queue/add", "thread/turns/list"],
        )
        queue_request = next(
            message for message in self.requests() if message.get("method") == "thread/queue/add"
        )
        self.assertEqual(queue_request["params"]["threadId"], "desktop-current")
        self.assertEqual(queue_request["params"]["clientUserMessageId"], self.delivery_id)
        self.assertEqual(queue_request["params"]["input"], [{"type":"text","text":"continue the existing Controller"}])
        list_request = next(
            message for message in self.requests() if message.get("method") == "thread/turns/list"
        )
        self.assertEqual(
            list_request["params"],
            {
                "threadId":"desktop-current",
                "limit":8,
                "sortDirection":"desc",
                "itemsView":"summary",
            },
        )

        self.assertEqual([event["phase"] for event in self.journal], ["submitting", "queued", "completed"])
        self.assertTrue(all(event["client_user_message_id"] == self.delivery_id for event in self.journal))
        self.assertNotIn("queued_submission_id", self.journal[0])
        self.assertEqual(self.journal[1]["queued_submission_id"], "queue-submission-1")
        self.assertEqual(self.journal[2]["queued_submission_id"], "queue-submission-1")
        self.assertEqual(self.journal[2]["turn_id"], "turn-queued-1")

    def test_unrelated_completed_turn_and_client_id_mismatch_never_confirm_queue(self) -> None:
        with self.assertRaises(bridge.CodexAppServerResultUnknown):
            self.execute("client_mismatch")

        self.assertEqual(
            [event["phase"] for event in self.journal],
            ["submitting", "queued", "result_unknown"],
        )
        self.assertFalse(any(event.get("phase") == "completed" for event in self.journal))
        self.assertIn("thread/turns/list", self.methods())

    def test_failed_turn_requires_durable_completion_timestamp(self) -> None:
        result = self.execute("failed")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["turn_id"], "turn-queued-failed")
        self.assertEqual(
            [event["phase"] for event in self.journal],
            ["submitting", "queued", "completed"],
        )
        self.assertEqual(self.journal[-1]["turn_status"], "failed")

    def test_status_without_positive_numeric_completed_at_is_not_terminal(self) -> None:
        for scenario in (
            "false_terminal_interrupted",
            "false_terminal_failed",
            "invalid_completed_at_bool",
            "invalid_completed_at_zero",
            "invalid_completed_at_string",
        ):
            with self.subTest(scenario=scenario):
                self.journal.clear()
                self.log_path.unlink(missing_ok=True)
                self.sentinel_path.unlink(missing_ok=True)
                with self.assertRaises(bridge.CodexAppServerResultUnknown):
                    self.execute(scenario)
                self.assertEqual(
                    [event["phase"] for event in self.journal],
                    ["submitting", "queued", "result_unknown"],
                )
                self.assertFalse(any(event.get("turn_id") for event in self.journal))

    def test_queue_add_response_loss_is_result_unknown_and_is_not_retried(self) -> None:
        with self.assertRaises(bridge.CodexAppServerResultUnknown):
            self.execute("queue_response_lost")

        self.assertEqual(self.methods().count("thread/queue/add"), 1)
        self.assertEqual(
            [event["phase"] for event in self.journal],
            ["submitting", "result_unknown"],
        )
        self.assertEqual(self.journal[-1]["client_user_message_id"], self.delivery_id)
        self.assertNotIn("thread/turns/list", self.methods())

    def test_superseded_fence_does_not_queue_or_write_submit_journal(self) -> None:
        result = self.execute("completed", superseded=True)

        self.assertEqual(result["status"], "superseded")
        self.assertEqual(self.fence_entries, 1)
        self.assertNotIn("thread/queue/add", self.methods())
        self.assertNotIn("thread/turns/list", self.methods())
        self.assertEqual(self.journal, [])
        self.assertFalse(self.sentinel_path.exists())

    def test_malformed_turn_list_after_queue_acceptance_is_result_unknown(self) -> None:
        for scenario in ("malformed_data", "malformed_items"):
            with self.subTest(scenario=scenario):
                self.journal.clear()
                self.log_path.unlink(missing_ok=True)
                self.sentinel_path.unlink(missing_ok=True)
                with self.assertRaises(bridge.CodexAppServerResultUnknown):
                    self.execute(scenario)
                self.assertEqual(
                    [event["phase"] for event in self.journal],
                    ["submitting", "queued", "result_unknown"],
                )


class DesktopQueueJournalPersistenceTests(unittest.TestCase):
    controller_id = "controller-1"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps({
            self.controller_id: str(self.repo.resolve()),
            "__controller_sessions__": {self.controller_id: {
                "desktop_codex": ["desktop-current"],
            }},
            "__controller_targets__": {self.controller_id: {"desktop_codex": {
                "status": "active", "session_id": "desktop-current", "generation": 4,
            }}},
            "__controller_execution_ownership__": {self.controller_id: {
                "active_host": "desktop_codex",
                "execution_target_session_id": "desktop-current",
                "generation": 7,
            }},
        }), encoding="utf-8")

    def journal_path(self, receipt_id: str) -> Path:
        identity = {
            "receipt_id": receipt_id,
            "controller_id": self.controller_id,
            "execution_target_session_id": "desktop-current",
            "target_generation": 4,
            "target_mode": "explicit_current",
            "ownership_generation": 7,
        }
        delivery_id = "aar-desktop-" + hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()
        return self.repo / ".git" / "adaptive-delivery" / "desktop-reentry" / f"{delivery_id}.json"

    def write_journal(self, receipt_id: str, *, phase: str) -> Path:
        path = self.journal_path(receipt_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "receipt_id": receipt_id,
            "controller_id": self.controller_id,
            "execution_target_session_id": "desktop-current",
            "target_generation": 4,
            "target_mode": "explicit_current",
            "ownership_generation": 7,
            "phase": phase,
            "client_user_message_id": path.stem,
        }), encoding="utf-8")
        return path

    def invoke(self, receipt_id: str, execute) -> dict:
        with patch.object(
            bridge,
            "resolve_desktop_codex_executable",
            return_value="/Applications/ChatGPT.app/Contents/Resources/codex",
        ), patch.object(
            bridge,
            "execute_codex_app_server_turn",
            side_effect=execute,
        ) as execute_mock:
            result = bridge.execute_desktop_host_resume(
                session_id=self.controller_id,
                repo=self.repo,
                registry=self.registry,
                supervisor_receipt_id=receipt_id,
            )
        result["_execute_mock"] = execute_mock
        return result

    def test_exact_receipt_journal_is_never_resubmitted_even_when_completed(self) -> None:
        self.write_journal("receipt-current", phase="completed")

        result = self.invoke("receipt-current", lambda **_: self.fail("must not submit"))

        self.assertEqual(result["state"], "DESKTOP_HOST_REENTRY_RESULT_UNKNOWN")
        result["_execute_mock"].assert_not_called()

    def test_unresolved_prior_receipt_for_same_controller_blocks_new_receipt(self) -> None:
        for phase in ("submitting", "queued", "result_unknown"):
            with self.subTest(phase=phase):
                previous = self.write_journal(f"receipt-prior-{phase}", phase=phase)
                result = self.invoke(f"receipt-next-{phase}", lambda **_: self.fail("must not submit"))
                self.assertEqual(result["state"], "DESKTOP_HOST_REENTRY_RESULT_UNKNOWN")
                result["_execute_mock"].assert_not_called()
                previous.unlink()

    def test_completed_prior_receipt_allows_distinct_receipt(self) -> None:
        self.write_journal("receipt-prior", phase="completed")

        result = self.invoke(
            "receipt-next",
            lambda **_: {"status": "completed", "turn_id": "turn-next", "command": []},
        )

        self.assertEqual(result["result"], "CONFIRMED")
        result["_execute_mock"].assert_called_once()

    def test_corrupt_exact_receipt_journal_fails_closed_without_submit(self) -> None:
        path = self.journal_path("receipt-current")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")

        result = self.invoke("receipt-current", lambda **_: self.fail("must not submit"))

        self.assertEqual(result["state"], "DESKTOP_HOST_REENTRY_RESULT_UNKNOWN")
        result["_execute_mock"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
