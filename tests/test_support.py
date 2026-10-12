"""Fresh workspace and explicit tool overrides remain reproducible."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from support import prepare_workspace, tool

class WorkspaceContract(unittest.TestCase):
    def test_missing_nested_directories_are_created_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'fresh checkout'
            prepare_workspace(base)
            prepare_workspace(base)
            self.assertTrue((base / '.test-state').is_dir())
            self.assertTrue((base / 'docs/evidence').is_dir())

    def test_path_and_explicit_executable_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'custom tools' / 'npm'
            binary.parent.mkdir()
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o755)
            with patch.dict(os.environ, {'PATH': str(binary.parent)}, clear=True):
                self.assertEqual(tool('npm', 'CSM_NPM'), str(binary))
            with patch.dict(os.environ, {'PATH': '', 'CSM_NPM': str(binary)}, clear=True):
                self.assertEqual(tool('npm', 'CSM_NPM'), str(binary))

    def test_bad_override_does_not_silently_fall_back(self):
        with patch.dict(os.environ, {'CSM_NPM': '/missing-csm-tool'}):
            with self.assertRaisesRegex(RuntimeError, 'CSM_NPM'):
                tool('npm', 'CSM_NPM')

if __name__ == '__main__':
    unittest.main(verbosity=2)
