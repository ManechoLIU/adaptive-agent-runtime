from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import lifecycle_hook as hook


class DesktopCuaRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'sessions' / 'rollout-source.jsonl'
        self.path.parent.mkdir()
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.arguments = {'code': 'await cua.getState();', 'title': 'Inspect'}
        self.item = {'type': 'McpToolCall', 'id': 'cua', 'server': 'cua_repl',
                     'tool': 'js', 'arguments': self.arguments, 'status': 'failed',
                     'result': {'isError': True, 'content': [{'type': 'text', 'text': 'error'}]}}
        self.state = {'active_turn_id': 'turn', 'controller_host': 'desktop_codex',
                      'pending_control_event': True, 'triggers': ['correction:open'],
                      'pending_terminal_receipts': ['/tmp/terminal.json'],
                      'inflight_tool_use_ids': ['cua', 'running'],
                      'inflight_tool_records': {
                          'cua': {'turn_id': 'turn', 'tool_name': 'mcp__cua_repl__js',
                                  'input_sha256': hook._json_sha256(self.arguments)},
                          'running': {'turn_id': 'turn', 'tool_name': 'Bash'}}}
        self.event = {'hook_event_name': 'PreToolUse', 'controller_host': 'desktop_codex',
                      'session_id': 'logical', 'controller_session_id': 'logical',
                      'source_session_id': 'source', 'turn_id': 'turn',
                      'transcript_path': str(self.path)}
        self.snapshot = {'root': str(self.root), 'head': 'abc', 'ledger_sha256': 'ledger',
                         'worktree_status_sha256': 'clean', 'ready_ids': [], 'runnable_ids': [],
                         'candidate_revisions': [], 'ledger_errors': [], 'assignment_liveness': {},
                         'rule_handshake': {'state': 'current', 'blocking': False}}

    def rows(self):
        return [{'type': 'session_meta', 'payload': {'id': 'source'}},
                {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn'}},
                {'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': 'turn',
                 'thread_id': 'source', 'item': copy.deepcopy(self.item)}}]

    def recover(self, rows=None, state=None, event=None, closed=False):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in (rows or self.rows())))
        return hook._reconcile_desktop_rollout(copy.deepcopy(state or self.state),
            event or self.event, self.snapshot, closed_turn=closed)

    def test_failed_and_successful_terminals_release_only_matching_call(self):
        for failed in (True, False):
            with self.subTest(failed=failed):
                rows = self.rows()
                rows[2]['payload']['item'].update(status='failed' if failed else 'completed',
                    result={'isError': failed, 'content': [{'type': 'text', 'text': 'returned'}]})
                result = self.recover(rows)
                self.assertEqual(result['inflight_tool_use_ids'], ['running'])
                self.assertNotIn('cua', result['inflight_tool_records'])
                self.assertEqual(result['tool_trace'][-1]['response_status'], failed)
                self.assertTrue(result['pending_control_event'])
                self.assertEqual(result['pending_terminal_receipts'], ['/tmp/terminal.json'])
                self.assertIn('correction:open', result['triggers'])
                self.assertFalse(result.get('must_yield', False))
                self.assertEqual(self.recover(rows, state=result)['tool_trace'], result['tool_trace'])

    def test_untrusted_or_mismatched_terminal_keeps_lock(self):
        for case in ('source', 'turn', 'id', 'server', 'tool', 'arguments', 'hash', 'status',
                     'result', 'error_type', 'content', 'failed_success', 'completed_error',
                     'record_missing', 'record_turn', 'record_tool', 'thread', 'boundary',
                     'no_start', 'duplicate', 'path', 'web'):
            with self.subTest(case=case):
                rows, state, event = self.rows(), copy.deepcopy(self.state), dict(self.event)
                payload = rows[2]['payload']
                item = payload['item']
                if case == 'source': rows[0]['payload']['id'] = 'other'
                elif case == 'turn': payload['turn_id'] = 'old'
                elif case == 'id': item['id'] = 'other'
                elif case == 'server': item['server'] = 'codex_app'
                elif case == 'tool': item['tool'] = 'set_thread_title'
                elif case == 'arguments': item['arguments'] = []
                elif case == 'hash': item['arguments'] = {'code': 'different'}
                elif case == 'status': item['status'] = 'in_progress'
                elif case == 'result': item['result'] = None
                elif case == 'error_type': item['result']['isError'] = 'true'
                elif case == 'content': item['result']['content'] = []
                elif case == 'failed_success': item['result']['isError'] = False
                elif case == 'completed_error': item['status'] = 'completed'
                elif case == 'record_missing': state['inflight_tool_records'].pop('cua')
                elif case == 'record_turn': state['inflight_tool_records']['cua']['turn_id'] = 'old'
                elif case == 'record_tool': state['inflight_tool_records']['cua']['tool_name'] = 'Bash'
                elif case == 'thread': payload['thread_id'] = 'other'
                elif case == 'boundary': rows.insert(2, {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'other'}})
                elif case == 'no_start': rows.pop(1)
                elif case == 'duplicate': rows.append(copy.deepcopy(rows[2]))
                elif case == 'path': event['transcript_path'] = str(self.root / 'outside.jsonl')
                elif case == 'web': event['controller_host'] = 'web'
                result = self.recover(rows, state, event)
                self.assertEqual(result['inflight_tool_use_ids'], ['cua', 'running'])
                self.assertFalse(result.get('tool_trace'))

    def test_closed_turn_consumes_terminal_without_closing_business_debt(self):
        rows = self.rows() + [{'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn'}}]
        result = self.recover(rows, event={**self.event, 'hook_event_name': 'Stop'}, closed=True)
        self.assertEqual(result['inflight_tool_use_ids'], ['running'])
        self.assertTrue(result['pending_control_event'])
        self.assertFalse(result.get('must_yield', False))

    def test_serial_guard_can_proceed_only_after_all_real_terminals_return(self):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in self.rows()))
        command = (f'{sys.executable} {Path(hook.__file__).with_name("control_event_guard.py")} '
                   f'{self.root / "receipt.json"} --ledger {self.root / "TASK_LEDGER.md"} '
                   f'--repo {self.root} --controller-session logical')
        event = {**self.event, 'tool_name': 'Bash', 'tool_use_id': 'guard',
                 'tool_input': {'command': command}}
        for still_running in (True, False):
            state = copy.deepcopy(self.state)
            if not still_running:
                state['inflight_tool_use_ids'] = ['cua']
                state['inflight_tool_records'].pop('running')
            output, result = hook.evaluate_event(event, snapshot=self.snapshot, prior_state=state)
            if still_running:
                self.assertEqual(output['hookSpecificOutput']['permissionDecision'], 'deny')
                self.assertEqual(result['inflight_tool_use_ids'], ['running'])
            else:
                self.assertEqual(output, {})
                self.assertEqual(result['inflight_tool_use_ids'], ['guard'])
            self.assertFalse(result.get('must_yield', False))


if __name__ == '__main__':
    unittest.main()
