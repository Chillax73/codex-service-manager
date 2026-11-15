#!/usr/bin/env python3
"""ABBA native-window benchmark; synthetic data only, never starts a collector.

Build the baseline before editing, then run with --baseline-infrastructure pointing
at its Infrastructure.swift. Both instrumented apps use current remaining sources,
-O, the same real backend and the same paused 960-service fixture. Test apps are
registered via scripts/csm in a separate test-only registry and stopped in finally.
"""
import argparse
import ctypes
import fcntl
import hashlib
import json
from pathlib import Path
import platform
import plistlib
import shutil
import statistics
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend'))
import engine

DRIVER = '''
    private func installReaderBenchmark() {
        let root = URL(fileURLWithPath: statePath)
        let previous = store.onRefresh
        store.onRefresh = { [weak self] in
            previous?()
            guard let self = self else { return }
            let row: [String: Any] = ["at": Date().timeIntervalSince1970,
                "pid": ProcessInfo.processInfo.processIdentifier,
                "services": self.store.services.count, "visible": self.window.isVisible,
                "window": self.window.windowNumber, "unavailable": self.store.observationUnavailable,
                "ms": self.store.readerBenchmarkMs, "wireBytes": self.store.readerBenchmarkWireBytes,
                "responseRows": self.store.readerBenchmarkRows]
            if let data = try? JSONSerialization.data(withJSONObject: row),
               let file = try? FileHandle(forWritingTo: root.appendingPathComponent("refresh.jsonl")) {
                file.seekToEndOfFile(); file.write(data + Data([10])); try? file.close()
            }
        }
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            let control = root.appendingPathComponent("bench-mode")
            if let mode = try? String(contentsOf: control, encoding: .utf8) {
                try? FileManager.default.removeItem(at: control)
                if mode == "menu" { self?.closeWindow() }
                if mode == "window" { self?.showWindow() }
            }
            if FileManager.default.fileExists(atPath: root.appendingPathComponent("quit").path) {
                NSApp.terminate(nil)
            }
        }
    }
'''


def run(args):
    return subprocess.check_output([str(v) for v in args], text=True)


def guard_collector():
    # Read-only: no capture import, no collector ensure/arm/hook calls.
    root = Path.home() / 'Library/Application Support/Codex Service Manager'
    import sqlite3
    with sqlite3.connect(root.joinpath('manager.sqlite').as_uri() + '?mode=ro', uri=True) as db:
        disabled = db.execute("SELECT value FROM metadata WHERE key='capture_disabled_cwds'").fetchone()
        runs = db.execute("SELECT count(*) FROM runs WHERE service_id='csm-auto-collector'").fetchone()[0]
    hooks = json.loads((Path.home() / '.codex/hooks.json').read_text())
    config = (Path.home() / '.codex/config.toml').read_text()
    assert hooks == {'hooks': {}} and 'hooks = false' in config
    count = len(json.loads(disabled[0])) if disabled else 0
    processes = run(['/bin/ps', '-axo', 'pid=,command=']).splitlines()
    assert not any('/capture.py ' in row and row.rstrip().endswith(' collect')
                   for row in processes), 'collector is running'
    return {'disabled_cwds': count, 'collector_runs': runs, 'hooks': False}


class Timebase(ctypes.Structure):
    _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]


def timebase():
    value = Timebase()
    if ctypes.CDLL('/usr/lib/libSystem.B.dylib').mach_timebase_info(ctypes.byref(value)) or not value.denom:
        raise RuntimeError('cannot read Mach clock conversion')
    return {'numer': value.numer, 'denom': value.denom}


def usage(pid):
    values = (ctypes.c_uint64 * 64)()
    if ctypes.CDLL('/usr/lib/libproc.dylib').proc_pid_rusage(pid, 4, ctypes.byref(values)):
        raise RuntimeError('cannot sample test PID')
    # Own user+system Mach ticks (not nanoseconds on Apple Silicon).
    return values[2] + values[3]


def fixture(root, count=960):
    engine.ROOT = root
    db = engine.database()
    cwd = str(root)
    for n in range(24):
        pid = f'project-{n:02}'
        db.execute("INSERT INTO projects(id,cwd,name,auto_scan,created_at) VALUES (?,?,?,0,1)",
                   (pid, cwd + f'/project-{n}', f'P0 测试项目 {n:02}'))
        for j in range((count + 23 - n) // 24):
            sid = f'fixture-{n:02}-{j:02}'
            spec = {'argv': ['/usr/bin/true', 'x' * 600], 'cwd': cwd, 'env': {}}
            db.execute("INSERT INTO services(id,project_id,name,kind,spec,created_at) VALUES (?,?,?,'task',?,1)",
                       (sid, pid, sid, json.dumps(spec)))
    db.execute("INSERT INTO metadata VALUES ('capture_disabled_cwds',?)", [json.dumps([cwd])])
    db.commit(); db.close()
    response = json.dumps({'ok': True, 'result': engine.snapshot_cached()}, ensure_ascii=False).encode() + b'\n'
    return {'projects': 24, 'services': count, 'wire_bytes': len(response),
            'all_project_scans_paused': True}


def build(directory, infrastructure, source=None, built=None, driver=DRIVER, manager_transform=None):
    directory.mkdir()
    bundle = directory / 'CSM P0 Benchmark.app'
    shutil.copytree(built or BASE / 'build/Codex Service Manager.app', bundle)
    plist = bundle / 'Contents/Info.plist'
    info = plistlib.loads(plist.read_bytes())
    info['CFBundleIdentifier'] = 'local.csm.reader-benchmark.' + directory.name
    plist.write_bytes(plistlib.dumps(info))
    source = source or BASE / 'app/Sources'
    manager = (source / 'ServiceManager.swift').read_text()
    needle = '        showWindow()\n    }\n    @objc func showWindow()'
    assert manager.count(needle) == 1
    manager = manager.replace(needle, '        showWindow()\n        installReaderBenchmark()\n    }\n' +
                              driver + '    @objc func showWindow()')
    if manager_transform:
        manager = manager_transform(manager)
    (directory / 'ServiceManager.swift').write_text(manager)
    store = (source / 'Store.swift').read_text()
    store = store.replace('    var onRefresh:',
                          '    var readerBenchmarkWireBytes = 0\n    var readerBenchmarkRows = 0\n'
                          '    var readerBenchmarkMs: Double { (ProcessInfo.processInfo.systemUptime - lastRefreshStarted) * 1000 }\n    var onRefresh:', 1)
    needle = '                    guard requestRevision == self.revision else { self.finishRefresh(); return }'
    assert store.count(needle) == 1
    store = store.replace(needle, needle + '''
                    self.readerBenchmarkWireBytes = self.snapshotConnection.benchmarkWireBytes
                    self.readerBenchmarkRows = self.snapshotConnection.benchmarkRows''')
    (directory / 'Store.swift').write_text(store)
    connection = infrastructure.read_text().replace('final class CachedSnapshotConnection {',
        'final class CachedSnapshotConnection {\n    var benchmarkWireBytes = 0\n    var benchmarkRows = 0')
    needle = '                return result\n'
    assert connection.count(needle) == 1
    connection = connection.replace(needle, '''                benchmarkWireBytes = line.count + 1
                benchmarkRows = (result["services"] as? NSArray)?.count ?? 0
                return result
''')
    (directory / 'Infrastructure.swift').write_text(connection)
    for name in ('Models.swift', 'Status.swift'):
        shutil.copyfile(source / name, directory / name)
    executable = bundle / 'Contents/MacOS/ServiceManager'
    subprocess.run(['/usr/bin/swiftc', '-O', '-swift-version', '5', '-target',
                    platform.machine() + '-apple-macos13.0', '-module-cache-path', str(BASE / 'build/module-cache'),
                    '-framework', 'AppKit', '-framework', 'SwiftUI',
                    *(str(directory / name) for name in ('ServiceManager.swift', 'Store.swift',
                                                        'Models.swift', 'Status.swift', 'Infrastructure.swift')),
                    '-o', str(executable)], check=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(bundle)], check=True)
    return executable


def records(root):
    return [json.loads(line) for line in (root / 'refresh.jsonl').read_text().splitlines() if line.endswith('}')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument('--baseline-infrastructure', type=Path)
    baseline.add_argument('--baseline-directory', type=Path,
                          help='all Swift sources plus their built Codex Service Manager.app')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=24)
    parser.add_argument('--services', type=int, default=960)
    parser.add_argument('--mode', choices=('window', 'menu'), default='window')
    parser.add_argument('--rounds', type=int, choices=(2, 4), default=4)
    parser.add_argument('--observe-idle', action='store_true',
                        help='enable observations of cached terminal test services for scheduler comparisons')
    args = parser.parse_args()
    if args.seconds < 10:
        parser.error('sample at least 10 seconds per round')
    if args.services < 24:
        parser.error('at least 24 services for the 24-project fixture')
    baseline_file = args.baseline_infrastructure or args.baseline_directory / 'Infrastructure.swift'
    before = guard_collector()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    root, registry = directory / 'fixture', directory / 'registry'
    root.mkdir(); registry.mkdir()
    locks = []
    for path in (root, registry):
        handle = (path / 'watcher.lock').open('a')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(handle)  # Prevent test-only exit watchers; production is untouched.
    scale = timebase()
    report = {'collector_before': before, 'fixture': fixture(root, args.services), 'seconds': args.seconds,
              'mode': args.mode,
              'mach_timebase': scale,
              'baseline_sha256': hashlib.sha256(baseline_file.read_bytes()).hexdigest(),
              'candidate_sha256': hashlib.sha256((BASE / 'app/Sources/Infrastructure.swift').read_bytes()).hexdigest(),
              'rounds': []}
    if args.observe_idle:
        if args.seconds > 70:
            parser.error('idle scheduler rounds must finish before the terminal observation deadline')
        engine.observe('fixture-00-00')
        projection = engine.query('SELECT projection FROM service_observations LIMIT 1', one=True)['projection']
        with engine.database() as db:
            db.execute('UPDATE projects SET auto_scan=1')
            db.execute('INSERT OR REPLACE INTO service_observations SELECT id,?,? FROM services',
                       [time.time(), projection])
        report['fixture']['all_project_scans_paused'] = False
        report['fixture']['cached_terminal_services'] = args.services
        report['fixture']['wire_bytes'] = len(json.dumps(
            {'ok': True, 'result': engine.snapshot_cached()}, ensure_ascii=False).encode()) + 1
    print(json.dumps(report['fixture']), flush=True)
    apps = {'before': build(directory / 'before', baseline_file, args.baseline_directory,
                            args.baseline_directory / 'Codex Service Manager.app' if args.baseline_directory else None),
            'after': build(directory / 'after', BASE / 'app/Sources/Infrastructure.swift')}
    report['source_sha256'] = {
        name: {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted((directory / name).glob('*.swift'))}
        for name in apps}
    csm = [BASE / 'scripts/csm', '--root', registry]
    order = ('before', 'after', 'after', 'before') if args.rounds == 4 else ('before', 'after')
    report['order'] = order
    for number, name in enumerate(order):
        sid = f'p0-{number}-{name}'
        (root / 'quit').unlink(missing_ok=True)
        (root / 'bench-mode').unlink(missing_ok=True)
        (root / 'refresh.jsonl').write_text('')
        if args.observe_idle:
            # Start each comparable round with equally fresh terminal observations.
            # Their normal 120/240 second deadline remains unchanged.
            engine.write('UPDATE service_observations SET checked_at=?', [time.time()])
        try:
            launched = json.loads(run([*csm, 'launch', '--task', '--id', sid, '--name', sid,
                                       '--cwd', root, '--', apps[name], '--root', root]))
            assert launched['ok'], launched
            deadline = time.monotonic() + 30
            while True:
                rows = records(root)
                if len(rows) >= 4:
                    break
                assert time.monotonic() < deadline, 'window did not refresh'
                time.sleep(.25)
            assert all(row['services'] == args.services and row['visible'] and not row['unavailable'] for row in rows)
            if args.mode == 'menu':
                (root / 'bench-mode').write_text('menu')
                deadline = time.monotonic() + 45
                while True:
                    rows = [row for row in records(root) if not row['visible']]
                    if len(rows) >= 3:
                        break
                    assert time.monotonic() < deadline, 'menu did not refresh'
                    time.sleep(.25)
            pid = rows[-1]['pid']
            procs = [line.strip().split(None, 2) for line in run(['/bin/ps', '-axo', 'pid=,ppid=,command=']).splitlines()]
            workers = [int(p) for p, parent, cmd in procs if int(parent) == pid and cmd.endswith(' snapshot-worker')]
            assert len(workers) == 1, workers
            wp = workers[0]
            start = time.monotonic(); wall = time.time(); cpu = usage(pid); worker_cpu = usage(wp)
            print(f'round {number + 1}: {name}, GUI PID {pid}, window {rows[-1]["window"]}', flush=True)
            (directory / 'active.json').write_text(json.dumps({'variant': name, 'pid': pid, 'window': rows[-1]['window']}))
            time.sleep(args.seconds)
            elapsed = time.monotonic() - start
            row = {'variant': name, 'seconds': elapsed,
                   'gui_cpu_ticks': usage(pid) - cpu, 'worker_cpu_ticks': usage(wp) - worker_cpu}
            for process in ('gui', 'worker'):
                row[process + '_cpu_percent'] = (row[process + '_cpu_ticks'] * scale['numer'] /
                                                scale['denom'] / 1e7 / elapsed)
            refreshes = [r for r in records(root) if r['at'] >= wall]
            assert len(refreshes) >= args.seconds / (3 if args.mode == 'window' else 12)
            assert all(r['services'] == args.services and r['visible'] == (args.mode == 'window')
                       and not r['unavailable'] for r in refreshes)
            row.update(refreshes=len(refreshes), median_refresh_ms=statistics.median(r['ms'] for r in refreshes),
                       wire_bytes=statistics.median(r['wireBytes'] for r in refreshes),
                       response_rows=statistics.median(r['responseRows'] for r in refreshes))
            report['rounds'].append(row)
            print(json.dumps(row), flush=True)
            shutil.copyfile(root / 'refresh.jsonl', directory / f'{number}-{name}-refresh.jsonl')
        finally:
            (root / 'quit').touch()
            time.sleep(1)
            stopped = json.loads(run([*csm, 'stop', sid]))
            assert stopped['ok'], stopped
    report['collector_after'] = guard_collector()
    assert report['collector_before'] == report['collector_after'], 'collector state changed'
    for name in apps:
        rows = [r for r in report['rounds'] if r['variant'] == name]
        report[name] = {key: statistics.mean(r[key] for r in rows)
                        for key in ('gui_cpu_percent', 'worker_cpu_percent', 'median_refresh_ms', 'wire_bytes', 'response_rows')}
    (directory / 'results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
