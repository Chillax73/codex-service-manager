#!/usr/bin/env python3
"""Compare native snapshot costs using an isolated, fixed 260-service workload."""
import argparse
from collections import Counter
import ctypes
import http.server
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time


BASE = Path(__file__).resolve().parents[1]
COUNTS = {'paused_terminal': 196, 'enabled_terminal': 36, 'managed_tasks': 19,
          'observed_task': 1, 'health_tasks': 4, 'containers': 4}

DRIVER = r'''
    private func installBenchmarkDriver() {
        let root = URL(fileURLWithPath: statePath)
        let snapshots = root.appendingPathComponent("bench-ui.jsonl")
        let control = root.appendingPathComponent("bench-control")
        let render = store.onRefresh
        store.onRefresh = { [weak self] in
            render?()
            guard let store = self?.store, store.services.count == 260 else { return }
            var values: [String: String] = [:]
            for service in store.services where ["observed", "docker-0", "health-0"].contains(service.id) {
                values[service.id] = service.id == "health-0" ? service.health : service.state
            }
            let sample: [String: Any] = ["at": Date().timeIntervalSince1970,
                                          "ms": store.benchmarkRefreshMs, "states": values]
            if let data = try? JSONSerialization.data(withJSONObject: sample),
               let file = try? FileHandle(forWritingTo: snapshots) {
                file.seekToEndOfFile()
                file.write(data + Data([10]))
                try? file.close()
            }
        }
        /* NATIVE_STATE_SETUP */
        Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            /* NATIVE_STATE_RECORD */
            guard let command = try? String(contentsOf: control, encoding: .utf8) else { return }
            try? FileManager.default.removeItem(at: control)
            switch command {
            case "menu": self.closeWindow()
            case "window": self.showWindow()
            case "quit": NSApp.terminate(nil)
            /* NATIVE_STATE_ACTIONS */
            default: break
            }
        }
    }
'''

NATIVE_STATE = r'''
            if verifying {
                let item = self.store.services.first { $0.id == "paused_terminal-0" }
                let values: [String: Any] = [
                    "unavailable": self.store.observationUnavailable,
                    "visible": self.window.isVisible,
                    "menuTitle": self.status.button?.title ?? "",
                    "selected": self.store.selectedService ?? "",
                    "logRun": self.store.logRunID ?? "",
                    "logPreserved": self.store.logs.contains("snapshot-fixture-log"),
                    "logOffset": self.benchmarkLogScroll()?.contentView.bounds.origin.y ?? -1,
                    "pausedCheckedAt": item?.lastCheckedAt ?? 0
                ]
                if let data = try? JSONSerialization.data(withJSONObject: values, options: .sortedKeys), data != lastView {
                    try? data.write(to: root.appendingPathComponent("bench-native.json"), options: .atomic)
                    lastView = data
                }
            }
'''

NATIVE_SCROLL = r'''
    private func benchmarkLogScroll() -> NSScrollView? {
        func search(_ view: NSView) -> NSScrollView? {
            if let scroll = view as? NSScrollView, (scroll.documentView?.frame.height ?? 0) > 5000 {
                return scroll
            }
            return view.subviews.lazy.compactMap(search).first
        }
        return window.contentView.flatMap(search)
    }
'''

# Audit only fixture descendants. Include short-lived Python writes in disk counters.
TRACE = '''
import atexit, ctypes, json, os, sys, time
path = os.environ.get("CSM_BENCH_TRACE")
if path:
    lib = ctypes.CDLL("/usr/lib/libproc.dylib")
    def trace(kind, value):
        with open(path, "a") as file:
            file.write(json.dumps([time.time(), kind, value]) + "\\n")
    trace("python", os.getpid())
    def audit(event, args):
        if event == "subprocess.Popen":
            trace("child", os.path.basename(os.fsdecode(args[0])))
    def finish():
        values = (ctypes.c_uint64 * 64)()
        if lib.proc_pid_rusage(os.getpid(), 4, ctypes.byref(values)) == 0:
            trace("exited_writes", values[19])
    sys.addaudithook(audit)
    atexit.register(finish)
'''


def prepare_bundle(directory, built, verify_native):
    bundle = directory / 'CSM Snapshot Benchmark.app'
    shutil.copytree(built, bundle)
    contents = bundle / 'Contents'
    plist = contents / 'Info.plist'
    info = plistlib.loads(plist.read_bytes())
    info['CFBundleIdentifier'] = 'local.csm.snapshot-benchmark.' + directory.name
    plist.write_bytes(plistlib.dumps(info))
    source = BASE / 'app/Sources'
    manager = (source / 'ServiceManager.swift').read_text()
    needle = '        showWindow()\n    }\n    @objc func showWindow()'
    assert manager.count(needle) == 1
    driver = DRIVER
    if verify_native:
        driver = driver.replace('/* NATIVE_STATE_SETUP */', 'var verifying = false; var lastView = Data()')
        driver = driver.replace('/* NATIVE_STATE_RECORD */', NATIVE_STATE)
        driver = driver.replace('/* NATIVE_STATE_ACTIONS */', '''
            case "inspect":
                verifying = true
                self.store.selectedService = "task-0"
            case "select-log":
                if let run = self.store.selected?.run["id"] as? String { self.store.loadRunLogs(run) }
                NotificationCenter.default.post(name: Notification.Name("CSMSnapshotBenchmarkLogTab"), object: nil)
            case "scroll":
                if let scroll = self.benchmarkLogScroll() {
                    scroll.contentView.scroll(to: NSPoint(x: 0, y: 400))
                    scroll.reflectScrolledClipView(scroll.contentView)
                }
            case "manual": self.store.refresh(projectID: "all")
''')
        driver += NATIVE_SCROLL
        selection = '        .onChange(of:store.selectedService)'
        assert selection in manager
        manager = manager.replace(selection, '''
        .onReceive(NotificationCenter.default.publisher(for: Notification.Name("CSMSnapshotBenchmarkLogTab"))) { _ in
            tab = "logs"
        }
''' + selection, 1)
    manager = manager.replace(needle, '        showWindow()\n        installBenchmarkDriver()\n    }\n'
                              + driver + '    @objc func showWindow()')
    (directory / 'ServiceManager.swift').write_text(manager)
    store = (source / 'Store.swift').read_text()
    (directory / 'Store.swift').write_text(store.replace(
        '    var onRefresh:', '    var benchmarkRefreshMs: Double { (ProcessInfo.processInfo.systemUptime - lastRefreshStarted) * 1000 }\n'
        '    var onRefresh:', 1))
    for name in ('Models.swift', 'Status.swift', 'Infrastructure.swift'):
        shutil.copyfile(source / name, directory / name)
    print('已固定待测 Swift 和 Python 源文件', flush=True)
    executable = contents / 'MacOS' / 'ServiceManager'
    subprocess.run(['/usr/bin/swiftc', '-swift-version', '5', '-target',
                    platform.machine() + '-apple-macos13.0', '-O', '-module-cache-path',
                    str(BASE / 'build/module-cache'), '-framework', 'SwiftUI', '-framework', 'AppKit',
                    *(str(directory / n) for n in ('ServiceManager.swift', 'Store.swift', 'Models.swift',
                                                   'Status.swift', 'Infrastructure.swift')),
                    '-o', str(executable)], check=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(bundle)], check=True)
    (directory / 'sitecustomize.py').write_text(TRACE)
    return executable, contents / 'Resources/backend'


def lines(path):
    if not path.exists():
        return []
    return [json.loads(row) for row in path.read_text().splitlines(keepends=True) if row.endswith('\n')]


def until(check, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.1)
    raise RuntimeError('等待夹具状态超时')


def stop_child(child):
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(5)


def usage(pid):
    data = (ctypes.c_uint64 * 64)()
    if ctypes.CDLL('/usr/lib/libproc.dylib').proc_pid_rusage(pid, 4, ctypes.byref(data)) != 0:
        raise RuntimeError('无法读取夹具进程资源计数')
    # UUID occupies two slots in macOS struct rusage_info_v4.
    return sum(data[i] for i in (2, 3, 12, 13)), data[19]


def worker_pids(parent):
    if parent is None:
        return []
    output = subprocess.check_output(['/bin/ps', '-axo', 'pid=,ppid=,command='], text=True)
    return [int(pid) for pid, ppid, command in (line.strip().split(None, 2) for line in output.splitlines())
            if int(ppid) == parent and command.endswith(' snapshot-worker')]


def verify_connection(engine, backend, root, app):
    def view():
        path = root / 'bench-native.json'
        return json.loads(path.read_text()) if path.exists() else {}

    run = engine.latest('task-0')
    Path(run['log']).write_text('snapshot-fixture-log\n' * 1200)
    control(root, 'inspect')
    until(lambda: view().get('selected') == 'task-0')
    control(root, 'select-log')
    until(lambda: view().get('logPreserved') and view().get('logRun') == run['id'])
    until(lambda: view().get('logOffset', -1) >= 0)
    control(root, 'scroll')
    until(lambda: view().get('logOffset', -1) >= 399)
    original = view()
    worker, = worker_pids(app.pid)
    control(root, 'menu')
    until(lambda: view().get('visible') is False)
    assert view()['menuTitle'].strip().isdigit()
    control(root, 'window')
    until(lambda: view().get('visible') is True)
    assert worker_pids(app.pid) == [worker]
    assert view()['selected'] == original['selected'] and view()['logRun'] == original['logRun']
    assert view()['logPreserved']
    assert abs(view()['logOffset'] - original['logOffset']) < 1

    before = engine.identity(worker)
    assert before and 'snapshot-worker' in before['command'] and str(root) in before['command']
    os.kill(worker, signal.SIGTERM)
    until(lambda: view().get('unavailable'))
    assert view()['logPreserved']
    control(root, 'manual')
    until(lambda: view().get('pausedCheckedAt', 0) > original['pausedCheckedAt'])
    until(lambda: view().get('unavailable') is False)
    replacement, = worker_pids(app.pid)
    assert replacement != worker and view()['logPreserved']
    cli = backend.parent / 'scripts/csm'
    listing = json.loads(subprocess.check_output([str(cli), '--root', str(root), 'list'], text=True))
    assert len(listing['result']['services']) == sum(COUNTS.values())
    control(root, 'quit', app)
    until(lambda: engine.identity(replacement) is None)
    return {'same_worker_after_reopen': True, 'stale_banner_and_reconnect': True,
            'manual_refresh_while_recovering': True, 'selection_and_log_scroll_preserved': True,
            'worker_reaped_on_exit': True, 'cli_live_list': True}


def phase(root, app, collector, watcher, config):
    time.sleep(config.warmup)
    parents = [collector.pid, watcher['pid']] + ([app.pid] if app else [])
    workers = worker_pids(app.pid if app else None)
    start_usage = [usage(pid) for pid in parents + workers]
    start, monotonic_start = time.time(), time.monotonic()
    time.sleep(config.seconds)
    seconds = time.monotonic() - monotonic_start
    end = time.time()
    assert workers == worker_pids(app.pid if app else None), '状态子进程采样期间变化'
    end_usage = [usage(pid) for pid in parents + workers]
    cpu = sum(b[0] - a[0] for a, b in zip(start_usage, end_usage))
    trace = [(kind, value) for at, kind, value in lines(root / 'bench-trace.jsonl') if start <= at <= end]
    children = Counter(value for kind, value in trace if kind == 'child')
    children['python'] = sum(kind == 'python' for kind, _ in trace)
    writes = sum(b[1] - a[1] for a, b in zip(start_usage, end_usage))
    writes += sum(value for kind, value in trace if kind == 'exited_writes')
    snapshots = [r['ms'] for r in lines(root / 'bench-ui.jsonl') if start <= r['at'] <= end]
    return {'seconds': seconds, 'cpu_percent_one_core': cpu / 1e7 / seconds, 'children': dict(children),
            'disk_write_bytes': writes, 'snapshot_ms': snapshots, 'workers': len(workers)}


def trial(engine, backend, executable, root, image, config):
    engine.ROOT = root
    label = 'csm.snapshot.fixture=' + root.name
    owned_tasks, containers = [], []
    collector = app = observed = watcher = None
    healthy = threading.Event()
    healthy.set()
    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if healthy.is_set() else 503)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        enabled = engine.project(root / 'enabled')
        paused = engine.project(root / 'paused')
        engine.write('UPDATE projects SET auto_scan=0 WHERE id=?', [paused['id']])
        for group, project in (('paused_terminal', paused), ('enabled_terminal', enabled)):
            for n in range(COUNTS[group]):
                sid = f'{group}-{n}'
                engine.record_spec(sid, project, sid, 'task', {})
                engine.set_run(engine.new_run(sid), state='succeeded', ended=engine.timestamp())
        for group, number in (('task', COUNTS['managed_tasks']), ('health', COUNTS['health_tasks'])):
            for n in range(number):
                sid = f'{group}-{n}'
                owned_tasks.append(sid)
                url = f'http://127.0.0.1:{server.server_port}/health' if group == 'health' else None
                engine.dispatch({'action': 'launch', 'id': sid, 'name': sid, 'cwd': enabled['cwd'],
                                 'kind': 'task', 'argv': [sys.executable, '-c', 'import time; time.sleep(900)'], 'url': url})
        observed = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(900)'])
        birth = engine.identity(observed.pid)['birth']
        engine.record_spec('observed', enabled, 'observed', 'task', {'management': 'observed', 'cwd': enabled['cwd']})
        run = engine.new_run('observed', {'pid': observed.pid, 'birth': birth})
        engine.set_run(run, state='running')
        for n in range(COUNTS['containers']):
            cid = engine.docker('run', '-d', '--pull=never', '--network', 'none', '--label', label,
                                '--entrypoint', '/bin/sh', image, '-c', 'exec sleep 900')
            containers.append(cid)
            sid = f'docker-{n}'
            spec = {'container_id': cid, 'cwd': enabled['cwd']}
            if n == 0:
                spec['management'] = 'observed'
            engine.record_spec(sid, enabled, sid, 'container', spec)
        for item in engine.query('SELECT id FROM services'):
            engine.observe(item['id'])
        (root / 'watcher-config.json').write_text(json.dumps({'bundle_ids': ['local.csm.benchmark-unused']}))
        (root / 'bench-ui.jsonl').touch()
        env = {**os.environ, 'PYTHONPATH': str(executable.parents[3]), 'PYTHONDONTWRITEBYTECODE': '1',
               'CSM_BENCH_TRACE': str(root / 'bench-trace.jsonl')}
        collector = subprocess.Popen([sys.executable, str(backend / 'capture.py'), '--root', str(root), 'collect'], env=env)
        args = [str(executable), '--root', str(root)]
        app = subprocess.Popen(args, env=env)
        watcher = until(lambda: metadata(engine, 'watcher'))
        until(lambda: lines(root / 'bench-ui.jsonl'))
        results = {'window': phase(root, app, collector, watcher, config)}
        control(root, 'menu')
        results['menu'] = phase(root, app, collector, watcher, config)
        control(root, 'quit', app)
        results['background'] = phase(root, None, collector, watcher, config)
        app = subprocess.Popen(args, env=env)
        until(lambda: any(r['at'] > time.time() - 3 for r in lines(root / 'bench-ui.jsonl')))
        changed = {}
        def measure(sid, action, *expected):
            at = time.time()
            action()
            sample = until(lambda: next((r for r in lines(root / 'bench-ui.jsonl')
                                         if r['at'] >= at and r['states'].get(sid) in expected), None), 45)
            changed[sid] = sample['at'] - at
        measure('observed', lambda: stop_child(observed), 'exited')
        measure('docker-0', lambda: engine.docker('stop', '-t', '1', containers[0]), 'stopped', 'exited', 'failed')
        measure('health-0', healthy.clear, 'unavailable')
        results['visible_change_seconds'] = changed
        if config.verify_native:
            results['native'] = verify_connection(engine, backend, root, app)
        return results
    finally:
        if app and app.poll() is None:
            try:
                control(root, 'quit', app)
            except (RuntimeError, subprocess.TimeoutExpired):
                stop_child(app)
        stop_child(observed)
        stop_child(collector)
        if watcher and engine.matches(watcher['pid'], watcher['birth'], '--watch'):
            os.kill(watcher['pid'], 15)
        for sid in owned_tasks:
            if engine.latest(sid):
                engine.dispatch({'action': 'force', 'id': sid})
        exact = engine.docker('ps', '-aq', '--filter', 'label=' + label).splitlines()
        if exact:
            engine.docker('rm', '-f', *exact)
        server.shutdown()
        server.server_close()


def metadata(engine, key):
    row = engine.query('SELECT value FROM metadata WHERE key=?', [key], True)
    return json.loads(row['value']) if row else None


def control(root, command, app=None):
    path = root / 'bench-control'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(command)
    temporary.replace(path)
    if app:
        app.wait(timeout=12)
        path.unlink(missing_ok=True)
    else:
        until(lambda: not path.exists())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=BASE / 'build/Codex Service Manager.app')
    parser.add_argument('--image', default='python:3.12-slim')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=24)
    parser.add_argument('--warmup', type=int, default=8)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--verify-native', action='store_true',
                        help='在改造后额外复验真实连接、断线、手动刷新和窗口状态')
    config = parser.parse_args()
    if min(config.seconds, config.warmup, config.repeat) < 1 or platform.system() != 'Darwin':
        parser.error('需要 macOS，且采样参数须为正数')
    parent = BASE / '.test-state'
    parent.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='snapshot-modes-', dir=parent))
    print('隔离夹具目录 ' + str(directory), flush=True)
    executable, backend = prepare_bundle(directory, config.app, config.verify_native)
    sys.path.insert(0, str(backend))
    import engine
    image = engine.docker('image', 'inspect', config.image, '--format', '{{.Id}}')
    report = {'macos': platform.mac_ver()[0], 'machine': platform.machine(), 'image': image, 'counts': COUNTS,
              'seconds': config.seconds, 'warmup': config.warmup, 'trials': []}
    for n in range(config.repeat):
        root = directory / f'{directory.name}-{n}'
        for name in ('enabled', 'paused'):
            (root / name).mkdir(parents=True)
        sample = trial(engine, backend, executable, root, image, config)
        report['trials'].append(sample)
        config.output.write_text(json.dumps(report, indent=2) + '\n')
        print(f'完成样本 {n + 1}/{config.repeat}', flush=True)
    for name in ('window', 'menu', 'background'):
        print(name, 'CPU median', round(statistics.median(t[name]['cpu_percent_one_core'] for t in report['trials']), 2))
    shutil.rmtree(directory)


if __name__ == '__main__':
    main()
