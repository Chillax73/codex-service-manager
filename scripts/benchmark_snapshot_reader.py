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
                "ms": self.store.readerBenchmarkMs]
            if let data = try? JSONSerialization.data(withJSONObject: row),
               let file = try? FileHandle(forWritingTo: root.appendingPathComponent("refresh.jsonl")) {
                file.seekToEndOfFile(); file.write(data + Data([10])); try? file.close()
            }
        }
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in
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
    assert not any('/capture.py collect' in row for row in processes), 'collector is running'
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


def fixture(root):
    engine.ROOT = root
    db = engine.database()
    cwd = str(root)
    for n in range(24):
        pid = f'project-{n:02}'
        db.execute("INSERT INTO projects(id,cwd,name,auto_scan,created_at) VALUES (?,?,?,0,1)",
                   (pid, cwd + f'/project-{n}', f'P0 测试项目 {n:02}'))
        for j in range(40):
            sid = f'fixture-{n:02}-{j:02}'
            spec = {'argv': ['/usr/bin/true', 'x' * 600], 'cwd': cwd, 'env': {}}
            db.execute("INSERT INTO services(id,project_id,name,kind,spec,created_at) VALUES (?,?,?,'task',?,1)",
                       (sid, pid, sid, json.dumps(spec)))
    db.execute("INSERT INTO metadata VALUES ('capture_disabled_cwds',?)", [json.dumps([cwd])])
    db.commit(); db.close()
    response = json.dumps({'ok': True, 'result': engine.snapshot_cached()}).encode() + b'\n'
    return {'projects': 24, 'services': 960, 'wire_bytes': len(response),
            'all_project_scans_paused': True}


def build(directory, infrastructure):
    directory.mkdir()
    bundle = directory / 'CSM P0 Benchmark.app'
    shutil.copytree(BASE / 'build/Codex Service Manager.app', bundle)
    plist = bundle / 'Contents/Info.plist'
    info = plistlib.loads(plist.read_bytes())
    info['CFBundleIdentifier'] = 'local.csm.reader-benchmark.' + directory.name
    plist.write_bytes(plistlib.dumps(info))
    source = BASE / 'app/Sources'
    manager = (source / 'ServiceManager.swift').read_text()
    needle = '        showWindow()\n    }\n    @objc func showWindow()'
    assert manager.count(needle) == 1
    manager = manager.replace(needle, '        showWindow()\n        installReaderBenchmark()\n    }\n' +
                              DRIVER + '    @objc func showWindow()')
    (directory / 'ServiceManager.swift').write_text(manager)
    store = (source / 'Store.swift').read_text()
    store = store.replace('    var onRefresh:',
                          '    var readerBenchmarkMs: Double { (ProcessInfo.processInfo.systemUptime - lastRefreshStarted) * 1000 }\n    var onRefresh:', 1)
    (directory / 'Store.swift').write_text(store)
    shutil.copyfile(infrastructure, directory / 'Infrastructure.swift')
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
    parser.add_argument('--baseline-infrastructure', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=24)
    args = parser.parse_args()
    if args.seconds < 10:
        parser.error('sample at least 10 seconds per round')
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
    report = {'collector_before': before, 'fixture': fixture(root), 'seconds': args.seconds,
              'mach_timebase': scale,
              'baseline_sha256': hashlib.sha256(args.baseline_infrastructure.read_bytes()).hexdigest(),
              'candidate_sha256': hashlib.sha256((BASE / 'app/Sources/Infrastructure.swift').read_bytes()).hexdigest(),
              'rounds': []}
    print(json.dumps(report['fixture']), flush=True)
    apps = {name: build(directory / name, path) for name, path in
            [('before', args.baseline_infrastructure), ('after', BASE / 'app/Sources/Infrastructure.swift')]}
    csm = [BASE / 'scripts/csm', '--root', registry]
    for number, name in enumerate(('before', 'after', 'after', 'before')):
        sid = f'p0-{number}-{name}'
        (root / 'quit').unlink(missing_ok=True)
        (root / 'refresh.jsonl').write_text('')
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
            assert all(row['services'] == 960 and row['visible'] and not row['unavailable'] for row in rows)
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
            assert len(refreshes) >= args.seconds / 3
            assert all(r['services'] == 960 and r['visible'] and not r['unavailable'] for r in refreshes)
            row.update(refreshes=len(refreshes), median_refresh_ms=statistics.median(r['ms'] for r in refreshes))
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
                        for key in ('gui_cpu_percent', 'worker_cpu_percent', 'median_refresh_ms')}
    (directory / 'results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
