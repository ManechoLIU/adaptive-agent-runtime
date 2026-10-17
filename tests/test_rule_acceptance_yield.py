"""Offline receipt fixtures for the pending-version safe-yield boundary."""
import copy
import json
import shlex
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

from tests import test_control_runtime_trace_binding as trace_tests
import lifecycle_hook as hook
import control_event_guard as guard


class RuleAcceptanceYieldTests(unittest.TestCase):
    def setUp(self):
        fixture = trace_tests.RuntimeTraceBindingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.env = patch.dict('os.environ', fixture.env)
        self.env.start()
        self.addCleanup(self.env.stop)
        fixture.snapshot['ready_packages'] = []
        fixture.snapshot['event_contract']['allowed_actions'] = ['recompute']
        fixture.snapshot['event_contract']['allowed_files'] = []
        fixture.snapshot['event_actions'][0]['action'] = 'recompute'
        fixture.snapshot['controller_actions'] = [{
            'id': 'open_child:VERIFY-1', 'decision': 'blocked',
            'reason_code': 'authorization', 'reason': 'version acceptance gates new work',
            'evidence': 'receipt:version-gate', 'wake_condition': 'version live acceptance succeeds'}]
        loop = fixture.snapshot['control_loop_receipt']
        loop['runnable_ids'] = []
        loop['closure_proven'] = True
        loop['open_correction_ids'] = []
        loop['controller_action_ids'] = ['open_child:VERIFY-1']
        loop['continuation_debt_ids'] = ['open_child:VERIFY-1']
        fixture.write_snapshot()
        self.snapshot = {
            'root': str(fixture.repo), 'head': fixture.head,
            'ledger_sha256': fixture.snapshot['ledger_sha256'],
            'worktree_status_sha256': 'fixture-clean',
            'ready_ids': [], 'runnable_ids': [], 'candidate_revisions': [],
            'assignment_liveness': {}, 'controller_corrections': [], 'ledger_errors': [],
            'task_states': {'VERIFY-1': 'VERIFY'}, 'control_loop_required': True,
            'rule_handshake': {'state': 'pending_live_e2e', 'blocking': True,
                               'installed_revision': 'candidate', 'loaded_revision': 'candidate'}}
        self.event = {
            'session_id': 'test-controller', 'controller_session_id': 'test-controller',
            'source_session_id': 'source', 'controller_host': 'desktop_codex',
            'controller_target_generation': 4, 'controller_ownership_generation': 6,
            'turn_id': 'turn-current', 'cwd': str(fixture.repo),
            'tool_name': 'Bash', 'tool_use_id': 'guard',
            'tool_input': {'command': shlex.join([sys.executable, str(guard.__file__), *fixture.arguments()])}}
        self.prior = {**fixture.state, 'source_session_id': 'source',
                      'pending_control_event': True, 'triggers': ['VERIFY:VERIFY-1']}

    def receipt(self, mutate=None):
        if mutate:
            mutate(self.fixture.snapshot)
            self.fixture.write_snapshot()
        with patch.dict(sys.modules, {'control_event_guard': guard}), \
                patch.object(guard, 'runtime_trace_bound_snapshot',
                          side_effect=lambda contract, **kwargs: {
                              **contract, 'machine_trace': hook.machine_trace_projection(self.prior)}):
            out, state = hook.evaluate_event({**self.event, 'hook_event_name': 'PreToolUse'},
                                              snapshot=self.snapshot, prior_state=self.prior)
        self.assertEqual(out, {})
        proposal = state['control_receipt_proposal']
        # Durable CLOSED is an isolated offline fixture, never a user runtime event.
        path = guard.controller_cycle_evidence_path(self.fixture.repo, proposal['event_id'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'record_kind': 'controller_cycle_evidence', 'evidence_id': proposal['event_id'],
            'cycle_id': proposal['event_id'], 'controller_id': 'test-controller',
            'terminal_status': 'CLOSED', 'snapshot_sha256': proposal['cycle_snapshot_sha256'],
            'main_revision': self.snapshot['head'], 'ledger_sha256': self.snapshot['ledger_sha256']}))
        _, state = hook.evaluate_event({**self.event, 'hook_event_name': 'PostToolUse',
                                       'tool_response': {'exit_code': 0, 'stdout': 'control-event: allowed'}},
                                      snapshot=self.snapshot, prior_state=state)
        self.assertTrue(state['must_yield'])
        return state

    def stop(self, state, snapshot=None, **event):
        return hook.evaluate_event({**self.event, 'hook_event_name': 'Stop', **event},
                                   snapshot=snapshot or self.snapshot, prior_state=state)

    def test_pending_version_safe_receipt_yields_without_erasing_business_debt(self):
        state = self.receipt()
        out, stopped = self.stop(state)
        self.assertIs(out.get('continue'), False)
        handoff = stopped['host_turn_handoff']
        self.assertEqual(handoff['state'], 'requested')
        self.assertEqual(handoff['execution_target_session_id'], 'source')
        self.assertEqual(handoff['target_generation'], 4)
        self.assertEqual(handoff['ownership_generation'], 6)
        self.assertTrue(stopped['pending_control_event'])
        self.assertIn('VERIFY:VERIFY-1', stopped['triggers'])
        self.assertEqual(stopped['snapshot']['task_states'], {'VERIFY-1': 'VERIFY'})
        self.assertIs(self.stop(stopped)[0].get('continue'), False)

    def test_unresolved_or_unbounded_contract_cannot_authorize_safe_yield(self):
        for case in ('assignment', 'executed', 'capacity', 'no_wake', 'open_debt', 'business_action', 'open_correction'):
            with self.subTest(case=case):
                saved = copy.deepcopy(self.fixture.snapshot)
                def mutate(contract):
                    if case == 'assignment': contract['new_assignments'] = [{'id': 'new'}]
                    elif case == 'executed': contract['controller_actions'][0]['decision'] = 'executed'
                    elif case == 'capacity': contract['controller_actions'][0]['reason_code'] = 'capacity'
                    elif case == 'no_wake': contract['controller_actions'][0].pop('wake_condition')
                    elif case == 'open_debt': contract['control_loop_receipt']['open_continuation_debt_ids'] = ['open_child:VERIFY-1']
                    elif case == 'business_action': contract['event_actions'][0]['action'] = 'ledger_sync'
                    else: contract['control_loop_receipt']['open_correction_ids'] = ['new']
                state = self.receipt(mutate)
                self.assertEqual(self.stop(state)[0].get('decision'), 'block')
                self.fixture.snapshot = saved

    def test_new_work_or_changed_facts_revoke_safe_yield(self):
        state = self.receipt()
        for key, value in (
            ('runnable_ids', ['READY-2']), ('candidate_revisions', ['new-head']),
            ('assignment_liveness', {'worker': {'state': 'running'}}),
            ('ledger_sha256', 'changed'), ('head', 'changed'),
            ('worktree_status_sha256', 'changed'),
            ('task_states', {'VERIFY-1': 'VERIFY', 'VERIFY-2': 'VERIFY'}),
            ('controller_corrections', [{'fingerprint': 'new'}]),
            ('rule_handshake', {'state': 'current', 'blocking': False}),
        ):
            with self.subTest(key=key):
                snapshot = {**self.snapshot, key: value}
                self.assertEqual(self.stop(copy.deepcopy(state), snapshot)[0].get('decision'), 'block')
        for key, value in (('pending_terminal_receipts', ['/tmp/new.json']),
                           ('inflight_tool_use_ids', ['running']),
                           ('next_action', 'do business work'), ('triggers', ['terminal_receipt_pending']),
                           ('rule_acceptance_yield', {'proposal': None}), ('must_yield', False)):
            with self.subTest(state_key=key):
                self.assertNotEqual(self.stop({**state, key: value})[0], {})
        self.assertFalse(hook._allows_rule_acceptance_yield(
            {**self.event, 'hook_event_name': 'Stop'}, {**state, 'triggers': []}, self.snapshot))
        self.assertEqual(self.stop(state, source_session_id='other')[0].get('decision'), 'block')
        self.assertEqual(self.stop(state, controller_target_generation=99)[0].get('decision'), 'block')
        self.assertEqual(self.stop(state, controller_ownership_generation=99)[0].get('decision'), 'block')
        started, resumed = hook.evaluate_event({**self.event, 'hook_event_name': 'SessionStart'},
                                               snapshot=self.snapshot, prior_state=state)
        self.assertTrue(resumed['pending_control_event'])
        self.assertIn('VERIFY:VERIFY-1', resumed['triggers'])
        self.assertNotEqual(started, {})

    def test_missing_or_invalid_identity_fence_never_arms_safe_yield(self):
        for key in ('controller_session_id', 'source_session_id', 'turn_id',
                    'controller_target_generation', 'controller_ownership_generation'):
            for value in (None, '', 0, True):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(hook._rule_acceptance_yield_facts(
                        {**self.event, key: value}, self.snapshot))

    def test_modified_receipt_file_and_later_tool_cannot_reuse_safe_yield(self):
        state = self.receipt()
        self.fixture.file.write_text(self.fixture.file.read_text() + ' ')
        self.assertEqual(self.stop(state)[0].get('decision'), 'block')
        self.fixture.write_snapshot()
        _, continued = hook.evaluate_event({**self.event, 'hook_event_name': 'PreToolUse',
                                           'tool_use_id': 'later', 'tool_input': {'command': 'true'}},
                                          snapshot=self.snapshot, prior_state=state)
        self.assertNotEqual(self.stop(continued)[0], {})


if __name__ == '__main__':
    unittest.main()
