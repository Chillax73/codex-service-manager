"""Use only preinstalled python image and uniquely labelled disposable resources."""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
import uuid

BASE = Path(__file__).resolve().parent
TAG = 'csm-poc-' + uuid.uuid4().hex[:10]
VOLUME = TAG + '-data'
COMPOSE = BASE / 'fixtures/compose.yaml'
checks = []
container_id = None
volume_created = False
env = dict(os.environ, CSM_POC_VOLUME=VOLUME)


def docker(*args, allow_error=False):
    p = subprocess.run(['docker', *args], env=env, capture_output=True, text=True, timeout=60)
    if p.returncode and not allow_error:
        raise RuntimeError(p.stderr.strip())
    return p.stdout.strip()


def compose(*args, **kwargs):
    return docker('compose', '-p', TAG, '-f', str(COMPOSE), *args, **kwargs)


def check(name, passed, evidence):
    checks.append({'name': name, 'passed': bool(passed), 'evidence': evidence})
    print(('PASS ' if passed else 'FAIL ') + name, flush=True)


def inspect(cid):
    return json.loads(docker('inspect', cid))[0]


def wait_http(cid):
    deadline = time.time() + 20
    while time.time() < deadline:
        item = inspect(cid)
        bindings = item['NetworkSettings']['Ports'].get('8000/tcp')
        if bindings:
            url = 'http://127.0.0.1:' + bindings[0]['HostPort']
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        return {'url': url, 'binding': bindings, 'state': item['State']['Status']}
            except Exception:
                pass
        time.sleep(.2)
    raise TimeoutError('container HTTP unavailable: ' + cid)


def snapshot():
    ids = docker('ps', '-aq').splitlines()
    return {cid: {key: inspect(cid)['State'][key] for key in ['Status', 'StartedAt']} for cid in ids}


before = snapshot()
try:
    docker('image', 'inspect', 'python:3.12-slim')
    container_id = docker('run', '-d', '--pull=never', '--name', TAG,
        '--label', 'csm.poc=' + TAG, '-p', '127.0.0.1::8000',
        'python:3.12-slim', 'python', '-m', 'http.server', '8000', '--bind', '0.0.0.0')
    first = wait_http(container_id)
    check('Single Docker container: run and localhost HTTP', first['state'] == 'running' and
          first['binding'][0]['HostIp'] == '127.0.0.1', first)
    docker('stop', '-t', '3', container_id)
    stopped = inspect(container_id)['State']
    check('Single Docker container: stop', stopped['Status'] == 'exited',
          {key: stopped[key] for key in ['Status', 'ExitCode', 'FinishedAt']})
    docker('start', container_id)
    restarted = wait_http(container_id)
    docker('restart', '-t', '3', container_id)
    again = wait_http(container_id)
    check('Single Docker container: start and restart', again['state'] == 'running',
          {'after_start': restarted, 'after_restart': again, 'container_id': container_id})
    docker('volume', 'create', '--label', 'csm.poc=' + TAG, VOLUME)
    volume_created = True
    compose('up', '-d', '--pull', 'never')
    ids = compose('ps', '-q').splitlines()
    ready = [wait_http(cid) for cid in ids]
    labels = [inspect(cid)['Config']['Labels']['com.docker.compose.project'] for cid in ids]
    check('Compose: two services with explicit project ownership', len(ids) == 2 and
          labels == [TAG, TAG], {'services': ready, 'project_labels': labels})
    compose('stop', '-t', '3', 'web')
    web_id = compose('ps', '-aq', 'web')
    worker_id = compose('ps', '-q', 'worker')
    check('Compose: individual stop leaves sibling running',
          inspect(web_id)['State']['Status'] == 'exited' and inspect(worker_id)['State']['Status'] == 'running',
          {'web': inspect(web_id)['State']['Status'], 'worker': inspect(worker_id)['State']['Status']})
    compose('start', 'web')
    wait_http(web_id)
    compose('stop', '-t', '3')
    check('Compose: whole group stop', all(inspect(cid)['State']['Status'] == 'exited' for cid in ids),
          {'states': [inspect(cid)['State']['Status'] for cid in ids]})
    compose('start')
    for cid in ids:
        wait_http(cid)
    marker = docker('exec', worker_id, 'cat', '/data/marker')
    compose('restart', '-t', '3')
    for cid in ids:
        wait_http(cid)
    after_marker = docker('exec', worker_id, 'cat', '/data/marker')
    check('Compose: whole group restart preserves volume data', marker == after_marker == 'round-one-marker',
          {'before_marker': marker, 'after_marker': after_marker, 'volume': VOLUME})
except Exception as exc:
    check('Docker experiment execution', False, {'error': str(exc)})
finally:
    cleanup_errors = []
    if volume_created:
        try:
            compose('down', '--timeout', '3')
        except Exception as exc:
            cleanup_errors.append(str(exc))
    if container_id:
        try:
            docker('rm', '-f', container_id)
        except Exception as exc:
            cleanup_errors.append(str(exc))
    if volume_created:
        try:
            volume = json.loads(docker('volume', 'inspect', VOLUME))[0]
            if volume['Labels'].get('csm.poc') != TAG:
                raise RuntimeError('test volume ownership mismatch')
            docker('volume', 'rm', VOLUME)
        except Exception as exc:
            cleanup_errors.append(str(exc))
    after = snapshot()
    check('Existing containers unchanged; disposable test resources removed',
          all(after.get(cid) == value for cid, value in before.items()) and
          not cleanup_errors and not any(TAG in inspect(cid)['Name'] for cid in after),
          {'existing_container_count': len(before), 'cleanup_errors': cleanup_errors,
           'existing_containers_unchanged': all(after.get(cid) == value for cid, value in before.items())})
    (BASE / 'docker-results.json').write_text(json.dumps({'generated_at': time.time(), 'tag': TAG,
        'checks': checks, 'image': 'python:3.12-slim', 'note': 'Real Docker CLI operations; not yet integrated with manager or Codex hooks.'},
        ensure_ascii=False, indent=2) + '\n')
sys_exit = 0 if all(item['passed'] for item in checks) else 1
raise SystemExit(sys_exit)
