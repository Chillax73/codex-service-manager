"""Use a disposable native app to exercise the real NSWorkspace exit watcher."""
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time

from support import BASE, prepare_workspace

sys.path.insert(0, str(BASE / 'backend'))
import engine


def wait_for(check, message, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.1)
    raise RuntimeError(message)


def metadata(key):
    row = engine.query('SELECT value FROM metadata WHERE key=?', [key], True)
    return json.loads(row['value']) if row else {}


def stop_child(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run(root, manager):
    engine.ROOT = root
    fixture = root / 'LifecycleFixture.app' / 'Contents'
    executable = fixture / 'MacOS' / 'LifecycleFixture'
    executable.parent.mkdir(parents=True)
    bundle_id = 'local.csm.lifecycle-' + engine.uid()
    (fixture / 'Info.plist').write_bytes(plistlib.dumps({
        'CFBundleExecutable': executable.name, 'CFBundleIdentifier': bundle_id,
        'CFBundleName': 'CSM Lifecycle Fixture', 'CFBundlePackageType': 'APPL'}))
    subprocess.run(['/usr/bin/swiftc', '-module-cache-path', str(BASE / 'build/module-cache'),
                    str(BASE / 'app/Sources/ExitFixture.swift'), '-o', str(executable)], check=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(fixture.parent)], check=True)
    (root / 'watcher-config.json').write_text(json.dumps({'bundle_ids': [bundle_id]}))
    control = root / 'fixture-control'
    ready = root / 'slow-ready'
    started = []
    watcher = app = None
    def start_watcher():
        return subprocess.Popen([str(manager), '--watch', '--root', str(root), '--engine', engine.__file__],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        project = engine.project(root)
        engine.dispatch({'action': 'project_policy', 'id': project['id'], 'manual': True, 'codex_exit': True})
        for sid in ('a-slow', 'b-stop', 'c-keep'):
            code = 'import time; time.sleep(180)'
            if sid == 'a-slow':
                code = ('import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                        f'pathlib.Path({str(ready)!r}).touch(); time.sleep(180)')
            started.append(sid)
            engine.dispatch({'action': 'launch', 'id': sid, 'name': sid, 'cwd': str(root),
                             'argv': [sys.executable, '-c', code]})
        wait_for(ready.exists, '未就绪的停止失败夹具')
        engine.dispatch({'action': 'service_keep', 'id': 'c-keep', 'keep': True})
        watcher = start_watcher()
        wait_for(lambda: metadata('watcher').get('pid') == watcher.pid, '未收到测试观察器心跳')
        app = subprocess.Popen([str(executable), '--control', str(control)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(lambda: app.pid in metadata('watcher').get('instances', []), '测试应用未被观察器发现')
        control.write_text('close')
        wait_for(lambda: not control.exists(), '测试窗口未关闭')
        assert app.poll() is None and not metadata('last_codex_exit')
        assert all(engine.observe(sid)['state'] == 'running' for sid in started)
        with engine.mutation_lock():
            control.write_text('quit')
            app.wait(timeout=10)
            wait_for(lambda: not metadata('watcher').get('instances', [app.pid]), '观察器没有收到退出通知')
            assert not metadata('last_codex_exit')
        def heartbeat_during_stop():
            result = metadata('last_codex_exit')
            heartbeat = metadata('watcher')
            return (result.get('status') == 'running' and heartbeat.get('updated', 0) > result['event_at'])
        wait_for(heartbeat_during_stop, '退出执行期间未看到独立心跳')
        wait_for(lambda: metadata('last_codex_exit').get('status') == 'completed', '退出批次未结束')
        result = metadata('last_codex_exit')
        assert [result[key] for key in ('succeeded', 'skipped', 'failed')] == [1, 1, 1], result
        assert result['failures'][0]['id'] == 'a-slow'
        assert engine.observe('b-stop')['state'] == 'stopped'
        assert engine.observe('c-keep')['state'] == 'running'
        stop_child(watcher)
        watcher = start_watcher()
        wait_for(lambda: metadata('watcher').get('pid') == watcher.pid, '观察器重启后无心跳')
        assert metadata('last_codex_exit') == result
        return {'window_close_did_not_trigger': True, 'heartbeat_during_exit': True,
                'mixed_exit_result': True, 'result_survives_watcher_restart': True}
    finally:
        stop_child(app)
        stop_child(watcher)
        for sid in started:
            if engine.query('SELECT id FROM services WHERE id=?', [sid], True):
                engine.dispatch({'action': 'force', 'id': sid})


def main():
    prepare_workspace()
    manager = BASE / 'build/Codex Service Manager.app/Contents/MacOS/ServiceManager'
    if not manager.is_file():
        raise RuntimeError('先运行 scripts/build.sh')
    # Keep the registry if verification or cleanup fails so fixture identities can be inspected.
    root = Path(tempfile.mkdtemp(prefix='lifecycle-native-', dir=BASE / '.test-state')).resolve()
    try:
        result = run(root, manager)
    except Exception:
        print('测试状态目录：' + str(root), file=sys.stderr)
        raise
    import shutil
    shutil.rmtree(root)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
