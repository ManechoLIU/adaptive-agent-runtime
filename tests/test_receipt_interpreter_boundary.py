"""A Python-looking executable must not forge a control receipt."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scripts import lifecycle_hook


class ReceiptInterpreterBoundaryTests(unittest.TestCase):
    def test_custom_python_name_is_not_a_trusted_interpreter(self):
        self.assertFalse(lifecycle_hook._is_trusted_receipt_python("/Users/Shared/untrusted/python3"))

    def test_bare_python_resolving_to_untrusted_path_is_rejected(self):
        with patch.object(lifecycle_hook.shutil, "which", return_value="/Users/Shared/untrusted/python3"):
            self.assertFalse(lifecycle_hook._is_trusted_receipt_python("python3"))

    def test_exact_hook_interpreter_is_accepted(self):
        self.assertTrue(lifecycle_hook._is_trusted_receipt_python(str(Path(sys.executable).resolve())))
