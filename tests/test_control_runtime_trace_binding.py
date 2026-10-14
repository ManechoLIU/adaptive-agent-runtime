"""Offline CLI/proposal regressions; no Host events or user lifecycle files are written."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import control_event_guard as guard
import lifecycle_hook as hook


class RuntimeTraceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Trace test")
        self.ledger = self.repo / "TASK_LEDGER.md"
        self.ledger.write_bytes((ROOT / "assets/templates/TASK_LEDGER.md").read_bytes())
        self.git("add", "TASK_LEDGER.md")
        self.git("commit", "-m", "test fixture")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.registry = self.base / "registry.json"
        self.registry.write_text(json.dumps({"test-controller": str(self.repo)}))
        self.states = self.base / "state"
        self.states.mkdir()
        self.state_path = self.states / "test-controller.json"
        # Isolated declarative state fixture, not an injected Pre/PostToolUse event.
        self.state = {"controller_host": "desktop_codex", "active_turn_id": "turn-current",
                      "tool_trace": [{"turn_id": "turn-current", "tool_use_id": "prepare-file",
                                      "tool_name": "Bash", "input_sha256": "a" * 64,
                                      "response_status": 0}], "inflight_tool_use_ids": []}
        self.write_state()
        ledger_sha = hashlib.sha256(self.ledger.read_bytes()).hexdigest()
        self.snapshot = {
            "event_contract": {"event_id": "bound-checkpoint", "event_type": "dispatch",
                               "primary_task": "CONTROL-WAVE-1", "candidate_revision": self.head,
                               "allowed_actions": ["ledger_sync"], "allowed_files": [],
                               "terminal_receipt": "test fixture checked"},
            "event_actions": [{"action": "ledger_sync", "primary_task": "CONTROL-WAVE-1",
                               "candidate_revision": self.head, "files": [],
                               "required_to_close_current_state": True}],
            "terminal_receipt_issued": True, "ledger_sha256": ledger_sha,
            "available_slots": 1,
            "capacity_projection": {"source": "host_runtime", "evidence": "receipt:test-capacity",
                                    "total_slots": 1, "occupied_task_ids": []},
            "ready_packages": [{"id": "INIT-01", "decision": "active", "task_id": "INIT-01-WRITER",
                                "delivered_ack": True}],
            "required_reviews": [], "candidate_packages": [], "new_assignments": [],
            "controller_actions": [], "correction_actions": [],
            "control_loop_receipt": {"scope": "project_wide", "ledger_sha256": ledger_sha,
                                     "runnable_ids": ["INIT-01"], "candidate_revisions": [],
                                     "controller_action_ids": [], "correction_fingerprints": [],
                                     "completed_steps": list(guard.CONTROL_LOOP_STEPS),
                                     "recomputed_after_actions": True},
            "machine_trace": {"turn_id": "turn-stale", "tool_use_ids": [], "trace_sha256": "stale"}}
        self.file = self.base / "snapshot.json"
        self.write_snapshot()
        self.env = {**os.environ, "PYTHONNOUSERSITE": "1", "AD_CONTROLLER_REGISTRY": str(self.registry),
                    "AD_LIFECYCLE_STATE_DIR": str(self.states)}

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True)

    def write_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def write_snapshot(self):
        self.file.write_text(json.dumps(self.snapshot))

    def arguments(self, *, bind=True, controller="test-controller"):
        return [str(self.file), "--ledger", str(self.ledger), "--repo", str(self.repo),
                "--controller-session", controller] + (["--bind-runtime-trace"] if bind else [])

    def run_guard(self, **kwargs):
        return subprocess.run([sys.executable, str(ROOT / "scripts/control_event_guard.py"),
                               *self.arguments(**kwargs)], env=self.env, cwd=self.repo,
                              capture_output=True, text=True, timeout=10)

    def proposal(self, *, isolated=False, extra_arguments=()):
        prefix = [sys.executable, *(["-s"] if isolated else [])]
        command = shlex.join([*prefix, str(ROOT / "scripts/control_event_guard.py"), *self.arguments(), *extra_arguments])
        # A separate interpreter avoids the legacy suite's module-loader aliases;
        # it reads only this test's saved state and never executes a Host callback.
        driver = "import sys,json;sys.path.insert(0,sys.argv[1]);import lifecycle_hook;print(json.dumps(lifecycle_hook._control_guard_proposal(sys.argv[2],cwd=sys.argv[3])))"
        result = subprocess.run([sys.executable, "-s", "-c", driver, str(ROOT / "scripts"),
                                 command, str(self.repo)], env=self.env, cwd=self.repo,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_bound_cli_accepts_fresh_runtime_trace_without_rewriting_prepared_file(self):
        original = self.file.read_bytes()
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("control-event: allowed", result.stdout)
        self.assertEqual(self.file.read_bytes(), original)
        record = json.loads(guard.controller_cycle_evidence_path(self.repo, "bound-checkpoint").read_text())
        self.assertEqual(record["terminal_status"], "CLOSED")
        self.assertEqual(record["validation_errors"], [])

    def test_legacy_cli_still_rejects_stale_caller_trace(self):
        result = self.run_guard(bind=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("machine_trace", result.stdout)

    def test_pre_proposal_uses_same_effective_fingerprint_as_real_cli_cycle(self):
        proposal = self.proposal()
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.get("runtime_machine_trace", {}).get("turn_id"), "turn-current")
        self.assertEqual(proposal["runtime_machine_trace"]["tool_use_ids"], ["prepare-file"])
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(guard.controller_cycle_evidence_path(self.repo, "bound-checkpoint").read_text())
        self.assertEqual(proposal["cycle_snapshot_sha256"], record["snapshot_sha256"])
        self.assertEqual(proposal["snapshot_sha256"], hashlib.sha256(self.file.read_bytes()).hexdigest())

    def test_binding_does_not_hide_invalid_business_capacity(self):
        self.snapshot["available_slots"] = 9
        self.write_snapshot()
        result = self.run_guard()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("available_slots", result.stdout)

    def test_binding_rejects_wrong_controller(self):
        result = self.run_guard(controller="foreign-controller")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("does not own", result.stdout)

    def test_binding_rejects_overflow_and_missing_current_turn(self):
        for change in ({"tool_trace_overflow": True}, {"active_turn_id": ""}):
            with self.subTest(change=change):
                saved = dict(self.state)
                self.state.update(change)
                self.write_state()
                result = self.run_guard()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.state = saved

    def test_binding_cannot_enable_web_user_halt_or_other_inflight_execution(self):
        for change in ({"controller_host": "web"}, {"user_halt": True},
                       {"inflight_tool_use_ids": ["unrelated-tool"]}):
            with self.subTest(change=change):
                saved = dict(self.state)
                self.state.update(change)
                self.write_state()
                result = self.run_guard()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.state = saved

    def test_bound_proposal_requires_current_turn_closed_cycle_and_unchanged_input(self):
        verify = getattr(hook, "_verified_bound_control_proposal", None)
        self.assertTrue(callable(verify), "bound receipt fingerprint verifier is missing")
        proposal = self.proposal()
        self.assertIsNotNone(proposal)
        proposal["tool_use_id"] = "local-correlation-id"
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        current = {"root": str(self.repo), "head": self.head,
                   "ledger_sha256": hashlib.sha256(self.ledger.read_bytes()).hexdigest()}
        values = dict(tool_use_id="local-correlation-id", controller_id="test-controller",
                      turn_id="turn-current", snapshot=current)
        self.assertTrue(verify(proposal, **values))
        for overrides in ({"tool_use_id": "another-command"}, {"controller_id": "foreign-controller"},
                          {"turn_id": "turn-later"}, {"snapshot": {**current, "head": "wrong-head"}}):
            with self.subTest(overrides=overrides):
                self.assertFalse(verify(proposal, **{**values, **overrides}))
        self.assertFalse(verify(None, **values))
        self.file.write_text(self.file.read_text() + " ")
        self.assertFalse(verify(proposal, **values))

    def test_bound_proposal_rejects_raw_effective_cycle_fingerprint_confusion(self):
        verify = getattr(hook, "_verified_bound_control_proposal", None)
        self.assertTrue(callable(verify), "bound receipt fingerprint verifier is missing")
        proposal = self.proposal()
        proposal["tool_use_id"] = "local-correlation-id"
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        current = {"root": str(self.repo), "head": self.head,
                   "ledger_sha256": hashlib.sha256(self.ledger.read_bytes()).hexdigest()}
        values = dict(tool_use_id="local-correlation-id", controller_id="test-controller",
                      turn_id="turn-current", snapshot=current)
        for mutation in ({"cycle_snapshot_sha256": proposal["snapshot_sha256"]},
                         {"event_id": "unrelated-cycle"}, {"runtime_machine_trace": {"turn_id": "stale"}}):
            with self.subTest(mutation=mutation):
                self.assertFalse(verify({**proposal, **mutation}, **values))
        path = guard.controller_cycle_evidence_path(self.repo, "bound-checkpoint")
        record = json.loads(path.read_text())
        record["terminal_status"] = "FAILED"
        path.write_text(json.dumps(record))
        self.assertFalse(verify(proposal, **values))

    def test_binding_rejects_ambiguous_flags_and_stdin(self):
        for arguments in (self.arguments() + ["--repo", str(self.repo)],
                          ["-", *self.arguments()[1:]],
                          [str(self.file), "--ledger", str(self.ledger), "--bind-runtime-trace"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(ROOT / "scripts/control_event_guard.py"), *arguments],
                                        env=self.env, cwd=self.repo, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn("control-event: allowed", result.stdout)

    def test_trace_change_during_validation_cannot_publish_closed_cycle(self):
        # Perturb only the isolated stored trace after real business validation.
        driver = """import sys,json
sys.path.insert(0,sys.argv[1])
import control_event_guard as guard
from pathlib import Path
original=guard.validate_snapshot
def validate(snapshot,**kwargs):
    errors=original(snapshot,**kwargs)
    path=Path(sys.argv[2]); state=json.loads(path.read_text())
    state['active_turn_id']='turn-later'; path.write_text(json.dumps(state))
    return errors
guard.validate_snapshot=validate
raise SystemExit(guard.main(sys.argv[3:]))
"""
        result = subprocess.run([sys.executable, "-c", driver, str(ROOT / "scripts"),
                                 str(self.state_path), *self.arguments()], env=self.env, cwd=self.repo,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("runtime machine trace changed", result.stdout)
        record = json.loads(guard.controller_cycle_evidence_path(self.repo, "bound-checkpoint").read_text())
        self.assertEqual(record["terminal_status"], "FAILED")

    def test_trusted_python_s_is_recognized_and_fingerprints_the_same_bound_view(self):
        command = shlex.join([sys.executable, "-s", str(ROOT / "scripts/control_event_guard.py"), *self.arguments()])
        self.assertTrue(hook._is_control_guard_command(command, controller_session_id="test-controller", cwd=self.repo))
        proposal = self.proposal(isolated=True)
        self.assertIsNotNone(proposal)
        result = subprocess.run([sys.executable, "-s", str(ROOT / "scripts/control_event_guard.py"),
                                 *self.arguments()], env=self.env, cwd=self.repo,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(guard.controller_cycle_evidence_path(self.repo, "bound-checkpoint").read_text())
        self.assertEqual(proposal["cycle_snapshot_sha256"], record["snapshot_sha256"])

    def test_isolated_python_shape_rejects_arbitrary_flags_wrappers_and_fake_interpreter(self):
        tail = [str(ROOT / "scripts/control_event_guard.py"), *self.arguments()]
        fake = self.base / "python3"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        for prefix in ([sys.executable, "-s", "-s"], [sys.executable, "-I"],
                       [sys.executable, "-s", "-c", "pass"], [str(fake), "-s"],
                       ["env", "PYTHONNOUSERSITE=1", sys.executable, "-s"]):
            with self.subTest(prefix=prefix):
                self.assertFalse(hook._is_control_guard_command(shlex.join([*prefix, *tail]),
                                 controller_session_id="test-controller", cwd=self.repo))

    def test_binding_rejects_equal_form_and_abbreviated_duplicate_flags(self):
        for extra in (["--repo=" + str(self.repo)], ["--controller-session=test-controller"],
                      ["--ledger=" + str(self.ledger)], ["--rep", str(self.repo)],
                      ["--controller-s", "test-controller"]):
            with self.subTest(extra=extra):
                result = subprocess.run([sys.executable, "-s", str(ROOT / "scripts/control_event_guard.py"),
                                         *self.arguments(), *extra], env=self.env, cwd=self.repo,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIsNone(self.proposal(isolated=True, extra_arguments=extra))

    def test_pure_cli_recognition_remains_strict(self):
        pure = shlex.join([sys.executable, str(ROOT / "scripts/control_event_guard.py"), *self.arguments()])
        self.assertTrue(hook._is_control_guard_command(pure, controller_session_id="test-controller", cwd=self.repo))
        for command in (pure + "\n", pure + " ; exit 0", "/bin/zsh -lc " + shlex.quote(pure),
                        "python3 - <<'PY'\nimport control_event_guard\nPY"):
            with self.subTest(command=command):
                self.assertFalse(hook._is_control_guard_command(command, controller_session_id="test-controller", cwd=self.repo))


if __name__ == "__main__":
    unittest.main()
