#!/usr/bin/env python3
"""Install one exact Adaptive Delivery revision with a machine-readable manifest."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

UTC = timezone.utc
PRODUCT_NAME = "Adaptive Agent Runtime"
SKILL_ID = "adaptive-delivery"
PRODUCT_SLUG = "adaptive-agent-runtime"
LEGACY_SKILL_IDS = ("adaptive-delivery",)
DEFAULT_AI_BRIDGE_EXECUTABLE = Path("/Applications/AI-Bridge.app/Contents/MacOS/ai-bridge")
DEFAULT_CODEX_HOOKS = Path.home() / ".codex" / "hooks.json"
DEFAULT_ZSHENV = Path.home() / ".zshenv"
WEB_BLOCK_START = "# >>> adaptive-delivery web lifecycle bridge >>>"
WEB_BLOCK_END = "# <<< adaptive-delivery web lifecycle bridge <<<"
MANIFEST_NAME = ".adaptive-delivery-install.json"
IMPACTS = {"none", "live_assignments"}



def _hooks_contain(path: Path, needle: str, *, skill_root: Path | None = None) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return False
    expected = (skill_root / "scripts" / needle).resolve() if skill_root is not None else None
    if expected is not None and not expected.is_file():
        return False
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                continue
            for handler in entry["hooks"]:
                if not isinstance(handler, dict):
                    continue
                command = handler.get("command")
                if not isinstance(command, str) or needle not in command:
                    continue
                if expected is None:
                    return True
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    continue
                if len(tokens) >= 2 and Path(tokens[1]).expanduser().resolve() == expected:
                    return True
    return False


def _zshenv_has_web_bridge(
    path: Path, *, skill_root: Path | None = None, ai_bridge_executable: Path | None = None
) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    start = text.find(WEB_BLOCK_START)
    end = text.find(WEB_BLOCK_END, start + len(WEB_BLOCK_START)) if start >= 0 else -1
    if start < 0 or end < 0:
        return False
    block = text[start:end + len(WEB_BLOCK_END)]
    command_line = next((line.strip() for line in block.splitlines() if " post-shell " in line and "web_lifecycle_bridge.py" in line), None)
    if command_line is None:
        return False
    command_text = command_line[:-7].rstrip() if command_line.endswith("|| true") else command_line
    try:
        tokens = shlex.split(command_text)
    except ValueError:
        return False
    if len(tokens) < 3 or tokens[2] != "post-shell":
        return False
    python_path = Path(tokens[0]).expanduser()
    script_path = Path(tokens[1]).expanduser()
    if not python_path.is_file() or not os.access(python_path, os.X_OK) or not script_path.is_file():
        return False
    if skill_root is not None:
        expected_script = (skill_root / "scripts" / "web_lifecycle_bridge.py").expanduser().resolve()
        if script_path.resolve() != expected_script:
            return False
    if ai_bridge_executable is not None:
        expected_assignment = f"_ad_web_bridge_executable={shlex.quote(str(ai_bridge_executable.expanduser().resolve()))}"
        if expected_assignment not in block:
            return False
    return True


def detect_host_capabilities(
    *,
    codex_executable: str | Path | None = None,
    ai_bridge_executable: str | Path = DEFAULT_AI_BRIDGE_EXECUTABLE,
    hooks_file: str | Path = DEFAULT_CODEX_HOOKS,
    zshenv_file: str | Path = DEFAULT_ZSHENV,
    skill_root: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    codex_path = Path(codex_executable).expanduser() if codex_executable else None
    if codex_path is None:
        discovered = shutil.which("codex")
        codex_path = Path(discovered) if discovered else None
    bridge_path = Path(ai_bridge_executable).expanduser()
    hooks_path = Path(hooks_file).expanduser()
    zshenv_path = Path(zshenv_file).expanduser()
    skill_root_path = Path(skill_root).expanduser().resolve() if skill_root is not None else None

    codex_available = bool(codex_path and codex_path.is_file() and os.access(codex_path, os.X_OK))
    lifecycle_configured = _hooks_contain(hooks_path, "lifecycle_hook.py", skill_root=skill_root_path)
    scoring_configured = _hooks_contain(hooks_path, "controller_scoring_hook.py", skill_root=skill_root_path)
    if not codex_available:
        desktop = {
            "status": "blocked", "adapter": "codex-native", "configured": False,
            "reason": "codex executable not detected",
        }
    elif lifecycle_configured and scoring_configured:
        desktop = {
            "status": "degraded", "adapter": "codex-native", "configured": True,
            "reason": "hooks configured; host trust/activation still requires verification",
        }
    else:
        desktop = {
            "status": "degraded", "adapter": "codex-native", "configured": False,
            "reason": "codex detected; lifecycle/scoring hooks are not fully configured",
        }

    bridge_available = bridge_path.is_file() and os.access(bridge_path, os.X_OK)
    bridge_configured = _zshenv_has_web_bridge(
        zshenv_path, skill_root=skill_root_path, ai_bridge_executable=bridge_path
    )
    if bridge_available and bridge_configured:
        web = {
            "status": "enabled", "adapter": "ai-bridge", "mode": "local_bridge",
            "configured": True, "reason": "AI-Bridge executable and shell lifecycle bridge detected",
        }
    elif bridge_available:
        web = {
            "status": "degraded", "adapter": "ai-bridge", "mode": "local_bridge",
            "configured": False, "reason": "AI-Bridge detected; shell lifecycle bridge is not configured",
        }
    else:
        web = {
            "status": "degraded", "adapter": "none", "mode": "pure_web_file",
            "configured": False, "reason": "AI-Bridge not detected; local repo/runtime access is unavailable",
        }
    return {
        "core": {"status": "enabled", "adapter": "adaptive-agent-runtime", "configured": True, "reason": "core governance is host-neutral"},
        "desktop_adapter": desktop,
        "web_local_adapter": web,
    }


def _read_hooks_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {}
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid hooks JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError("hooks config root must be an object")
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    return value


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove_matching_handlers(entries: list[Any], needle: str) -> list[Any]:
    kept: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            if needle not in str(entry):
                kept.append(entry)
            continue
        remaining = [handler for handler in entry["hooks"] if needle not in str(handler)]
        if remaining:
            preserved = dict(entry)
            preserved["hooks"] = remaining
            kept.append(preserved)
    return kept


def install_codex_hooks(
    hooks_file: str | Path,
    target: str | Path,
    *,
    python_executable: str | None = None,
) -> dict[str, Any]:
    path = Path(hooks_file).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    config = _read_hooks_config(path)
    hooks = config["hooks"]
    python = python_executable or sys.executable
    lifecycle_command = f"{shlex.quote(str(python))} {shlex.quote(str(target_path / 'scripts' / 'lifecycle_hook.py'))}"
    scoring_command = f"{shlex.quote(str(python))} {shlex.quote(str(target_path / 'scripts' / 'controller_scoring_hook.py'))}"

    lifecycle_specs = {
        "SessionStart": ("startup|resume|clear|compact", "Loading Adaptive Agent Runtime controller state", True),
        "PostToolUse": ("*", "Checking Adaptive Agent Runtime lifecycle", True),
        "SubagentStop": (None, "Recording Adaptive Agent Runtime candidate", False),
        "Stop": (None, "Closing Adaptive Agent Runtime control event", False),
    }
    for event_name, (matcher, status, inject_context) in lifecycle_specs.items():
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            raise ValueError(f"{event_name} hooks must be a list")
        entries[:] = _remove_matching_handlers(entries, "lifecycle_hook.py")
        handler: dict[str, Any] = {
            "type": "command", "command": lifecycle_command, "timeout": 5, "statusMessage": status,
        }
        if inject_context:
            handler["additionalContextLimit"] = 4096
        group: dict[str, Any] = {"hooks": [handler]}
        if matcher is not None:
            group["matcher"] = matcher
        entries.append(group)

    for event_name, inject_context in (("UserPromptSubmit", True), ("Stop", False)):
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            raise ValueError(f"{event_name} hooks must be a list")
        entries[:] = _remove_matching_handlers(entries, "controller_scoring_hook.py")
        handler = {
            "type": "command", "command": scoring_command, "timeout": 5,
            "statusMessage": "Enforcing Adaptive Agent Runtime controller scoring model",
        }
        if inject_context:
            handler["additionalContextLimit"] = 0
        entries.append({"hooks": [handler]})

    _write_text_atomic(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return config


def _web_zshenv_block(target: Path, bridge: Path, python_executable: str) -> str:
    script = target / "scripts" / "web_lifecycle_bridge.py"
    bridge_literal = shlex.quote(str(bridge))
    python_literal = shlex.quote(str(python_executable))
    script_literal = shlex.quote(str(script))
    return f"""{WEB_BLOCK_START}
_ad_web_bridge_executable={bridge_literal}
_ad_web_parent=$(/bin/ps -p \"$PPID\" -o command= 2>/dev/null)
if [[ \"$_ad_web_parent\" == *\"$_ad_web_bridge_executable\"* ]]; then
  _ad_web_cwd=\"$PWD\"
  _ad_web_command=\"$ZSH_EXECUTION_STRING\"
  _ad_web_lifecycle_exit() {{
    local _ad_web_exit_code=$?
    trap - EXIT
    {python_literal} {script_literal} post-shell --cwd \"$_ad_web_cwd\" --command \"$_ad_web_command\" --exit-code \"$_ad_web_exit_code\"
    local _ad_web_bridge_exit_code=$?
    if [[ \"$_ad_web_exit_code\" -ne 0 ]]; then
      exit \"$_ad_web_exit_code\"
    fi
    exit \"$_ad_web_bridge_exit_code\"
  }}
  trap _ad_web_lifecycle_exit EXIT
fi
unset _ad_web_parent _ad_web_bridge_executable
{WEB_BLOCK_END}"""

def install_ai_bridge_zshenv(
    zshenv_file: str | Path,
    target: str | Path,
    ai_bridge_executable: str | Path = DEFAULT_AI_BRIDGE_EXECUTABLE,
    *,
    python_executable: str | None = None,
) -> None:
    path = Path(zshenv_file).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    bridge = Path(ai_bridge_executable).expanduser().resolve()
    python = python_executable or sys.executable
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    start = current.find(WEB_BLOCK_START)
    if start >= 0:
        end = current.find(WEB_BLOCK_END, start)
        if end < 0:
            raise ValueError("existing web lifecycle bridge block is unterminated")
        end += len(WEB_BLOCK_END)
        current = current[:start].rstrip() + "\n" + current[end:].lstrip("\n")
    block = _web_zshenv_block(target_path, bridge, python)
    prefix = current.rstrip()
    text = (prefix + "\n\n" if prefix else "") + block + "\n"
    _write_text_atomic(path, text)


def configure_host_adapters(
    target: str | Path,
    *,
    codex_executable: str | Path | None = None,
    ai_bridge_executable: str | Path = DEFAULT_AI_BRIDGE_EXECUTABLE,
    hooks_file: str | Path = DEFAULT_CODEX_HOOKS,
    zshenv_file: str | Path = DEFAULT_ZSHENV,
    python_executable: str | None = None,
) -> dict[str, dict[str, Any]]:
    target_path = Path(target).expanduser().resolve()
    codex_path = Path(codex_executable).expanduser() if codex_executable else None
    if codex_path is None:
        discovered = shutil.which("codex")
        codex_path = Path(discovered) if discovered else None
    if codex_path is not None and codex_path.is_file() and os.access(codex_path, os.X_OK):
        install_codex_hooks(hooks_file, target_path, python_executable=python_executable)
    bridge = Path(ai_bridge_executable).expanduser()
    if bridge.is_file() and os.access(bridge, os.X_OK):
        install_ai_bridge_zshenv(zshenv_file, target_path, bridge, python_executable=python_executable)
    return detect_host_capabilities(
        codex_executable=codex_path,
        ai_bridge_executable=bridge,
        hooks_file=hooks_file,
        zshenv_file=zshenv_file,
        skill_root=target_path,
    )


def _git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _changed_files(source: Path, previous_revision: str | None, revision: str, tracked: list[str]) -> list[str]:
    if not previous_revision:
        return tracked
    exists = subprocess.run(
        ["git", "-C", str(source), "cat-file", "-e", f"{previous_revision}^{{commit}}"],
        capture_output=True,
    )
    if exists.returncode != 0:
        return tracked
    output = _git(source, "diff", "--name-only", f"{previous_revision}..{revision}")
    return sorted(line for line in output.splitlines() if line.strip())



def _revision_tree_entries(source: Path, revision: str) -> list[tuple[str, str, str, str]]:
    raw = subprocess.run(
        ["git", "-C", str(source), "ls-tree", "-rz", "--full-tree", revision],
        check=True,
        capture_output=True,
    ).stdout
    entries: list[tuple[str, str, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        mode, kind, object_id = meta.decode("ascii").split(" ", 2)
        path = raw_path.decode("utf-8")
        entries.append((mode, kind, object_id, path))
    return entries


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _materialize_revision(source: Path, revision: str, destination: Path) -> list[str]:
    entries = _revision_tree_entries(source, revision)
    tracked: list[str] = []
    for mode, kind, object_id, relative in entries:
        if kind != "blob":
            raise ValueError(f"unsupported tracked object in install revision: {relative} ({kind})")
        tracked.append(relative)
        dst = destination / relative
        if dst.exists() or dst.is_symlink():
            _remove_path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        content = subprocess.run(
            ["git", "-C", str(source), "cat-file", "blob", object_id],
            check=True,
            capture_output=True,
        ).stdout
        if mode == "120000":
            os.symlink(content.decode("utf-8"), dst)
        else:
            dst.write_bytes(content)
            dst.chmod(0o755 if mode == "100755" else 0o644)
    return sorted(tracked)


def _promote_staged_install(stage: Path, target: Path) -> None:
    backup = target.parent / f".{target.name}.backup-{next(tempfile._get_candidate_names())}"
    had_target = target.exists() or target.is_symlink()
    if had_target:
        os.replace(target, backup)
    try:
        os.replace(stage, target)
    except Exception:
        if had_target and (backup.exists() or backup.is_symlink()):
            os.replace(backup, target)
        raise
    if had_target and (backup.exists() or backup.is_symlink()):
        _remove_path(backup)


def install_skill(
    source: str | Path,
    target: str | Path,
    *,
    summary: str,
    impact: str,
    stop_condition: str,
    previous_revision: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    if impact not in IMPACTS:
        raise ValueError("impact must be none or live_assignments")
    if not summary.strip() or not stop_condition.strip():
        raise ValueError("summary and stop_condition are required")
    if source_path == target_path or source_path in target_path.parents:
        raise ValueError("target must be outside the source repository")
    if _git(source_path, "status", "--porcelain=v1", "--untracked-files=no"):
        raise ValueError("source repository must be tracked-clean before installation")

    revision = _git(source_path, "rev-parse", "HEAD")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    prior_manifest = _read_manifest(target_path / MANIFEST_NAME)
    prior_files = prior_manifest.get("files", {}) if isinstance(prior_manifest.get("files"), dict) else {}
    prior_revision = previous_revision or str(prior_manifest.get("revision", "")).strip() or None

    stage = Path(tempfile.mkdtemp(prefix=f".{target_path.name}.stage-", dir=target_path.parent))
    try:
        # The installed skill directory is machine-owned. Build it solely from the frozen
        # revision so incomplete/legacy manifests cannot preserve stale executable files.
        tracked = _materialize_revision(source_path, revision, stage)
        hashes = {relative: _sha256(stage / relative) for relative in tracked}
        installed_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "product_name": PRODUCT_NAME,
            "skill_id": SKILL_ID,
            "product_slug": PRODUCT_SLUG,
            "legacy_skill_ids": list(LEGACY_SKILL_IDS),
            "revision": revision,
            "previous_revision": prior_revision,
            "installed_at": installed_at,
            "source_root": str(source_path),
            "summary": summary.strip(),
            "impact": impact,
            "stop_condition": stop_condition.strip(),
            "changed_files": _changed_files(source_path, prior_revision, revision, tracked),
            "capabilities": detect_host_capabilities(skill_root=target_path),
            "files": hashes,
        }
        _write_json_atomic(stage / MANIFEST_NAME, manifest)
        for relative, expected in hashes.items():
            if _sha256(stage / relative) != expected:
                raise ValueError(f"staged file hash mismatch: {relative}")
        _promote_staged_install(stage, target_path)
    finally:
        if stage.exists():
            shutil.rmtree(stage)

    for relative, expected in hashes.items():
        if _sha256(target_path / relative) != expected:
            raise ValueError(f"installed file hash mismatch: {relative}")
    return manifest



def _snapshot_path(path: Path, backup_root: Path, label: str) -> dict[str, Any]:
    path = path.expanduser().resolve(strict=False)
    exists = path.exists() or path.is_symlink()
    state: dict[str, Any] = {"exists": exists, "path": str(path), "kind": "missing", "backup": None}
    if not exists:
        return state
    backup = backup_root / label
    if path.is_symlink():
        state.update({"kind": "symlink", "target": os.readlink(path)})
    elif path.is_dir():
        shutil.copytree(path, backup, symlinks=True)
        state.update({"kind": "dir", "backup": str(backup)})
    else:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup, follow_symlinks=False)
        state.update({"kind": "file", "backup": str(backup)})
    return state


def _restore_snapshot(state: dict[str, Any]) -> None:
    path = Path(str(state["path"]))
    if path.exists() or path.is_symlink():
        _remove_path(path)
    if not state.get("exists"):
        return
    kind = state.get("kind")
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        os.symlink(str(state["target"]), path)
    elif kind == "dir":
        shutil.copytree(Path(str(state["backup"])), path, symlinks=True)
    elif kind == "file":
        shutil.copy2(Path(str(state["backup"])), path, follow_symlinks=False)
    else:
        raise ValueError(f"unsupported snapshot kind: {kind}")


def _rollback_install_transaction(states: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for state in reversed(states):
        try:
            _restore_snapshot(state)
        except (OSError, ValueError) as exc:
            errors.append(f"{state.get('path')}: {exc}")
    return errors


def _install_resource_lock_paths(target: Path, hooks: Path, zshenv: Path) -> list[Path]:
    paths = [
        target.parent / f".{target.name}.install.lock",
        hooks.parent / f".{hooks.name}.adaptive-agent-runtime.lock",
        zshenv.parent / f".{zshenv.name}.adaptive-agent-runtime.lock",
    ]
    return sorted(set(path.resolve(strict=False) for path in paths), key=lambda item: str(item))


def _acquire_install_resource_locks(paths: list[Path]):
    handles = []
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise
            handles.append(handle)
        return handles
    except Exception:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        raise


def _release_install_resource_locks(handles) -> None:
    for handle in reversed(handles):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install an exact Adaptive Agent Runtime revision with manifest and host-adapter evidence.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", default=str(Path.home() / ".agents" / "skills" / "adaptive-delivery"))
    parser.add_argument("--summary", required=True)
    parser.add_argument("--impact", required=True, choices=sorted(IMPACTS))
    parser.add_argument("--stop-condition", required=True)
    parser.add_argument("--previous-revision")
    parser.add_argument("--no-configure-host-adapters", action="store_true")
    parser.add_argument("--codex")
    parser.add_argument("--ai-bridge", default=str(DEFAULT_AI_BRIDGE_EXECUTABLE))
    parser.add_argument("--hooks-file", default=str(DEFAULT_CODEX_HOOKS))
    parser.add_argument("--zshenv-file", default=str(DEFAULT_ZSHENV))
    args = parser.parse_args(argv)

    target_path = Path(args.target).expanduser().resolve(strict=False)
    hooks_path = Path(args.hooks_file).expanduser().resolve(strict=False)
    zshenv_path = Path(args.zshenv_file).expanduser().resolve(strict=False)
    lock_paths = _install_resource_lock_paths(target_path, hooks_path, zshenv_path)
    try:
        try:
            install_locks = _acquire_install_resource_locks(lock_paths)
        except BlockingIOError:
            print(f"adaptive-agent-runtime-install: blocked: another installer is active for shared install resources: {target_path}")
            return 1
        if args.no_configure_host_adapters:
            try:
                manifest = install_skill(
                    args.source, target_path, summary=args.summary, impact=args.impact,
                    stop_condition=args.stop_condition, previous_revision=args.previous_revision,
                )
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                print(f"adaptive-agent-runtime-install: blocked: {error}")
                return 1
            print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            return 0
        return _run_install_transaction(args, target_path, hooks_path, zshenv_path)
    finally:
        if 'install_locks' in locals():
            _release_install_resource_locks(install_locks)


def _run_install_transaction(args, target_path: Path, hooks_path: Path, zshenv_path: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="adaptive-agent-runtime-install-rollback-") as backup_dir:
        backup_root = Path(backup_dir)
        snapshots = [
            _snapshot_path(target_path, backup_root, "target"),
            _snapshot_path(hooks_path, backup_root, "hooks"),
            _snapshot_path(zshenv_path, backup_root, "zshenv"),
        ]
        try:
            manifest = install_skill(
                args.source, target_path, summary=args.summary, impact=args.impact,
                stop_condition=args.stop_condition, previous_revision=args.previous_revision,
            )
            manifest["capabilities"] = configure_host_adapters(
                target_path,
                codex_executable=args.codex,
                ai_bridge_executable=args.ai_bridge,
                hooks_file=hooks_path,
                zshenv_file=zshenv_path,
            )
            _write_json_atomic(target_path / MANIFEST_NAME, manifest)
        except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
            rollback_errors = _rollback_install_transaction(snapshots)
            suffix = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
            print(f"adaptive-agent-runtime-install: blocked and rolled back: {error}{suffix}")
            return 1

    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
