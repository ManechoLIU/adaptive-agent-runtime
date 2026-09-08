from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import web_reentry_adapter


class WebReentryAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.origin_verifier_patcher = patch.object(
            web_reentry_adapter,
            "_registered_web_origin_attestation_verifier",
            return_value=lambda **kwargs: {
                "origin_host": "chatgpt_web",
                "origin_conversation_id": kwargs["expected_target_session_id"],
                "origin_attested": True,
                "call_receipt": "test-host-call-receipt",
            },
        )
        self.origin_verifier_patcher.start()

    def tearDown(self) -> None:
        self.origin_verifier_patcher.stop()

    def make_identity(
        self,
        root: Path,
        *,
        web_session_id: str = "web-current",
        include_ownership: bool = True,
    ) -> tuple[Path, Path, Path]:
        repo = root / "repo"
        repo.mkdir()
        registry = root / "controllers.json"
        registry_payload = {
            "controller-1": str(repo.resolve()),
            "__controller_sessions__": {"controller-1": {"web": [web_session_id, "web-old"]}},
            "__controller_targets__": {
                "controller-1": {
                    "web": {
                        "status": "active",
                        "session_id": web_session_id,
                        "generation": 3,
                    }
                }
            },
        }
        if include_ownership:
            registry_payload["__controller_execution_ownership__"] = {
                "controller-1": {
                    "active_host": "web",
                    "execution_target_session_id": web_session_id,
                    "generation": 7,
                }
            }
        registry.write_text(json.dumps(registry_payload), encoding="utf-8")
        lease = root / "web-leases.json"
        lease.write_text(json.dumps({
            "schema_version": 1,
            "leases": {"controller-1": {
                "repo": str(repo.resolve()),
                "controller_id": "controller-1",
                "web_session_id": web_session_id,
                "authorized_at_unix": int(time.time()) - 10,
                "expires_at_unix": int(time.time()) + 3600,
                "provenance": "manual_user_authorized",
                "mode": "resume_only",
            }},
        }), encoding="utf-8")
        return repo, registry, lease

    def test_resolve_reentry_session_requires_current_resume_only_lease_and_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            self.assertEqual(
                web_reentry_adapter.resolve_reentry_session(
                    controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease
                ),
                "web-current",
            )

    def test_resolve_reentry_session_refuses_expired_or_unbound_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, lease = self.make_identity(root)
            payload = json.loads(lease.read_text())
            payload["leases"]["controller-1"]["expires_at_unix"] = int(time.time()) - 1
            lease.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(PermissionError):
                web_reentry_adapter.resolve_reentry_session(
                    controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease
                )

            payload["leases"]["controller-1"]["expires_at_unix"] = int(time.time()) + 3600
            payload["leases"]["controller-1"]["web_session_id"] = "not-bound"
            lease.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(PermissionError):
                web_reentry_adapter.resolve_reentry_session(
                    controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease
                )

    def test_reentry_without_canonical_web_ownership_never_calls_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(
                Path(tmp), include_ownership=False
            )
            calls: list[dict] = []

            result = web_reentry_adapter.execute_web_reentry(
                controller_id="controller-1",
                repo=repo,
                registry_path=registry,
                lease_path=lease,
                lifecycle_state={
                    "pending_control_event": True,
                    "requires_user": False,
                    "wake_generation": 9,
                },
                browser_call=lambda arguments: calls.append(dict(arguments)) or {},
            )

            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["state"], "WEB_REENTRY_IDENTITY_UNAVAILABLE")
            self.assertEqual(calls, [])

    def test_reentry_without_registered_host_origin_verifier_never_calls_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            calls: list[dict] = []

            with patch.object(
                web_reentry_adapter,
                "_registered_web_origin_attestation_verifier",
                return_value=None,
            ):
                result = web_reentry_adapter.execute_web_reentry(
                    controller_id="controller-1",
                    repo=repo,
                    registry_path=registry,
                    lease_path=lease,
                    lifecycle_state={
                        "pending_control_event": True,
                        "requires_user": False,
                        "wake_generation": 9,
                    },
                    browser_call=lambda arguments: calls.append(dict(arguments)) or {},
                )

            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["state"], "WEB_REENTRY_IDENTITY_UNAVAILABLE")
            self.assertEqual(
                result["error_code"],
                "WEB_HOST_ATTESTATION_VERIFIER_UNAVAILABLE",
            )
            self.assertEqual(calls, [])

    def test_reentry_without_explicit_canonical_web_target_never_calls_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            payload = json.loads(registry.read_text(encoding="utf-8"))
            del payload["__controller_targets__"]["controller-1"]["web"]
            payload["__controller_sessions__"]["controller-1"]["web"] = [
                "web-current"
            ]
            registry.write_text(json.dumps(payload), encoding="utf-8")
            calls: list[dict] = []

            result = web_reentry_adapter.execute_web_reentry(
                controller_id="controller-1",
                repo=repo,
                registry_path=registry,
                lease_path=lease,
                lifecycle_state={
                    "pending_control_event": True,
                    "requires_user": False,
                    "wake_generation": 9,
                },
                browser_call=lambda arguments: calls.append(dict(arguments)) or {},
            )

            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["state"], "WEB_REENTRY_IDENTITY_UNAVAILABLE")
            self.assertEqual(calls, [])

    def test_legacy_browser_attested_target_is_quarantined_before_browser_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            payload = json.loads(registry.read_text(encoding="utf-8"))
            payload["__controller_targets__"]["controller-1"]["web"].update({
                "provenance": "host_attested_same_controller_recovery",
                "binding_mode": "resume_only",
                "host_identity_receipt_sha256": "a" * 64,
            })
            registry.write_text(json.dumps(payload), encoding="utf-8")
            calls: list[dict] = []

            result = web_reentry_adapter.execute_web_reentry(
                controller_id="controller-1",
                repo=repo,
                registry_path=registry,
                lease_path=lease,
                lifecycle_state={
                    "pending_control_event": True,
                    "requires_user": False,
                },
                browser_call=lambda arguments: calls.append(dict(arguments)) or {},
            )

            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["state"], "WEB_REENTRY_IDENTITY_UNAVAILABLE")
            self.assertEqual(calls, [])

    def test_ai_bridge_destination_consistency_requires_matching_live_chatgpt_tab(self) -> None:
        calls: list[dict] = []

        def browser_call(arguments: dict) -> dict:
            calls.append(dict(arguments))
            self.assertEqual(arguments, {"action": "list_tabs"})
            return {"tabs": [{
                "tab_id": "tab-controller",
                "url": "https://chatgpt.com/c/web-current",
                "title": "SelfAlone Controller",
            }]}

        receipt = {
            "host": "web",
            "web_session_id": "web-current",
            "tab_id": "tab-controller",
            "url": "https://chatgpt.com/c/web-current",
            "source": "ai_bridge_browser",
        }
        self.assertTrue(web_reentry_adapter.verify_ai_bridge_web_destination_tab_consistency(
            controller_id="controller-1", host="web",
            expected_target_session_id="web-current", expected_target_generation=3,
            expected_target_mode="same_controller_session_recovery",
            host_execution_receipt=receipt,
            adapter_attempt={"repo": "/tmp/repo"}, browser_call=browser_call,
        ))
        forged = dict(receipt); forged["tab_id"] = "forged-tab"
        self.assertFalse(web_reentry_adapter.verify_ai_bridge_web_destination_tab_consistency(
            controller_id="controller-1", host="web",
            expected_target_session_id="web-current", expected_target_generation=3,
            expected_target_mode="same_controller_session_recovery",
            host_execution_receipt=forged,
            adapter_attempt={"repo": "/tmp/repo"}, browser_call=browser_call,
        ))
        self.assertEqual(len(calls), 2)

    def test_ai_bridge_destination_consistency_rejects_caller_only_claim_without_live_tab(self) -> None:
        receipt = {
            "host": "web", "web_session_id": "web-current",
            "tab_id": "tab-controller", "url": "https://chatgpt.com/c/web-current",
            "source": "ai_bridge_browser",
        }
        self.assertFalse(web_reentry_adapter.verify_ai_bridge_web_destination_tab_consistency(
            controller_id="controller-1", host="web",
            expected_target_session_id="web-current", expected_target_generation=0,
            expected_target_mode="same_controller_session_recovery",
            host_execution_receipt=receipt, adapter_attempt={},
            browser_call=lambda _args: {"tabs": []},
        ))

    def test_reentry_checkpoint_recomputes_dag_and_route_before_any_new_dispatch(self) -> None:
        prompt = web_reentry_adapter.build_reentry_prompt(
            controller_id="controller-1",
            lifecycle_state={
                "pending_control_event": True,
                "requires_user": False,
                "wake_generation": 9,
                "triggers": ["RUNNABLE:T-NEXT", "terminal_receipt_pending"],
            },
            terminal_receipts=["/tmp/terminal.json"],
        )
        self.assertIn("DAG / READY / WIP", prompt)
        self.assertIn("canonical route", prompt)
        self.assertIn("provider/model", prompt)
        self.assertIn("prepared canonical Web dispatch", prompt)
        self.assertIn("Do not create a new ChatGPT Web child", prompt)
        self.assertIn("RUNNABLE:T-NEXT", prompt)

    def test_existing_target_tab_is_focused_then_composer_submitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            calls: list[dict] = []

            def browser_call(arguments: dict) -> dict:
                calls.append(dict(arguments))
                if arguments["action"] == "list_tabs":
                    return {"tabs": [{
                        "tab_id": "tab-1",
                        "active": False,
                        "url": "https://chatgpt.com/g/g-p-proj/c/web-current",
                    }]}
                if arguments["action"] == "snapshot":
                    return {"tab_id": "tab-1", "nodes": [
                        {"node_id": "composer", "role": "textbox", "name": "Chat with ChatGPT"},
                    ]}
                return {"ok": True}

            result = web_reentry_adapter.execute_web_reentry(
                controller_id="controller-1",
                repo=repo,
                registry_path=registry,
                lease_path=lease,
                lifecycle_state={"pending_control_event": True, "requires_user": False, "wake_generation": 7},
                browser_call=browser_call,
            )

            self.assertEqual(result["result"], "CONFIRMED")
            self.assertEqual(result["state"], "WEB_REENTRY_SUBMITTED")
            self.assertEqual(result["execution_target_session_id"], "web-current")
            self.assertEqual(result["target_generation"], 3)
            self.assertEqual(result["ownership_generation"], 7)
            self.assertEqual(result["target_mode"], "explicit_current")
            host_receipt = result["host_execution_receipt"]
            self.assertEqual(host_receipt["host"], "web")
            self.assertEqual(host_receipt["web_session_id"], "web-current")
            self.assertEqual(host_receipt["tab_id"], "tab-1")
            self.assertEqual(host_receipt["source"], "ai_bridge_browser")
            self.assertEqual(host_receipt["call_receipt"], "test-host-call-receipt")
            self.assertEqual(host_receipt["url"], "https://chatgpt.com/g/g-p-proj/c/web-current")
            self.assertEqual([c["action"] for c in calls], ["list_tabs", "focus_tab", "snapshot", "type"])
            self.assertEqual(calls[-1]["node_id"], "composer")
            self.assertTrue(calls[-1]["submit"])
            self.assertIn("Continue this existing registered Web Controller", calls[-1]["text"])

    def test_target_generation_change_before_submit_never_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            calls: list[dict] = []

            def browser_call(arguments: dict) -> dict:
                calls.append(dict(arguments))
                if arguments["action"] == "list_tabs":
                    return {
                        "tabs": [{
                            "tab_id": "tab-1",
                            "url": "https://chatgpt.com/c/web-current",
                        }]
                    }
                if arguments["action"] == "snapshot":
                    changed = json.loads(registry.read_text(encoding="utf-8"))
                    changed["__controller_targets__"]["controller-1"]["web"] = {
                        "status": "active",
                        "session_id": "web-other",
                        "generation": 4,
                    }
                    changed["__controller_sessions__"]["controller-1"]["web"].append(
                        "web-other"
                    )
                    changed["__controller_execution_ownership__"]["controller-1"] = {
                        "active_host": "web",
                        "execution_target_session_id": "web-other",
                        "generation": 8,
                    }
                    registry.write_text(json.dumps(changed), encoding="utf-8")
                    return {
                        "nodes": [{
                            "node_id": "composer",
                            "role": "textbox",
                            "name": "Chat with ChatGPT",
                        }]
                    }
                return {"ok": True}

            result = web_reentry_adapter.execute_web_reentry(
                controller_id="controller-1",
                repo=repo,
                registry_path=registry,
                lease_path=lease,
                lifecycle_state={
                    "pending_control_event": True,
                    "requires_user": False,
                },
                browser_call=browser_call,
            )

            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["state"], "WEB_REENTRY_SUPERSEDED_TARGET")
            self.assertFalse(any(call["action"] == "type" for call in calls))

    def test_submit_holds_registry_fence_against_target_rotation(self) -> None:
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            fence_observed: list[bool] = []

            def browser_call(arguments: dict) -> dict:
                if arguments["action"] == "list_tabs":
                    return {"tabs": [{
                        "tab_id": "tab-1",
                        "url": "https://chatgpt.com/c/web-current",
                    }]}
                if arguments["action"] == "snapshot":
                    return {"nodes": [{
                        "node_id": "composer",
                        "role": "textbox",
                        "name": "Chat with ChatGPT",
                    }]}
                if arguments["action"] == "type":
                    lock_path = web_reentry_adapter.target_guard.registry_lock_path(
                        registry
                    )
                    with lock_path.open("a+") as contender:
                        try:
                            fcntl.flock(
                                contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                            )
                        except BlockingIOError:
                            fence_observed.append(True)
                        else:
                            fence_observed.append(False)
                            fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
                return {"ok": True}

            result = web_reentry_adapter.execute_web_reentry(
                controller_id="controller-1",
                repo=repo,
                registry_path=registry,
                lease_path=lease,
                lifecycle_state={
                    "pending_control_event": True,
                    "requires_user": False,
                },
                browser_call=browser_call,
            )

            self.assertEqual(result["result"], "CONFIRMED")
            self.assertEqual(fence_observed, [True])

    def test_missing_target_tab_opens_exact_leased_conversation_before_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            calls: list[dict] = []

            def browser_call(arguments: dict) -> dict:
                calls.append(dict(arguments))
                action = arguments["action"]
                if action == "list_tabs":
                    return {"tabs": []}
                if action == "new_tab":
                    return {"tab_id": "new-tab", "url": arguments["url"]}
                if action == "snapshot":
                    return {"tab_id": "new-tab", "nodes": [
                        {"node_id": "composer", "role": "textbox", "name": "Chat with ChatGPT"},
                    ]}
                return {"ok": True}

            result = web_reentry_adapter.execute_web_reentry(
                controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease,
                lifecycle_state={"pending_control_event": True, "requires_user": False},
                browser_call=browser_call,
            )

            self.assertEqual(result["result"], "CONFIRMED")
            new_tab = next(c for c in calls if c["action"] == "new_tab")
            self.assertEqual(new_tab["url"], "https://chatgpt.com/c/web-current")

    def test_active_response_defers_without_typing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            calls: list[dict] = []

            def browser_call(arguments: dict) -> dict:
                calls.append(dict(arguments))
                if arguments["action"] == "list_tabs":
                    return {"tabs": [{"tab_id": "tab-1", "url": "https://chatgpt.com/c/web-current"}]}
                if arguments["action"] == "snapshot":
                    return {"nodes": [
                        {"node_id": "stop", "role": "button", "name": "Stop generating"},
                        {"node_id": "composer", "role": "textbox", "name": "Chat with ChatGPT"},
                    ]}
                return {"ok": True}

            result = web_reentry_adapter.execute_web_reentry(
                controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease,
                lifecycle_state={"pending_control_event": True, "requires_user": False},
                browser_call=browser_call,
            )
            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["state"], "WEB_REENTRY_DEFERRED_ACTIVE")
            self.assertFalse(any(c["action"] == "type" for c in calls))

    def test_requires_user_or_closed_lifecycle_never_submits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            for lifecycle in (
                {"pending_control_event": False, "requires_user": False},
                {"pending_control_event": True, "requires_user": True},
            ):
                calls: list[dict] = []
                result = web_reentry_adapter.execute_web_reentry(
                    controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease,
                    lifecycle_state=lifecycle, browser_call=lambda args: calls.append(args) or {},
                )
                self.assertEqual(result["result"], "DEFERRED")
                self.assertEqual(calls, [])

    def test_default_browser_path_reuses_one_mcp_session_for_all_browser_actions(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            calls: list[dict] = []
            class FakeMcp:
                def __init__(self, _url: str) -> None:
                    pass
                def browser(self, arguments: dict) -> dict:
                    calls.append(dict(arguments))
                    if arguments["action"] == "list_tabs":
                        return {"tabs": [{"tab_id":"tab-1","url":"https://chatgpt.com/c/web-current"}]}
                    if arguments["action"] == "snapshot":
                        return {"nodes":[{"node_id":"composer","role":"textbox","name":"Chat with ChatGPT"}]}
                    return {"ok": True}
            with patch.object(web_reentry_adapter, "discover_ai_bridge_mcp_url", return_value="http://127.0.0.1:1234/mcp/x"), patch.object(
                web_reentry_adapter, "_McpSession", wraps=FakeMcp
            ) as session_cls:
                result = web_reentry_adapter.execute_web_reentry(
                    controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease,
                    lifecycle_state={"pending_control_event":True,"requires_user":False},
                )
            self.assertEqual(result["result"], "CONFIRMED")
            self.assertEqual(session_cls.call_count, 1)
            self.assertEqual([c["action"] for c in calls], ["list_tabs","focus_tab","snapshot","type"])

    def test_new_tab_local_approval_is_returned_as_retryable_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            calls=[]
            def browser_call(arguments: dict) -> dict:
                calls.append(dict(arguments))
                if arguments["action"] == "list_tabs": return {"tabs": []}
                if arguments["action"] == "new_tab":
                    return {"state":"waiting_for_local_approval","approval_id":"approval-1","expires_at_unix":4102444800}
                return {}
            result=web_reentry_adapter.execute_web_reentry(
                controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease,
                lifecycle_state={"pending_control_event":True,"requires_user":False}, browser_call=browser_call,
            )
            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["state"], "WEB_REENTRY_WAITING_LOCAL_APPROVAL")
            self.assertEqual(result["approval_id"], "approval-1")
            self.assertEqual(result["approval_expires_at_unix"], 4102444800)

    def test_retry_passes_existing_local_approval_id_to_same_new_tab_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_identity(Path(tmp))
            calls=[]
            def browser_call(arguments: dict) -> dict:
                calls.append(dict(arguments))
                if arguments["action"] == "list_tabs": return {"tabs": []}
                if arguments["action"] == "new_tab": return {"tab_id":"tab-2"}
                if arguments["action"] == "snapshot": return {"nodes":[{"node_id":"composer","role":"textbox","name":"Chat with ChatGPT"}]}
                return {"ok":True}
            result=web_reentry_adapter.execute_web_reentry(
                controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease,
                lifecycle_state={"pending_control_event":True,"requires_user":False}, browser_call=browser_call,
                approval_id="approval-1",
            )
            self.assertEqual(result["result"], "CONFIRMED")
            new_tab=next(c for c in calls if c["action"]=="new_tab")
            self.assertEqual(new_tab["approval_id"], "approval-1")


class AiBridgeMcpDiscoveryTests(unittest.TestCase):
    def test_discovery_selects_only_live_loopback_endpoint_and_accepts_url_prefix(self) -> None:
        ps = (
            "user 1 tunnel-client run --mcp.server-url url=http://127.0.0.1:52377/mcp/live-id\n"
            "user 2 tunnel-client run --mcp.server-url http://127.0.0.1:61518/mcp/stale-id\n"
        )
        probed: list[str] = []
        def probe(url: str) -> bool:
            probed.append(url)
            return ":52377/" in url
        self.assertEqual(
            web_reentry_adapter.discover_ai_bridge_mcp_url(ps_text=ps, endpoint_probe=probe),
            "http://127.0.0.1:52377/mcp/live-id",
        )
        self.assertEqual(set(probed), {
            "http://127.0.0.1:52377/mcp/live-id",
            "http://127.0.0.1:61518/mcp/stale-id",
        })

    def test_discovery_fails_closed_when_zero_or_multiple_live_endpoints_exist(self) -> None:
        ps = (
            "user 1 tunnel-client run --mcp.server-url http://127.0.0.1:52377/mcp/a\n"
            "user 2 tunnel-client run --mcp.server-url http://127.0.0.1:52378/mcp/b\n"
        )
        with self.assertRaisesRegex(RuntimeError, "exactly one live"):
            web_reentry_adapter.discover_ai_bridge_mcp_url(
                ps_text=ps, endpoint_probe=lambda _url: False
            )
        with self.assertRaisesRegex(RuntimeError, "exactly one live"):
            web_reentry_adapter.discover_ai_bridge_mcp_url(
                ps_text=ps, endpoint_probe=lambda _url: True
            )

    def test_discovers_loopback_mcp_url_without_exposing_non_loopback_endpoint(self) -> None:
        ps = (
            "user 1 ... tunnel-client run --mcp.server-url "
            "http://127.0.0.1:52377/mcp/opaque-value --log.level warn\n"
        )
        self.assertEqual(
            web_reentry_adapter.discover_ai_bridge_mcp_url(ps_text=ps),
            "http://127.0.0.1:52377/mcp/opaque-value",
        )
        with self.assertRaises(RuntimeError):
            web_reentry_adapter.discover_ai_bridge_mcp_url(
                ps_text="x --mcp.server-url https://example.com/mcp/not-local"
            )


if __name__ == "__main__":
    unittest.main()


class WebReentryContinuationRegressionTests(unittest.TestCase):
    def test_transient_web_reentry_failure_rearms_existing_continuation_supervisor(self) -> None:
        import subprocess
        from unittest.mock import patch
        from scripts import web_lifecycle_bridge as bridge

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
            }), encoding="utf-8")
            state_path = root / "auto-stop.json"
            state_path.write_text(json.dumps({
                "receipt_id": "terminal:writer-1", "session_id": "controller-1",
                "repo": str(repo.resolve()), "state": "RESUME_PENDING",
                "pending_control_event": True, "retry_count": 2,
            }), encoding="utf-8")
            lifecycle_state = {
                "pending_control_event": True, "requires_user": False, "controller_host": "web",
                "wake_generation": 7, "triggers": ["subagent_stopped:writer-1", "READY:T-NEXT"],
            }
            failed = {
                "operation": "web_reentry", "result": "DEFERRED", "state": "WEB_REENTRY_PENDING",
                "returncode": 78, "failure_class": "web_reentry_unavailable",
                "error_code": "WEB_REENTRY_UNAVAILABLE",
                "stderr_tail": "AI-Bridge browser tool returned an error",
                "execution_target_session_id": "web-current", "target_generation": 0,
                "target_mode": "web_lease",
            }
            with patch.object(bridge, "_load_lifecycle_state", return_value=lifecycle_state), patch.object(
                bridge, "execute_web_reentry", return_value=failed
            ), patch.object(bridge, "schedule_auto_native_stop") as schedule:
                rc = bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="terminal:writer-1",
                    registry=registry, codex="codex", delay_seconds=0, state_path=state_path,
                )
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(saved["retry_count"], 3)
            self.assertEqual(saved["failure_class"], "web_reentry_unavailable")
            schedule.assert_called_once()
            self.assertGreaterEqual(schedule.call_args.kwargs["delay_seconds"], 1.0)
            self.assertLessEqual(schedule.call_args.kwargs["delay_seconds"], 60.0)

class ManualFencedWebReentryTests(unittest.TestCase):
    def make_manual_identity(self, root: Path) -> tuple[Path, Path, Path]:
        repo = root / "repo"
        repo.mkdir()
        registry = root / "controllers.json"
        registry.write_text(json.dumps({
            "controller-1": str(repo.resolve()),
            "__controller_sessions__": {"controller-1": {"web": ["web-current", "web-old"]}},
            "__controller_targets__": {"controller-1": {"web": {
                "status": "active",
                "session_id": "web-current",
                "generation": 4,
                "provenance": "manual_user_authorized",
                "binding_mode": "temporary",
                "host_attested": False,
            }}},
            "__controller_execution_ownership__": {"controller-1": {
                "active_host": "web",
                "execution_target_session_id": "web-current",
                "generation": 4,
                "provenance": "manual_user_authorized",
            }},
        }), encoding="utf-8")
        lease = root / "web-leases.json"
        now = int(time.time())
        lease.write_text(json.dumps({
            "schema_version": 1,
            "leases": {"controller-1": {
                "repo": str(repo.resolve()),
                "controller_id": "controller-1",
                "web_session_id": "web-current",
                "authorized_at_unix": now - 100,
                "rotated_at_unix": now - 10,
                "expires_at_unix": now + 3600,
                "provenance": "manual_user_authorized",
                "mode": "resume_only",
            }},
        }), encoding="utf-8")
        return repo, registry, lease

    def browser_success(self, calls: list[dict]):
        def browser_call(arguments: dict) -> dict:
            calls.append(dict(arguments))
            action = arguments["action"]
            if action == "list_tabs":
                return {"tabs": [{
                    "tab_id": "tab-controller",
                    "url": "https://chatgpt.com/g/g-p-project/c/web-current",
                    "title": "Controller",
                }]}
            if action == "snapshot":
                return {"nodes": [{
                    "node_id": "composer",
                    "role": "textbox",
                    "name": "Chat with ChatGPT",
                }]}
            return {"ok": True}
        return browser_call

    def test_missing_host_verifier_allows_only_manual_fenced_exact_current_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_manual_identity(Path(tmp))
            calls: list[dict] = []
            with patch.object(
                web_reentry_adapter,
                "_registered_web_origin_attestation_verifier",
                return_value=None,
            ):
                result = web_reentry_adapter.execute_web_reentry(
                    controller_id="controller-1", repo=repo,
                    registry_path=registry, lease_path=lease,
                    lifecycle_state={"pending_control_event": True, "requires_user": False, "wake_generation": 12},
                    browser_call=self.browser_success(calls),
                )
        self.assertEqual(result["result"], "CONFIRMED")
        self.assertEqual(result["state"], "WEB_REENTRY_MANUAL_FENCED_SUBMITTED")
        self.assertEqual(result["delivery_authorization"], "manual_fenced")
        self.assertFalse(result["host_attested"])
        self.assertFalse(result["strong_web_identity_established"])
        self.assertEqual(result["execution_target_session_id"], "web-current")
        self.assertEqual(result["target_generation"], 4)
        self.assertEqual(result["ownership_generation"], 4)
        self.assertEqual([item["action"] for item in calls], ["list_tabs", "focus_tab", "snapshot", "type"])
        receipt = result["host_execution_receipt"]
        self.assertEqual(receipt["authorization"], "manual_fenced")
        self.assertFalse(receipt["host_attested"])
        self.assertNotIn("call_receipt", receipt)

    def test_manual_fenced_reentry_rejects_generation_or_lease_mismatch_before_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, lease = self.make_manual_identity(root)
            payload = json.loads(registry.read_text())
            payload["__controller_execution_ownership__"]["controller-1"]["generation"] = 5
            registry.write_text(json.dumps(payload), encoding="utf-8")
            calls: list[dict] = []
            with patch.object(web_reentry_adapter, "_registered_web_origin_attestation_verifier", return_value=None):
                result = web_reentry_adapter.execute_web_reentry(
                    controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease,
                    lifecycle_state={"pending_control_event": True, "requires_user": False},
                    browser_call=lambda args: calls.append(dict(args)) or {},
                )
            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["error_code"], "WEB_HOST_ATTESTATION_VERIFIER_UNAVAILABLE")
            self.assertEqual(calls, [])

            payload["__controller_execution_ownership__"]["controller-1"]["generation"] = 4
            registry.write_text(json.dumps(payload), encoding="utf-8")
            lease_payload = json.loads(lease.read_text())
            lease_payload["leases"]["controller-1"]["web_session_id"] = "web-old"
            lease.write_text(json.dumps(lease_payload), encoding="utf-8")
            with patch.object(web_reentry_adapter, "_registered_web_origin_attestation_verifier", return_value=None):
                result = web_reentry_adapter.execute_web_reentry(
                    controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease,
                    lifecycle_state={"pending_control_event": True, "requires_user": False},
                    browser_call=lambda args: calls.append(dict(args)) or {},
                )
            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(calls, [])

    def test_invalid_registered_host_verifier_never_falls_back_to_manual_fenced_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_manual_identity(Path(tmp))
            calls: list[dict] = []
            with patch.object(
                web_reentry_adapter,
                "_registered_web_origin_attestation_verifier",
                return_value=lambda **_kwargs: {"origin_attested": False},
            ):
                result = web_reentry_adapter.execute_web_reentry(
                    controller_id="controller-1", repo=repo,
                    registry_path=registry, lease_path=lease,
                    lifecycle_state={"pending_control_event": True, "requires_user": False},
                    browser_call=lambda args: calls.append(dict(args)) or {},
                )
        self.assertEqual(result["result"], "DEFERRED")
        self.assertEqual(result["error_code"], "WEB_HOST_ATTESTATION_INVALID")
        self.assertEqual(calls, [])

    def test_manual_fenced_submit_holds_registry_and_lease_fences(self) -> None:
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, lease = self.make_manual_identity(Path(tmp))
            fence_observed: list[tuple[bool, bool]] = []

            def browser_call(arguments: dict) -> dict:
                if arguments["action"] == "list_tabs":
                    return {"tabs": [{"tab_id": "tab-1", "url": "https://chatgpt.com/c/web-current"}]}
                if arguments["action"] == "snapshot":
                    return {"nodes": [{"node_id": "composer", "role": "textbox", "name": "Chat with ChatGPT"}]}
                if arguments["action"] == "type":
                    reg_blocked = lease_blocked = False
                    reg_lock_path = web_reentry_adapter.target_guard.registry_lock_path(registry)
                    lease_lock_path = lease.with_suffix(lease.suffix + ".lock")
                    with reg_lock_path.open("a+") as contender:
                        try:
                            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            reg_blocked = True
                        else:
                            fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
                    with lease_lock_path.open("a+") as contender:
                        try:
                            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            lease_blocked = True
                        else:
                            fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
                    fence_observed.append((reg_blocked, lease_blocked))
                return {"ok": True}

            with patch.object(web_reentry_adapter, "_registered_web_origin_attestation_verifier", return_value=None):
                result = web_reentry_adapter.execute_web_reentry(
                    controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease,
                    lifecycle_state={"pending_control_event": True, "requires_user": False},
                    browser_call=browser_call,
                )
        self.assertEqual(result["result"], "CONFIRMED")
        self.assertEqual(fence_observed, [(True, True)])

def _explicit_bridge_verifier_rejection_never_falls_back_when_module_verifier_missing(self):
    with tempfile.TemporaryDirectory() as tmp:
        repo, registry, lease = self.make_manual_identity(Path(tmp))
        calls: list[dict] = []
        def reject(**_kwargs):
            raise RuntimeError("bridge verifier rejected")
        with patch.object(web_reentry_adapter, "_registered_web_origin_attestation_verifier", return_value=None):
            result = web_reentry_adapter.execute_web_reentry(
                controller_id="controller-1", repo=repo,
                registry_path=registry, lease_path=lease,
                lifecycle_state={"pending_control_event": True, "requires_user": False},
                browser_call=lambda args: calls.append(dict(args)) or {},
                origin_verifier=reject,
            )
    self.assertEqual(result["result"], "DEFERRED")
    self.assertEqual(result["error_code"], "WEB_HOST_ATTESTATION_INVALID")
    self.assertIn("bridge verifier rejected", result["stderr_tail"])
    self.assertEqual(calls, [])

ManualFencedWebReentryTests.test_explicit_bridge_verifier_rejection_never_falls_back_when_module_verifier_missing = _explicit_bridge_verifier_rejection_never_falls_back_when_module_verifier_missing
