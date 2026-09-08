import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.install_skill import install_skill
import scripts.rule_handshake as rule_handshake_module
from scripts.rule_handshake import (
    acknowledge_rule_revision,
    evaluate_rule_handshake,
    live_e2e_acceptance_path,
    rule_state_path,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def make_source(base: Path) -> tuple[Path, str]:
    source = base / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test")
    (source / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (source / "scripts").mkdir()
    (source / "scripts" / "x.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    return source, git(source, "rev-parse", "HEAD")


def make_project(base: Path, revision_text: str = "old") -> Path:
    repo = base / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "TASK_LEDGER.md").write_text(
        f"# Tasks\n\n- 规则版本：`adaptive-delivery@{revision_text}`\n\n| ID | 状态 | 证据 | 下一步 |\n| --- | --- | --- | --- |\n| `F1` | `ACTIVE` | x | y |\n",
        encoding="utf-8",
    )
    git(repo, "add", "TASK_LEDGER.md")
    git(repo, "commit", "-m", "init")
    return repo



def write_current_controller_registry(
    registry: Path,
    repo: Path,
    *,
    controller_id: str = "controller-1",
    host: str = "web",
    source_session_id: str = "web-current",
    target_generation: int = 1,
    ownership_generation: int = 1,
    verified_web: bool = True,
) -> None:
    web_identity = {}
    if host == "web":
        web_identity = (
            {
                "provenance": "host_attested_same_controller_recovery",
                "binding_mode": "resume_only",
                "host_attested": True,
                "identity_proof": "host_attested_origin",
            }
            if verified_web
            else {
                "provenance": "manual_user_authorized",
                "binding_mode": "temporary",
                "host_attested": False,
            }
        )
    registry.write_text(json.dumps({
        controller_id: str(repo.resolve()),
        "__controller_sessions__": {controller_id: {host: [source_session_id]}},
        "__controller_targets__": {controller_id: {host: {
            "status": "active",
            "session_id": source_session_id,
            "generation": target_generation,
            "provenance": "test_current_target",
            **web_identity,
        }}},
        "__controller_execution_ownership__": {controller_id: {
            "active_host": host,
            "execution_target_session_id": source_session_id,
            "generation": ownership_generation,
            "provenance": "test_current_target",
        }},
    }), encoding="utf-8")



def controller_action_kwargs(registry_path: Path, controller_id: str = "controller-1") -> dict[str, str]:
    registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    ownership = (registry.get("__controller_execution_ownership__") or {}).get(controller_id)
    if not isinstance(ownership, dict):
        raise AssertionError("test fixture requires canonical execution ownership")
    return {
        "execution_host": str(ownership["active_host"]),
        "source_session_id": str(ownership["execution_target_session_id"]),
    }


def acknowledge_rule_revision(repo, controller_session_id, revision, **kwargs):
    registry_path = Path(kwargs["registry_path"])
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    ownership = (registry.get("__controller_execution_ownership__") or {}).get(controller_session_id)
    if isinstance(ownership, dict):
        execution_host = str(ownership.get("active_host") or "web")
        source_session_id = str(ownership.get("execution_target_session_id") or "missing-source")
    else:
        execution_host = "web"
        source_session_id = "missing-source"
    return rule_handshake_module.acknowledge_rule_revision(
        repo,
        controller_session_id,
        revision,
        execution_host=execution_host,
        source_session_id=source_session_id,
        **kwargs,
    )


class RuleHandshakeTests(unittest.TestCase):
    def test_install_manifest_records_exact_revision_and_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, previous_revision = make_source(base)
            (source / "scripts" / "x.py").write_text("print('next')\n", encoding="utf-8")
            git(source, "add", "scripts/x.py")
            git(source, "commit", "-m", "next runtime revision")
            revision = git(source, "rev-parse", "HEAD")
            target = base / "installed"
            manifest = install_skill(
                source,
                target,
                summary="runtime governance",
                impact="live_assignments",
                stop_condition="load exact revision before launch",
                previous_revision=previous_revision,
                now=NOW,
            )
            self.assertEqual(manifest["revision"], revision)
            self.assertEqual(manifest["previous_revision"], previous_revision)
            self.assertEqual(manifest["upgrade_lineage"]["status"], "linear")
            self.assertEqual(manifest["impact"], "live_assignments")
            self.assertEqual(manifest["product_name"], "Adaptive Agent Runtime")
            self.assertEqual(manifest["skill_id"], "adaptive-agent-runtime")
            self.assertEqual(manifest["product_slug"], "adaptive-agent-runtime")
            self.assertIn("adaptive-delivery", manifest["legacy_skill_ids"])
            self.assertEqual(set(manifest["files"]), {"SKILL.md", "scripts/x.py"})
            self.assertTrue((target / ".adaptive-delivery-install.json").is_file())

    def test_pending_ack_wrong_revision_unregistered_and_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision = make_source(base)
            target = base / "installed"
            install_skill(source, target, summary="rules", impact="live_assignments", stop_condition="ack", now=NOW)
            repo = make_project(base)
            registry = base / "controllers.json"
            write_current_controller_registry(registry, repo)

            pending = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(pending["state"], "pending_ack")
            self.assertTrue(pending["blocking"])
            with self.assertRaisesRegex(ValueError, "does not match installed revision"):
                acknowledge_rule_revision(repo, "controller-1", "wrong", skill_root=target, registry_path=registry, now=NOW)
            with self.assertRaisesRegex(ValueError, "registered controller"):
                acknowledge_rule_revision(repo, "other", revision, skill_root=target, registry_path=registry, now=NOW)

            (target / "SKILL.md").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "installation integrity"):
                acknowledge_rule_revision(repo, "controller-1", revision, skill_root=target, registry_path=registry, now=NOW)
            integrity = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(integrity["state"], "integrity_error")
            self.assertTrue(integrity["blocking"])

    def test_ack_then_ledger_sync_reaches_current_and_state_is_shared_by_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision = make_source(base)
            target = base / "installed"
            install_skill(source, target, summary="rules", impact="live_assignments", stop_condition="ack", now=NOW)
            repo = make_project(base)
            wt = base / "worker"
            git(repo, "worktree", "add", str(wt), "-b", "worker")
            registry = base / "controllers.json"
            write_current_controller_registry(registry, repo)

            receipt = acknowledge_rule_revision(
                repo, "controller-1", revision, skill_root=target, registry_path=registry, now=NOW
            )
            self.assertEqual(receipt["loaded_revision"], revision)
            self.assertEqual(rule_state_path(repo), rule_state_path(wt))
            stale = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(stale["state"], "ledger_stale")
            self.assertTrue(stale["blocking"])

            ledger = repo / "TASK_LEDGER.md"
            ledger.write_text(ledger.read_text(encoding="utf-8").replace("adaptive-delivery@old", f"adaptive-delivery@{revision}"), encoding="utf-8")
            current = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(current["state"], "current")
            self.assertFalse(current["blocking"])
            current_from_wt = evaluate_rule_handshake(wt, ledger=ledger, skill_root=target, registry_path=registry)
            self.assertEqual(current_from_wt["loaded_revision"], revision)

    def test_critical_live_runtime_update_requires_real_e2e_after_ack_and_ledger_sync(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, previous_revision = make_source(base)
            bridge = source / "scripts" / "web_lifecycle_bridge.py"
            bridge.write_text("VALUE = 2\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "change live continuation")
            revision = git(source, "rev-parse", "HEAD")
            target = base / "installed"
            install_skill(
                source, target, summary="live continuation", impact="live_assignments",
                stop_condition="real continuation e2e", previous_revision=previous_revision, now=NOW,
            )
            repo = make_project(base)
            registry = base / "controllers.json"
            write_current_controller_registry(registry, repo)
            acknowledge_rule_revision(
                repo, "controller-1", revision, skill_root=target, registry_path=registry, now=NOW
            )
            ledger = repo / "TASK_LEDGER.md"
            ledger.write_text(
                ledger.read_text(encoding="utf-8").replace(
                    "adaptive-delivery@old", f"adaptive-delivery@{revision}"
                ),
                encoding="utf-8",
            )

            status = evaluate_rule_handshake(
                repo, skill_root=target, registry_path=registry
            )

            self.assertEqual(status["state"], "pending_live_e2e")
            self.assertTrue(status["blocking"])
            self.assertIn("scripts/web_lifecycle_bridge.py", status["live_e2e_changed_files"])

    def test_explicit_live_e2e_deferral_is_revision_scoped_and_auditable(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, previous_revision = make_source(base)
            bridge = source / "scripts" / "web_lifecycle_bridge.py"
            bridge.write_text("VALUE = 2\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "change live continuation")
            revision = git(source, "rev-parse", "HEAD")
            target = base / "installed"
            install_skill(
                source, target, summary="live continuation", impact="live_assignments",
                stop_condition="real continuation e2e", previous_revision=previous_revision, now=NOW,
            )
            repo = make_project(base)
            registry = base / "controllers.json"
            write_current_controller_registry(registry, repo)
            acknowledge_rule_revision(
                repo, "controller-1", revision, skill_root=target, registry_path=registry, now=NOW
            )
            ledger = repo / "TASK_LEDGER.md"
            ledger.write_text(
                ledger.read_text(encoding="utf-8").replace(
                    "adaptive-delivery@old", f"adaptive-delivery@{revision}"
                ), encoding="utf-8"
            )

            receipt = rule_handshake_module.defer_live_e2e(
                repo, "controller-1", revision,
                reason="host attestation unavailable until replacement bridge",
                skill_root=target, registry_path=registry,
                **controller_action_kwargs(registry), now=NOW,
            )
            status = evaluate_rule_handshake(
                repo, skill_root=target, registry_path=registry
            )

            self.assertEqual(receipt["status"], "deferred")
            self.assertEqual(receipt["installed_revision"], revision)
            self.assertEqual(receipt["controller_session_id"], "controller-1")
            self.assertEqual(status["state"], "current_deferred_live_e2e")
            self.assertFalse(status["blocking"])
            self.assertTrue(status["live_e2e_required"])
            self.assertEqual(status["live_e2e_deferred_revision"], revision)
            self.assertIn("host attestation unavailable", status["live_e2e_deferred_reason"])

    def test_forged_live_e2e_acceptance_without_machine_evidence_stays_blocking(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, previous_revision = make_source(base)
            bridge = source / "scripts" / "web_lifecycle_bridge.py"
            bridge.write_text("VALUE = 2\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "change live continuation")
            revision = git(source, "rev-parse", "HEAD")
            target = base / "installed"
            install_skill(
                source, target, summary="live continuation", impact="live_assignments",
                stop_condition="real continuation e2e", previous_revision=previous_revision, now=NOW,
            )
            repo = make_project(base)
            registry = base / "controllers.json"
            write_current_controller_registry(registry, repo)
            acknowledge_rule_revision(
                repo, "controller-1", revision, skill_root=target, registry_path=registry, now=NOW
            )
            ledger = repo / "TASK_LEDGER.md"
            ledger.write_text(
                ledger.read_text(encoding="utf-8").replace(
                    "adaptive-delivery@old", f"adaptive-delivery@{revision}"
                ), encoding="utf-8"
            )
            acceptance = live_e2e_acceptance_path(repo)
            acceptance.parent.mkdir(parents=True, exist_ok=True)
            acceptance.write_text(json.dumps({
                "status": "accepted",
                "installed_revision": revision,
                "controller_session_id": "controller-1",
            }), encoding="utf-8")

            status = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)

            self.assertEqual(status["state"], "pending_live_e2e")
            self.assertTrue(status["blocking"])

    def test_real_confirmed_wake_followed_by_closed_cycle_can_finalize_live_e2e(self):
        self.assertTrue(
            hasattr(rule_handshake_module, "accept_live_e2e"),
            "rule handshake must expose a machine live-E2E finalizer",
        )
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, previous_revision = make_source(base)
            bridge = source / "scripts" / "web_lifecycle_bridge.py"
            bridge.write_text("VALUE = 2\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "change live continuation")
            revision = git(source, "rev-parse", "HEAD")
            target = base / "installed"
            install_skill(
                source, target, summary="live continuation", impact="live_assignments",
                stop_condition="real continuation e2e", previous_revision=previous_revision, now=NOW,
            )
            repo = make_project(base)
            registry = base / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-1": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 2,
                        }
                    }
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "desktop_codex",
                        "execution_target_session_id": "desktop-current",
                        "generation": 7,
                    }
                },
            }), encoding="utf-8")
            acknowledge_rule_revision(
                repo, "controller-1", revision, skill_root=target, registry_path=registry, now=NOW
            )
            ledger = repo / "TASK_LEDGER.md"
            ledger.write_text(
                ledger.read_text(encoding="utf-8").replace(
                    "adaptive-delivery@old", f"adaptive-delivery@{revision}"
                ), encoding="utf-8"
            )
            state_dir = rule_state_path(repo).parent
            wake = state_dir / "controller-wake-receipt.json"
            wake.write_text(json.dumps({
                "controller_id": "controller-1",
                "selected_host": "desktop_codex",
                "result": "CONFIRMED",
                "execution_target_session_id": "desktop-current",
                "target_generation": 2,
                "ownership_generation": 7,
                "completed_at_unix_ms": int(NOW.timestamp() * 1000) + 1000,
            }), encoding="utf-8")
            cycle_dir = state_dir / "controller-cycle-evidence"
            cycle_dir.mkdir(parents=True, exist_ok=True)
            cycle = cycle_dir / "closed.json"
            cycle.write_text(json.dumps({
                "record_kind": "controller_cycle_evidence",
                "controller_id": "controller-1",
                "terminal_status": "CLOSED",
                "validation_errors": [],
                "recorded_at": "2026-08-30T01:00:02+00:00",
            }), encoding="utf-8")

            receipt = rule_handshake_module.accept_live_e2e(
                repo,
                "controller-1",
                revision,
                skill_root=target,
                registry_path=registry,
                **controller_action_kwargs(registry),
                now=datetime(2026, 8, 30, 1, 0, 3, tzinfo=UTC),
            )
            self.assertEqual(receipt["status"], "accepted")
            current = evaluate_rule_handshake(
                repo, skill_root=target, registry_path=registry
            )
            self.assertEqual(current["state"], "current")
            self.assertFalse(current["blocking"])

            registry_value = json.loads(registry.read_text(encoding="utf-8"))
            registry_value["__controller_sessions__"]["controller-1"]["desktop_codex"].append("desktop-next")
            registry_value["__controller_targets__"]["controller-1"]["desktop_codex"] = {
                "status": "active",
                "session_id": "desktop-next",
                "generation": 3,
            }
            registry.write_text(json.dumps(registry_value), encoding="utf-8")
            after_legitimate_target_rotation = evaluate_rule_handshake(
                repo, skill_root=target, registry_path=registry
            )
            self.assertEqual(after_legitimate_target_rotation["state"], "current")
            self.assertFalse(after_legitimate_target_rotation["blocking"])

    def test_live_e2e_rejects_stale_ownership_generation_before_acceptance(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, previous_revision = make_source(base)
            (source / "scripts" / "web_lifecycle_bridge.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            git(source, "add", ".")
            git(source, "commit", "-m", "change live continuation")
            revision = git(source, "rev-parse", "HEAD")
            target = base / "installed"
            install_skill(
                source,
                target,
                summary="live continuation",
                impact="live_assignments",
                stop_condition="real continuation e2e",
                previous_revision=previous_revision,
                now=NOW,
            )
            repo = make_project(base)
            registry = base / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["desktop-current"]}
                },
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active",
                    "session_id": "desktop-current",
                    "generation": 2,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "desktop_codex",
                    "execution_target_session_id": "desktop-current",
                    "generation": 9,
                }},
            }), encoding="utf-8")
            acknowledge_rule_revision(
                repo,
                "controller-1",
                revision,
                skill_root=target,
                registry_path=registry,
                now=NOW,
            )
            ledger = repo / "TASK_LEDGER.md"
            ledger.write_text(ledger.read_text(encoding="utf-8").replace(
                "adaptive-delivery@old", f"adaptive-delivery@{revision}"
            ), encoding="utf-8")
            state_dir = rule_state_path(repo).parent
            (state_dir / "controller-wake-receipt.json").write_text(json.dumps({
                "controller_id": "controller-1",
                "selected_host": "desktop_codex",
                "result": "CONFIRMED",
                "execution_target_session_id": "desktop-current",
                "target_generation": 2,
                "ownership_generation": 8,
                "completed_at_unix_ms": int(NOW.timestamp() * 1000) + 1000,
            }), encoding="utf-8")
            cycle_dir = state_dir / "controller-cycle-evidence"
            cycle_dir.mkdir(parents=True, exist_ok=True)
            (cycle_dir / "closed.json").write_text(json.dumps({
                "record_kind": "controller_cycle_evidence",
                "controller_id": "controller-1",
                "terminal_status": "CLOSED",
                "validation_errors": [],
                "recorded_at": "2026-08-30T01:00:02+00:00",
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "ownership generation"):
                rule_handshake_module.accept_live_e2e(
                    repo,
                    "controller-1",
                    revision,
                    skill_root=target,
                    registry_path=registry,
                    **controller_action_kwargs(registry),
                    now=datetime(2026, 8, 30, 1, 0, 3, tzinfo=UTC),
                )

            frozen = state_dir / "runtime-live-e2e-evidence" / f"{revision}.wake.json"
            self.assertFalse(frozen.exists())

    def test_failed_live_e2e_does_not_freeze_invalid_wake_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, previous_revision = make_source(base)
            bridge = source / "scripts" / "web_lifecycle_bridge.py"
            bridge.write_text("VALUE = 2\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "change live continuation")
            revision = git(source, "rev-parse", "HEAD")
            target = base / "installed"
            install_skill(
                source, target, summary="live continuation", impact="live_assignments",
                stop_condition="real continuation e2e", previous_revision=previous_revision, now=NOW,
            )
            repo = make_project(base)
            registry = base / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-1": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 2,
                        }
                    }
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "desktop_codex",
                        "execution_target_session_id": "desktop-current",
                        "generation": 2,
                    }
                },
            }), encoding="utf-8")
            acknowledge_rule_revision(
                repo, "controller-1", revision, skill_root=target, registry_path=registry, now=NOW
            )
            ledger = repo / "TASK_LEDGER.md"
            ledger.write_text(ledger.read_text(encoding="utf-8").replace(
                "adaptive-delivery@old", f"adaptive-delivery@{revision}"
            ), encoding="utf-8")
            state_dir = rule_state_path(repo).parent
            (state_dir / "controller-wake-receipt.json").write_text(json.dumps({
                "controller_id": "controller-1",
                "selected_host": "desktop_codex",
                "result": "DEFERRED",
                "execution_target_session_id": "desktop-current",
                "target_generation": 2,
                "completed_at_unix_ms": int(NOW.timestamp() * 1000) + 1000,
            }), encoding="utf-8")
            cycle_dir = state_dir / "controller-cycle-evidence"
            cycle_dir.mkdir(parents=True, exist_ok=True)
            (cycle_dir / "closed.json").write_text(json.dumps({
                "record_kind": "controller_cycle_evidence",
                "controller_id": "controller-1",
                "terminal_status": "CLOSED",
                "validation_errors": [],
                "recorded_at": "2026-08-30T01:00:02+00:00",
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "wake evidence is not confirmed"):
                rule_handshake_module.accept_live_e2e(
                    repo, "controller-1", revision, skill_root=target, registry_path=registry,
                    **controller_action_kwargs(registry), now=NOW
                )

            frozen = state_dir / "runtime-live-e2e-evidence" / f"{revision}.wake.json"
            self.assertFalse(frozen.exists())

    def test_live_e2e_debt_survives_later_nonimpacting_install_until_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision1 = make_source(base)
            target = base / "installed"
            repo = make_project(base, revision_text=revision1)
            registry = base / "controllers.json"
            write_current_controller_registry(registry, repo)
            install_skill(source, target, summary="baseline", impact="none", stop_condition="none", now=NOW)
            acknowledge_rule_revision(repo, "controller-1", revision1, skill_root=target, registry_path=registry, now=NOW)

            critical = source / "scripts" / "web_lifecycle_bridge.py"
            critical.write_text("VALUE = 2\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "critical continuation change")
            revision2 = git(source, "rev-parse", "HEAD")
            install_skill(
                source, target, summary="critical", impact="live_assignments",
                stop_condition="real e2e", previous_revision=revision1, now=NOW,
            )
            acknowledge_rule_revision(repo, "controller-1", revision2, skill_root=target, registry_path=registry, now=NOW)
            ledger = repo / "TASK_LEDGER.md"
            ledger.write_text(ledger.read_text(encoding="utf-8").replace(
                f"adaptive-delivery@{revision1}", f"adaptive-delivery@{revision2}"
            ), encoding="utf-8")
            self.assertEqual(
                evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)["state"],
                "pending_live_e2e",
            )

            (source / "README.md").write_text("docs only\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "docs only")
            revision3 = git(source, "rev-parse", "HEAD")
            install_skill(
                source, target, summary="docs", impact="none", stop_condition="next turn",
                previous_revision=revision2, now=NOW,
            )
            acknowledge_rule_revision(repo, "controller-1", revision3, skill_root=target, registry_path=registry, now=NOW)
            ledger.write_text(ledger.read_text(encoding="utf-8").replace(
                f"adaptive-delivery@{revision2}", f"adaptive-delivery@{revision3}"
            ), encoding="utf-8")

            status = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(status["state"], "pending_live_e2e")
            self.assertTrue(status["blocking"])
            self.assertEqual(status["live_e2e_required_since_revision"], revision2)

    def test_live_e2e_rejects_confirmed_wake_that_predates_rule_ack(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, previous_revision = make_source(base)
            (source / "scripts" / "web_lifecycle_bridge.py").write_text("VALUE = 2\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "critical continuation change")
            revision = git(source, "rev-parse", "HEAD")
            target = base / "installed"
            install_skill(
                source, target, summary="critical", impact="live_assignments",
                stop_condition="real e2e", previous_revision=previous_revision, now=NOW,
            )
            repo = make_project(base)
            registry = base / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-current"]}},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 2
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "desktop_codex",
                    "execution_target_session_id": "desktop-current",
                    "generation": 2,
                }},
            }), encoding="utf-8")
            acknowledge_rule_revision(repo, "controller-1", revision, skill_root=target, registry_path=registry, now=NOW)
            ledger = repo / "TASK_LEDGER.md"
            ledger.write_text(ledger.read_text(encoding="utf-8").replace(
                "adaptive-delivery@old", f"adaptive-delivery@{revision}"
            ), encoding="utf-8")
            state_dir = rule_state_path(repo).parent
            (state_dir / "controller-wake-receipt.json").write_text(json.dumps({
                "controller_id": "controller-1",
                "selected_host": "desktop_codex",
                "result": "CONFIRMED",
                "execution_target_session_id": "desktop-current",
                "target_generation": 2,
                "completed_at_unix_ms": int(NOW.timestamp() * 1000) - 1000,
            }), encoding="utf-8")
            cycle_dir = state_dir / "controller-cycle-evidence"
            cycle_dir.mkdir(parents=True, exist_ok=True)
            (cycle_dir / "closed.json").write_text(json.dumps({
                "record_kind": "controller_cycle_evidence",
                "controller_id": "controller-1",
                "terminal_status": "CLOSED",
                "validation_errors": [],
                "recorded_at": "2026-08-30T01:00:02+00:00",
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "wake.*rule ACK"):
                rule_handshake_module.accept_live_e2e(
                    repo, "controller-1", revision, skill_root=target, registry_path=registry,
                    **controller_action_kwargs(registry), now=NOW
                )

    def test_later_nonimpacting_install_cannot_clear_unacked_live_impact_debt(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision1 = make_source(base)
            target = base / "installed"
            repo = make_project(base, revision_text=revision1)
            registry = base / "controllers.json"
            write_current_controller_registry(registry, repo)

            install_skill(source, target, summary="baseline", impact="none", stop_condition="none", now=NOW)
            acknowledge_rule_revision(repo, "controller-1", revision1, skill_root=target, registry_path=registry, now=NOW)

            critical = source / "scripts" / "run_external_agent.mjs"
            critical.write_text("export const route = 2;\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "live routing change")
            revision2 = git(source, "rev-parse", "HEAD")
            install_skill(
                source, target, summary="live routing", impact="live_assignments",
                stop_condition="ack before launch", previous_revision=revision1, now=NOW,
            )

            (source / "README.md").write_text("docs only\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "docs only")
            revision3 = git(source, "rev-parse", "HEAD")
            install_skill(
                source, target, summary="docs only", impact="none", stop_condition="next turn",
                previous_revision=revision2, now=NOW,
            )

            result = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(result["loaded_revision"], revision1)
            self.assertEqual(result["installed_revision"], revision3)
            self.assertEqual(result["state"], "pending_ack")
            self.assertTrue(result["blocking"])
            self.assertEqual(result["effective_impact"], "live_assignments")
            self.assertIn("scripts/run_external_agent.mjs", result["unacked_changed_files"])

    def test_loaded_controller_with_only_unacked_nonimpacting_changes_stays_nonblocking(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision1 = make_source(base)
            target = base / "installed"
            repo = make_project(base, revision_text=revision1)
            registry = base / "controllers.json"
            write_current_controller_registry(registry, repo)
            install_skill(source, target, summary="baseline", impact="none", stop_condition="none", now=NOW)
            acknowledge_rule_revision(repo, "controller-1", revision1, skill_root=target, registry_path=registry, now=NOW)

            (source / "README.md").write_text("docs only\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "docs only")
            revision2 = git(source, "rev-parse", "HEAD")
            install_skill(source, target, summary="docs only", impact="none", stop_condition="next turn", previous_revision=revision1, now=NOW)

            result = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(result["installed_revision"], revision2)
            self.assertEqual(result["effective_impact"], "none")
            self.assertFalse(result["blocking"])

    def test_unverifiable_cumulative_change_range_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision = make_source(base)
            target = base / "installed"
            repo = make_project(base)
            install_skill(source, target, summary="docs only", impact="none", stop_condition="next turn", now=NOW)
            state_path = rule_state_path(repo)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"loaded_revision": "missing-old-revision", "controller_session_id": "controller-1"}), encoding="utf-8")
            result = evaluate_rule_handshake(repo, skill_root=target, registry_path=base / "missing.json")
            self.assertEqual(result["effective_impact"], "live_assignments")
            self.assertTrue(result["blocking"])

    def test_nonimpacting_update_surfaces_drift_without_blocking_launch(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision = make_source(base)
            target = base / "installed"
            install_skill(source, target, summary="docs only", impact="none", stop_condition="none", now=NOW)
            repo = make_project(base)
            result = evaluate_rule_handshake(repo, skill_root=target, registry_path=base / "missing.json")
            self.assertEqual(result["installed_revision"], revision)
            self.assertEqual(result["state"], "pending_ack")
            self.assertFalse(result["blocking"])


if __name__ == "__main__":
    unittest.main()


def _fake_project_chat_cannot_ack_by_claiming_logical_controller_id(self):
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        source, revision = make_source(base)
        target = base / "installed"
        install_skill(source, target, summary="rules", impact="live_assignments", stop_condition="ack", now=NOW)
        repo = make_project(base)
        registry = base / "controllers.json"
        write_current_controller_registry(
            registry, repo, host="web", source_session_id="web-real-controller",
            target_generation=3, ownership_generation=7,
        )
        with self.assertRaisesRegex(ValueError, "canonical current execution target"):
            rule_handshake_module.acknowledge_rule_revision(
                repo,
                "controller-1",
                revision,
                skill_root=target,
                registry_path=registry,
                execution_host="web",
                source_session_id="web-fake-project-chat",
                now=NOW,
            )
        self.assertFalse(rule_state_path(repo).exists())


def _current_web_target_ack_records_exact_source_and_generations(self):
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        source, revision = make_source(base)
        target = base / "installed"
        install_skill(source, target, summary="rules", impact="live_assignments", stop_condition="ack", now=NOW)
        repo = make_project(base)
        registry = base / "controllers.json"
        write_current_controller_registry(
            registry, repo, host="web", source_session_id="web-real-controller",
            target_generation=3, ownership_generation=7,
        )
        receipt = rule_handshake_module.acknowledge_rule_revision(
            repo,
            "controller-1",
            revision,
            skill_root=target,
            registry_path=registry,
            execution_host="web",
            source_session_id="web-real-controller",
            now=NOW,
        )
        self.assertEqual(receipt["execution_host"], "web")
        self.assertEqual(receipt["source_session_id"], "web-real-controller")
        self.assertEqual(receipt["target_generation"], 3)
        self.assertEqual(receipt["ownership_generation"], 7)
        self.assertEqual(receipt["controller_action_source"]["source_session_id"], "web-real-controller")


RuleHandshakeTests.test_fake_project_chat_cannot_ack_by_claiming_logical_controller_id = _fake_project_chat_cannot_ack_by_claiming_logical_controller_id
RuleHandshakeTests.test_current_web_target_ack_records_exact_source_and_generations = _current_web_target_ack_records_exact_source_and_generations


def _fake_project_chat_cannot_accept_or_defer_live_e2e(self):
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        source, previous_revision = make_source(base)
        bridge = source / "scripts" / "web_lifecycle_bridge.py"
        bridge.write_text("VALUE = 2\n", encoding="utf-8")
        git(source, "add", ".")
        git(source, "commit", "-m", "critical live change")
        revision = git(source, "rev-parse", "HEAD")
        target = base / "installed"
        install_skill(
            source, target, summary="critical", impact="live_assignments",
            stop_condition="real e2e", previous_revision=previous_revision, now=NOW,
        )
        repo = make_project(base)
        registry = base / "controllers.json"
        write_current_controller_registry(
            registry, repo, host="web", source_session_id="web-real-controller",
            target_generation=3, ownership_generation=7,
        )
        acknowledge_rule_revision(
            repo, "controller-1", revision,
            skill_root=target, registry_path=registry, now=NOW,
        )
        ledger = repo / "TASK_LEDGER.md"
        ledger.write_text(
            ledger.read_text(encoding="utf-8").replace(
                "adaptive-delivery@old", f"adaptive-delivery@{revision}"
            ), encoding="utf-8"
        )
        status = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
        self.assertEqual(status["state"], "pending_live_e2e")

        fake_source = {
            "execution_host": "web",
            "source_session_id": "web-fake-project-chat",
        }
        with self.assertRaisesRegex(ValueError, "canonical current execution target"):
            rule_handshake_module.defer_live_e2e(
                repo, "controller-1", revision,
                reason="fake chat must not defer",
                skill_root=target, registry_path=registry,
                **fake_source, now=NOW,
            )
        with self.assertRaisesRegex(ValueError, "canonical current execution target"):
            rule_handshake_module.accept_live_e2e(
                repo, "controller-1", revision,
                skill_root=target, registry_path=registry,
                **fake_source, now=NOW,
            )

        after = rule_handshake_module.load_rule_state(repo)
        self.assertNotIn("live_e2e_deferred_revision", after)
        self.assertFalse(live_e2e_acceptance_path(repo).exists())


RuleHandshakeTests.test_fake_project_chat_cannot_accept_or_defer_live_e2e = _fake_project_chat_cannot_accept_or_defer_live_e2e


def _manual_unverified_current_web_target_cannot_ack_even_if_caller_claims_exact_target(self):
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        source, revision = make_source(base)
        target = base / "installed"
        install_skill(source, target, summary="rules", impact="live_assignments", stop_condition="ack", now=NOW)
        repo = make_project(base)
        registry = base / "controllers.json"
        write_current_controller_registry(
            registry, repo, host="web", source_session_id="web-real-controller",
            target_generation=3, ownership_generation=7, verified_web=False,
        )
        with self.assertRaisesRegex(ValueError, "not authorized for Controller actions"):
            rule_handshake_module.acknowledge_rule_revision(
                repo,
                "controller-1",
                revision,
                skill_root=target,
                registry_path=registry,
                execution_host="web",
                source_session_id="web-real-controller",
                now=NOW,
            )
        self.assertFalse(rule_state_path(repo).exists())


RuleHandshakeTests.test_manual_unverified_current_web_target_cannot_ack_even_if_caller_claims_exact_target = _manual_unverified_current_web_target_cannot_ack_even_if_caller_claims_exact_target
