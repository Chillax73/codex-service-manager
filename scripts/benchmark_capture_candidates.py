#!/usr/bin/env python3
"""Compare real collector daemons in disposable state, never production state.

Both versions run the unmodified collect() loop and real process/listener probes.
Synthetic history and a labelled self-root checkpoint provide a repeatable active
loop without launching a business service. All test daemons register through csm.
The root checkpoint exercises FIFO/ack but is not a real desktop hook callback.
"""
import argparse
from contextlib import closing
import ctypes
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend'))
sys.path.insert(0, str(BASE / 'tests'))
import capture
import engine
from support import stop_test_watcher
from benchmark_snapshot_reader import timebase


def usage(pid):
    values = (ctypes.c_uint64 * 64)()
    if ctypes.CDLL('/usr/lib/libproc.dylib').proc_pid_rusage(pid, 4, ctypes.byref(values)):
        raise RuntimeError('test collector disappeared')
    return {'own': values[2] + values[3], 'children': values[12] + values[13],
            'rss': values[8], 'footprint': values[9], 'birth': values[10],
            'read': values[18], 'write': values[19]}


def measure(pid, seconds):
    scale = timebase()
    factor = scale['numer'] / scale['denom'] / 1e7
    before = usage(pid); started = time.monotonic()
    time.sleep(seconds)
    after = usage(pid); elapsed = time.monotonic() - started
    assert before['birth'] == after['birth']
    own = (after['own'] - before['own']) * factor / elapsed
    children = (after['children'] - before['children']) * factor / elapsed
    return {'seconds': elapsed, 'own_cpu_percent': own, 'children_cpu_percent': children,
            'total_cpu_percent': own + children, 'rss_mib': after['rss'] / 1048576,
            'footprint_mib': after['footprint'] / 1048576,
            'disk_write_kib': (after['write'] - before['write']) / 1024}


def seed(root, count):
    engine.ROOT = root
    project = engine.project(root)
    now = time.time()
    with closing(engine.database()) as db, db:
        db.executemany('INSERT INTO services(id,project_id,name,kind,spec) VALUES (?,?,?,?,?)',
                       [(f'history-{i}', project['id'], f'History {i}', 'task',
                         json.dumps({'management': 'observed', 'source': 'global_hook'})) for i in range(count)])
        db.executemany('INSERT INTO runs(id,service_id,started,ended,state,runtime) VALUES (?,?,?,?,?,?)',
                       [(f'run-{i}', f'history-{i}', now - 100, now - 50, 'exited', '{}') for i in range(count)])
        db.executemany('INSERT INTO service_observations VALUES (?,?,?)',
                       [(f'history-{i}', now, json.dumps({'state': 'exited', 'run': None})) for i in range(count)])


def cli(root, *args):
    value = json.loads(subprocess.check_output([str(BASE / 'scripts/csm'), '--root', str(root), *args], text=True))
    if not value.get('ok'):
        raise RuntimeError('test service command failed: ' + str(value))
    return value['result']


def heartbeat(root):
    previous = engine.ROOT
    engine.ROOT = root
    try:
        row = engine.query("SELECT value FROM metadata WHERE key='capture'", one=True)
        return json.loads(row['value']) if row else None
    finally:
        engine.ROOT = previous


def arm_self(root, pid):
    engine.ROOT = root
    event = {'id': 'b' * 32, 'cwd': str(root), 'created': time.time(), 'session_id': 'synthetic-benchmark'}
    capture.save_event(event)
    started = time.monotonic()
    fd = os.open(capture.channel_dir() / 'arm.fifo', os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        os.write(fd, (json.dumps({'id': event['id'], 'pid': pid}) + '\n').encode())
    finally:
        os.close(fd)
    ack = capture.channel_dir() / (event['id'] + '.ack')
    while not ack.exists() and time.monotonic() - started < 5:
        time.sleep(.01)
    if not ack.exists():
        raise RuntimeError('test checkpoint acknowledgment missing')
    ack.unlink()
    return (time.monotonic() - started) * 1000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-backend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--count', type=int, default=1000)
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--idle-seconds', type=int, default=15)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    output = args.output.resolve()
    output.relative_to((BASE / '.test-state').resolve())
    output.mkdir(parents=True, exist_ok=True)
    control = output / 'control'; control.mkdir(exist_ok=True)
    before = args.baseline_backend.resolve(); after = BASE / 'backend'
    report = {'count': args.count, 'mach_timebase': timebase(), 'rounds': [],
              'source_sha256': {name: hashlib.sha256((path / 'capture.py').read_bytes()).hexdigest()
                                for name, path in [('before', before), ('after', after)]}}
    # Alternating pair order reduces drift: AB, BA, AB.
    order = [v for i in range(args.rounds) for v in (('before', 'after') if i % 2 == 0 else ('after', 'before'))]
    try:
        for index, variant in enumerate(order):
            root = output / f'{index}-{variant}'
            root.mkdir()  # Refuse to reuse old fixture state.
            seed(root, args.count)
            sid = f'capture-benchmark-{index}'
            backend = before if variant == 'before' else after
            started = False
            try:
                result = cli(control, 'launch', '--id', sid, '--name', sid, '--cwd', str(root),
                             '--task', '--', sys.executable, str(backend / 'capture.py'), '--root', str(root), 'collect')
                started = True
                pid = result['run']['runtime']['child_pid']
                deadline = time.monotonic() + 15
                while not heartbeat(root) and time.monotonic() < deadline:
                    time.sleep(.1)
                status = heartbeat(root)
                assert status and status['pid'] == pid and status['error'] is None
                time.sleep(5)
                row = {'index': index, 'variant': variant, 'idle': measure(pid, args.idle_seconds)}
                row['checkpoint_ack_ms'] = arm_self(root, pid)
                time.sleep(5)
                # Short buckets allow progress output without blocking for >60s.
                remaining = args.seconds; samples = []
                while remaining:
                    duration = min(30, remaining)
                    samples.append(measure(pid, duration)); remaining -= duration
                    print(json.dumps({'index': index, 'variant': variant, 'active_sample': samples[-1]}), flush=True)
                elapsed = sum(sample['seconds'] for sample in samples)
                row['active'] = {key: sum(s[key] * s['seconds'] for s in samples) / elapsed
                                 for key in ('own_cpu_percent', 'children_cpu_percent', 'total_cpu_percent', 'rss_mib', 'footprint_mib')}
                row['active']['seconds'] = elapsed
                row['active']['samples'] = samples
                status = heartbeat(root)
                assert status['pid'] == pid and status['error'] is None and status['tracked'] >= 1
                assert time.time() - status['updated'] < 12
                engine.ROOT = root
                assert engine.query('SELECT count(*) AS n FROM services', one=True)['n'] == args.count
                row['history_preserved'] = True
                report['rounds'].append(row)
                (output / 'results.json').write_text(json.dumps(report, indent=2))
                print(json.dumps({'completed': index, 'variant': variant, 'idle': row['idle'],
                                  'active_cpu': row['active']['total_cpu_percent']}), flush=True)
            finally:
                if started:
                    cli(control, 'stop', sid)
    finally:
        stop_test_watcher(control)
    report['means'] = {variant: {mode: {key: statistics.mean(row[mode][key] for row in report['rounds'] if row['variant'] == variant)
                                       for key in ('own_cpu_percent', 'children_cpu_percent', 'total_cpu_percent', 'rss_mib', 'footprint_mib')}
                                for mode in ('idle', 'active')} for variant in ('before', 'after')}
    (output / 'results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report['means']), flush=True)


if __name__ == '__main__':
    main()
