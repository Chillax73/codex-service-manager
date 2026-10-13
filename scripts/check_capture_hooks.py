#!/usr/bin/env python3
"""Inspect configured global capture hooks via the supported local protocol.

Read-only: no model request, hook trust change, transcript or complete config read.
"""
import argparse
import json
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'Codex Service Manager 自动观察'


def check(cwds):
    executable = shutil.which('codex')
    if not executable:
        raise RuntimeError('未找到 Codex CLI')
    inbox = queue.Queue()
    process = subprocess.Popen([executable, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT)
    def read():
        for line in process.stdout:
            try:
                inbox.put(json.loads(line))
            except ValueError:
                continue
    threading.Thread(target=read, daemon=True).start()
    def request(method, params, request_id):
        process.stdin.write(json.dumps({'method': method, 'params': params, 'id': request_id}) + '\n')
        process.stdin.flush()
        until = time.time() + 15
        while time.time() < until:
            value = inbox.get(timeout=max(.1, until - time.time()))
            if value.get('id') == request_id:
                if 'error' in value:
                    raise RuntimeError(str(value['error']))
                return value['result']
        raise TimeoutError(method)
    try:
        request('initialize', {'clientInfo': {'name': 'csm_capture_check', 'version': '0.2'},
                               'capabilities': {'experimentalApi': True}}, 1)
        process.stdin.write(json.dumps({'method': 'initialized'}) + '\n'); process.stdin.flush()
        data = request('hooks/list', {'cwds': cwds or [str(ROOT)]}, 2)
        result = []
        for entry in data.get('data', []):
            hooks = [{key: hook.get(key) for key in ('eventName', 'enabled', 'trustStatus', 'source', 'matcher', 'currentHash')}
                     for hook in entry.get('hooks', []) if hook.get('statusMessage') == LABEL]
            result.append({'cwd': entry.get('cwd'), 'capture_hooks': hooks,
                           'ready': len(hooks) == 2 and all(h['enabled'] and h['trustStatus'] == 'trusted' and h['source'] == 'user' for h in hooks)})
        return result
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate(); process.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('cwd', nargs='*')
    args = parser.parse_args()
    print(json.dumps(check(args.cwd), ensure_ascii=False, indent=2))
