"""Offline coverage for the official Codex hook-trust readiness probe."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import install_skill


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _camel_case(value: str) -> str:
    return value[:1].lower() + value[1:]


class DesktopHookTrustProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill_root = self.root / "adaptive-delivery"
        scripts = self.skill_root / "scripts"
        scripts.mkdir(parents=True)
        for name in (
            "lifecycle_hook.py",
            "controller_scoring_hook.py",
            "project_context_guard.py",
        ):
            (scripts / name).write_text(f"# {name}\n", encoding="utf-8")
        self.hooks_path = self.root / "hooks.json"
        install_skill.install_codex_hooks(
            self.hooks_path,
            self.skill_root,
            python_executable="/usr/bin/python3",
        )
        self.codex_path = self.root / "codex"
        self.codex_path.write_text("#!/bin/sh\n", encoding="utf-8")
        self.codex_path.chmod(0o755)
        self.official_hooks = self._official_hooks()
        self.assertEqual(len(self.official_hooks), 11)

    def _official_hooks(self) -> list[dict]:
        config = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        source = str(self.hooks_path.resolve())
        hooks: list[dict] = []
        for event_name, groups in config["hooks"].items():
            for group_index, group in enumerate(groups):
                for handler_index, handler in enumerate(group["hooks"]):
                    hooks.append({
                        "key": (
                            f"{source}:{_snake_case(event_name)}:"
                            f"{group_index}:{handler_index}"
                        ),
                        "eventName": _camel_case(event_name),
                        "handlerType": handler["type"],
                        "command": handler["command"],
                        "sourcePath": source,
                        "source": "user",
                        "pluginId": None,
                        "enabled": True,
                        "isManaged": False,
                        "currentHash": "sha256:" + "a" * 64,
                        "trustStatus": "trusted",
                    })
        return hooks

    def _probe(self, hooks: list[dict], *, errors: list | None = None) -> dict:
        result = {
            "data": [{
                "cwd": str(self.skill_root.resolve()),
                "hooks": hooks,
                "warnings": [],
                "errors": errors or [],
            }],
        }
        with patch.object(
            install_skill,
            "_read_codex_hooks",
            return_value=result,
            create=True,
        ):
            return install_skill.probe_codex_hook_trust(
                codex_path=self.codex_path,
                hooks_path=self.hooks_path,
                skill_root=self.skill_root,
            )

    def test_exact_trusted_or_managed_hooks_are_ready_with_per_hook_evidence(self) -> None:
        report = self._probe(copy.deepcopy(self.official_hooks))

        self.assertEqual(report["status"], "ready")
        self.assertEqual(len(report["hooks"]), 11)
        self.assertEqual(
            {item["key"] for item in report["hooks"]},
            {item["key"] for item in self.official_hooks},
        )
        self.assertTrue(all(item["ready"] is True for item in report["hooks"]))

        managed = copy.deepcopy(self.official_hooks)
        managed[0]["trustStatus"] = "managed"
        managed[0]["isManaged"] = True
        managed_report = self._probe(managed)
        self.assertEqual(managed_report["status"], "ready")

    def test_nonexact_disabled_or_untrusted_hook_never_reports_ready(self) -> None:
        mutations = {
            "modified": lambda hooks: hooks[0].update(trustStatus="modified"),
            "untrusted": lambda hooks: hooks[0].update(trustStatus="untrusted"),
            "disabled": lambda hooks: hooks[0].update(enabled=False),
            "managed_without_host_proof": lambda hooks: hooks[0].update(
                trustStatus="managed", isManaged=False
            ),
            "missing": lambda hooks: hooks.pop(0),
            "duplicate": lambda hooks: hooks.append(copy.deepcopy(hooks[0])),
            "wrong_command": lambda hooks: hooks[0].update(command="/bin/true"),
            "invalid_current_hash": lambda hooks: hooks[0].update(
                currentHash="sha256:" + "A" * 64
            ),
            "trusted_plugin_impostor": lambda hooks: hooks[0].update(
                sourcePath=str(self.root / "plugin" / "hooks.json"),
                source="plugin",
                pluginId="trusted-plugin",
            ),
            "malformed": lambda hooks: hooks.__setitem__(0, {"enabled": True}),
        }
        for case, mutate in mutations.items():
            with self.subTest(case=case):
                hooks = copy.deepcopy(self.official_hooks)
                mutate(hooks)
                report = self._probe(hooks)
                self.assertEqual(report["status"], "review_required")
                self.assertEqual(len(report["hooks"]), 11)
                self.assertTrue(any(item["ready"] is False for item in report["hooks"]))

    def test_incomplete_config_cannot_redefine_the_required_hook_set(self) -> None:
        config = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        config["hooks"]["PreToolUse"] = []
        self.hooks_path.write_text(json.dumps(config), encoding="utf-8")
        remaining_official_hooks = self._official_hooks()
        self.assertEqual(len(remaining_official_hooks), 10)

        report = self._probe(remaining_official_hooks)

        self.assertEqual(report["status"], "review_required")
        self.assertNotEqual(report["status"], "ready")

    def test_official_query_error_or_invalid_result_is_unavailable(self) -> None:
        error_report = self._probe(
            copy.deepcopy(self.official_hooks),
            errors=[{"message": "hook registry unavailable"}],
        )
        self.assertEqual(error_report["status"], "unavailable")
        self.assertNotEqual(error_report["status"], "ready")

        with patch.object(
            install_skill,
            "_read_codex_hooks",
            side_effect=OSError("official hook query failed"),
            create=True,
        ):
            unavailable = install_skill.probe_codex_hook_trust(
                codex_path=self.codex_path,
                hooks_path=self.hooks_path,
                skill_root=self.skill_root,
            )
        self.assertEqual(unavailable["status"], "unavailable")

        wrong_cwd = {
            "data": [{
                "cwd": str(self.root / "other-project"),
                "hooks": copy.deepcopy(self.official_hooks),
                "warnings": [],
                "errors": [],
            }],
        }
        with patch.object(
            install_skill,
            "_read_codex_hooks",
            return_value=wrong_cwd,
        ):
            unavailable = install_skill.probe_codex_hook_trust(
                codex_path=self.codex_path,
                hooks_path=self.hooks_path,
                skill_root=self.skill_root,
            )
        self.assertEqual(unavailable["status"], "unavailable")

    def test_official_stdio_probe_handles_success_eof_and_timeout_with_cleanup(self) -> None:
        fake = self.root / "fake-codex"
        fake.write_text(
            f"#!{sys.executable}\n" + r'''
import json
import os
import sys
import time

scenario = os.environ["FAKE_HOOK_LIST_SCENARIO"]
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if scenario == "eof":
        raise SystemExit(0)
    if scenario == "timeout":
        time.sleep(60)
    if method == "initialize":
        print(json.dumps({"id":message["id"],"result":{}}), flush=True)
    elif method == "hooks/list":
        cwd = message["params"]["cwds"][0]
        print(json.dumps({"id":message["id"],"result":{"data":[{
            "cwd":cwd,"hooks":[],"warnings":[],"errors":[]
        }]}}), flush=True)
''',
            encoding="utf-8",
        )
        fake.chmod(0o755)
        real_popen = install_skill.subprocess.Popen

        def invoke(scenario: str, *, force_timeout: bool = False):
            processes = []
            failure = None

            def capture(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                processes.append(process)
                return process

            try:
                with patch.dict(os.environ, {"FAKE_HOOK_LIST_SCENARIO": scenario}), patch.object(
                    install_skill.subprocess, "Popen", side_effect=capture
                ):
                    if force_timeout:
                        with patch.object(
                            install_skill.time, "monotonic", side_effect=[0.0, 11.0]
                        ):
                            with self.assertRaises(TimeoutError):
                                install_skill._read_codex_hooks(fake, self.skill_root)
                        result = None
                    else:
                        result = install_skill._read_codex_hooks(fake, self.skill_root)
            except Exception as error:  # Re-raise only after verifying owned-process cleanup.
                failure = error
                result = None
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            if failure is not None:
                raise failure
            return result

        success = invoke("success")
        self.assertEqual(success["data"][0]["cwd"], str(self.skill_root))
        with self.assertRaises(ValueError):
            invoke("eof")
        invoke("timeout", force_timeout=True)

    def test_desktop_codex_resolver_falls_back_only_when_resolver_module_is_absent(self) -> None:
        explicit = self.root / "explicit-codex"
        with patch("builtins.__import__", side_effect=AssertionError("must not import")), patch.object(
            install_skill.shutil, "which", side_effect=AssertionError("must not search PATH")
        ):
            self.assertEqual(install_skill._desktop_codex_path(explicit), explicit)

        original_import = __import__

        def dependency_failure(name, globals=None, locals=None, fromlist=(), level=0):
            if name in {"web_lifecycle_bridge", "scripts.web_lifecycle_bridge"}:
                raise ModuleNotFoundError(
                    "No module named 'resolver_internal_dependency'",
                    name="resolver_internal_dependency",
                )
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=dependency_failure), patch.object(
            install_skill.shutil, "which", return_value="/wrong/path/codex"
        ) as which:
            self.assertIsNone(install_skill._desktop_codex_path(None))
            which.assert_not_called()

        def resolver_absent(name, globals=None, locals=None, fromlist=(), level=0):
            if name in {"web_lifecycle_bridge", "scripts.web_lifecycle_bridge"}:
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return original_import(name, globals, locals, fromlist, level)

        fallback = self.root / "path-codex"
        with patch("builtins.__import__", side_effect=resolver_absent), patch.object(
            install_skill.shutil, "which", return_value=str(fallback)
        ) as which:
            self.assertEqual(install_skill._desktop_codex_path(None), fallback)
            which.assert_called_once_with("codex")


if __name__ == "__main__":
    unittest.main()
