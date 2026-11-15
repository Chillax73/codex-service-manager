#!/usr/bin/env python3
"""Real AppKit window checks in a registered, collector-free synthetic fixture."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'scripts'))
import benchmark_snapshot_reader as bench
import engine

DRIVER = r'''
    private func testLogScroll() -> NSScrollView? {
        func search(_ view: NSView) -> [NSScrollView] {
            var found: [NSScrollView] = []
            if let scroll = view as? NSScrollView, (scroll.documentView?.frame.height ?? 0) > 5000 {
                found.append(scroll)
            }
            return found + view.subviews.flatMap(search)
        }
        guard let content = window.contentView else { return nil }
        return search(content).max { left, right in
            left.convert(left.bounds, to: content).origin.x < right.convert(right.bounds, to: content).origin.x
        }
    }
    private func installReaderBenchmark() {
        let root = URL(fileURLWithPath: statePath)
        var refreshes = 0
        let previous = store.onRefresh
        store.onRefresh = { previous?(); refreshes += 1 }
        Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            let state: [String: Any] = ["pid": ProcessInfo.processInfo.processIdentifier,
                "count": self.store.services.count, "visible": self.window.isVisible,
                "firstState": self.store.services.first?.state ?? "",
                "unavailable": self.store.observationUnavailable, "refreshes": refreshes,
                "selected": self.store.selectedService ?? "", "logRun": self.store.logRunID ?? "",
                "logOK": self.store.logs.hasPrefix("native-history-fixture"),
                "offset": self.testLogScroll()?.contentView.bounds.origin.y ?? -1,
                "window": self.window.windowNumber]
            if let data = try? JSONSerialization.data(withJSONObject: state) {
                try? data.write(to: root.appendingPathComponent("native-state.json"), options: .atomic)
            }
            let control = root.appendingPathComponent("native-control")
            guard let action = try? String(contentsOf: control, encoding: .utf8) else { return }
            try? FileManager.default.removeItem(at: control)
            switch action {
            case "select": self.store.selectedService = "fixture-00-00"
            case "logs":
                self.store.logRunID = "native-history-run"
                self.store.logs = "native-history-fixture\n" + String(repeating: "historical output line\n", count: 2000)
                NotificationCenter.default.post(name: Notification.Name("CSMNativeTestLogs"), object: nil)
            case "scroll":
                if let scroll = self.testLogScroll() {
                    scroll.contentView.scroll(to: NSPoint(x: 0, y: 400))
                    scroll.reflectScrolledClipView(scroll.contentView)
                }
            case "manual": self.store.refresh(projectID: "all")
            case "menu": self.closeWindow()
            case "window": self.showWindow()
            case "quit": NSApp.terminate(nil)
            default: break
            }
        }
    }
'''


def main():
    root = (BASE / '.test-state' / ('native-snapshot-' + str(time.time_ns()))).resolve()
    fixture, registry, source = root / 'fixture', root / 'registry', root / 'source'
    for path in (fixture, registry, source):
        path.mkdir(parents=True)
    locks = []
    for path in (fixture, registry):
        lock = (path / 'watcher.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); locks.append(lock)
    before = bench.guard_collector()
    bench.fixture(fixture, 260)
    for path in (BASE / 'app/Sources').glob('*.swift'):
        (source / path.name).write_bytes(path.read_bytes())
    manager = source / 'ServiceManager.swift'
    text = manager.read_text()
    needle = '        .onChange(of:store.selectedService)'
    assert needle in text
    text = text.replace(needle, '''        .onReceive(NotificationCenter.default.publisher(for: Notification.Name("CSMNativeTestLogs"))) { _ in tab = "logs" }
''' + needle, 1)
    manager.write_text(text)
    bench.DRIVER = DRIVER
    executable = bench.build(root / 'app', source / 'Infrastructure.swift', source)
    csm = [BASE / 'scripts/csm', '--root', registry]
    sid = 'native-snapshot-regression'
    backend = executable.parent.parent / 'Resources/backend/engine.py'
    offline = backend.with_suffix('.offline')
    checks = []

    def read():
        try:
            return json.loads((fixture / 'native-state.json').read_text())
        except FileNotFoundError:
            return {}

    def until(predicate, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = read()
            if predicate(value):
                return value
            time.sleep(.2)
        raise AssertionError(read())

    def control(action):
        (fixture / 'native-control').write_text(action)

    def preserved(value):
        return (value.get('selected') == 'fixture-00-00' and value.get('logRun') == 'native-history-run'
                and value.get('logOK') and value.get('offset', 0) > 390)

    try:
        started = json.loads(bench.run([*csm, 'launch', '--task', '--source', 'internal', '--id', sid,
                                       '--name', sid, '--cwd', fixture, '--', executable, '--root', fixture]))
        assert started['ok'], started
        state = until(lambda s: s.get('count') == 260 and s.get('visible') and s.get('refreshes', 0) >= 2)
        pid = state['pid']; checks.append('real window loaded 260 services')
        control('select'); until(lambda s: s.get('selected') == 'fixture-00-00')
        control('logs'); until(lambda s: s.get('logOK') and s.get('offset', -1) >= 0)
        control('scroll'); state = until(preserved)
        last = state['refreshes']
        until(lambda s: preserved(s) and s.get('refreshes', 0) >= last + 3)
        checks.append('selection, historical log and actual scroll retained across 3 refreshes')
        control('manual'); until(lambda s: preserved(s) and s.get('firstState') == 'never_started')
        checks.append('manual refresh retained selected log and scroll')
        rows = [line.strip().split(None, 2) for line in bench.run(['/bin/ps', '-axo', 'pid=,ppid=,command=']).splitlines()]
        workers = [int(p) for p, parent, cmd in rows if int(parent) == pid and cmd.endswith(' snapshot-worker')]
        assert len(workers) == 1
        identity = engine.identity(workers[0]); assert identity and str(backend) in identity['command']
        backend.rename(offline)
        assert engine.matches(workers[0], identity['birth'], str(backend))
        os.kill(workers[0], signal.SIGTERM)
        until(lambda s: preserved(s) and s.get('unavailable'))
        checks.append('real worker failure shown without losing log or scroll')
        offline.rename(backend)
        until(lambda s: preserved(s) and not s.get('unavailable'), timeout=30)
        checks.append('real worker recovered without losing log or scroll')
        control('menu'); state = until(lambda s: not s.get('visible', True))
        last = state['refreshes']
        until(lambda s: not s.get('visible', True) and s.get('refreshes', 0) > last, timeout=16)
        checks.append('menu mode continued refreshing')
        control('window'); until(lambda s: preserved(s) and s.get('visible') and s.get('refreshes', 0) > last)
        checks.append('reopened real window preserved selection and scroll')
        print(json.dumps({'checks': checks, 'state': read(), 'evidence': str(root)}, ensure_ascii=False), flush=True)
        (root / 'result.json').write_text(json.dumps({'checks': checks, 'state': read()}, indent=2))
    finally:
        if offline.exists():
            offline.rename(backend)
        control('quit'); time.sleep(1)
        stopped = json.loads(bench.run([*csm, 'stop', sid])); assert stopped['ok'], stopped
        assert bench.guard_collector() == before


if __name__ == '__main__':
    main()
