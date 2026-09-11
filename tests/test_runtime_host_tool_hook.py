from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, SKILL_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


target_guard = load_module("controller_target_guard", "scripts/controller_target_guard.py")
web_bridge = load_module("web_lifecycle_bridge", "scripts/web_lifecycle_bridge.py")
lifecycle_hook = load_module("lifecycle_hook", "scripts/lifecycle_hook.py")
runtime_hook = load_module("runtime_host_tool_hook", "scripts/runtime_host_tool_hook.py")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class RuntimeHostToolHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.repo = self.repo.resolve()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "TASK_LEDGER.md").write_text("task ledger\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "TASK_LEDGER.md"], check=True)
        subprocess.run([
            "git", "-C", str(self.repo), "-c", "user.name=Test",
            "-c", "user.email=test@example.com", "commit", "-qm", "base",
        ], check=True)
        self.controller_id = "controller-1"
        self.conversation_id = "0123456789abcdef0123"
        self.registry = self.root / "controllers.json"
        self.registry.write_text(json.dumps({
            self.controller_id: str(self.repo.resolve()),
            "__controller_sessions__": {self.controller_id: {"web": [self.conversation_id]}},
            "__controller_targets__": {self.controller_id: {"web": {
                "status": "active", "session_id": self.conversation_id, "generation": 4,
            }}},
            "__controller_execution_ownership__": {self.controller_id: {
                "active_host": "web", "execution_target_session_id": self.conversation_id,
                "generation": 9,
            }},
        }), encoding="utf-8")
        identity = target_guard.agent_target.logical_agent_identity(
            agent_type="controller", agent_id=self.controller_id
        )
        target = target_guard.agent_target.verified_execution_target(
            logical_agent=identity, host="web",
            execution_target_session_id=self.conversation_id,
            target_generation=4, ownership_generation=9, provenance="test_host_entry",
        )
        self.turn = target_guard.agent_target.verified_execution_turn(
            verified_target=target, runtime_invocation_id="runtime-invocation-1",
            provenance="test_runtime_web_turn",
        )
        self.lifecycle = self.root / "lifecycle.json"
        self.lifecycle.write_text(json.dumps({
            "active_turn_id": self.turn["turn_id"],
            "pending_control_event": True,
            "triggers": ["READY:task-1"],
            "web_turn_lease": {
                "contract": "runtime_web_turn_lease_v1", "status": "active",
                "generation": 1, "turn_id": self.turn["turn_id"],
                "runtime_invocation_id": self.turn["runtime_invocation_id"],
                "execution_target_session_id": self.conversation_id,
                "target_generation": 4, "ownership_generation": 9,
                "watcher_nonce": "watcher-nonce",
            },
        }), encoding="utf-8")
        self.snapshot = self.repo / "control-snapshot.json"
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        self.snapshot.write_text(json.dumps({
            "head": head,
            "ledger_sha256": hashlib.sha256((self.repo / "TASK_LEDGER.md").read_bytes()).hexdigest(),
            "event_contract": {
                "event_id": "control-event-1", "event_type": "dispatch",
                "primary_task": "task-1",
            }
        }), encoding="utf-8")
        self.command = " ".join((
            sys.executable,
            str(SKILL_ROOT / "scripts" / "control_event_guard.py"),
            str(self.snapshot),
            "--ledger", str(self.repo / "TASK_LEDGER.md"),
            "--repo", str(self.repo),
            "--controller-session", self.controller_id,
        ))
        normalized = {
            "arguments": {"command": self.command, "cwd": str(self.repo), "timeout": 30},
            "tool_name": "run_command",
        }
        self.normalized_utf8 = canonical_json(normalized).decode()
        self.normalized_sha256 = hashlib.sha256(self.normalized_utf8.encode()).hexdigest()
        self.pre = self.host_pre_receipt()
        self.verifier = self.make_verifier()

    def host_pre_receipt(self) -> dict[str, object]:
        return {
            "schema_version": 1, "provenance": "lab_host_tool_pre_receipt_v1",
            "request_digest_profile": "lab_run_command_digest_v1",
            "pre_receipt_id": "hpr_pre-1", "capability_id": "hic_cap-1",
            "host_tool_execution_id": "hte_1", "bridge_call_id": "bc_1",
            "bridge_challenge_nonce": "challenge", "transport_binding_id": "tb_1",
            "workspace": "/Users/echoman/Documents/ChatGPT/Local-Agent-Bridge",
            "production_release_revision": "1" * 40, "production_endpoint_generation": 1,
            "bridge_principal_id": "bridge-principal", "bridge_execution_session_id": "bridge-session",
            "bridge_capability_id": "bridge-capability", "bridge_workspace_id": "local-agent-bridge-production",
            "tool_name": "run_command", "outer_request_sha256": "1" * 64,
            "forwarded_request_sha256": "2" * 64,
            "normalized_request_sha256": self.normalized_sha256,
            "retry_outer_request_sha256": "4" * 64,
            "conversation_id": self.conversation_id, "browser_target_id": "target-1",
            "top_frame_id": "frame-1", "loader_id": "loader-1",
            "generation_anchor_sha256": "3" * 64, "secure_origin": "https://chatgpt.com",
            "extension_binding_sha256": "5" * 64, "extension_instance_id_sha256": "6" * 64,
            "extension_loaded_at_ms": 1, "pre_nonce": "nonce-1", "issued_at_unix_ms": 2,
            "host_epoch_binding_sha256": "7" * 64, "host_mac_sha256": "8" * 64,
        }

    def host_terminal_receipt(self, response_sha256: str) -> dict[str, object]:
        omitted = {"pre_receipt_id", "pre_nonce", "issued_at_unix_ms", "host_mac_sha256"}
        return {
            **{key: value for key, value in self.pre.items() if key not in omitted},
            "provenance": "lab_host_tool_terminal_receipt_v1",
            "terminal_receipt_id": "htr_terminal-1",
            "pre_receipt_id": self.pre["pre_receipt_id"],
            "pre_receipt_sha256": hashlib.sha256(canonical_json(self.pre)).hexdigest(),
            "backend_route": "/v1/tools/run_command", "http_status": 200,
            "response_body_sha256": response_sha256, "terminal_classification": "SUCCESS",
            "issued_at_unix_ms": 3, "host_mac_sha256": "9" * 64,
        }

    def current_entry(self) -> dict[str, object]:
        return {
            "provenance": "runtime_host_current_entry_v1",
            "entry_scope": "runtime_invocation", "machine_source": "host_invocation_context_v1",
            "conversation_id": self.conversation_id, "browser_target_id": "target-1",
            "top_frame_id": "frame-1", "loader_id": "loader-1",
            "generation_anchor_sha256": "3" * 64, "secure_origin": "https://chatgpt.com",
            "target_generation": 4, "ownership_generation": 9,
            "runtime_invocation_id": self.turn["runtime_invocation_id"],
            "host_receipt_id": "entry-1", "observed_at_unix_ms": int(time.time() * 1000),
        }

    def make_verifier(self):
        calls: list[dict[str, object]] = []

        def verify(**_kwargs):
            return True

        def verify_tool_pre(**kwargs):
            calls.append({"operation": "verify_tool_pre", **kwargs})
            return {
                "ok": True, "protocol": "runtime_host_verifier_cli_v2",
                "operation": "verify_tool_pre",
                "receipt_sha256": hashlib.sha256(canonical_json(kwargs["receipt"])).hexdigest(),
                "receipt_id": kwargs["receipt"]["pre_receipt_id"],
                "capability_id": kwargs["receipt"]["capability_id"],
                "host_tool_execution_id": kwargs["receipt"]["host_tool_execution_id"],
                "current_entry": self.current_entry(),
            }

        def verify_tool_terminal(**kwargs):
            calls.append({"operation": "verify_tool_terminal", **kwargs})
            return {
                "ok": True, "protocol": "runtime_host_verifier_cli_v2",
                "operation": "verify_tool_terminal",
                "receipt_sha256": hashlib.sha256(canonical_json(kwargs["receipt"])).hexdigest(),
                "receipt_id": kwargs["receipt"]["terminal_receipt_id"],
                "capability_id": kwargs["receipt"]["capability_id"],
                "host_tool_execution_id": kwargs["receipt"]["host_tool_execution_id"],
                "current_entry": self.current_entry(),
                "pre_chain": {
                    "pre_receipt_id": kwargs["receipt"]["pre_receipt_id"],
                    "pre_receipt_sha256": kwargs["receipt"]["pre_receipt_sha256"],
                },
            }

        verify.verify_tool_pre = verify_tool_pre
        verify.verify_tool_terminal = verify_tool_terminal
        verify.calls = calls
        return verify

    def request(self, op: str, **extra: object) -> dict[str, object]:
        receipt_name = "pre_receipt" if op == "pre" else "terminal_receipt"
        receipt = self.pre if op == "pre" else extra.pop(receipt_name)
        return {
            "request_id": f"request-{op}", "protocol": "runtime_host_tool_hook_v1", "op": op,
            "bridge_call_id": "bc_1", "host_tool_execution_id": "hte_1",
            receipt_name: receipt,
            "tool_intent": {
                "schema_version": 1, "provenance": "runtime_host_tool_intent_v1",
                "request_digest_profile": "lab_run_command_digest_v1",
                "normalized_request_utf8": self.normalized_utf8,
            },
            **extra,
        }

    def call(self, request: dict[str, object]) -> dict[str, object]:
        return runtime_hook.handle_request(
            request, registry_path=self.registry, lifecycle_path=self.lifecycle,
            verifier=self.verifier,
        )

    def test_verified_pre_is_prepared_and_dispatches_same_execution_id(self) -> None:
        result = self.call(self.request("pre"))
        self.assertEqual(result["protocol"], "runtime_host_tool_hook_v1")
        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["host_tool_execution_id"], "hte_1")
        record = json.loads(self.registry.read_text())["__controller_host_tool_receipts__"][self.controller_id]["web"]["hte_1"]
        self.assertEqual(record["state"], "PREPARED")
        state = json.loads(self.lifecycle.read_text())
        self.assertEqual(state["control_receipt_inflight"], "hte_1")

    def test_pre_rejects_noncanonical_intent_and_caller_identity(self) -> None:
        for mutation in (
            {"controller_id": self.controller_id},
            {"tool_use_id": "caller-tool"},
            {"tool_intent": self.request("pre")["tool_intent"] | {
                "normalized_request_utf8": self.normalized_utf8 + " "
            }},
            {"tool_intent": self.request("pre")["tool_intent"] | {
                "normalized_request_utf8": '{"arguments":{"command":"x","command":"y","cwd":"%s","timeout":30},"tool_name":"run_command"}' % self.repo
            }},
        ):
            request = self.request("pre") | mutation
            with self.subTest(mutation=mutation):
                with self.assertRaises((PermissionError, ValueError)):
                    self.call(request)

    def test_pre_requires_verifier_v2_tool_capability(self) -> None:
        def legacy_verifier(**_kwargs):
            return True

        with self.assertRaisesRegex(PermissionError, "v2 tool capability"):
            runtime_hook.handle_request(
                self.request("pre"), registry_path=self.registry,
                lifecycle_path=self.lifecycle, verifier=legacy_verifier,
            )

    def test_registered_v2_verifier_cli_is_used_across_the_real_process_boundary(self) -> None:
        executable = self.root / "runtime-verifier"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json,sys,time\n"
            "r=json.loads(sys.stdin.read())\n"
            "receipt=r['receipt']; phase=r['operation'].removeprefix('verify_tool_')\n"
            "entry={'provenance':'runtime_host_current_entry_v1','entry_scope':'runtime_invocation',"
            "'machine_source':'host_invocation_context_v1','conversation_id':receipt['conversation_id'],"
            "'browser_target_id':receipt['browser_target_id'],'top_frame_id':receipt['top_frame_id'],"
            "'loader_id':receipt['loader_id'],'generation_anchor_sha256':receipt['generation_anchor_sha256'],"
            "'secure_origin':receipt['secure_origin'],'target_generation':r['expected_target_generation'],"
            "'ownership_generation':r['expected_ownership_generation'],"
            "'runtime_invocation_id':'runtime-invocation-1','host_receipt_id':'entry-cli',"
            "'observed_at_unix_ms':int(time.time()*1000)}\n"
            "out={'ok':True,'protocol':'runtime_host_verifier_cli_v2','operation':r['operation'],"
            "'receipt_sha256':r['receipt_sha256'],'receipt_id':receipt[phase+'_receipt_id'],"
            "'capability_id':receipt['capability_id'],"
            "'host_tool_execution_id':receipt['host_tool_execution_id'],'current_entry':entry}\n"
            "print(json.dumps(out))\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        config = self.root / "host-verifiers.json"
        config.write_text(json.dumps({
            "schema_version": 1,
            "verifiers": {"web": {
                "protocol": "runtime_host_verifier_cli_v2",
                "executable": str(executable), "sha256": digest,
                "bundle_sha256": {str(executable): digest},
            }},
        }), encoding="utf-8")
        config.chmod(0o600)
        result = runtime_hook.handle_request(
            self.request("pre"), registry_path=self.registry,
            lifecycle_path=self.lifecycle, verifier_config_path=config,
        )
        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["host_tool_execution_id"], "hte_1")

    def test_pre_rejects_changed_cwd_guard_argv_and_snapshot_symlink(self) -> None:
        snapshot_link = self.repo / "snapshot-link.json"
        snapshot_link.symlink_to(self.snapshot)
        invalid_arguments = (
            {"command": self.command, "cwd": "/tmp", "timeout": 30},
            {
                "command": self.command.replace(
                    str(self.repo / "TASK_LEDGER.md"), str(self.repo / "OTHER_LEDGER.md")
                ),
                "cwd": str(self.repo), "timeout": 30,
            },
            {
                "command": self.command.replace(str(self.snapshot), str(snapshot_link)),
                "cwd": str(self.repo), "timeout": 30,
            },
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                raw = canonical_json({"arguments": arguments, "tool_name": "run_command"}).decode()
                receipt = deepcopy(self.pre)
                receipt["normalized_request_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
                request = self.request("pre")
                request["pre_receipt"] = receipt
                request["tool_intent"] = {
                    "schema_version": 1, "provenance": "runtime_host_tool_intent_v1",
                    "request_digest_profile": "lab_run_command_digest_v1",
                    "normalized_request_utf8": raw,
                }
                with self.assertRaises(PermissionError):
                    self.call(request)

    def test_generic_web_event_rejects_every_internal_terminal_marker(self) -> None:
        event = {
            "hook_event_name": "PostToolUse", "controller_host": "web",
            "execution_host": "web", "event_source": "web",
        }
        for field in (
            "_runtime_host_terminal_commit", "host_tool_terminal_commit",
            "verified_host_tool_receipt", "internal_trust", "trusted_internal_event",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                PermissionError, "cannot supply internal Host trust fields"
            ):
                lifecycle_hook.process_verified_web_event(
                    {**event, field: {}}, registry_path=self.registry,
                    lifecycle_path=self.lifecycle,
                )

    def terminal_payload(self) -> tuple[dict[str, object], dict[str, object]]:
        backend = {
            "jsonrpc": "2.0", "id": "bc_1", "result": {
                "isError": False,
                "structuredContent": {
                    "toolName": "run_command", "command": self.command, "cwd": str(self.repo),
                    "stdout": "control-event: allowed", "stderr": "", "success": True,
                    "exitCode": 0, "elapsedMs": 1.25, "timedOut": False,
                },
            },
        }
        raw = canonical_json(backend).decode()
        terminal = self.host_terminal_receipt(hashlib.sha256(raw.encode()).hexdigest())
        request = self.request(
            "terminal", terminal_receipt=terminal,
            backend_http_status=200, response_body_sha256=terminal["response_body_sha256"],
            backend_response={
                "schema_version": 1, "provenance": "runtime_host_backend_response_v1",
                "response_body_utf8": raw,
            },
        )
        return request, terminal

    def write_guard_evidence(self) -> Path:
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        evidence = {
            "schema_version": 1, "record_kind": "controller_cycle_evidence",
            "evidence_id": "control-event-1", "controller_id": self.controller_id,
            "cycle_id": "control-event-1", "terminal_status": "CLOSED",
            "snapshot_sha256": hashlib.sha256(self.snapshot.read_bytes()).hexdigest(),
            "ledger_sha256": hashlib.sha256((self.repo / "TASK_LEDGER.md").read_bytes()).hexdigest(),
            "main_revision": head, "validation_errors": [],
        }
        path = load_module("control_event_guard", "scripts/control_event_guard.py").controller_cycle_evidence_path(
            self.repo, "control-event-1"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence), encoding="utf-8")
        return path

    def test_terminal_requires_structured_success_and_closes_after_lifecycle_commit(self) -> None:
        self.call(self.request("pre"))
        evidence_path = self.write_guard_evidence()
        request, _terminal = self.terminal_payload()
        result = self.call(request)
        self.assertEqual(result["state"], "CLOSED")
        record = json.loads(self.registry.read_text())["__controller_host_tool_receipts__"][self.controller_id]["web"]["hte_1"]
        self.assertEqual(record["state"], "CLOSED")
        state = json.loads(self.lifecycle.read_text())
        commit = state["host_tool_terminal_commits"]["hte_1"]
        self.assertEqual(commit["provenance"], "host_tool_terminal_commit_v1")
        self.assertEqual(commit["guard_evidence_sha256"], hashlib.sha256(evidence_path.read_bytes()).hexdigest())
        self.assertEqual(len(state["tool_trace"]), 1)

    def test_terminal_rejects_every_non_success_backend_shape_before_pending(self) -> None:
        self.call(self.request("pre"))
        base_request, _terminal = self.terminal_payload()
        base_json = json.loads(base_request["backend_response"]["response_body_utf8"])
        mutations = {
            "jsonrpc_error": {
                "jsonrpc": "2.0", "id": "bc_1", "error": {"code": -1, "message": "failed"}
            },
            "is_error": deepcopy(base_json),
            "nonzero_exit": deepcopy(base_json),
            "missing_exit": deepcopy(base_json),
            "timed_out": deepcopy(base_json),
            "command_mismatch": deepcopy(base_json),
            "cwd_mismatch": deepcopy(base_json),
        }
        mutations["is_error"]["result"]["isError"] = True
        mutations["nonzero_exit"]["result"]["structuredContent"]["exitCode"] = 1
        del mutations["missing_exit"]["result"]["structuredContent"]["exitCode"]
        mutations["timed_out"]["result"]["structuredContent"]["timedOut"] = True
        mutations["command_mismatch"]["result"]["structuredContent"]["command"] = "printf forged"
        mutations["cwd_mismatch"]["result"]["structuredContent"]["cwd"] = "/tmp"
        for name, backend in mutations.items():
            with self.subTest(name=name):
                request = deepcopy(base_request)
                raw = canonical_json(backend).decode()
                digest = hashlib.sha256(raw.encode()).hexdigest()
                request["backend_response"]["response_body_utf8"] = raw
                request["response_body_sha256"] = digest
                request["terminal_receipt"] = self.host_terminal_receipt(digest)
                with self.assertRaises(PermissionError):
                    self.call(request)
        request = deepcopy(base_request)
        request["backend_response"]["response_body_utf8"] += " "
        with self.assertRaisesRegex(PermissionError, "bytes.*digest"):
            self.call(request)
        record = json.loads(self.registry.read_text())["__controller_host_tool_receipts__"][self.controller_id]["web"]["hte_1"]
        self.assertEqual(record["state"], "PREPARED")

    def test_terminal_without_pre_and_generation_rotation_fail_closed(self) -> None:
        request, terminal = self.terminal_payload()
        with self.assertRaisesRegex(PermissionError, "not prepared"):
            self.call(request)
        self.call(self.request("pre"))
        registry = json.loads(self.registry.read_text())
        registry["__controller_targets__"][self.controller_id]["web"]["generation"] = 5
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(PermissionError, "stale"):
            self.call(request)

    def test_terminal_capability_must_match_pre_before_pending(self) -> None:
        self.call(self.request("pre"))
        request, _terminal = self.terminal_payload()
        changed_terminal = deepcopy(request["terminal_receipt"])
        changed_terminal["capability_id"] = "hic_changed"
        request["terminal_receipt"] = changed_terminal
        with self.assertRaisesRegex(PermissionError, "pre receipt does not match"):
            self.call(request)
        record = json.loads(self.registry.read_text())["__controller_host_tool_receipts__"][self.controller_id]["web"]["hte_1"]
        self.assertEqual(record["state"], "PREPARED")

    def test_terminal_rejects_ownership_and_turn_rotation(self) -> None:
        self.call(self.request("pre"))
        request, _terminal = self.terminal_payload()
        registry = json.loads(self.registry.read_text())
        registry["__controller_execution_ownership__"][self.controller_id]["generation"] = 10
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(PermissionError, "stale"):
            self.call(request)

        registry["__controller_execution_ownership__"][self.controller_id]["generation"] = 9
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        target = target_guard.agent_target.verified_execution_target(
            logical_agent=target_guard.agent_target.logical_agent_identity(
                agent_type="controller", agent_id=self.controller_id
            ),
            host="web", execution_target_session_id=self.conversation_id,
            target_generation=4, ownership_generation=9, provenance="test_host_entry",
        )
        self.turn = target_guard.agent_target.verified_execution_turn(
            verified_target=target, runtime_invocation_id="runtime-invocation-2",
            provenance="test_runtime_web_turn",
        )
        state = json.loads(self.lifecycle.read_text())
        state["active_turn_id"] = self.turn["turn_id"]
        state["web_turn_lease"].update({
            "turn_id": self.turn["turn_id"],
            "runtime_invocation_id": self.turn["runtime_invocation_id"],
        })
        self.lifecycle.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(PermissionError, "tuple does not match"):
            self.call(request)

    def test_terminal_rejects_symlinked_guard_evidence_and_stays_pending(self) -> None:
        self.call(self.request("pre"))
        evidence_path = self.write_guard_evidence()
        alternate = evidence_path.with_name("alternate-evidence.json")
        alternate.write_bytes(evidence_path.read_bytes())
        evidence_path.unlink()
        evidence_path.symlink_to(alternate)
        request, _terminal = self.terminal_payload()
        with self.assertRaisesRegex(PermissionError, "evidence is unavailable"):
            self.call(request)
        record = json.loads(self.registry.read_text())["__controller_host_tool_receipts__"][self.controller_id]["web"]["hte_1"]
        self.assertEqual(record["state"], "TERMINAL_PENDING")

    def test_terminal_failure_stays_pending_and_identical_retry_does_not_duplicate_trace(self) -> None:
        self.call(self.request("pre"))
        self.write_guard_evidence()
        request, _terminal = self.terminal_payload()
        original_write = target_guard._write_registry

        def fail_lifecycle(path, value):
            if Path(path) == self.lifecycle:
                raise OSError("lifecycle write failed")
            return original_write(path, value)

        with patch.object(target_guard, "_write_registry", side_effect=fail_lifecycle):
            with self.assertRaisesRegex(OSError, "lifecycle write failed"):
                self.call(request)
        record = json.loads(self.registry.read_text())["__controller_host_tool_receipts__"][self.controller_id]["web"]["hte_1"]
        self.assertEqual(record["state"], "TERMINAL_PENDING")

        changed = deepcopy(request)
        changed_terminal = deepcopy(changed["terminal_receipt"])
        changed_terminal["terminal_receipt_id"] = "htr_changed"
        changed["terminal_receipt"] = changed_terminal
        with self.assertRaisesRegex(PermissionError, "pending execution"):
            self.call(changed)

        failed_once = False

        def fail_registry_once(path, value):
            nonlocal failed_once
            if Path(path) == self.registry and not failed_once:
                failed_once = True
                raise OSError("registry close failed")
            return original_write(path, value)

        with patch.object(target_guard, "_write_registry", side_effect=fail_registry_once):
            with self.assertRaisesRegex(OSError, "registry close failed"):
                self.call(request)
        state = json.loads(self.lifecycle.read_text())
        self.assertEqual(state["host_tool_terminal_commits"]["hte_1"]["provenance"], "host_tool_terminal_commit_v1")
        self.assertEqual(len(state["tool_trace"]), 1)
        # An older CLOSED execution in the same turn must not authorize the
        # current receipt_tool_use_id whose registry CAS is still pending.
        registry_state = json.loads(self.registry.read_text())
        records = registry_state["__controller_host_tool_receipts__"][self.controller_id]["web"]
        old_record = deepcopy(records["hte_1"])
        old_record["state"] = "CLOSED"
        old_record["tuple"]["host_tool_execution_id"] = "hte_old"
        old_record["guard_evidence_sha256"] = "d" * 64
        records["hte_old"] = old_record
        self.registry.write_text(json.dumps(registry_state), encoding="utf-8")
        old_commit = deepcopy(state["host_tool_terminal_commits"]["hte_1"])
        old_commit["tuple"]["host_tool_execution_id"] = "hte_old"
        old_commit["guard_contract_sha256"] = "d" * 64
        state["host_tool_terminal_commits"]["hte_old"] = old_commit
        self.lifecycle.write_text(json.dumps(state), encoding="utf-8")
        stop = {
            "hook_event_name": "Stop", "session_id": self.controller_id,
            "controller_session_id": self.controller_id,
            "source_session_id": self.conversation_id, "web_session_id": self.conversation_id,
            "controller_host": "web", "execution_host": "web", "event_source": "web",
            "verified_execution_turn": self.turn, "turn_id": self.turn["turn_id"],
            "cwd": str(self.repo),
        }
        with self.assertRaisesRegex(PermissionError, "CAS.*CLOSED"):
            lifecycle_hook.process_verified_web_event(
                stop, registry_path=self.registry, lifecycle_path=self.lifecycle
            )
        result = self.call(request)
        self.assertEqual(result["state"], "CLOSED")
        self.assertEqual(len(json.loads(self.lifecycle.read_text())["tool_trace"]), 1)
        current = json.loads(self.lifecycle.read_text())
        with patch.object(
            lifecycle_hook, "evaluate_event",
            return_value=({"cas_validator_passed": True}, current),
        ):
            output, _state = lifecycle_hook.process_verified_web_event(
                stop, registry_path=self.registry, lifecycle_path=self.lifecycle
            )
        self.assertEqual(output, {"cas_validator_passed": True})

    def test_real_unix_server_correlates_request_and_owns_socket_mode(self) -> None:
        parent = self.root / "socket-parent"
        parent.mkdir(mode=0o700)
        socket_path = parent / "runtime.sock"
        stop = threading.Event()
        thread = threading.Thread(target=runtime_hook.serve_unix_socket, kwargs={
            "socket_path": socket_path, "registry_path": self.registry,
            "lifecycle_path": self.lifecycle, "verifier": self.verifier,
            "stop_event": stop,
        }, daemon=True)
        thread.start()
        for _ in range(100):
            if socket_path.exists():
                break
            time.sleep(0.01)
        self.assertTrue(socket_path.exists())
        self.assertEqual(socket_path.lstat().st_mode & 0o777, 0o600)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            for _ in range(100):
                try:
                    client.connect(str(socket_path))
                    break
                except ConnectionRefusedError:
                    time.sleep(0.01)
            else:
                self.fail("Runtime Host hook socket did not begin accepting")
            client.sendall(canonical_json(self.request("pre")) + b"\n")
            response = b""
            while not response.endswith(b"\n"):
                response += client.recv(65536)
        payload = json.loads(response)
        self.assertEqual(payload["request_id"], "request-pre")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["decision"], "ALLOW")
        stop.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(socket_path.exists())

    def test_unix_server_rejects_unsafe_paths_and_bad_frames(self) -> None:
        non_private = self.root / "non-private"
        non_private.mkdir(mode=0o755)
        with self.assertRaisesRegex(PermissionError, "0700"):
            runtime_hook.serve_unix_socket(
                socket_path=non_private / "runtime.sock", stop_event=threading.Event()
            )
        private = self.root / "private"
        private.mkdir(mode=0o700)
        existing = private / "runtime.sock"
        existing.write_text("occupied", encoding="utf-8")
        with self.assertRaisesRegex(PermissionError, "pre-existing"):
            runtime_hook.serve_unix_socket(
                socket_path=existing, stop_event=threading.Event()
            )
        socket_link = private / "linked.sock"
        socket_link.symlink_to(existing)
        with self.assertRaisesRegex(PermissionError, "pre-existing"):
            runtime_hook.serve_unix_socket(
                socket_path=socket_link, stop_event=threading.Event()
            )
        link_parent = self.root / "linked-private"
        link_parent.symlink_to(private, target_is_directory=True)
        with self.assertRaisesRegex(PermissionError, "real directory"):
            runtime_hook.serve_unix_socket(
                socket_path=link_parent / "other.sock", stop_event=threading.Event()
            )

        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.sendall(b"{}")
        right.shutdown(socket.SHUT_WR)
        with self.assertRaisesRegex(ValueError, "newline"):
            runtime_hook._read_frame(left)
        left.close()
        right.close()
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        sender = threading.Thread(
            target=right.sendall,
            args=(b"x" * (runtime_hook.MAX_FRAME_BYTES + 1),),
            daemon=True,
        )
        sender.start()
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            runtime_hook._read_frame(left)
        left.close()
        sender.join(1)
        right.close()

        left, right = socket.socketpair()
        def drip() -> None:
            try:
                for _ in range(5):
                    right.sendall(b"x")
                    time.sleep(0.03)
            except OSError:
                pass
        sender = threading.Thread(target=drip, daemon=True)
        sender.start()
        started = time.monotonic()
        with patch.object(runtime_hook, "IO_TIMEOUT_SECONDS", 0.06):
            with self.assertRaises(TimeoutError):
                runtime_hook._read_frame(left)
        self.assertLess(time.monotonic() - started, 0.2)
        left.close()
        right.close()
        sender.join(1)


if __name__ == "__main__":
    unittest.main()
