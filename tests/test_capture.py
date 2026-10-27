"""Real process acceptance for global capture; synthetic hook input is labelled.

Desktop callback coverage is separate. Fixtures deliberately use ordinary shell
commands to exercise the integration gap; all fixture PIDs belong to this test.
"""
import importlib.util
import argparse
import json
import os
from pathlib import Path
import signal
import shlex
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
import adoption
from support import prepare_workspace, stop_test_watcher, tool


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

    def test_01_direct_service_project_capabilities_and_dedup(self):
        port = free_port()
        child, _ = self.submit('exec /usr/bin/python3 -m http.server ' + str(port) + ' --bind 127.0.0.1', self.b)
        try:
            item = self.wait_items([port])[0]
            self.assertEqual(item['management'], 'attached')
            self.assertEqual(engine.project_record(item['project_id'])['cwd'], str(self.b))
            self.assertEqual(item['run']['runtime']['pid'], child.pid)  # shell exec optimisation is covered
            self.assertTrue(item['can_stop'])
            self.assertFalse(item['can_start'] or item['can_force'])
            self.assertFalse(item['automatic_end'])
            result = engine.batch({'ids': [item['id']], 'operation': 'stop'}, 'manual')
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
            self.assertIn(engine.observe(item['id'])['state'], ('exited', 'unknown'))
            self.assertFalse(engine.observe(item['id'])['can_stop'])
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

    def wait_restart(self, sid):
        until = time.time() + 10
        while time.time() < until:
            item = engine.observe(sid)
            if item['can_restart']:
                return item
            time.sleep(.2)
        self.fail('重启配置未关联：' + json.dumps(item, ensure_ascii=False))

    def test_09_ordinary_restart_becomes_supervised_and_deduplicates(self):
        port = free_port()
        child, _ = self.submit('/usr/bin/python3 -m http.server ' + str(port) + ' --bind 127.0.0.1')
        item = self.wait_items([port])[0]
        try:
            item = self.wait_restart(item['id'])
            before = item['run']['id']
            result = engine.dispatch({'action': 'restart', 'id': item['id']})
            self.assertEqual(result['id'], item['id'])
            self.assertNotEqual(result['run']['id'], before)
            self.assertEqual(result['management'], 'managed')
            self.assertIn('supervisor_pid', result['run']['runtime'])
            self.assertTrue(result['automatic_end'])
            child.communicate(timeout=5)
            time.sleep(2)
            self.assertEqual(len(self.wait_items([port])), 1)
            self.assertEqual(len(engine.dispatch({'action': 'history', 'id': item['id']})), 2)
            engine.dispatch({'action': 'stop', 'id': item['id']})
            self.assertTrue(engine.observe(item['id'])['can_start'])
            engine.dispatch({'action': 'start', 'id': item['id']})
            self.assertEqual(len(engine.dispatch({'action': 'history', 'id': item['id']})), 3)
        finally:
            engine.dispatch({'action': 'stop', 'id': item['id']})
            self.finish(child)

    def test_10_legacy_record_stop_and_explicit_recipe_without_relaunch(self):
        port = free_port()
        child, _ = self.submit('nohup /usr/bin/python3 -m http.server ' + str(port) + ' >legacy.log 2>&1 &')
        child.communicate(timeout=5)
        item = self.wait_items([port])[0]
        try:
            self.assertTrue(item['can_stop'])
            self.assertFalse(item['can_restart'])
            before = item['run']['id']
            result = engine.dispatch({'action': 'configure_observed', 'id': item['id'],
                                     'argv': ['/usr/bin/python3', '-m', 'http.server', str(port)],
                                     'env': {'CSM_TEST_SECRET': 'private-fixture-value'}})
            self.assertEqual(result['run']['id'], before)
            self.assertTrue(result['can_restart'])
            self.assertNotIn('private-fixture-value', json.dumps(result))
            engine.dispatch({'action': 'restart', 'id': item['id']})
        finally:
            engine.dispatch({'action': 'stop', 'id': item['id']})
        self.assertTrue(engine.port_free(port))

    def test_11_identity_mismatch_cannot_signal_actual_service(self):
        port = free_port()
        child, _ = self.submit('/usr/bin/python3 -m http.server ' + str(port))
        item = self.wait_items([port])[0]
        original = item['run']['runtime']
        try:
            engine.set_run(item['run']['id'], runtime={**original, 'birth': 'not-this-process'})
            with self.assertRaises(RuntimeError):
                engine.dispatch({'action': 'stop', 'id': item['id']})
            self.assertIsNone(child.poll())
            self.assertFalse(engine.port_free(port))
        finally:
            self.finish(child)

    def test_12_failed_stop_blocks_restart_and_force_targets_owned_tree(self):
        port = free_port()
        fixture = self.a / 'stubborn.py'
        fixture.write_text('import signal,http.server\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                           'http.server.HTTPServer(("127.0.0.1",' + str(port) + '),http.server.SimpleHTTPRequestHandler).serve_forever()\n')
        child, _ = self.submit('/usr/bin/python3 stubborn.py')
        item = self.wait_items([port])[0]
        try:
            engine.dispatch({'action': 'configure_observed', 'id': item['id'], 'argv': ['/usr/bin/python3', str(fixture)]})
            with self.assertRaisesRegex(RuntimeError, '超时'):
                engine.dispatch({'action': 'restart', 'id': item['id']})
            result = engine.observe(item['id'])
            self.assertEqual(result['state'], 'stop_failed')
            self.assertTrue(result['can_force'])
            self.assertEqual(result['run']['id'], item['run']['id'])
            self.assertEqual(len(engine.dispatch({'action': 'history', 'id': item['id']})), 1)
            engine.dispatch({'action': 'force', 'id': item['id']})
            child.communicate(timeout=5)
            self.assertTrue(engine.port_free(port))
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL); child.communicate(timeout=5)

    def test_13_shared_launcher_is_not_stopped(self):
        ports = [free_port(), free_port()]
        fixture = self.a / 'shared.py'
        fixture.write_text('import subprocess,time,sys\n'
                          'children=[subprocess.Popen([sys.executable,"-m","http.server",str(p)]) for p in ' + repr(ports) + ']\n'
                          'time.sleep(90)\n')
        child, _ = self.submit('/usr/bin/python3 shared.py')
        items = self.wait_items(ports)
        try:
            for item in items:
                self.assertFalse(item['can_stop'])
                self.assertIn('多个监听服务', item['capability_reason'])
                with self.assertRaisesRegex(RuntimeError, '多个监听服务'):
                    engine.dispatch({'action': 'stop', 'id': item['id']})
            self.assertIsNone(child.poll())
            self.assertTrue(all(not engine.port_free(p) for p in ports))
        finally:
            self.finish(child)

    def test_14_single_launcher_and_children_stop_without_other_project(self):
        port, other_port = free_port(), free_port()
        other = engine.dispatch({'action': 'launch', 'name': 'unrelated-project', 'cwd': str(self.b),
                                 'argv': ['/usr/bin/python3', '-m', 'http.server', str(other_port)], 'ports': [other_port]})
        fixture = self.a / 'launcher.py'
        fixture.write_text('import subprocess,time,sys\n'
                          'child=subprocess.Popen([sys.executable,"-m","http.server","' + str(port) + '"])\n'
                          'while True:\n if child.poll() is not None: child=subprocess.Popen([sys.executable,"-m","http.server","' + str(port) + '"])\n time.sleep(.1)\n')
        child, _ = self.submit('/usr/bin/python3 launcher.py')
        item = self.wait_items([port])[0]
        try:
            engine.dispatch({'action': 'stop', 'id': item['id']})
            child.communicate(timeout=5)
            time.sleep(.5)
            self.assertTrue(engine.port_free(port))
            self.assertEqual(engine.observe(other['id'])['run']['id'], other['run']['id'])
            self.assertFalse(engine.port_free(other_port))
        finally:
            self.finish(child)
            engine.dispatch({'action': 'stop', 'id': other['id']})

    def test_15_recipe_parser_rejects_shells_and_sensitive_parameters(self):
        for command in ('npm run dev && npm run api', 'TOKEN=secret npm run dev',
                        'java -jar server.jar --password=hunter2', 'node "$ENTRY"',
                        '/usr/bin/python3 multiple.py', 'npm run dev >out.log &',
                        'node main.js # sample-secret', 'mvn spring-boot:run -Dapi_key=abc'):
            self.assertIsNone(adoption.launch_recipe(command), command)
        for command in ('npm run dev -- --port 5173', 'npx vite --host 127.0.0.1',
                        'java -jar "server app.jar"', 'mvn spring-boot:run', 'node main.js'):
            self.assertIsNotNone(adoption.launch_recipe(command), command)

    def test_16_manual_batch_controls_observed_but_project_end_skips(self):
        port = free_port()
        child, _ = self.submit('nohup /usr/bin/python3 -m http.server ' + str(port) + ' >batch.log 2>&1 &')
        child.communicate(timeout=5)
        item = self.wait_items([port])[0]
        try:
            result = engine.dispatch({'action': 'end_project', 'project_id': item['project_id']})
            self.assertEqual(next(r for r in result if r['id'] == item['id'])['result'], 'skipped')
            self.assertFalse(engine.port_free(port))
            result = engine.dispatch({'action': 'batch', 'ids': [item['id']], 'operation': 'stop'})
            self.assertEqual(result[0]['result'], 'succeeded')
        finally:
            if engine.matches(item['run']['runtime']['pid'], item['run']['runtime']['birth']):
                os.kill(item['run']['runtime']['pid'], signal.SIGTERM)

    def test_17_npm_vite_ordinary_launch_restart_and_tree_stop(self):
        port = free_port()
        cwd = BASE / 'tests/fixtures/frontend'
        argv = [tool('npm', 'CSM_NPM'), 'run', 'dev', '--', '--port', str(port)]
        child, _ = self.submit(' '.join(shlex.quote(w) for w in argv), cwd)
        item = self.wait_items([port], timeout=30)[0]
        try:
            self.wait_restart(item['id'])
            result = engine.dispatch({'action': 'restart', 'id': item['id']})
            self.assertNotEqual(result['run']['id'], item['run']['id'])
            child.communicate(timeout=5)
            self.wait_items([port], timeout=30)
        finally:
            engine.dispatch({'action': 'stop', 'id': item['id']})
            self.finish(child)
        self.assertTrue(engine.port_free(port))

    def test_18_maven_spring_ordinary_launch_restart_and_tree_stop(self):
        port = free_port()
        cwd = BASE / 'tests/fixtures/spring'
        argv = [tool('mvn', 'CSM_MAVEN'), '-o', '-B', '-ntp', 'spring-boot:run',
                '-Dspring-boot.run.arguments=--server.address=127.0.0.1 --server.port=' + str(port)]
        child, _ = self.submit(' '.join(shlex.quote(w) for w in argv), cwd)
        item = self.wait_items([port], timeout=60)[0]
        try:
            self.wait_restart(item['id'])
            result = engine.dispatch({'action': 'restart', 'id': item['id']})
            self.assertNotEqual(result['run']['id'], item['run']['id'])
            child.communicate(timeout=10)
            self.wait_items([port], timeout=60)
        finally:
            engine.dispatch({'action': 'stop', 'id': item['id']})
            self.finish(child)
        self.assertTrue(engine.port_free(port))

    def test_19_exited_listener_with_live_launcher_cannot_start_duplicate(self):
        port = free_port()
        fixture = self.a / 'delayed-launcher.py'
        fixture.write_text('import subprocess,time,sys\n'
                          'child=subprocess.Popen([sys.executable,"-m","http.server","' + str(port) + '"])\n'
                          'child.wait()\ntime.sleep(90)\n')
        child, _ = self.submit('/usr/bin/python3 delayed-launcher.py')
        item = self.wait_items([port])[0]
        try:
            engine.dispatch({'action': 'configure_observed', 'id': item['id'], 'argv': ['/usr/bin/python3', str(fixture)]})
            until = time.time() + 10
            while not engine.latest(item['id'])['runtime'].get('launch_members') and time.time() < until:
                time.sleep(.2)
            self.assertTrue(engine.latest(item['id'])['runtime'].get('launch_members'))
            runtime = item['run']['runtime']
            os.kill(runtime['pid'], signal.SIGTERM)
            until = time.time() + 5
            while engine.matches(runtime['pid'], runtime['birth']) and time.time() < until:
                time.sleep(.1)
            result = engine.observe(item['id'])
            self.assertEqual(result['state'], 'unknown')
            self.assertFalse(result['can_start'] or result['can_restart'])
            with self.assertRaises(RuntimeError):
                engine.dispatch({'action': 'start', 'id': item['id']})
            self.assertIsNone(child.poll())
        finally:
            self.finish(child)

    def test_20_rpc_adoption_uses_selected_state_root(self):
        port = free_port()
        child, _ = self.submit('/usr/bin/python3 -m http.server ' + str(port) + ' --bind 127.0.0.1')
        item = self.wait_items([port])[0]
        def rpc(req):
            output = subprocess.check_output([sys.executable, str(BASE / 'backend/engine.py'),
                                              '--root', str(self.root), 'rpc'], input=json.dumps(req).encode())
            result = json.loads(output)
            self.assertTrue(result['ok'], result)
            return result['result']
        try:
            self.wait_restart(item['id'])
            snapshot = rpc({'action': 'snapshot'})
            selected = next(s for s in snapshot['services'] if s['id'] == item['id'])
            self.assertTrue(selected['can_stop'] and selected['can_restart'])
            result = rpc({'action': 'restart', 'id': item['id']})
            self.assertNotEqual(result['run']['id'], item['run']['id'])
            self.assertTrue(result['run']['log'].startswith(str(self.root)))
        finally:
            engine.dispatch({'action': 'stop', 'id': item['id']})
            self.finish(child)

    def test_21_registered_task_survives_ordinary_listener_stop(self):
        port = free_port()
        fixture = self.a / 'registered-sibling.py'
        fixture.write_text('import subprocess,time,sys\n'
                          'subprocess.run(' + repr([str(BASE / 'scripts/csm'), '--root', str(self.root),
                          'launch', '--name', 'registered-sibling', '--task', '--cwd', str(self.a), '--',
                          '/usr/bin/python3', '-c', 'import time;time.sleep(120)']) + ',check=True)\n'
                          'subprocess.Popen([sys.executable,"-m","http.server","' + str(port) + '"])\n'
                          'time.sleep(90)\n')
        child, _ = self.submit('/usr/bin/python3 registered-sibling.py')
        item = self.wait_items([port])[0]
        sibling = next(s for s in engine.snapshot()['services'] if s['name'] == 'registered-sibling')
        try:
            engine.dispatch({'action': 'stop', 'id': item['id']})
            self.assertEqual(engine.observe(sibling['id'])['state'], 'running')
            self.assertEqual(engine.observe(sibling['id'])['run']['id'], sibling['run']['id'])
            child.communicate(timeout=5)
            self.assertTrue(engine.port_free(port))
        finally:
            engine.dispatch({'action': 'stop', 'id': sibling['id']})
            self.finish(child)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', choices=['core', 'projects', 'full'], default='core')
    args = parser.parse_args()
    prepare_workspace()
    project_tests = ('test_17_', 'test_18_')
    names = unittest.defaultTestLoader.getTestCaseNames(CaptureAcceptance)
    selected = [name for name in names if args.suite == 'full' or
                (name.startswith(project_tests) == (args.suite == 'projects'))]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(CaptureAcceptance(name) for name in selected))
    evidence = {'generated_at': time.time(), 'tests_run': result.testsRun, 'passed': result.wasSuccessful(),
                'failures': [(str(t), s) for t, s in result.failures], 'errors': [(str(t), s) for t, s in result.errors],
                'state_root': str(CaptureAcceptance.root), 'hook_input': 'synthetic; actual processes and collector',
                'desktop_callback_verified_by_this_test': False}
    evidence['suite'] = args.suite
    (BASE / 'docs/evidence/capture-tests.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    sys.exit(0 if result.wasSuccessful() else 1)
