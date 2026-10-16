import json
import sys
import tempfile
import unittest
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import lifecycle_hook as hook
from scripts import rule_handshake, web_agent_execution, goal_display_sync
from tests.test_rule_handshake import make_project, write_current_controller_registry


class DesktopNativeDispatchTests(unittest.TestCase):
    def invoke_spawn(self, *, registered=True, errors=None, ownership=None, pending_goal=False, rotate=False, desktop_entry=False, stale_alias=False, bad_turn=False, host_origin="codex_work_desktop", ended=False, registry_case=None):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = make_project(base)
            registry = base / 'registry.json'
            write_current_controller_registry(registry, repo, controller_id='logical',
                host='desktop_codex', source_session_id='current', target_generation=4,
                ownership_generation=6)
            data = json.loads(registry.read_text())
            if not registered:
                data = {}
            elif ownership is not None:
                data['__controller_execution_ownership__']['logical'] = ownership
            registry.write_text(json.dumps(data))
            state_root = base / 'state'
            state_root.mkdir()
            if pending_goal:
                sync = goal_display_sync.start_target_goal_rebind(None,
                    {'ledger_sha256': 'ledger', 'objective': 'F1', 'project_name': 'Test'},
                    controller_id='logical', source_session_id='current', host='desktop_codex',
                    target_generation=4, turn_id='turn', host_capabilities={'get_goal', 'create_goal', 'set_thread_title', 'list_threads'})
                (state_root / 'logical.json').write_text(json.dumps({'active_turn_id': 'turn',
                    'goal_display_sync': sync, 'pending_control_event': True}))
            event = {'hook_event_name': 'PreToolUse', 'session_id': 'current', 'turn_id': 'turn',
                'cwd': str(repo), 'tool_name': 'collaboration.spawn_agent', 'tool_use_id': 'spawn',
                'tool_input': {'task_name': 'native-child', 'model': 'gpt-5.6-sol', 'agent_type': 'default'}}
            if desktop_entry:
                transcript = base / 'sessions' / 'probe.jsonl'
                transcript.parent.mkdir()
                transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {
                    'id': 'current', 'session_id': 'current', 'source': 'vscode',
                    'thread_source': 'user', 'originator': host_origin,
                }}) + '\n' + json.dumps({'type': 'event_msg', 'payload': {
                    'type': 'task_started', 'turn_id': 'wrong' if bad_turn else 'turn',
                }}) + '\n')
                if ended:
                    with transcript.open('a') as stream:
                        stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete'}}) + '\n')
                event['transcript_path'] = str(transcript)
            if stale_alias:
                data = json.loads(registry.read_text())
                data.setdefault('__controller_sessions__', {})['logical'] = {'desktop_codex': ['current']}
                registry.write_text(json.dumps(data))
            if registry_case:
                data = json.loads(registry.read_text())
                if registry_case == 'string_alias':
                    data['__controller_sessions__'] = {'logical': {'desktop_codex': 'current'}}
                elif registry_case == 'target_only':
                    data['__controller_targets__'] = {'logical': {'desktop_codex': {'session_id': 'current'}}}
                elif registry_case == 'ownership_only':
                    data['__controller_execution_ownership__'] = {'logical': {'execution_target_session_id': 'current'}}
                elif registry_case == 'invalid_target':
                    data['logical'] = str(repo)
                    data['__controller_targets__'] = {'logical': {'desktop_codex': {'status': 'active', 'generation': 1}}}
                elif registry_case == 'invalid_ownership':
                    data['logical'] = str(repo)
                    data['__controller_execution_ownership__'] = {'logical': {'active_host': 'desktop_codex', 'generation': 1}}
                elif registry_case == 'invalid_session_host':
                    data['logical'] = str(repo)
                    data['__controller_sessions__'] = {'logical': {'invalid_host': ['another']}}
                elif registry_case == 'orphan_alias':
                    data['__controller_sessions__'] = {'orphan': {'desktop_codex': ['another']}}
                registry.write_text('broken JSON'  if registry_case == 'corrupt' else json.dumps(data))
            snapshot = {'root': str(repo), 'git_common_dir': str((repo / '.git').resolve()),
                        'control_loop_required': False, 'rule_handshake': {}}
            output = StringIO()
            original = rule_handshake.validate_controller_action_source
            def validate(*args, **kwargs):
                result = original(*args, **kwargs)
                if rotate:
                    changed = json.loads(registry.read_text())
                    changed['__controller_execution_ownership__']['logical']['generation'] += 1
                    registry.write_text(json.dumps(changed))
                return result
            with patch.dict(hook.os.environ, {'CODEX_HOME': str(base)}), patch.object(hook, 'REGISTRY_PATH', registry), patch.object(hook, 'STATE_ROOT', state_root), \
                 patch.object(hook, 'project_snapshot', return_value=snapshot), \
                 patch.object(rule_handshake, 'launch_guard_errors', return_value=errors or []), \
                 patch.object(rule_handshake, 'validate_controller_action_source', side_effect=validate), \
                 patch.object(web_agent_execution, 'require_prepared_web_dispatch', side_effect=PermissionError('Web unavailable')) as web_gate, \
                 patch.object(sys, 'stdin', StringIO(json.dumps(event))), redirect_stdout(output):
                hook.run_hook()
            return web_gate.call_count, output.getvalue()

    def test_host_attested_ordinary_desktop_spawn_does_not_require_web_ticket(self):
        calls, output = self.invoke_spawn(registered=False, desktop_entry=True)
        self.assertEqual(calls, 0)
        self.assertNotIn('deny', output)

    def test_stale_controller_alias_cannot_become_ordinary_desktop(self):
        calls, output = self.invoke_spawn(registered=False, desktop_entry=True, stale_alias=True)
        self.assertEqual(calls, 1)
        self.assertIn('deny', output)

    def test_wrong_host_turn_still_requires_web_ticket(self):
        calls, output = self.invoke_spawn(registered=False, desktop_entry=True, bad_turn=True)
        self.assertEqual(calls, 1)
        self.assertIn('deny', output)

    def test_non_desktop_origin_does_not_escape_web_gate(self):
        calls, output = self.invoke_spawn(registered=False, desktop_entry=True, host_origin="web")
        self.assertEqual(calls, 1)
        self.assertIn('deny', output)

    def test_completed_desktop_turn_cannot_authorize_new_spawn(self):
        calls, output = self.invoke_spawn(registered=False, desktop_entry=True, ended=True)
        self.assertEqual(calls, 1)
        self.assertIn('deny', output)

    def test_all_controller_lineage_and_corrupt_registry_fail_closed(self):
        for case in ('string_alias', 'target_only', 'ownership_only', 'corrupt', 'invalid_target', 'invalid_ownership', 'orphan_alias', 'invalid_session_host'):
            with self.subTest(case=case):
                calls, output = self.invoke_spawn(registered=False, desktop_entry=True, registry_case=case)
                self.assertEqual(calls, 1)
                self.assertIn('deny', output)

    def test_verified_current_desktop_controller_does_not_enter_web_ticket_gate(self):
        calls, output = self.invoke_spawn()
        self.assertEqual(calls, 0)
        self.assertNotIn('deny', output)

    def test_pending_runtime_rules_continue_to_block_native_launch(self):
        calls, output = self.invoke_spawn(errors=['pending_live_e2e'])
        self.assertEqual(calls, 0)
        self.assertEqual(json.loads(output)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('pending_live_e2e', output)

    def test_unregistered_source_still_requires_web_dispatch_ticket(self):
        calls, output = self.invoke_spawn(registered=False)
        self.assertEqual(calls, 1)
        self.assertIn('Web Agent dispatch gate rejected', output)

    def test_foreign_missing_or_invalid_ownership_cannot_bypass_web_ticket_gate(self):
        for ownership in [
            {'active_host': 'web', 'execution_target_session_id': 'foreign', 'generation': 7},
            {},
            {'active_host': 'desktop_codex', 'execution_target_session_id': 'current', 'generation': True},
        ]:
            with self.subTest(ownership=ownership):
                calls, output = self.invoke_spawn(ownership=ownership)
                self.assertEqual(calls, 1)
                self.assertIn('Web Agent dispatch gate rejected', output)

    def test_pending_goal_still_blocks_after_native_route(self):
        calls, output = self.invoke_spawn(pending_goal=True)
        self.assertEqual(calls, 0)
        self.assertEqual(json.loads(output)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('get_goal', output)

    def test_ownership_rotation_before_lifecycle_commit_denies_native_dispatch(self):
        calls, output = self.invoke_spawn(rotate=True)
        self.assertEqual(calls, 0)
        self.assertEqual(json.loads(output)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('source changed', output)

    def test_degraded_identity_cannot_bypass_web_ticket_gate(self):
        with patch.object(hook.target_guard, 'controller_identity_projection', return_value={'identity_state': 'DEGRADED', 'controller_actions_allowed': False}):
            calls, output = self.invoke_spawn()
        self.assertEqual(calls, 1)
        self.assertIn('Web Agent dispatch gate rejected', output)


if __name__ == '__main__':
    unittest.main()
