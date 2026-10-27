"""Synthetic hooks, real Bash/zsh semantics; no collector or listening service."""
import json
import hashlib
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend'))
import capture
import engine


class ShellContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous = engine.ROOT
        engine.ROOT = self.root

    def tearDown(self):
        engine.ROOT = self.previous
        self.temp.cleanup()

    def rewrite(self, command):
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
                   'session_id': 'synthetic', 'tool_use_id': engine.uid(),
                   'cwd': str(self.root), 'tool_input': {'command': command, 'timeout': 2000}}
        with patch.object(capture, 'ensure_collector'), patch.object(capture, 'save_event'):
            result = capture.hook(payload)
        updated = result['hookSpecificOutput']['updatedInput']
        self.assertEqual(updated['timeout'], 2000)
        return updated['command']

    def test_shell_boundary_matrix(self):
        cases = {
            'streams_and_exit': 'printf "out\\n"; printf "err\\n" >&2; (exit 7) # trailing comment',
            'heredoc': "cat <<'EOF'\n$HOME `literal` ; &\nEOF",
            'multiline': 'value="a b"\nif [ "$value" = "a b" ]; then\n printf "%s\\n" "$value"\nfi',
            'pipeline': 'printf "a\\nb\\n" | /usr/bin/sed -n 2p; false',
            'pipefail': 'set -o pipefail; false | cat',
            'errexit': 'set -e\nprintf "before\\n"\nfalse\nprintf "unreachable\\n"',
            'exit_trap': "trap 'printf \"exit:%s\\n\" \"$?\"' EXIT; false",
            'term_trap': "trap 'printf \"term\\n\"; exit 23' TERM; kill -TERM $$; printf unreachable",
            'explicit_exit': 'printf "before\\n"; exit 12; printf unreachable',
            'exec': 'exec /usr/bin/python3 -c "import sys;print(123);sys.exit(9)"',
        }
        for shell in ('/bin/bash', '/bin/zsh'):
            for label, command in cases.items():
                with self.subTest(shell=shell, case=label):
                    expected = subprocess.run([shell, '-c', command], capture_output=True, text=True, timeout=5)
                    actual = subprocess.run([shell, '-c', self.rewrite(command)], capture_output=True, text=True, timeout=5)
                    self.assertEqual((actual.returncode, actual.stdout, actual.stderr),
                                     (expected.returncode, expected.stdout, expected.stderr))

    def test_timeout_termination_is_not_swallowed(self):
        for shell in ('/bin/bash', '/bin/zsh'):
            with self.subTest(shell=shell):
                command = 'exec /usr/bin/python3 -c "import time;print(123,flush=True);time.sleep(60)"'
                child = subprocess.Popen([shell, '-c', self.rewrite(command)], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    ready, _, _ = select.select([child.stdout], [], [], 5)
                    self.assertTrue(ready, '超时夹具未就绪')
                    first_line = child.stdout.readline()
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.communicate(timeout=.2)
                    os.killpg(child.pid, signal.SIGTERM)
                    stdout, stderr = child.communicate(timeout=5)
                    self.assertEqual(child.returncode, -signal.SIGTERM)
                    self.assertEqual(first_line + stdout, '123\n')
                    self.assertEqual(stderr, '')
                finally:
                    if child.poll() is None:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.communicate()

    def test_disabled_project_does_not_start_collector_or_rewrite(self):
        engine.dispatch({'action': 'capture_policy', 'cwd': str(self.root), 'enabled': False})
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'fixture',
                   'cwd': str(self.root / '.'), 'tool_input': {'command': 'printf untouched'}}
        with patch.object(capture, 'ensure_collector') as ensure:
            self.assertEqual(capture.hook(payload), {})
            ensure.assert_not_called()
        self.assertFalse((self.root / 'capture-events').exists())
        engine.dispatch({'action': 'capture_policy', 'cwd': str(self.root), 'enabled': True})
        self.assertEqual(capture.disabled_projects(), [])
        with self.assertRaises(ValueError):
            engine.dispatch({'action': 'capture_policy', 'cwd': str(self.root), 'enabled': 'false'})

    def test_duplicate_and_unrelated_hooks_are_untouched(self):
        command = self.rewrite('printf once')
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'fixture',
                   'cwd': str(self.root), 'tool_input': {'command': command}}
        self.assertEqual(capture.hook(payload), {})
        payload['tool_name'] = 'Read'
        self.assertEqual(capture.hook(payload), {})

    def test_failure_diagnostic_does_not_contain_exception_text(self):
        message = capture.record_failure('hook', RuntimeError('token=private-fixture https://secret.invalid'))
        diagnostic = engine.query('SELECT value FROM metadata WHERE key=?', ['capture_diagnostic'], True)['value']
        self.assertEqual(set(json.loads(diagnostic)), {'stage', 'type', 'at'})
        self.assertNotIn('private-fixture', message + diagnostic)
        self.assertNotIn('secret.invalid', message + diagnostic)
        event_id = hashlib.sha256(b'synthetic\0missing-arm').hexdigest()[:32]
        capture.save_event({'id': event_id, 'created': 1, 'cwd': str(self.root)})
        capture.hook({'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                      'session_id': 'synthetic', 'tool_use_id': 'missing-arm',
                      'tool_response': {'output': 'private-fixture-output'}})
        diagnostic = engine.query('SELECT value FROM metadata WHERE key=?', ['capture_diagnostic'], True)['value']
        self.assertEqual(json.loads(diagnostic)['stage'], 'checkpoint')
        self.assertNotIn('private-fixture-output', diagnostic)


if __name__ == '__main__':
    unittest.main(verbosity=2)
