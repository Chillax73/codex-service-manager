"""Two separate Codex shell calls verify survival beyond the launching tool call."""
import json
import os
from pathlib import Path
import socket
import sys
import time
import urllib.request
import uuid
import manager

BASE = Path(__file__).resolve().parent
ACTIVE = BASE / '.state/cross-boundary.json'


def wait(root, sid):
    deadline = time.time() + 8
    while time.time() < deadline:
        observed = manager.status(root, sid)
        if observed['health']:
            return observed
        time.sleep(.1)
    raise TimeoutError('HTTP not available')


if sys.argv[1] == 'start':
    if ACTIVE.exists():
        raise RuntimeError('previous cross-call test needs cleanup first')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    root = BASE / '.state' / ('cross-' + uuid.uuid4().hex[:8])
    sid = 'desktop-tool-python'
    spec = {'cwd': str(BASE / 'fixtures'), 'argv': [sys.executable, str(BASE / 'fixtures/http_service.py'),
            '--port', str(port)], 'env': {}, 'url': 'http://127.0.0.1:' + str(port), 'kind': 'service', 'policy': 'manual'}
    info = {'root': str(root), 'service_id': sid, 'start_tool_call_at': time.time(),
            'source': 'Codex desktop shell tool explicitly invokes manager; not hook auto-discovery'}
    ACTIVE.parent.mkdir(exist_ok=True)
    ACTIVE.write_text(json.dumps(info))
    manager.start(root, sid, spec)
    info['first_observation'] = wait(root, sid)
    ACTIVE.write_text(json.dumps(info))
    print(json.dumps({'started': True, 'pid': info['first_observation']['run']['child_pid'],
                      'url': spec['url']}))
else:
    info = json.loads(ACTIVE.read_text())
    root = Path(info['root'])
    sid = info['service_id']
    try:
        info['next_tool_call_at'] = time.time()
        info['second_observation'] = wait(root, sid)
        manager.stop(root, sid)
        manager.start(root, sid)
        info['restart_observation'] = wait(root, sid)
    except Exception as exc:
        info['error'] = str(exc)
    finally:
        info['cleanup'] = manager.stop(root, sid)
        with manager.connect(root) as db:
            info['history'] = [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY started')]
        info['passed'] = ('error' not in info and len(info['history']) == 2 and
                          info['second_observation']['run']['id'] == info['first_observation']['run']['id'] and
                          info['cleanup']['run']['state'] == 'stopped' and
                          not manager.identity(info['cleanup']['run']['child_pid']))
        (BASE / 'cross-boundary-results.json').write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n')
        ACTIVE.unlink()
    print(json.dumps({'passed': info['passed'], 'run_count': len(info['history']), 'cleanup': info['cleanup']['observed_state']}))
    raise SystemExit(0 if info['passed'] else 1)
