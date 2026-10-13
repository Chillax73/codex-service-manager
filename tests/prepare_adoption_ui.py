"""Test-owned ordinary processes for actual native UI acceptance.

Run this fixture through scripts/csm --task in its separate ownership root.
Synthetic hook inputs test integration; UI actions are performed on real windows.
"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend'))
import capture
import engine
sys.path.insert(0, str(BASE / 'tests'))
from support import stop_test_watcher

root = Path(sys.argv[1]).resolve()
root.relative_to(BASE / '.test-state')
engine.ROOT = root
project = root / '普通启动接管验收'; project.mkdir(parents=True, exist_ok=True)
capture.ensure_collector()
children = []
items = []
manifest = {'root': str(root), 'project': str(project), 'services': []}

try:
    for label in ('自动启动配置', '历史服务补配置'):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        if label == '自动启动配置':
            command = '/usr/bin/python3 -m http.server ' + str(port) + ' --bind 127.0.0.1'
        else:
            file = project / 'legacy.py'
            file.write_text('import http.server\nhttp.server.HTTPServer(("127.0.0.1",' + str(port) + '),http.server.SimpleHTTPRequestHandler).serve_forever()\n')
            command = '/usr/bin/python3 legacy.py'
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'session_id': 'native-ui-fixture',
                   'tool_use_id': engine.uid(), 'cwd': str(project), 'tool_input': {'command': command}}
        rewritten = capture.hook(payload)['hookSpecificOutput']['updatedInput']['command']
        child = subprocess.Popen(['/bin/zsh', '-lc', rewritten], cwd=project, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(child)
        until = time.time() + 20
        while time.time() < until:
            found = [s for s in engine.snapshot()['services'] if port in s['spec'].get('ports', [])]
            if found and found[0]['can_stop'] and (label != '自动启动配置' or found[0]['can_restart']):
                item = found[0]; items.append(item['id'])
                engine.write('UPDATE services SET name=? WHERE id=?', [label, item['id']])
                manifest['services'].append({'name': label, 'id': item['id'], 'port': port,
                                             'first_run': item['run']['id'], 'command': command})
                break
            time.sleep(.2)
        else:
            raise RuntimeError('UI 夹具未就绪')
    (BASE / 'docs/evidence/adoption-ui-fixtures.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(manifest, ensure_ascii=False), flush=True)
    until = time.time() + 600
    while time.time() < until and not (root / 'finish-ui').exists():
        time.sleep(.5)
finally:
    for sid in items:
        try:
            item = engine.observe(sid)
            if item['can_stop']:
                engine.dispatch({'action': 'stop', 'id': sid})
        except Exception as exc:
            print('fixture cleanup: ' + str(exc), flush=True)
    for child in children:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
    engine.dispatch({'action': 'stop', 'id': capture.INTERNAL_ID})
    stop_test_watcher(root)
