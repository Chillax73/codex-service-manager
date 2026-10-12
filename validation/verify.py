"""Real-process integration experiments; saves observations and cleans its own fixtures."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

import manager

BASE = Path(__file__).resolve().parent
ROOT = BASE / '.state' / ('round1-' + uuid.uuid4().hex[:8])
RESULT = BASE / 'process-results.json'
checks = []
owned = []


def cli(*args):
    p = subprocess.run([sys.executable, str(BASE / 'manager.py'), '--root', str(ROOT), *args],
                       capture_output=True, text=True, timeout=20)
    if p.returncode:
        raise RuntimeError(p.stderr.strip())
    return json.loads(p.stdout)


def check(name, passed, evidence):
    checks.append({'name': name, 'passed': bool(passed), 'evidence': evidence})
    print(('PASS ' if passed else 'FAIL ') + name, flush=True)


def wait_status(sid, predicate, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = cli('status', sid)
        if predicate(result):
            return result
        time.sleep(.1)
    raise TimeoutError('status timeout: ' + sid + ': ' + json.dumps(result))


def port():
    with socket.socket() as handle:
        handle.bind(('127.0.0.1', 0))
        return handle.getsockname()[1]


def port_open(number):
    with socket.socket() as handle:
        handle.settimeout(.3)
        return handle.connect_ex(('127.0.0.1', number)) == 0


def start(sid, argv, url=None, kind='service', policy='manual', cwd=None, env=None):
    owned.append(sid)
    args = ['start', sid, '--cwd', str(cwd or BASE / 'fixtures'), '--kind', kind, '--policy', policy]
    if url:
        args += ['--url', url]
    for key, value in (env or {}).items():
        args += ['--env', key + '=' + value]
    return cli(*args, '--argv', *argv)


try:
    ROOT.mkdir(parents=True)
    number = port()
    url = 'http://127.0.0.1:' + str(number)
    child_file = ROOT / 'child.pid'
    first = start('python', [sys.executable, str(BASE / 'fixtures/http_service.py'), '--port', str(number),
                           '--child-file', str(child_file)], url=url, env={'POC_LABEL': 'restart-preserved'})
    first = wait_status('python', lambda item: item['health'] is True)
    with urllib.request.urlopen(url) as response:
        body = response.read().decode()
    child_pid = int(child_file.read_text())
    check('Python service: real HTTP response and project ownership',
          body == 'poc-python:restart-preserved' and first['project'] == str(BASE / 'fixtures'),
          {'url': url, 'body': body, 'run': first['run'], 'project': first['project']})
    recovered = cli('status', 'python')
    check('Fresh manager process: persisted configuration and live status',
          recovered['run']['id'] == first['run']['id'] and recovered['health'] is True,
          {'run_id': recovered['run']['id'], 'health': recovered['health']})
    duplicate = subprocess.run([sys.executable, str(BASE / 'manager.py'), '--root', str(ROOT),
                               'start', 'python', '--argv', sys.executable, '-c', 'pass'], capture_output=True, text=True)
    check('Duplicate start refused', duplicate.returncode != 0,
          {'exit_code': duplicate.returncode, 'error': duplicate.stderr.splitlines()[-1]})
    original_birth = first['run']['supervisor_birth']
    manager.update(ROOT, first['run']['id'], supervisor_birth='intentionally-invalid-poc-identity')
    refused = subprocess.run([sys.executable, str(BASE / 'manager.py'), '--root', str(ROOT), 'stop', 'python'],
                             capture_output=True, text=True)
    manager.update(ROOT, first['run']['id'], supervisor_birth=original_birth)
    check('Mismatched process identity: refuse stop and preserve the live service',
          refused.returncode != 0 and cli('status', 'python')['health'] is True,
          {'stop_exit_code': refused.returncode, 'original_service_healthy': cli('status', 'python')['health']})
    stopped = cli('stop', 'python')
    deadline = time.time() + 3
    while manager.identity(child_pid) and time.time() < deadline:
        time.sleep(.1)
    check('Stop: root process, child process and listening port cleared',
          stopped['run']['state'] == 'stopped' and not manager.identity(first['run']['child_pid']) and
          not manager.identity(child_pid) and not port_open(number),
          {'state': stopped['run']['state'], 'child_pid': child_pid,
           'child_identity': manager.identity(child_pid), 'port_open': port_open(number)})
    restarted = cli('restart', 'python')
    restarted = wait_status('python', lambda item: item['health'] is True)
    with urllib.request.urlopen(url) as response:
        restart_body = response.read().decode()
    history = cli('history', 'python')
    check('Restart: cwd, argv, explicit environment restored; two distinct runs',
          len(history) == 2 and history[0]['id'] != history[1]['id'] and
          restarted['spec'] == first['spec'] and restart_body == body,
          {'history': history, 'body': restart_body, 'config_matches': restarted['spec'] == first['spec']})
    bad = start('occupied-port', [sys.executable, str(BASE / 'fixtures/http_service.py'), '--port', str(number)])
    bad = wait_status('occupied-port', lambda item: item['run']['ended'] is not None)
    log = Path(bad['run']['log']).read_text()
    check('Occupied port: failed run with diagnostic log; original service stays healthy',
          bad['run']['state'] == 'failed' and 'Address already in use' in log and cli('status', 'python')['health'],
          {'run': bad['run'], 'log_tail': log[-1000:]})
    cli('stop', 'python')
    for sid, argv, expected in [
        ('node', ['node', str(BASE / 'fixtures/http_service.js')], 'poc-node'),
        ('java', ['java', str(BASE / 'fixtures/PocHttp.java')], 'poc-java')]:
        number = port()
        url = 'http://127.0.0.1:' + str(number)
        start(sid, argv + [str(number)], url=url)
        live = wait_status(sid, lambda item: item['health'] is True, timeout=15)
        with urllib.request.urlopen(url) as response:
            response_body = response.read().decode()
        cli('restart', sid)
        again = wait_status(sid, lambda item: item['health'] is True, timeout=15)
        stopped = cli('stop', sid)
        check(sid.title() + ': start, HTTP health, restart, stop and history',
              response_body == expected and not port_open(number) and len(cli('history', sid)) == 2,
              {'first_pid': live['run']['child_pid'], 'second_pid': again['run']['child_pid'],
               'state': stopped['run']['state'], 'port_open': port_open(number)})
    for sid, code, expected in [('task-ok', 0, 'completed'), ('task-failed', 7, 'failed')]:
        start(sid, [sys.executable, '-c', 'import sys,time; time.sleep(.3); sys.exit(' + str(code) + ')'], kind='task')
        finished = wait_status(sid, lambda item: item['run']['ended'] is not None)
        check('Background task: ' + expected, finished['run']['state'] == expected and
              finished['run']['exit_code'] == code, finished['run'])
    start('task-cancel', [sys.executable, '-c', 'import time; time.sleep(600)'], kind='task')
    cancelled = cli('stop', 'task-cancel')
    check('Background task: explicit cancellation', cancelled['run']['state'] == 'stopped', cancelled['run'])
    unreachable = 'http://127.0.0.1:' + str(port())
    unready = start('running-not-healthy', [sys.executable, '-c', 'import time; time.sleep(600)'], url=unreachable)
    unready = cli('status', 'running-not-healthy')
    check('Process running is distinct from HTTP readiness',
          unready['child_alive'] is True and unready['health'] is False and unready['observed_state'] == 'running',
          {'child_alive': unready['child_alive'], 'health': unready['health'], 'state': unready['observed_state']})
    cli('stop', 'running-not-healthy')
    a = ROOT / 'project-a'
    b = ROOT / 'project-b'
    a.mkdir()
    b.mkdir()
    for sid, policy, cwd in [('a-both', 'both', a), ('a-manual', 'manual', a),
                             ('a-keep', 'keep', a), ('b-both', 'both', b)]:
        start(sid, [sys.executable, '-c', 'import time; time.sleep(600)'], policy=policy, cwd=cwd)
    cli('end-project', str(a), '--trigger', 'SessionEnd')
    check('Policy simulation: SessionEnd alone does not stop services',
          all(cli('status', sid)['child_alive'] for sid in ['a-both', 'a-manual', 'a-keep', 'b-both']),
          {'input': 'synthetic SessionEnd; not actual desktop event'})
    cli('end-project', str(a), '--trigger', 'codex_exit')
    states = {sid: cli('status', sid)['observed_state'] for sid in ['a-both', 'a-manual', 'a-keep', 'b-both']}
    check('Policy simulation: Codex exit respects keep/manual and other-project isolation',
          states == {'a-both': 'stopped', 'a-manual': 'running', 'a-keep': 'running', 'b-both': 'running'}, states)
    cli('end-project', str(a), '--trigger', 'manual')
    states = {sid: cli('status', sid)['observed_state'] for sid in ['a-manual', 'a-keep', 'b-both']}
    check('Policy simulation: manual finish respects service exception and project isolation',
          states == {'a-manual': 'stopped', 'a-keep': 'running', 'b-both': 'running'}, states)
except Exception as exc:
    check('experiment execution', False, {'error': str(exc)})
finally:
    cleanup = {}
    for sid in owned:
        try:
            stopped = cli('stop', sid)
            cleanup[sid] = {'ended': stopped['run']['ended'] is not None,
                            'root_process_alive': bool(manager.identity(stopped['run']['child_pid']))}
        except Exception as exc:
            cleanup[sid] = {'error': str(exc)}
    result = {'generated_at': time.time(), 'state_root': str(ROOT), 'checks': checks, 'cleanup': cleanup,
              'note': 'Real local processes. Project-end inputs are synthetic; hooks and desktop exit are separate experiments.'}
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print('Evidence: ' + str(RESULT), flush=True)
sys.exit(0 if checks and all(item['passed'] for item in checks) and
         all(item.get('ended') and not item.get('root_process_alive') for item in cleanup.values()) else 1)
