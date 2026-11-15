#!/usr/bin/env python3
"""Native window/menu + real collector/worker, isolated fixtures and AB/BA/AB.

Each round covers window-idle, menu-idle, window-one and menu-three. Default
measurement is 120 seconds per scenario. Synthetic FIFO events are labelled;
real HTTP processes are registered in a separate ownership registry via csm.
Production hooks, capture policy, daemons and business services are untouched.
"""
import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend'))
import capture
import engine
from benchmark_capture_candidates import cli, heartbeat, seed, usage
from benchmark_docker_load import DockerLoad, HISTORY_COUNT
from benchmark_snapshot_reader import DRIVER, build, records, timebase

# Keep the requested native mode stable. The original one-shot driver could
# reopen via normal AppKit activation after consuming its command file.
# Only benchmark bundles contain this driver; production UI is untouched.
MODE_DRIVER = DRIVER.replace('                try? FileManager.default.removeItem(at: control)\n', '').replace(
    'if mode == "menu" { self?.closeWindow() }',
    'if mode == "menu", self?.window.isVisible == true { self?.closeWindow() }').replace(
    'if mode == "window" { self?.showWindow() }',
    'if mode == "window", self?.window.isVisible == false { self?.showWindow() }')


def protect_benchmark_mode(source):
    needle = '    @objc func showWindow() {\n'
    assert source.count(needle) == 1
    return source.replace(needle, needle + '''
        let control = URL(fileURLWithPath: statePath).appendingPathComponent("bench-mode")
        if (try? String(contentsOf: control, encoding: .utf8)) == "menu" { return }
''').replace('window.title = "Codex Service Manager"',
            'window.title = "P4-03 CPU Test / " + (Bundle.main.bundleIdentifier ?? "fixture")')

TRACE = '''
import collections,json,os,sqlite3,sys,threading,time
path=os.environ.get('CSM_PROBE_TRACE')
if path:
    counts=collections.Counter()
    lock=threading.Lock()
    def audit(event,args):
        with lock:
            if event=='sqlite3.connect': counts['sqlite_connect']+=1
            elif event=='subprocess.Popen':
                exe=os.path.basename(os.fsdecode(args[0]))
                counts[exe]+=1
                if exe=='docker' and len(args[1])>1:
                    command=args[1][1]
                    if command=='compose':
                        command='compose ps' if 'ps' in args[1] else 'compose other'
                    if command in ('inspect','compose ps','compose other','ps'):
                        counts['docker '+command]+=1
    sys.addaudithook(audit)
    original_connect=sqlite3.connect
    class TrackedConnection(sqlite3.Connection):
        def execute(self, sql, /, *args):
            result=super().execute(sql, *args)
            if sql.startswith('UPDATE runs '):
                with lock: counts['runs_update']+=max(0,result.rowcount)
            elif sql.startswith('INSERT INTO service_observations('):
                with lock: counts['observation_write']+=max(0,result.rowcount)
            return result
    def connect(*args,**kwargs):
        if len(args)<6:
            kwargs.setdefault('factory',TrackedConnection)
        return original_connect(*args,**kwargs)
    sqlite3.connect=connect
    def report():
        while True:
            with lock: snapshot=dict(counts)
            with open(path,'a') as f:
                f.write(json.dumps({'at':time.time(),'pid':os.getpid(),'counts':snapshot})+'\\n')
            time.sleep(2)
    threading.Thread(target=report,daemon=True).start()
'''

QUERY_CHILD = '''
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import engine,adoption,observation_schedule
engine.ROOT=Path(sys.argv[2]);observation_schedule.ensure()
keeper=engine.database()
original=engine.database;opened=0
def database():
    global opened
    opened+=1
    return original()
engine.database=database
adoption.exclusive_scope({'id':'not-a-fixture'},set(),{})
opened=0;samples=[]
for _ in range(3):
    start=time.process_time();adoption.exclusive_scope({'id':'not-a-fixture'},set(),{})
    samples.append((time.process_time()-start)*1000)
print(json.dumps({'cpu_ms':samples,'connections_per_call':opened/3}))
keeper.close()
'''


def query_components(args, out):
    report = []
    for count in (260, 1000, 5000):
        root = out / str(count); root.mkdir(); seed(root, count)
        row = {'history': count}
        for variant, backend in [('before', args.baseline_backend.resolve()), ('after', BASE / 'backend')]:
            row[variant] = json.loads(subprocess.check_output(
                [sys.executable, '-c', QUERY_CHILD, str(backend), str(root)], text=True))
        report.append(row); print(json.dumps(row), flush=True)
    (out / 'results.json').write_text(json.dumps(report, indent=2))


def wait_for(predicate, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise RuntimeError('fixture readiness timed out')


def registered_run(control, sid):
    with closing(sqlite3.connect((control / 'manager.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM runs WHERE service_id=? ORDER BY started DESC LIMIT 1', [sid]).fetchone()
        return dict(row) if row is not None else None


def verify_test_cleanup(control):
    """Verify process identities, including a failed supervisor's registry row.

    Never rewrite an unknown run into a successful stop. Preserve its error and
    record independent absence evidence before resuming a different test run.
    """
    if not (control / 'manager.sqlite').exists():
        return []
    with closing(sqlite3.connect(control / 'manager.sqlite')) as db:
        rows = db.execute('SELECT service_id,runtime,ended,error FROM runs').fetchall()
    anomalies = []
    for sid, raw, ended, error in rows:
        runtime = json.loads(raw)
        for role in ('child', 'supervisor'):
            assert not engine.matches(runtime.get(role + '_pid'), runtime.get(role + '_birth')), sid
        assert not engine.group_members(runtime['pgid']), sid
        if ended is None:
            anomalies.append({'service': sid, 'registry_error': error, 'verified_absent_at': time.time()})
    return anomalies


def temperature():
    helper = Path('/private/tmp/codex-read-core-temperature')
    if not helper.exists():
        return None
    values = {}
    for line in subprocess.check_output([str(helper)], text=True).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] in ('Tp01', 'Tp05', 'Tp09', 'Tp0D'):
            values[parts[0]] = float(parts[1])
    return {'at': time.time(), 'mean': statistics.mean(values.values()), 'max': max(values.values())}


def request_checkpoint(root, pid, eid):
    engine.ROOT = root
    capture.save_event({'id': eid, 'cwd': str(root), 'created': time.time(), 'session_id': 'synthetic-p4-benchmark'})
    started = time.monotonic()
    fd = os.open(capture.channel_dir() / 'arm.fifo', os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        os.write(fd, (json.dumps({'id': eid, 'pid': pid}) + '\n').encode())
    finally:
        os.close(fd)
    ack = capture.channel_dir() / (eid + '.ack')
    wait_for(ack.exists, 5); ack.unlink()
    return (time.monotonic() - started) * 1000


def port_ready(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.2):
            return True
    except OSError:
        return False


def add_fixture(control, root, index, owned):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    sid = 'http-' + root.name + '-' + str(index)
    owned.append(sid)  # Keep ownership even if launch confirmation fails.
    result = cli(control, 'launch', '--id', sid, '--name', sid, '--cwd', str(root),
                 '--port', str(port), '--', sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1')
    pid = result['run']['runtime']['child_pid']
    wait_for(lambda: port_ready(port))
    start = time.monotonic()
    ack = request_checkpoint(root, pid, f'{index + 1:032x}')
    def associated():
        engine.ROOT = root
        for row in engine.query("SELECT id,spec FROM services WHERE spec LIKE '%global_hook%'"):
            if port in json.loads(row['spec']).get('ports', []):
                return row['id']
    observed = wait_for(associated)
    return {'id': sid, 'observed': observed, 'pid': pid, 'port': port,
            'checkpoint_ack_ms': ack, 'association_ms': (time.monotonic() - start) * 1000}


def audit_counts(path, pids):
    result = {role: {} for role in pids}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            for role, pid in pids.items():
                if row['pid'] == pid:
                    result[role] = row['counts']
    return result


def measure(pids, seconds, trace, root, mode, expected_rows=None):
    scale = timebase(); factor = scale['numer'] / scale['denom'] / 1e7
    samples = []; remaining = seconds
    before_counts = audit_counts(trace, pids); wall = time.time()
    while remaining:
        duration = min(30, remaining)
        before = {name: usage(pid) for name, pid in pids.items()}; start = time.monotonic()
        time.sleep(duration)
        elapsed = time.monotonic() - start
        after = {name: usage(pid) for name, pid in pids.items()}
        row = {}
        for name in pids:
            a, b = before[name], after[name]
            assert a['birth'] == b['birth']
            row[name] = {'own_cpu': (b['own'] - a['own']) * factor / elapsed,
                         'child_cpu': (b['children'] - a['children']) * factor / elapsed,
                         'rss_mib': b['rss'] / 1048576, 'footprint_mib': b['footprint'] / 1048576,
                         'write_kib': (b['write'] - a['write']) / 1024}
        sample = {'seconds': elapsed, 'processes': row, 'temperature': temperature()}
        samples.append(sample); remaining -= duration
        print(json.dumps({'phase': mode, 'remaining': remaining, 'cpu': {k: round(v['own_cpu'] + v['child_cpu'], 2) for k, v in row.items()}}), flush=True)
    after_counts = audit_counts(trace, pids)
    counts = {role: {key: value - before_counts[role].get(key, 0) for key, value in data.items()}
              for role, data in after_counts.items()}
    refreshes = [r for r in records(root) if r['at'] >= wall]
    invalid = [r for r in refreshes if (r['unavailable'] or r['visible'] != mode.startswith('window')
                                        or (expected_rows is not None and r['responseRows'] != expected_rows))]
    if not refreshes or invalid:
        (root / (mode + '-rejected.json')).write_text(json.dumps(
            {'start': wall, 'end': time.time(), 'samples': samples, 'invalid_refreshes': invalid}, indent=2))
        raise AssertionError('invalid native mode or unavailable snapshot: ' + mode)
    duration = sum(s['seconds'] for s in samples)
    means = {name: {key: sum(s['processes'][name][key] * s['seconds'] for s in samples) / duration
                    for key in ('own_cpu', 'child_cpu', 'rss_mib', 'footprint_mib')} for name in pids}
    return {'seconds': duration, 'samples': samples, 'means': means, 'counts': counts,
             'refreshes': len(refreshes), 'refresh_ms': statistics.median(r['ms'] for r in refreshes),
            'response_bytes': statistics.median(r['wireBytes'] for r in refreshes)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-backend', type=Path, required=True)
    parser.add_argument('--candidate-backend', type=Path, default=BASE / 'backend',
                        help='use the baseline here too for a baseline/baseline smoke run')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=120)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--count', type=int, help='total history including Docker records (default 1035 in Docker mode)')
    parser.add_argument('--workload', choices=('http', 'docker'), default='http')
    parser.add_argument('--stagger', action='store_true', help='stagger Docker history timestamps')
    parser.add_argument('--components-only', action='store_true')
    parser.add_argument('--resume', action='store_true', help='resume completed rounds after verifying test cleanup and source hashes')
    args = parser.parse_args()
    if args.count is None:
        args.count = 1035 if args.workload == 'docker' else 1000
    if args.workload == 'docker' and (args.count < HISTORY_COUNT or args.resume):
        parser.error('Docker mode requires at least 53 records and fresh output')
    if args.seconds < 10 or args.rounds < 1:
        parser.error('require at least ten seconds per scenario and one pair')
    out = args.output.resolve(); out.relative_to((BASE / '.test-state').resolve())
    out.mkdir(exist_ok=args.resume)
    if args.components_only:
        query_components(args, out)
        return
    control = out / 'control'; control.mkdir(exist_ok=args.resume)
    lock = (control / 'watcher.lock').open('a'); fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    trace_dir = out / 'trace'; trace_dir.mkdir(exist_ok=args.resume); (trace_dir / 'sitecustomize.py').write_text(TRACE)
    apps = {}
    for variant, backend in [('before', args.baseline_backend.resolve()), ('after', args.candidate_backend.resolve())]:
        executable = (out / variant / 'CSM P0 Benchmark.app/Contents/MacOS/ServiceManager' if args.resume else
                      build(out / variant, BASE / 'app/Sources/Infrastructure.swift', driver=MODE_DRIVER,
                            manager_transform=protect_benchmark_mode))
        bundled = executable.parent.parent / 'Resources/backend'
        for path in backend.glob('*.py'):
            if args.resume:
                assert path.read_bytes() == (bundled / path.name).read_bytes(), path
            else:
                shutil.copy2(path, bundled / path.name)
        if not args.resume:
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(executable.parents[2])], check=True)
        apps[variant] = (executable, bundled)
    report = {'count': args.count, 'seconds_per_scenario': args.seconds, 'rounds': [],
              'workload': args.workload, 'stagger': args.stagger,
              'native_sha256': {v: hashlib.sha256(executable.read_bytes()).hexdigest()
                                for v, (executable, _) in apps.items()},
              'source_sha256': {v: {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in b.glob('*.py')}
                                for v, (_, b) in apps.items()}}
    if args.resume:
        saved = json.loads((out / 'results.json').read_text())
        for key in ('count', 'seconds_per_scenario', 'source_sha256'):
            assert saved[key] == report[key], key
        report = saved
        report.setdefault('resumes', []).append({'at': time.time(), 'cleanup_anomalies': verify_test_cleanup(control)})
    (out / 'results.json').write_text(json.dumps(report, indent=2))
    order = [v for n in range(args.rounds) for v in (('before', 'after') if n % 2 == 0 else ('after', 'before'))]
    load = DockerLoad(out) if args.workload == 'docker' else None
    try:
        if load:
            report['docker_fixture'] = load.prepare()
        run_rounds(args, out, control, apps, report, order, load, trace_dir)
    finally:
        lock.close()
        if load:
            load.close()


def run_rounds(args, out, control, apps, report, order, load, trace_dir):
    for number, variant in enumerate(order):
        if number < len(report['rounds']):
            assert report['rounds'][number]['variant'] == variant
            continue
        root = out / f'round-{number}-{variant}'; root.mkdir()
        history_count = args.count - HISTORY_COUNT if load else args.count
        seed(root, history_count)
        if load:
            load.settle_terminal_history(root)
            seeded_at = time.time()
            load.seed(root, seeded_at, stagger=args.stagger)
        root_lock = (root / 'watcher.lock').open('a'); fcntl.flock(root_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (root / 'refresh.jsonl').touch(); trace = root / 'audit.jsonl'
        executable, backend = apps[variant]; owned = []; fixtures = []; gui_id = 'gui-' + str(number)
        row = {'variant': variant, 'number': number, 'scenarios': {}, 'fixtures': fixtures}
        env = ['--env', 'PYTHONPATH=' + str(trace_dir), '--env', 'CSM_PROBE_TRACE=' + str(trace)]
        try:
            collector_id = 'collector-' + str(number)
            owned.append(collector_id)
            result = cli(control, 'launch', '--id', collector_id, '--name', collector_id, '--cwd', str(root), '--task',
                         *env, '--', sys.executable, str(backend / 'capture.py'), '--root', str(root), 'collect')
            collector_pid = result['run']['runtime']['child_pid']
            wait_for(lambda: heartbeat(root))
            gui_id = 'gui-' + str(number)
            owned.append(gui_id)
            result = cli(control, 'launch', '--id', gui_id, '--name', gui_id, '--cwd', str(root), '--task',
                         *env, '--', str(executable), '--root', str(root))
            gui_pid = result['run']['runtime']['child_pid']
            wait_for(lambda: len(records(root)) >= 3)
            rows = [line.split(None, 2) for line in subprocess.check_output(['/bin/ps', '-axo', 'pid=,ppid=,command='], text=True).splitlines()]
            workers = [int(p) for p, parent, command in rows if int(parent) == gui_pid and command.endswith(' snapshot-worker')]
            assert len(workers) == 1
            pids = {'gui': gui_pid, 'collector': collector_pid, 'worker': workers[0]}
            (out / 'active.json').write_text(json.dumps({'root': str(root), 'variant': variant, 'pids': pids}))
            if load:
                wait_for(lambda: load.historical_ready(root, seeded_at), 50)
            scenarios = [('window-idle', 0), ('menu-idle', 0)] + (
                [('window-active', 1), ('menu-active', 1)] if load else [('window-one', 1), ('menu-three', 3)])
            for mode, active in scenarios:
                if load and active and not fixtures:
                    load.activate()
                    load.seed(root, time.time(), active=True)
                    fixtures.append({'kind': 'docker'})
                    wait_for(lambda: load.verify_active(root, True), 40)
                while not load and len(fixtures) < active:
                    fixtures.append(add_fixture(control, root, len(fixtures), owned))
                (root / 'bench-mode').write_text(mode.split('-')[0]); requested = time.time()
                wait_for(lambda: any(r['at'] > requested and r['visible'] == mode.startswith('window') for r in records(root)))
                time.sleep(5)
                print(json.dumps({'round': number, 'variant': variant, 'begin': mode}), flush=True)
                expected_count = args.count + (len(load.active_specs) if load and active else 0)
                expected_rows = (expected_count if mode.startswith('window') else min(16, expected_count)) if load else None
                sample = measure(pids, args.seconds, trace, root, mode, expected_rows)
                row['scenarios'][mode] = sample
                if load and args.seconds >= 60:
                    if not any(sample['counts'][role].get('docker inspect',0) > 0 for role in ('worker','collector')):
                        raise AssertionError('no Docker inspect commands recorded in ' + mode)
                engine.ROOT = root
                if load:
                    assert engine.query('SELECT COUNT(*) AS n FROM services', one=True)['n'] == expected_count
                    if active:
                        assert load.verify_active(root, True)
                else:
                    assert engine.query("SELECT count(*) AS n FROM services WHERE id LIKE 'history-%'", one=True)['n'] == history_count
                    for fixture in fixtures:
                        assert engine.latest(fixture['observed'])['state'] == 'running'
                status = heartbeat(root); assert status['error'] is None and time.time() - status['updated'] < 15
                (out / f'round-{number}.json').write_text(json.dumps(row, indent=2))
            # Real fixture exit must become visible through normal observation.
            (root / 'bench-mode').write_text('window')
            if load:
                assert any(s['counts']['worker'].get('docker inspect',0) > 0 for s in row['scenarios'].values())
                load.deactivate()
                start = time.monotonic()
                wait_for(lambda: load.verify_active(root, False), 30)
                row['docker_exit_observed_ms'] = (time.monotonic() - start) * 1000
            else:
                for fixture in fixtures:
                    start = time.monotonic(); cli(control, 'stop', fixture['id']); owned.remove(fixture['id'])
                    wait_for(lambda: engine.latest(fixture['observed'])['ended'] is not None, 20)
                    fixture['exit_observed_ms'] = (time.monotonic() - start) * 1000
            report['rounds'].append(row)
            (out / 'results.json').write_text(json.dumps(report, indent=2))
            print(json.dumps({'completed_round': number, 'variant': variant}), flush=True)
        finally:
            (root / 'quit').touch()
            cleanup_errors = []
            quit_started = time.monotonic()
            # Allow native termination and supervisor reaping to finish before
            # issuing a csm stop. Do not race two shutdown paths.
            if gui_id in owned:
                try:
                    wait_for(lambda: (registered_run(control, gui_id) or {}).get('ended') is not None, 15)
                except RuntimeError:
                    pass  # The normal identity-checked csm stop remains below.
            for sid in reversed(owned):
                try:
                    cli(control, 'stop', sid)
                except Exception as exc:
                    cleanup_errors.append({'id': sid, 'error_type': type(exc).__name__})
            root_lock.close()
            row['native_shutdown_ms'] = (time.monotonic() - quit_started) * 1000
            (out / f'round-{number}.json').write_text(json.dumps(row, indent=2))
            if report['rounds'] and report['rounds'][-1] is row:
                (out / 'results.json').write_text(json.dumps(report, indent=2))
            if cleanup_errors:
                (root / 'cleanup-errors.json').write_text(json.dumps(cleanup_errors))
                raise RuntimeError('test cleanup incomplete; see isolated cleanup-errors.json')


if __name__ == '__main__':
    main()
