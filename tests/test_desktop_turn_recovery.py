from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lifecycle_hook


class DesktopTurnRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.transcript = self.sessions / "rollout-source.jsonl"
        self.snapshot = {
            "root": str(self.root), "head": "abc", "ledger_sha256": "ledger",
            "worktree_status_sha256": "clean", "ready_ids": [], "runnable_ids": [],
            "candidate_revisions": [], "ledger_errors": [], "assignment_liveness": {},
            "rule_handshake": {"state": "current", "blocking": False},
        }
        self.prior = {
            "active_turn_id": "old", "must_yield": False,
            "pending_control_event": True, "triggers": ["ledger_changed"],
            "tool_trace_overflow": True,
            "tool_trace": [{"turn_id": "old", "tool_use_id": "old-call"}],
            "inflight_tool_use_ids": ["unfinished"],
            "pending_terminal_receipts": ["/tmp/result.json"],
            "next_action": "review existing candidate", "requires_user": False,
            "snapshot": self.snapshot,
        }
        self.write_transcript()
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", {"CODEX_HOME": str(self.root)}).start()

    def write_transcript(self, session="source", turn="new", terminal=False):
        rows = [
            {"type": "session_meta", "payload": {"id": session}},
            {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "old"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
        ]
        if terminal:
            rows.append({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}})
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def append_command_completion(
        self,
        *,
        tool_use_id="new-call",
        turn="new",
        command="git status --short",
        exit_code=0,
        output="done",
        stderr="",
        aggregated_output=None,
        formatted_output=None,
        status="completed",
    ):
        row = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": turn,
                "item": {
                    "type": "CommandExecution",
                    "id": tool_use_id,
                    "command": ["/bin/zsh", "-lc", command],
                    "status": status,
                    "stdout": output,
                    "aggregated_output": (
                        output if aggregated_output is None else aggregated_output
                    ),
                    "formatted_output": (
                        output if formatted_output is None else formatted_output
                    ),
                    "stderr": stderr,
                    "exit_code": exit_code,
                },
            },
        }
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def append_file_change_completion(
        self,
        *,
        tool_use_id="new-call",
        turn="new",
        event_type="item_completed",
    ):
        row = {
            "type": "event_msg",
            "payload": {
                "type": event_type,
                "turn_id": turn,
                "item": {
                    "type": "FileChange",
                    "id": tool_use_id,
                    "changes": [],
                },
            },
        }
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def make_guard_case(self):
        repo = Path(tempfile.mkdtemp(dir=self.root, prefix="repo-"))
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        ledger = repo / "TASK_LEDGER.md"
        ledger.write_text("# Ledger\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "TASK_LEDGER.md"], check=True)
        subprocess.run(
            [
                "git", "-C", str(repo), "-c", "user.name=Test",
                "-c", "user.email=test@example.com", "commit", "-qm", "base",
            ],
            check=True,
        )
        guard_snapshot = {
            "event_contract": {
                "event_id": "event-1",
                "event_type": "stop_yield",
                "terminal_receipt": "closed",
            },
            "goal_rollover": {"status": "continued"},
        }
        guard_snapshot_path = repo / "receipt.json"
        guard_snapshot_path.write_text(json.dumps(guard_snapshot), encoding="utf-8")
        command = (
            f"{sys.executable} {Path(lifecycle_hook.__file__).with_name('control_event_guard.py')} "
            f"{guard_snapshot_path} --ledger {ledger} --repo {repo} "
            "--controller-session logical"
        )
        snapshot = {
            **self.snapshot,
            "root": str(repo),
            "head": subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            "ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
            "control_loop_required": True,
        }
        event = self.event()
        event["cwd"] = str(repo)
        event["tool_use_id"] = "guard-call"
        event["tool_input"] = {"command": command}
        return repo, guard_snapshot, command, snapshot, event

    def write_closed_evidence(self, repo, guard_snapshot, snapshot):
        event_id = guard_snapshot["event_contract"]["event_id"]
        common_dir = Path(
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        )
        if not common_dir.is_absolute():
            common_dir = (repo / common_dir).resolve()
        target = (
            common_dir / "adaptive-delivery" / "controller-cycle-evidence"
            / f"{hashlib.sha256(event_id.encode()).hexdigest()}.json"
        )
        target.parent.mkdir(parents=True)
        canonical_snapshot = json.dumps(
            guard_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        target.write_text(json.dumps({
            "schema_version": 1,
            "record_kind": "controller_cycle_evidence",
            "evidence_id": event_id,
            "cycle_id": event_id,
            "controller_id": "logical",
            "terminal_status": "CLOSED",
            "main_revision": snapshot["head"],
            "ledger_sha256": snapshot["ledger_sha256"],
            "snapshot_sha256": hashlib.sha256(canonical_snapshot).hexdigest(),
        }), encoding="utf-8")

    def event(self, name="PreToolUse", turn="new"):
        return {
            "hook_event_name": name, "session_id": "logical",
            "controller_session_id": "logical", "source_session_id": "source",
            "controller_host": "desktop_codex", "turn_id": turn,
            "transcript_path": str(self.transcript), "cwd": str(self.root),
            "tool_name": "Bash", "tool_use_id": "new-call",
            "tool_input": {"command": "git status --short"},
        }

    def test_delegated_turn_uses_host_start_without_user_prompt(self):
        output, state = lifecycle_hook.evaluate_event(
            self.event(), snapshot=self.snapshot, prior_state=self.prior,
        )
        self.assertEqual(output, {})
        self.assertEqual(state["active_turn_id"], "new")
        self.assertFalse(state["tool_trace_overflow"])
        self.assertEqual(state["tool_trace"], [])
        self.assertEqual(state["inflight_tool_use_ids"], ["new-call"])
        self.assertTrue(state["pending_control_event"])
        self.assertEqual(state["pending_terminal_receipts"], ["/tmp/result.json"])

    def test_turn_transition_archives_old_trace_before_replacing_it(self):
        path = self.root / "state.json"
        lifecycle_hook.write_json(path, self.prior)
        output, state = lifecycle_hook.persist_event_state(path, self.event(), self.snapshot)
        self.assertEqual(output, {})
        self.assertEqual(state["active_turn_id"], "new")
        archive = path.with_suffix(".turns.jsonl")
        records = [json.loads(line) for line in archive.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tool_trace"], self.prior["tool_trace"])
        self.assertEqual(records[0]["inflight_tool_use_ids"], ["unfinished"])
        self.assertTrue(records[0]["tool_trace_overflow"])
        self.assertNotIn("command", archive.read_text())

    def test_unproven_or_foreign_turn_cannot_unlock(self):
        for case in ("missing", "foreign", "stale", "terminal", "outside", "web", "nested"):
            with self.subTest(case=case):
                self.write_transcript()
                event = self.event()
                if case == "missing":
                    event.pop("transcript_path")
                elif case == "foreign":
                    self.write_transcript(session="another-session")
                elif case == "stale":
                    self.write_transcript(turn="later")
                elif case == "terminal":
                    self.write_transcript(terminal=True)
                elif case == "outside":
                    outside = self.root / "fake.jsonl"
                    outside.write_bytes(self.transcript.read_bytes())
                    event["transcript_path"] = str(outside)
                elif case == "web":
                    event["controller_host"] = "web"
                else:
                    self.write_transcript(turn="old")
                    with self.transcript.open("a") as stream:
                        stream.write(json.dumps({"type": "response_item", "payload": {
                            "type": "message", "text": json.dumps({"type": "task_started", "turn_id": "new"}),
                        }}) + "\n")
                prior = {**self.prior, "must_yield": True}
                output, state = lifecycle_hook.evaluate_event(event, snapshot=self.snapshot, prior_state=prior)
                self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(state["active_turn_id"], "old")
                self.assertTrue(state["must_yield"])
                self.assertEqual(state["tool_trace"], prior["tool_trace"])

    def test_overflow_stop_ends_loop_without_closing_pending_work(self):
        for _ in range(3):
            output, state = lifecycle_hook.evaluate_event(
                self.event("Stop", turn="old"), snapshot=self.snapshot, prior_state=self.prior,
            )
            self.assertIs(output.get("continue"), False)
            self.assertNotEqual(output.get("decision"), "block")
            self.assertTrue(state["pending_control_event"])
            self.assertTrue(state["tool_trace_overflow"])
            self.assertFalse(state["must_yield"])
            self.assertEqual(state["tool_trace"], self.prior["tool_trace"])
            self.assertNotIn("goal_block_authorization", state)
            self.assertEqual(state["adapter_fault"]["code"], "tool_trace_overflow")
            self.prior = state

    def test_unresolved_inflight_at_stop_is_not_an_infinite_receipt_retry(self):
        prior = {**self.prior, "tool_trace_overflow": False}
        output, state = lifecycle_hook.evaluate_event(
            self.event("Stop", turn="old"), snapshot=self.snapshot, prior_state=prior,
        )
        self.assertIs(output.get("continue"), False)
        self.assertEqual(state["inflight_tool_use_ids"], ["unfinished"])
        self.assertTrue(state["pending_control_event"])

    def test_unverified_turn_stop_preserves_old_state_and_ends_loop(self):
        event = self.event("Stop")
        event.pop("transcript_path")
        prior = {**self.prior, "tool_trace_overflow": False, "inflight_tool_use_ids": []}
        output, state = lifecycle_hook.evaluate_event(event, snapshot=self.snapshot, prior_state=prior)
        self.assertIs(output.get("continue"), False)
        self.assertEqual(state["active_turn_id"], "old")
        self.assertEqual(state["adapter_fault"]["code"], "unverified_turn_boundary")

    def test_archive_failure_denies_transition_without_losing_old_evidence(self):
        path = self.root / "state.json"
        lifecycle_hook.write_json(path, self.prior)
        path.with_suffix(".turns.jsonl").mkdir()
        output, state = lifecycle_hook.persist_event_state(path, self.event(), self.snapshot)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(state["active_turn_id"], "old")
        self.assertEqual(lifecycle_hook.load_json(path), self.prior)

    def test_late_tool_result_cannot_pollute_or_unlock_current_turn(self):
        prior = {**self.prior, "active_turn_id": "new", "must_yield": True,
                 "tool_trace_overflow": False, "tool_trace": [], "inflight_tool_use_ids": []}
        event = self.event("PostToolUse", turn="old")
        event["tool_response"] = {"exit_code": 0}
        _output, state = lifecycle_hook.evaluate_event(event, snapshot=self.snapshot, prior_state=prior)
        self.assertEqual(state["tool_trace"], [])
        self.assertTrue(state["must_yield"])
        self.assertEqual(state["active_turn_id"], "new")

    def test_trace_overflow_blocks_new_tool_without_erasing_trace(self):
        output, state = lifecycle_hook.evaluate_event(
            self.event(turn="old"), snapshot=self.snapshot, prior_state=self.prior,
        )
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(state["tool_trace"], self.prior["tool_trace"])
        self.assertEqual(state["inflight_tool_use_ids"], ["unfinished"])

    def test_completed_tool_trace_is_hash_archived_without_overflow(self):
        state = {
            "active_turn_id": "long-controller-turn",
            "tool_trace": [],
            "tool_trace_overflow": False,
            "inflight_tool_use_ids": [],
        }
        for index in range(lifecycle_hook.MAX_TOOL_TRACE_ENTRIES + 1):
            lifecycle_hook._record_tool_trace(state, {
                "hook_event_name": "PostToolUse",
                "turn_id": "long-controller-turn",
                "tool_use_id": f"completed-{index}",
                "tool_name": "Bash",
                "tool_input": {"command": f"git status --short #{index}"},
                "tool_response": {"exit_code": 0},
            })

        self.assertFalse(state["tool_trace_overflow"])
        self.assertEqual(len(state["tool_trace"]), lifecycle_hook.MAX_TOOL_TRACE_ENTRIES)
        self.assertEqual(state["tool_trace"][0]["tool_use_id"], "completed-1")
        self.assertEqual(state["tool_trace_archive"]["entry_count"], 1)
        self.assertTrue(state["tool_trace_archive"]["trace_sha256"])
        projection = lifecycle_hook.machine_trace_projection(state)
        self.assertEqual(projection["archived_entry_count"], 1)
        self.assertEqual(projection["archived_trace_sha256"], state["tool_trace_archive"]["trace_sha256"])

    def test_trace_overflow_retains_every_unfinished_tool(self):
        tool_ids = [f"inflight-{index}" for index in range(lifecycle_hook.MAX_TOOL_TRACE_ENTRIES)]
        state = {
            "active_turn_id": "parallel-controller-turn",
            "tool_trace": [
                {"turn_id": "parallel-controller-turn", "tool_use_id": tool_id}
                for tool_id in tool_ids
            ],
            "tool_trace_overflow": False,
            "inflight_tool_use_ids": tool_ids + ["inflight-new"],
        }
        lifecycle_hook._record_tool_trace(state, {
            "hook_event_name": "PostToolUse",
            "turn_id": "parallel-controller-turn",
            "tool_use_id": "inflight-new",
            "tool_name": "Bash",
            "tool_input": {"command": "git status --short"},
        })

        self.assertTrue(state["tool_trace_overflow"])
        self.assertEqual(len(state["tool_trace"]), lifecycle_hook.MAX_TOOL_TRACE_ENTRIES + 1)
        self.assertEqual(
            {item["tool_use_id"] for item in state["tool_trace"]},
            set(state["inflight_tool_use_ids"]),
        )
        self.assertNotIn("tool_trace_archive", state)

    def test_valid_delegated_turn_unlocks_prior_receipt_once(self):
        prior = {**self.prior, "must_yield": True, "receipt_turn_id": "old"}
        output, state = lifecycle_hook.evaluate_event(self.event(), snapshot=self.snapshot, prior_state=prior)
        self.assertEqual(output, {})
        self.assertFalse(state["must_yield"])
        self.assertNotIn("receipt_turn_id", state)
        state["must_yield"] = True
        output, state = lifecycle_hook.evaluate_event(self.event(), snapshot=self.snapshot, prior_state=state)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(state["must_yield"])

    def test_normal_pending_stop_keeps_blocking_without_confirmed_reentry(self):
        prior = {**self.prior, "tool_trace_overflow": False, "inflight_tool_use_ids": []}
        first_output, first_state = lifecycle_hook.evaluate_event(
            self.event("Stop", turn="old"), snapshot=self.snapshot, prior_state=prior,
        )
        self.assertEqual(first_output.get("decision"), "block")

        second_output, second_state = lifecycle_hook.evaluate_event(
            self.event("Stop", turn="old"), snapshot=self.snapshot, prior_state=first_state,
        )

        self.assertEqual(second_output.get("decision"), "block")
        self.assertNotEqual(second_output.get("continue"), False)
        self.assertTrue(second_state["pending_control_event"])
        self.assertEqual(second_state["triggers"], first_state["triggers"])
        self.assertEqual(second_state["pending_terminal_receipts"], ["/tmp/result.json"])
        self.assertNotIn("host_turn_handoff", second_state)
        self.assertEqual(second_state["stop_continuations"], 2)

    def test_pending_stop_terminalizes_only_after_confirmed_desktop_reentry(self):
        prior = {**self.prior, "tool_trace_overflow": False, "inflight_tool_use_ids": []}
        event = self.event("Stop", turn="old")
        event["controller_session_id"] = "logical"
        event["source_session_id"] = "desktop-current"
        first_output, first_state = lifecycle_hook.evaluate_event(
            event, snapshot=self.snapshot, prior_state=prior,
        )
        self.assertEqual(first_output.get("decision"), "block")
        first_state["desktop_reentry"] = {
            "result": "CONFIRMED",
            "state": "RESUME_SUCCEEDED",
            "controller_id": "logical",
            "execution_target_session_id": "desktop-current",
            "debt_fingerprint": lifecycle_hook.continuation_debt_fingerprint(first_state),
        }
        second_output, second_state = lifecycle_hook.evaluate_event(
            event, snapshot=self.snapshot, prior_state=first_state,
        )
        self.assertEqual(second_output.get("decision"), "block")
        self.assertNotEqual(second_output.get("continue"), False)
        self.assertTrue(second_state["pending_control_event"])
        self.assertEqual(second_state["pending_terminal_receipts"], ["/tmp/result.json"])
        self.assertNotIn("host_turn_handoff", second_state)

    def test_verify_host01_blocks_stop_until_confirmed_desktop_reentry(self):
        snapshot = {
            **self.snapshot,
            "ready_ids": [],
            "runnable_ids": [],
            "task_states": {"HOST-01": "VERIFY"},
        }
        prior = {
            "active_turn_id": "old",
            "must_yield": False,
            "pending_control_event": False,
            "triggers": [],
            "tool_trace_overflow": False,
            "inflight_tool_use_ids": [],
            "snapshot": snapshot,
        }
        event = self.event("Stop", turn="old")
        event["controller_session_id"] = "logical"
        event["source_session_id"] = "desktop-current"
        first_output, first_state = lifecycle_hook.evaluate_event(
            event, snapshot=snapshot, prior_state=prior,
        )
        self.assertEqual(first_output.get("decision"), "block")
        self.assertTrue(first_state["pending_control_event"])
        self.assertIn("VERIFY:HOST-01", first_state["triggers"])

        second_output, second_state = lifecycle_hook.evaluate_event(
            event, snapshot=snapshot, prior_state=first_state,
        )
        self.assertEqual(second_output.get("decision"), "block")
        self.assertNotEqual(second_output.get("continue"), False)
        self.assertTrue(second_state["pending_control_event"])

        second_state["desktop_reentry"] = {
            "result": "CONFIRMED",
            "state": "RESUME_SUCCEEDED",
            "controller_id": "logical",
            "execution_target_session_id": "desktop-current",
            "debt_fingerprint": lifecycle_hook.continuation_debt_fingerprint(second_state),
        }
        third_output, third_state = lifecycle_hook.evaluate_event(
            event, snapshot=snapshot, prior_state=second_state,
        )
        self.assertEqual(third_output.get("decision"), "block")
        self.assertNotEqual(third_output.get("continue"), False)
        self.assertTrue(third_state["pending_control_event"])
        self.assertNotIn("host_turn_handoff", third_state)

    def test_host_turn_handoff_arms_existing_same_controller_supervisor(self):
        state_path = self.root / "handoff-state.json"
        state = {
            "pending_control_event": True,
            "requires_user": False,
            "controller_host": "desktop_codex",
            "wake_generation": 4,
            "host_turn_handoff": {
                "schema_version": 1,
                "state": "requested",
                "turn_id": "old",
                "wake_generation": 4,
                "debt_fingerprint": "debt-1",
            },
        }
        lifecycle_hook.write_json(state_path, state)
        calls = []

        result = lifecycle_hook.arm_host_turn_handoff(
            path=state_path,
            lifecycle_state=state,
            controller_id="logical",
            repo=self.root,
            registry=self.root / "registry.json",
            supervisor_ensurer=lambda **kwargs: calls.append(kwargs) or True,
        )

        self.assertEqual(result["state"], "delegated")
        self.assertEqual(result["delivery_state"], "supervisor_started")
        self.assertEqual(calls[0]["session_id"], "logical")
        saved = lifecycle_hook.load_json(state_path)
        self.assertTrue(saved["pending_control_event"])
        self.assertEqual(saved["host_turn_handoff"]["debt_fingerprint"], "debt-1")
        self.assertEqual(saved["host_turn_handoff"]["state"], "delegated")

    def test_fresh_turn_reenters_handoff_and_resets_only_turn_local_stop_count(self):
        prior = {
            **self.prior,
            "tool_trace_overflow": False,
            "inflight_tool_use_ids": [],
            "stop_continuations": 2,
            "stop_continuation_turn_id": "old",
            "host_turn_handoff": {
                "schema_version": 1,
                "state": "delegated",
                "turn_id": "old",
                "wake_generation": 4,
                "debt_fingerprint": "debt-1",
            },
        }
        output, state = lifecycle_hook.evaluate_event(
            self.event("UserPromptSubmit", turn="new"),
            snapshot=self.snapshot,
            prior_state=prior,
        )

        self.assertEqual(output, {})
        self.assertEqual(state["active_turn_id"], "new")
        self.assertEqual(state["stop_continuations"], 0)
        self.assertNotIn("stop_continuation_turn_id", state)
        self.assertNotIn("host_turn_handoff", state)
        self.assertEqual(state["last_host_turn_handoff"]["state"], "reentered")
        self.assertEqual(state["last_host_turn_handoff"]["reentry_turn_id"], "new")
        self.assertTrue(state["pending_control_event"])
        self.assertEqual(state["pending_terminal_receipts"], ["/tmp/result.json"])

    def inflight_record(self, *, command="git status --short", turn="new"):
        tool_input = {"command": command}
        input_bytes = json.dumps(
            tool_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return {
            "turn_id": turn,
            "tool_name": "Bash",
            "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        }

    def test_rollout_command_completion_without_inflight_record_stays_pending(self):
        self.append_command_completion(exit_code=7, status="failed", output="failed")
        prior = {
            **self.prior,
            "active_turn_id": "new",
            "tool_trace_overflow": False,
            "tool_trace": [],
            "inflight_tool_use_ids": ["new-call", "still-running"],
        }
        event = self.event()
        event["tool_use_id"] = "next-call"
        output, state = lifecycle_hook.evaluate_event(
            event, snapshot=self.snapshot, prior_state=prior,
        )
        self.assertEqual(output, {})
        self.assertEqual(
            state["inflight_tool_use_ids"],
            ["new-call", "still-running", "next-call"],
        )
        self.assertEqual(state["tool_trace"], [])

    def test_rollout_command_completion_with_exact_record_clears_only_that_tool(self):
        self.append_command_completion(exit_code=7, status="failed", output="failed")
        prior = {
            **self.prior,
            "active_turn_id": "new",
            "tool_trace_overflow": False,
            "tool_trace": [],
            "inflight_tool_use_ids": ["new-call", "still-running"],
            "inflight_tool_records": {
                "new-call": self.inflight_record(),
            },
        }
        event = self.event()
        event["tool_use_id"] = "next-call"
        output, state = lifecycle_hook.evaluate_event(
            event, snapshot=self.snapshot, prior_state=prior,
        )
        self.assertEqual(output, {})
        self.assertEqual(state["inflight_tool_use_ids"], ["still-running", "next-call"])
        completed = next(
            item for item in state["tool_trace"]
            if item["tool_use_id"] == "new-call"
        )
        self.assertEqual(completed["response_status"], 7)

    def test_exact_turn_item_completed_in_tail_is_reconciled_when_task_started_is_outside_fixed_tail(self):
        tail_bytes = 8 * 1024 * 1024
        prefix = "".join(
            json.dumps(row) + "\n"
            for row in (
                {"type": "session_meta", "payload": {"id": "source"}},
                {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "old"}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "new"}},
            )
        ).encode("utf-8")
        task_started_at = prefix.find(b"task_started")
        with self.transcript.open("wb") as stream:
            stream.write(prefix)
            stream.write(b'{"type":"response_item","payload":{"type":"message","text":"')
            stream.write(b"x" * tail_bytes)
            stream.write(b'"}}\n')
            item_completed_at = stream.tell()
        self.append_command_completion()
        size = self.transcript.stat().st_size
        self.assertGreater(size, tail_bytes)
        self.assertLess(task_started_at, size - tail_bytes)
        self.assertGreaterEqual(item_completed_at, size - tail_bytes)

        prior = {
            **self.prior,
            "active_turn_id": "new",
            "tool_trace_overflow": False,
            "tool_trace": [],
            "inflight_tool_use_ids": ["new-call", "still-running"],
            "inflight_tool_records": {
                "new-call": self.inflight_record(),
            },
        }
        _output, state = lifecycle_hook.evaluate_event(
            self.event("Stop"), snapshot=self.snapshot, prior_state=prior,
        )
        self.assertEqual(state["inflight_tool_use_ids"], ["still-running"])
        completed = next(
            item for item in state["tool_trace"]
            if item["tool_use_id"] == "new-call"
        )
        self.assertEqual(completed["response_status"], 0)

    def test_truncated_rollout_does_not_reconcile_old_completion_after_later_turn_started(self):
        tail_bytes = 8 * 1024 * 1024
        prefix = "".join(
            json.dumps(row) + "\n"
            for row in (
                {"type": "session_meta", "payload": {"id": "source"}},
                {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "old"}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "new"}},
            )
        ).encode("utf-8")
        task_started_at = prefix.find(b"task_started")
        with self.transcript.open("wb") as stream:
            stream.write(prefix)
            stream.write(b'{"type":"response_item","payload":{"type":"message","text":"')
            stream.write(b"x" * tail_bytes)
            stream.write(b'"}}\n')
            later_turn_started_at = stream.tell()
            stream.write(json.dumps({
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "later"},
            }).encode("utf-8") + b"\n")
        self.append_command_completion()
        size = self.transcript.stat().st_size
        self.assertGreater(size, tail_bytes)
        self.assertLess(task_started_at, size - tail_bytes)
        self.assertGreaterEqual(later_turn_started_at, size - tail_bytes)

        prior = {
            **self.prior,
            "active_turn_id": "new",
            "tool_trace_overflow": False,
            "tool_trace": [],
            "inflight_tool_use_ids": ["new-call"],
            "inflight_tool_records": {"new-call": self.inflight_record()},
        }
        output, state = lifecycle_hook.evaluate_event(
            self.event("Stop"), snapshot=self.snapshot, prior_state=prior,
        )

        self.assertIs(output.get("continue"), False)
        self.assertEqual(state["inflight_tool_use_ids"], ["new-call"])
        self.assertEqual(state["tool_trace"], [])

    def test_truncated_rollout_rebuilds_target_boundary_after_older_turn_started(self):
        tail_bytes = 8 * 1024 * 1024
        prefix = "".join(
            json.dumps(row) + "\n"
            for row in (
                {"type": "session_meta", "payload": {"id": "source"}},
                {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "old"}},
            )
        ).encode("utf-8")
        with self.transcript.open("wb") as stream:
            stream.write(prefix)
            stream.write(b'{"type":"response_item","payload":{"type":"message","text":"')
            stream.write(b"x" * tail_bytes)
            stream.write(b'"}}\n')
            older_turn_started_at = stream.tell()
            stream.write(json.dumps({
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "old"},
            }).encode("utf-8") + b"\n")
            target_turn_started_at = stream.tell()
            stream.write(json.dumps({
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "new"},
            }).encode("utf-8") + b"\n")
        self.append_command_completion()
        size = self.transcript.stat().st_size
        self.assertGreater(size, tail_bytes)
        self.assertGreaterEqual(older_turn_started_at, size - tail_bytes)
        self.assertGreaterEqual(target_turn_started_at, size - tail_bytes)

        prior = {
            **self.prior,
            "active_turn_id": "new",
            "tool_trace_overflow": False,
            "tool_trace": [],
            "inflight_tool_use_ids": ["new-call", "still-running"],
            "inflight_tool_records": {"new-call": self.inflight_record()},
        }
        _output, state = lifecycle_hook.evaluate_event(
            self.event("Stop"), snapshot=self.snapshot, prior_state=prior,
        )

        self.assertEqual(state["inflight_tool_use_ids"], ["still-running"])
        completed = next(
            item for item in state["tool_trace"]
            if item["tool_use_id"] == "new-call"
        )
        self.assertEqual(completed["response_status"], 0)

    def test_small_rollout_without_exact_task_started_does_not_reconcile_completion(self):
        self.transcript.write_text(json.dumps({
            "type": "session_meta", "payload": {"id": "source"},
        }) + "\n")
        self.append_command_completion()
        prior = {
            **self.prior,
            "active_turn_id": "new",
            "tool_trace_overflow": False,
            "tool_trace": [],
            "inflight_tool_use_ids": ["new-call"],
            "inflight_tool_records": {"new-call": self.inflight_record()},
        }

        output, state = lifecycle_hook.evaluate_event(
            self.event("Stop"), snapshot=self.snapshot, prior_state=prior,
        )

        self.assertIs(output.get("continue"), False)
        self.assertEqual(state["inflight_tool_use_ids"], ["new-call"])
        self.assertEqual(state["tool_trace"], [])

    def test_rollout_file_change_completion_clears_only_exact_current_apply_patch(self):
        for case in (
            "exact", "wrong_turn", "wrong_id", "not_completed", "wrong_tool",
            "missing_record",
        ):
            with self.subTest(case=case):
                self.write_transcript()
                completion_turn = "new" if case != "wrong_turn" else "old"
                completion_id = "new-call" if case != "wrong_id" else "other-call"
                self.append_file_change_completion(
                    tool_use_id=completion_id,
                    turn=completion_turn,
                    event_type="item_started" if case == "not_completed" else "item_completed",
                )
                prior = {
                    **self.prior,
                    "active_turn_id": "new",
                    "tool_trace_overflow": False,
                    "tool_trace": [],
                    "inflight_tool_use_ids": ["new-call", "still-running"],
                    "inflight_tool_records": {} if case == "missing_record" else {
                        "new-call": {
                            "turn_id": "new",
                            "tool_name": "Bash" if case == "wrong_tool" else "apply_patch",
                            "input_sha256": "a" * 64,
                        },
                    },
                }
                event = self.event()
                event["tool_use_id"] = "next-call"
                _output, state = lifecycle_hook.evaluate_event(
                    event, snapshot=self.snapshot, prior_state=prior,
                )
                if case == "exact":
                    self.assertEqual(
                        state["inflight_tool_use_ids"],
                        ["still-running", "next-call"],
                    )
                else:
                    self.assertEqual(
                        state["inflight_tool_use_ids"],
                        ["new-call", "still-running", "next-call"],
                    )

    def test_rollout_completion_requires_exact_record_turn_and_command_hash(self):
        for case in ("wrong_turn", "missing_hash", "wrong_hash"):
            with self.subTest(case=case):
                self.write_transcript()
                self.append_command_completion()
                record = self.inflight_record()
                if case == "wrong_turn":
                    record["turn_id"] = "old"
                elif case == "missing_hash":
                    record.pop("command_sha256")
                else:
                    record["command_sha256"] = "0" * 64
                prior = {
                    **self.prior,
                    "active_turn_id": "new",
                    "tool_trace_overflow": False,
                    "tool_trace": [],
                    "inflight_tool_use_ids": ["new-call"],
                    "inflight_tool_records": {"new-call": record},
                }
                output, state = lifecycle_hook.evaluate_event(
                    self.event("Stop"), snapshot=self.snapshot, prior_state=prior,
                )
                self.assertIs(output.get("continue"), False)
                self.assertEqual(state["inflight_tool_use_ids"], ["new-call"])
                self.assertEqual(state["tool_trace"], [])

    def test_rollout_completion_updates_existing_exact_id_trace_in_place(self):
        self.append_command_completion()
        prior = {
            **self.prior,
            "active_turn_id": "new",
            "tool_trace_overflow": False,
            "tool_trace": [{
                "turn_id": "new",
                "tool_use_id": "new-call",
                "tool_name": "Bash",
                "input_sha256": "a" * 64,
                "response_status": None,
            }],
            "inflight_tool_use_ids": ["new-call"],
            "inflight_tool_records": {
                "new-call": self.inflight_record(),
            },
        }
        event = self.event()
        event["tool_use_id"] = "next-call"
        _output, state = lifecycle_hook.evaluate_event(
            event, snapshot=self.snapshot, prior_state=prior,
        )
        matching = [
            item for item in state["tool_trace"]
            if item["tool_use_id"] == "new-call"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["response_status"], 0)
        self.assertEqual(matching[0]["input_sha256"], "a" * 64)

    def test_rollout_recovery_fails_closed_for_untrusted_or_nonmatching_evidence(self):
        for case in (
            "foreign_session", "foreign_turn", "later_turn", "outside",
            "terminal", "unknown_id", "nonterminal",
        ):
            with self.subTest(case=case):
                self.write_transcript()
                completion_id = "new-call"
                completion_turn = "new"
                status = "completed"
                if case == "foreign_session":
                    self.write_transcript(session="another-session")
                elif case == "foreign_turn":
                    completion_turn = "other-turn"
                elif case == "unknown_id":
                    completion_id = "another-call"
                elif case == "nonterminal":
                    status = "running"
                self.append_command_completion(
                    tool_use_id=completion_id, turn=completion_turn, status=status,
                )
                event = self.event("Stop")
                if case == "outside":
                    outside = self.root / "outside.jsonl"
                    outside.write_bytes(self.transcript.read_bytes())
                    event["transcript_path"] = str(outside)
                elif case == "terminal":
                    with self.transcript.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({
                            "type": "event_msg",
                            "payload": {"type": "task_complete", "turn_id": "new"},
                        }) + "\n")
                elif case == "later_turn":
                    with self.transcript.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({
                            "type": "event_msg",
                            "payload": {"type": "task_started", "turn_id": "later"},
                        }) + "\n")
                prior = {
                    **self.prior,
                    "active_turn_id": "new",
                    "tool_trace_overflow": False,
                    "tool_trace": [],
                    "inflight_tool_use_ids": ["new-call"],
                }
                output, state = lifecycle_hook.evaluate_event(
                    event, snapshot=self.snapshot, prior_state=prior,
                )
                self.assertIs(output.get("continue"), False)
                self.assertEqual(state["inflight_tool_use_ids"], ["new-call"])
                self.assertEqual(state["tool_trace"], [])

    def test_idless_ai_bridge_post_cannot_close_or_impersonate_inflight_guard(self):
        _repo, _guard_snapshot, command, snapshot, pre_event = self.make_guard_case()
        _pre_output, prior = lifecycle_hook.evaluate_event(
            pre_event,
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "new",
                "pending_control_event": True,
                "triggers": ["ledger_changed"],
            },
        )
        event = self.event("PostToolUse")
        event.pop("tool_use_id")
        event["tool_name"] = "AI-Bridge.shell_command"
        event["tool_input"] = {"command": command}
        event["tool_response"] = {"exit_code": 0, "output": "control-event: allowed"}
        _output, state = lifecycle_hook.evaluate_event(
            event, snapshot=snapshot, prior_state=prior,
        )
        self.assertEqual(state["inflight_tool_use_ids"], ["guard-call"])
        self.assertEqual(state["control_receipt_inflight"], "guard-call")
        self.assertFalse(state["must_yield"])
        self.assertEqual(state["tool_trace"], [])

    def test_rollout_guard_does_not_trust_allowed_marker_from_stderr(self):
        repo, guard_snapshot, command, snapshot, pre_event = self.make_guard_case()
        _pre_output, pre_state = lifecycle_hook.evaluate_event(
            pre_event,
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "new",
                "pending_control_event": True,
                "triggers": ["ledger_changed"],
            },
        )
        self.append_command_completion(
            tool_use_id="guard-call",
            command=command,
            output="",
            stderr="control-event: allowed",
        )
        self.write_closed_evidence(repo, guard_snapshot, snapshot)
        stop = self.event("Stop")
        stop["cwd"] = str(repo)
        output, state = lifecycle_hook.evaluate_event(
            stop, snapshot=snapshot, prior_state=pre_state,
        )
        self.assertEqual(output["decision"], "block")
        self.assertFalse(state["must_yield"])

    def test_rollout_guard_requires_allowed_marker_in_stdout(self):
        for field in ("aggregated_output", "formatted_output"):
            with self.subTest(field=field):
                self.write_transcript()
                repo, guard_snapshot, command, snapshot, pre_event = self.make_guard_case()
                _pre_output, pre_state = lifecycle_hook.evaluate_event(
                    pre_event,
                    snapshot=snapshot,
                    prior_state={
                        "active_turn_id": "new",
                        "pending_control_event": True,
                        "triggers": ["ledger_changed"],
                    },
                )
                outputs = {
                    "aggregated_output": "",
                    "formatted_output": "",
                }
                outputs[field] = "control-event: allowed"
                self.append_command_completion(
                    tool_use_id="guard-call",
                    command=command,
                    output="",
                    **outputs,
                )
                self.write_closed_evidence(repo, guard_snapshot, snapshot)
                stop = self.event("Stop")
                stop["cwd"] = str(repo)
                output, state = lifecycle_hook.evaluate_event(
                    stop, snapshot=snapshot, prior_state=pre_state,
                )
                self.assertEqual(output["decision"], "block")
                self.assertFalse(state["must_yield"])

    def test_rollout_guard_completion_closes_receipt_only_with_matching_durable_evidence(self):
        repo, guard_snapshot, command, snapshot, pre_event = self.make_guard_case()
        pre_output, pre_state = lifecycle_hook.evaluate_event(
            pre_event,
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "new",
                "pending_control_event": True,
                "triggers": ["ledger_changed"],
            },
        )
        self.assertEqual(pre_output, {})
        self.append_command_completion(
            tool_use_id="guard-call", command=command, output="control-event: allowed",
        )
        self.write_closed_evidence(repo, guard_snapshot, snapshot)
        stop = self.event("Stop")
        stop["cwd"] = str(repo)
        output, state = lifecycle_hook.evaluate_event(
            stop, snapshot=snapshot, prior_state=pre_state,
        )
        self.assertEqual(output, {})
        self.assertEqual(state["inflight_tool_use_ids"], [])
        self.assertTrue(state["must_yield"])
        self.assertEqual(state["receipt_turn_id"], "new")
        self.assertFalse(state["pending_control_event"])

    def test_missing_desktop_guard_post_is_recovered_from_native_transcript_on_stop(self):
        repo, guard_snapshot, command, snapshot, pre_event = self.make_guard_case()
        _pre_output, pre_state = lifecycle_hook.evaluate_event(
            pre_event,
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "new",
                "pending_control_event": True,
                "triggers": ["ledger_changed"],
            },
        )
        incomplete_post = self.event("PostToolUse")
        incomplete_post["cwd"] = str(repo)
        incomplete_post["tool_use_id"] = "guard-call"
        incomplete_post["tool_input"] = {"command": command}
        _post_output, post_state = lifecycle_hook.evaluate_event(
            incomplete_post, snapshot=snapshot, prior_state=pre_state,
        )
        self.assertEqual(post_state["inflight_tool_use_ids"], ["guard-call"])
        self.assertEqual(post_state["control_receipt_inflight"], "guard-call")
        self.assertEqual(
            post_state["control_receipt_proposal"]["event_id"],
            guard_snapshot["event_contract"]["event_id"],
        )

        self.append_command_completion(
            tool_use_id="guard-call", command=command, output="control-event: allowed",
        )
        self.write_closed_evidence(repo, guard_snapshot, snapshot)
        stop = self.event("Stop")
        stop["cwd"] = str(repo)
        output, state = lifecycle_hook.evaluate_event(
            stop, snapshot=snapshot, prior_state=post_state,
        )
        self.assertEqual(output, {})
        self.assertTrue(state["must_yield"])
        self.assertEqual(state["receipt_turn_id"], "new")
        self.assertEqual(state["inflight_tool_use_ids"], [])

    def test_missing_guard_post_recovery_is_exposed_before_same_turn_continuation(self):
        repo, guard_snapshot, command, snapshot, pre_event = self.make_guard_case()
        _pre_output, pre_state = lifecycle_hook.evaluate_event(
            pre_event,
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "new",
                "pending_control_event": True,
                "triggers": ["ledger_changed"],
            },
        )
        incomplete_post = self.event("PostToolUse")
        incomplete_post["cwd"] = str(repo)
        incomplete_post["tool_use_id"] = "guard-call"
        incomplete_post["tool_input"] = {"command": command}
        _post_output, post_state = lifecycle_hook.evaluate_event(
            incomplete_post, snapshot=snapshot, prior_state=pre_state,
        )
        self.append_command_completion(
            tool_use_id="guard-call", command=command, output="control-event: allowed",
        )
        self.write_closed_evidence(repo, guard_snapshot, snapshot)

        recovered_observations = []
        continuation = self.event("PreToolUse")
        continuation["cwd"] = str(repo)
        continuation["tool_use_id"] = "continuation-call"
        continuation["tool_name"] = "Bash"
        continuation["tool_input"] = {"command": "git status --short"}
        output, state = lifecycle_hook.evaluate_event(
            continuation,
            snapshot=snapshot,
            prior_state=post_state,
            recovered_event_observer=lambda event, event_output, event_state: (
                recovered_observations.append((event, event_output, dict(event_state)))
            ),
        )

        self.assertEqual(output, {})
        self.assertEqual(len(recovered_observations), 1)
        recovered_event, recovered_output, recovered_state = recovered_observations[0]
        self.assertEqual(recovered_event["hook_event_name"], "PostToolUse")
        self.assertEqual(recovered_event["tool_use_id"], "guard-call")
        self.assertEqual(recovered_output, {})
        self.assertTrue(recovered_state["must_yield"])
        self.assertEqual(recovered_state["receipt_turn_id"], "new")
        self.assertFalse(state["must_yield"])
        self.assertTrue(state["pending_control_event"])
        self.assertIn("post_receipt_action_started", state["triggers"])

    def test_rollout_guard_recovery_uses_durable_evidence_after_input_is_consumed(self):
        repo, guard_snapshot, command, snapshot, pre_event = self.make_guard_case()
        _pre_output, pre_state = lifecycle_hook.evaluate_event(
            pre_event,
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "new",
                "pending_control_event": True,
                "triggers": ["ledger_changed"],
            },
        )
        self.append_command_completion(
            tool_use_id="guard-call", command=command, output="control-event: allowed",
        )
        self.write_closed_evidence(repo, guard_snapshot, snapshot)
        Path(pre_state["control_receipt_proposal"]["snapshot_path"]).unlink()
        stop = self.event("Stop")
        stop["cwd"] = str(repo)

        output, state = lifecycle_hook.evaluate_event(
            stop, snapshot=snapshot, prior_state=pre_state,
        )

        self.assertEqual(output, {})
        self.assertEqual(state["inflight_tool_use_ids"], [])
        self.assertTrue(state["must_yield"])
        self.assertEqual(state["receipt_turn_id"], "new")
        self.assertFalse(state["pending_control_event"])

    def test_rollout_guard_stdout_without_closed_evidence_cannot_close_receipt(self):
        repo, _guard_snapshot, command, snapshot, pre_event = self.make_guard_case()
        _pre_output, pre_state = lifecycle_hook.evaluate_event(
            pre_event,
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "new",
                "pending_control_event": True,
                "triggers": ["ledger_changed"],
            },
        )
        self.append_command_completion(
            tool_use_id="guard-call", command=command, output="control-event: allowed",
        )
        stop = self.event("Stop")
        stop["cwd"] = str(repo)
        output, state = lifecycle_hook.evaluate_event(
            stop, snapshot=snapshot, prior_state=pre_state,
        )
        self.assertEqual(output["decision"], "block")
        self.assertEqual(state["inflight_tool_use_ids"], [])
        self.assertFalse(state["must_yield"])
        self.assertTrue(state["pending_control_event"])

    def test_native_same_id_guard_post_still_closes_without_rollout_fallback(self):
        _repo, _guard_snapshot, command, snapshot, pre_event = self.make_guard_case()
        _pre_output, pre_state = lifecycle_hook.evaluate_event(
            pre_event,
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "new",
                "pending_control_event": True,
                "triggers": ["ledger_changed"],
            },
        )
        post = dict(pre_event)
        post["hook_event_name"] = "PostToolUse"
        post["tool_response"] = {"exit_code": 0, "output": "control-event: allowed"}
        _output, state = lifecycle_hook.evaluate_event(
            post, snapshot=snapshot, prior_state=pre_state,
        )
        self.assertEqual(state["inflight_tool_use_ids"], [])
        self.assertTrue(state["must_yield"])


if __name__ == "__main__":
    unittest.main()
