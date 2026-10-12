"""Probe the installed Codex protocol without model calls or bypassing hook trust."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
STATE = BASE / '.state'
STATE.mkdir(exist_ok=True)
events = ['SessionStart', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionEnd']
command = '/usr/bin/python3 ' + str(BASE / 'hook_recorder.py')
groups = {event: [{'hooks': [{'type': 'command', 'command': command,
                            'timeout': 3 if event == 'SessionEnd' else 5,
                            'statusMessage': '记录服务管理验证事件'}]}] for event in events}
draft = {'description': '本项目服务管理第一轮验证；仅记录事件元数据，不操作服务。', 'hooks': groups}
(BASE / 'hooks.json').write_text(json.dumps(draft, ensure_ascii=False, indent=2) + '\n')
# Per-invocation overrides are temporary; hook trust is deliberately not bypassed.
args = [shutil.which('codex'), 'app-server', '--stdio', '-c', 'log_dir=' + json.dumps(str(STATE / 'codex-logs'))]
project_only = '--project-only' in sys.argv
if not project_only:
    for event in events:
        group = '[{hooks=[{type="command",command=' + json.dumps(command) + ',timeout=' + str(3 if event == 'SessionEnd' else 5) + '}]}]'
        args += ['-c', 'hooks.' + event + '=' + group]
process = None
inbox = queue.Queue()
notifications = []
result = {'generated_at': time.time(), 'source': ('installed codex app-server, project configuration' if project_only else
          'installed codex app-server, temporary invocation hooks'),
          'draft': str(BASE / 'hooks.json'), 'model_calls': 0, 'trust_bypassed': False}


def read_lines():
    for line in process.stdout:
        try:
            inbox.put(json.loads(line))
        except ValueError:
            pass
    inbox.put({'probe_process_exited': True})


def request(method, params, rid):
    process.stdin.write(json.dumps({'id': rid, 'method': method, 'params': params}) + '\n')
    process.stdin.flush()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            item = inbox.get(timeout=max(.1, deadline - time.time()))
        except queue.Empty:
            break
        if item.get('id') == rid and ('result' in item or 'error' in item):
            return item
        if item.get('probe_process_exited'):
            raise RuntimeError('Codex app-server exited before responding; inspect probe stderr')
        notifications.append(item)
    raise TimeoutError(method)


try:
    with (STATE / 'hooks-probe.stderr').open('w') as stderr:
        process = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=stderr, text=True, bufsize=1)
        threading.Thread(target=read_lines, daemon=True).start()
        result['initialize'] = request('initialize', {'clientInfo': {'name': 'csm_validation', 'version': '0.1'},
                         'capabilities': {'experimentalApi': True}}, 1)
        process.stdin.write(json.dumps({'method': 'initialized'}) + '\n')
        process.stdin.flush()
        listing = request('hooks/list', {'cwds': [str(ROOT)]}, 2)
        result['hooks_list'] = listing
        print('hooks/list: ' + ('response received' if 'result' in listing else 'error'), flush=True)
        data = listing.get('result', {}).get('data', [])
        metadata = [h for entry in data for h in entry.get('hooks', []) if h.get('command') == command]
        result['configured_hook_count'] = len(metadata)
        result['trust_statuses'] = sorted(set(h['trustStatus'] for h in metadata))
        print('PoC hooks: ' + str(len(metadata)) + '; trust: ' + str(result['trust_statuses']), flush=True)
        config = request('config/read', {'cwd': str(ROOT), 'includeLayers': True}, 3)
        # Discard configuration contents, which can contain unrelated sensitive values.
        result['configuration_layers'] = [{'name': layer.get('name'), 'disabledReason': layer.get('disabledReason')}
             for layer in config.get('result', {}).get('layers', [])]
        result['runtime_event_capture'] = ('pending actual desktop session events' if len(metadata) == 5 and
            result['trust_statuses'] == ['trusted'] else 'pending user hook review/trust and actual desktop session')
        result['scope'] = 'Protocol/configuration discovery only; no agent turn or desktop lifecycle was executed.'
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
except Exception as exc:
    result['error'] = str(exc)
finally:
    if process and process.poll() is None:
        process.terminate()
        process.wait(timeout=5)
    result['notifications'] = [n for n in notifications if 'hook' in n.get('method', '').lower()]
    destination = BASE / ('project-hook-results.json' if project_only else 'hook-results.json')
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print('Evidence: ' + str(destination), flush=True)
raise SystemExit(1 if 'error' in result or (not project_only and result.get('configured_hook_count') != 5) else 0)
