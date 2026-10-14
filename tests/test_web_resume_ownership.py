"""Recovery must preserve an existing owner's fencing epoch."""
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import web_lifecycle_bridge as bridge


class WebResumeOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.registry = self.repo / 'controllers.json'

    def seed(self, host='web', strong=True):
        target = {'status': 'active', 'session_id': 'web-current', 'generation': 2,
                  'provenance': 'host_attested_same_controller_recovery',
                  'identity_proof': 'host_attested_origin', 'binding_mode': 'resume_only'}
        if not strong:
            target.update(provenance='manual_user_authorized', host_attested=False)
        self.registry.write_text(json.dumps({
            'controller-1': str(self.repo),
            '__controller_sessions__': {'controller-1': {'web': ['web-current']}},
            '__controller_targets__': {'controller-1': {'web': target}},
            '__controller_execution_ownership__': {'controller-1': {
                'active_host': host,
                'execution_target_session_id': 'web-current' if host == 'web' else 'desktop-current',
                'generation': 6}},
        }))

    def resume(self, session='web-current'):
        return bridge.recover_same_controller_web_session(
            repo=self.repo, web_session_id=session, registry_path=self.registry,
            host_identity_receipt=None)

    def test_same_web_owner_resume_does_not_rotate_or_write_registry(self):
        self.seed()
        before = self.registry.read_bytes()
        first, second = self.resume(), self.resume()
        self.assertEqual(first['result'], 'ALREADY_VERIFIED')
        self.assertEqual(first['ownership_generation'], 6)
        self.assertEqual(second['ownership_generation'], 6)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_current_bound_web_cannot_claim_desktop_without_handoff(self):
        self.seed(host='desktop_codex')
        before = self.registry.read_bytes()
        result = self.resume()
        self.assertEqual(result['result'], 'DEFERRED')
        self.assertEqual(result['reason'], 'SAFE_HOST_HANDOFF_REQUIRED')
        self.assertEqual(result['active_host'], 'desktop_codex')
        self.assertEqual(self.registry.read_bytes(), before)

    def test_new_web_cannot_claim_desktop_without_handoff(self):
        self.seed(host='desktop_codex')
        before = self.registry.read_bytes()
        result = self.resume('web-successor')
        self.assertEqual(result['reason'], 'SAFE_HOST_HANDOFF_REQUIRED')
        self.assertEqual(self.registry.read_bytes(), before)

    def test_unverified_current_web_cannot_claim_desktop_even_with_verifier(self):
        self.seed(host='desktop_codex', strong=False)
        before = self.registry.read_bytes()
        with patch.object(bridge, '_registered_peer_attestation_verifier', return_value=lambda **kw: True) as verifier:
            result = self.resume()
        self.assertEqual(result['reason'], 'SAFE_HOST_HANDOFF_REQUIRED')
        verifier.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), before)
