"""Real process acceptance for global capture; synthetic hook input is labelled.

Desktop callback coverage is separate. Fixtures deliberately use ordinary shell
commands to exercise the integration gap; all fixture PIDs belong to this test.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend'))
import capture
import engine
from support import stop_test_watcher


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class CaptureAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='csm-capture-', dir=str(BASE / '.test-state'))
        cls.root = Path(cls.temp.name)
        engine.ROOT = cls.root
        cls.a = cls.root / 'project-a'; cls.a.mkdir()
        cls.b = cls.root / 'project-b'; cls.b.mkdir()
        cls.children = []
        capture.ensure_collector()

    @classmethod
    def tearDownClass(cls):
        for child in cls.children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
        engine.dispatch({'action': 'stop', 'id': capture.INTERNAL_ID})
        stop_test_watcher(cls.root)
        # Preserve evidence; no user data is present in the isolated root.
        cls.temp._finalizer.detach()

    def submit(self, command, cwd=None):
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'session_id': 'test-' + self.id(),
                   'tool_use_id': engine.uid(), 'cwd': str(cwd or self.a), 'tool_input': {'command': command}}
        result = capture.hook(payload)
        rewritten = result['hookSpecificOutput']['updatedInput']['command']
        child = subprocess.Popen(['/bin/zsh', '-lc', rewritten], cwd=cwd or self.a,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.children.append(child)
        return child, payload

    def wait_items(self, ports, timeout=15):
        until = time.time() + timeout
        while time.time() < until:
            items = engine.snapshot()['services']
            found = [s for s in items if s['state'] == 'running' and set(s['spec'].get('ports', [])) & set(ports)]
            if len(found) == len(ports):
                return found
            time.sleep(.2)
        self.fail('自动观察超时：' + json.dumps(items, ensure_ascii=False))

    def finish(self, child):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
        child.communicate(timeout=5)

    def test_01_direct_service_project_readonly_and_dedup(self):
        port = free_port()
        child, _ = self.submit('exec /usr/bin/python3 -m http.server ' + str(port) + ' --bind 127.0.0.1', self.b)
        try:
            item = self.wait_items([port])[0]
            self.assertEqual(item['management'], 'observed')
            self.assertEqual(engine.project_record(item['project_id'])['cwd'], str(self.b))
            self.assertEqual(item['run']['runtime']['pid'], child.pid)  # shell exec optimisation is covered
            for action in ('start', 'stop', 'restart', 'force'):
                with self.assertRaisesRegex(RuntimeError, '尚未接管'):
                    engine.dispatch({'action': action, 'id': item['id']})
            self.assertFalse(item['can_stop'] or item['can_start'] or item['can_restart'])
            result = engine.dispatch({'action': 'batch', 'ids': [item['id']], 'operation': 'stop'})
            self.assertEqual(result[0]['result'], 'skipped')
            before = item['run']['id']; time.sleep(2)
            self.assertEqual(engine.observe(item['id'])['run']['id'], before)
            self.assertEqual(len(self.wait_items([port])), 1)
        finally:
            self.finish(child)
        item = engine.observe(item['id'])
        self.assertEqual(item['state'], 'exited')
        self.assertIsNone(item['run']['exit_code'])

    def test_02_script_launches_two_services_and_reparents(self):
        ports = [free_port(), free_port()]
        fixture = self.a / 'multiple.py'
        fixture.write_text('import subprocess,time,sys\n'
            'children=[subprocess.Popen([sys.executable,"-m","http.server",str(p),"--bind","127.0.0.1"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) for p in ' + repr(ports) + ']\n'
            'time.sleep(3)\n')
        child, _ = self.submit('/usr/bin/python3 multiple.py')
        try:
            items = self.wait_items(ports)
            child.communicate(timeout=10)
            time.sleep(1)
            self.assertEqual({s['run']['runtime']['pid'] for s in self.wait_items(ports)},
                             {s['run']['runtime']['pid'] for s in items})
        finally:
            # This test owns every descendant; identity verification precedes cleanup.
            for item in locals().get('items', []):
                runtime = item['run']['runtime']
                if engine.matches(runtime['pid'], runtime['birth']):
                    os.kill(runtime['pid'], signal.SIGTERM)
            if child.poll() is None:
                self.finish(child)

    def test_03_shell_output_exit_status_and_private_metadata(self):
        child, payload = self.submit('printf "fixture-output\\n"; /usr/bin/python3 -c "import sys;sys.exit(7)" # sample-secret-not-to-store')
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(stdout, 'fixture-output\n')
        self.assertEqual(stderr, '')
        self.assertEqual(child.returncode, 7)
        payload.update(hook_event_name='PostToolUse', tool_response={'output': stdout})
        capture.hook(payload)
        content = '\n'.join(p.read_text() for p in capture.event_dir().glob('*.json'))
        self.assertNotIn('sample-secret-not-to-store', content)
        self.assertNotIn('fixture-output', content)
        self.assertNotIn('transcript_path', content)

    def test_04_companion_processes_are_not_duplicated(self):
        port = free_port()
        item = engine.dispatch({'action': 'launch', 'name': 'managed-fixture', 'cwd': str(self.a),
                               'argv': ['/usr/bin/python3', '-m', 'http.server', str(port)], 'ports': [port]})
        try:
            event = {'id': engine.uid(), 'session_id': 'fixture', 'tool_use_id': 'fixture', 'cwd': str(self.a),
                     'created': time.time(), 'shell': capture.process_table()[item['run']['runtime']['child_pid']]}
            capture.save_event(event)
            time.sleep(3)
            matching = [s for s in engine.snapshot()['services'] if port in s['spec'].get('ports', []) and s['state'] == 'running']
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]['id'], item['id'])
        finally:
            engine.dispatch({'action': 'stop', 'id': item['id']})

    def test_05_identity_change_and_new_run(self):
        port = free_port()
        first, _ = self.submit('/usr/bin/python3 -m http.server ' + str(port))
        item = self.wait_items([port])[0]
        self.finish(first)
        self.assertEqual(engine.observe(item['id'])['state'], 'exited')
        second, _ = self.submit('/usr/bin/python3 -m http.server ' + str(port))
        try:
            next_item = self.wait_items([port])[0]
            self.assertEqual(item['id'], next_item['id'])
            self.assertNotEqual(item['run']['id'], next_item['run']['id'])
            self.assertEqual(len(engine.dispatch({'action': 'history', 'id': item['id']})), 2)
            engine.set_run(next_item['run']['id'], runtime={**next_item['run']['runtime'], 'birth': 'wrong'})
            self.assertEqual(engine.observe(item['id'])['state'], 'exited')
            self.assertIsNone(second.poll())
        finally:
            self.finish(second)

    def test_06_portless_task_and_collector_recovery(self):
        child, payload = self.submit('/usr/bin/python3 -c "import time;time.sleep(90)"')
        try:
            until = time.time() + 45
            while time.time() < until:
                items = [s for s in engine.snapshot()['services'] if s['state'] == 'running' and
                         s['spec'].get('tool_use_id') == payload['tool_use_id'] and s['kind'] == 'task']
                if items:
                    break
                time.sleep(.5)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['kind'], 'task')
            before = items[0]['run']['id']
            engine.dispatch({'action': 'stop', 'id': capture.INTERNAL_ID})
            self.assertFalse(engine.snapshot()['capture']['alive'])
            capture.ensure_collector()
            time.sleep(2)
            self.assertTrue(engine.snapshot()['capture']['alive'])
            self.assertEqual(engine.observe(items[0]['id'])['run']['id'], before)
        finally:
            self.finish(child)

    def test_07_installer_preserves_unrelated_hooks(self):
        spec = importlib.util.spec_from_file_location('installer', BASE / 'scripts/install_global_capture.py')
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        unrelated = {'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo original'}]}]}, 'description': 'original'}
        installed = installer.prepare(unrelated)
        self.assertEqual(installer.prepare(installed, remove=True), unrelated)
        self.assertEqual(installer.prepare(installed), installed)
        instructions = '原有用户约定\n'
        added = installer.prepare_instructions(instructions)
        self.assertTrue(added.startswith(instructions))
        self.assertEqual(installer.prepare_instructions(added), added)
        self.assertEqual(installer.prepare_instructions(added, remove=True).strip(), instructions.strip())

    def test_08_nohup_background_service_survives_shell_return(self):
        port = free_port()
        child, _ = self.submit('nohup /usr/bin/python3 -m http.server ' + str(port) +
                               ' --bind 127.0.0.1 >server.log 2>&1 &')
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(stdout, '')
        item = None
        try:
            item = self.wait_items([port])[0]
            self.assertNotEqual(item['run']['runtime']['pid'], child.pid)
            self.assertEqual(item['state'], 'running')
        finally:
            if item and engine.matches(item['run']['runtime']['pid'], item['run']['runtime']['birth']):
                os.kill(item['run']['runtime']['pid'], signal.SIGTERM)


if __name__ == '__main__':
    (BASE / '.test-state').mkdir(exist_ok=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CaptureAcceptance))
    evidence = {'generated_at': time.time(), 'tests_run': result.testsRun, 'passed': result.wasSuccessful(),
                'failures': [(str(t), s) for t, s in result.failures], 'errors': [(str(t), s) for t, s in result.errors],
                'state_root': str(CaptureAcceptance.root), 'hook_input': 'synthetic; actual processes and collector',
                'desktop_callback_verified_by_this_test': False}
    (BASE / 'docs/evidence/capture-tests.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    sys.exit(0 if result.wasSuccessful() else 1)
