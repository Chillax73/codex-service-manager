#!/usr/bin/env python3
"""Isolated real-task snapshot baseline; no business state or listening ports."""
import argparse
from collections import Counter
import json
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend'))
import engine


def worker(root):
    engine.ROOT = Path(root)
    calls, lock = Counter(), threading.Lock()
    original = subprocess.run
    def counted(argv, *args, **kwargs):
        with lock:
            calls[Path(argv[0]).name] += 1
        return original(argv, *args, **kwargs)
    start = time.perf_counter()
    cpu = time.process_time()
    with patch.object(subprocess, 'run', counted):
        data = engine.snapshot()
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = {'snapshot_ms': (time.perf_counter() - start) * 1000,
              'python_cpu_ms': (time.process_time() - cpu) * 1000,
              'query_cpu_ms': (children.ru_utime + children.ru_stime) * 1000,
              'external_calls': dict(calls), 'services': len(data['services'])}
    if any(item['state'] != 'running' for item in data['services']):
        raise RuntimeError('基线夹具未全部运行')
    print(json.dumps(result))


def benchmark(counts, samples):
    (BASE / '.test-state').mkdir(exist_ok=True)
    result = {'system': platform.system(), 'macos': platform.mac_ver()[0],
              'architecture': platform.machine(), 'python': platform.python_version(),
              'fixture': 'managed portless sleeping tasks; no health URL, Docker, watcher or collector',
              'samples_per_count': samples, 'results': []}
    previous = engine.ROOT
    directory = tempfile.mkdtemp(prefix='snapshot-benchmark-', dir=BASE / '.test-state')
    engine.ROOT = Path(directory)
    try:
        try:
            ids = []
            for count in sorted(set(counts)):
                while len(ids) < count:
                    sid = 'benchmark-' + str(len(ids))
                    ids.append(sid)
                    engine.dispatch({'action': 'launch', 'id': sid, 'name': sid,
                                     'cwd': directory, 'kind': 'task',
                                     'argv': [sys.executable, '-c', 'import time;time.sleep(1800)']})
                observations = []
                for _ in range(samples):
                    start = time.perf_counter()
                    output = subprocess.check_output([sys.executable, str(Path(__file__).resolve()),
                                                      '--worker', directory], text=True)
                    observation = json.loads(output)
                    observation['instrumented_process_ms'] = (time.perf_counter() - start) * 1000
                    start = time.perf_counter()
                    output = subprocess.check_output([sys.executable, str(BASE / 'backend/engine.py'),
                                                      '--root', directory, 'rpc'],
                                                     input='{"action":"snapshot"}', text=True)
                    observation['rpc_process_ms'] = (time.perf_counter() - start) * 1000
                    if not json.loads(output)['ok']:
                        raise RuntimeError('基线 RPC 失败')
                    observations.append(observation)
                result['results'].append({'services': count, 'samples': observations,
                    'median_snapshot_ms': statistics.median(o['snapshot_ms'] for o in observations),
                    'median_rpc_process_ms': statistics.median(o['rpc_process_ms'] for o in observations)})
        finally:
            failures = []
            for row in engine.query('SELECT id FROM services'):
                try:
                    engine.dispatch({'action': 'force', 'id': row['id']})
                except Exception as exc:
                    failures.append(type(exc).__name__)
            if failures:
                raise RuntimeError('测试清理失败；身份记录保存在 .test-state/' + Path(directory).name)
            import shutil
            shutil.rmtree(directory)
    finally:
        engine.ROOT = previous
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--counts', nargs='+', type=int, default=[5, 20, 50])
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--worker', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
    else:
        if not args.counts or any(n < 1 or n > 100 for n in args.counts) or not 1 <= args.samples <= 20:
            parser.error('服务数为 1–100，采样次数为 1–20')
        print(json.dumps(benchmark(args.counts, args.samples), indent=2))
