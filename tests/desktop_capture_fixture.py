"""One foreground desktop acceptance fixture; all children auto-clean on exit.

Run as an ordinary command in a fresh Codex chat with trusted global hooks. This
script never calls capture hooks or registers its children: only actual desktop
callbacks can cause their appearance. No pre-existing process/container is touched.
"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=90)
    args = parser.parse_args()
    children = []
    report = {'cwd': str(Path.cwd()), 'started': time.time(), 'ports': [], 'pids': []}
    path = Path.cwd() / 'csm-desktop-capture-fixture.json'
    try:
        for _ in range(2):
            port = free_port()
            child = subprocess.Popen([sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            children.append(child); report['ports'].append(port); report['pids'].append(child.pid)
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)'], start_new_session=True)
        children.append(child); report['pids'].append(child.pid)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print('CSM_CAPTURE_FIXTURE ' + json.dumps(report, ensure_ascii=False), flush=True)
        until = time.time() + min(max(args.seconds, 45), 180)
        while time.time() < until:
            if any(p.poll() is not None for p in children):
                raise RuntimeError('验收夹具提前退出')
            time.sleep(.5)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)
        report['ended'] = time.time()
        report['exit_codes'] = [p.returncode for p in children]
        report['cleanup_complete'] = all(p.poll() is not None for p in children)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print('CSM_CAPTURE_FIXTURE_CLEANED ' + json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
