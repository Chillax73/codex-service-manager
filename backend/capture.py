#!/usr/bin/env python3
"""Global shell hooks and a read-only, independently registered service collector.

Never reads transcripts or process environments. Commands/output are inspected only
in memory for Docker identities; persisted events use an explicit metadata whitelist.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import stat
import sys
import time

import engine

INTERNAL_ID = 'csm-auto-collector'
ARM_MARKER = '# CSM_CAPTURE_ARM'


def channel_dir():
    # Tool sandboxes allow their local temporary directory; no network permission
    # or write access to Application Support is needed by the shell-side helper.
    digest = hashlib.sha256(str(engine.ROOT).encode()).hexdigest()[:20]
    return Path('/private/tmp') / ('csm-capture-' + str(os.getuid()) + '-' + digest)


def open_channel():
    path = channel_dir()
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('自动采集通信目录身份或权限无效')
    fifo = path / 'arm.fifo'
    if not fifo.exists():
        os.mkfifo(fifo, 0o600)
    info = fifo.lstat()
    if not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('自动采集通信管道身份无效')
    return os.open(fifo, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)


def receive_arms(fd, tracked):
    try:
        data = os.read(fd, 65536)
    except BlockingIOError:
        return
    table = None
    for line in data.splitlines():
        try:
            request = json.loads(line)
            event_id, pid = request['id'], int(request['pid'])
            if not re.fullmatch(r'[a-f0-9]{32}', event_id):
                continue
            path = event_dir() / (event_id + '.json')
            if not path.exists():
                continue
            event = json.loads(path.read_text())
            table = process_table() if table is None else table
            if pid not in table:
                continue
            if event.get('armed') and event['shell']['birth'] != table[pid]['birth']:
                continue
            if not event.get('armed'):
                event.update(shell=table[pid], cwd=process_cwd(pid, event['cwd']), armed=time.time())
                if event.get('launch_recipe'):
                    event['launch_env'] = {k: str(v) for k, v in request.get('env', {}).items()
                                           if k in ('PATH', 'LANG', 'JAVA_HOME')}
            hint = event.get('docker_hint')
            if hint and hint['type'] == 'compose':
                cwd = Path(event['cwd'])
                hint['files'] = [str((cwd / f).resolve()) for f in hint['files']]
                if not hint['files']:
                    hint['files'] = [str((cwd / f).resolve()) for f in
                                     ('compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml')
                                     if (cwd / f).exists()][:1]
            save_event(event)
            # Before acknowledging the shell's final checkpoint, seed all still
            # attached children. The shell may exit/reparent them immediately.
            tracked[pid] = {'birth': table[pid]['birth'], 'event': event_id, 'root': True}
            changed = True
            while changed:
                changed = False
                for child_pid, item in table.items():
                    if child_pid not in tracked and item['ppid'] in tracked:
                        parent = tracked[item['ppid']]
                        tracked[child_pid] = {'birth': item['birth'], 'event': parent['event'], 'root': False}
                        changed = True
            engine.write('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                         ['capture_tracking', json.dumps({str(k): v for k, v in tracked.items()})])
            (channel_dir() / (event_id + '.ack')).touch(mode=0o600)
        except (ValueError, KeyError, OSError):
            continue


def event_dir():
    path = engine.ROOT / 'capture-events'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def save_event(event):
    path = event_dir() / (event['id'] + '.json')
    temporary = path.with_suffix('.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(event, ensure_ascii=False))
    temporary.replace(path)


def load_events():
    result = []
    for path in event_dir().glob('*.json'):
        try:
            result.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return result


def process_table():
    """No command arguments or environment are collected."""
    output = engine.command(['/bin/ps', '-axo', 'pid=,ppid=,lstart=,stat=,comm='], timeout=5)
    result = {}
    for line in output.splitlines():
        parts = line.split(None, 8)
        if len(parts) == 9 and parts[0].isdigit() and 'Z' not in parts[7]:
            pid = int(parts[0])
            result[pid] = {'pid': pid, 'ppid': int(parts[1]), 'birth': ' '.join(parts[2:7]),
                           'name': Path(parts[8]).name}
    return result


def listeners():
    # lsof exits 1 when no sockets match; that is an empty observation, not a failure.
    p = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP', '-sTCP:LISTEN', '-Fpn'],
                       capture_output=True, text=True, timeout=8)
    if p.returncode not in (0, 1):
        raise RuntimeError('无法观察本机监听端口')
    result, pid = {}, None
    for line in p.stdout.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            pid = int(line[1:]); result.setdefault(pid, set())
        elif pid and line.startswith('n'):
            match = re.search(r':(\d+)$', line)
            if match:
                result[pid].add(int(match.group(1)))
    return {pid: sorted(ports) for pid, ports in result.items()}


def process_cwd(pid, fallback):
    p = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
                       capture_output=True, text=True, timeout=3)
    for line in p.stdout.splitlines():
        if line.startswith('n') and Path(line[1:]).is_dir():
            return str(Path(line[1:]).resolve())
    return fallback


def docker_hints(command):
    """Only top-level Docker invocations are classified; unknown scripts aren't guessed."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words:
        return None
    while words and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0]):
        words.pop(0)
    if not words or Path(words[0]).name != 'docker':
        return None
    if len(words) >= 3 and words[1] == 'compose' and any(w in ('up', 'start', 'restart') for w in words[2:]):
        hint = {'type': 'compose', 'files': [], 'project': None}
        for i, word in enumerate(words[2:], 2):
            if word in ('-f', '--file') and i + 1 < len(words):
                hint['files'].append(words[i + 1])
            if word in ('-p', '--project-name') and i + 1 < len(words):
                hint['project'] = words[i + 1]
            if word.startswith('--file='):
                hint['files'].append(word.split('=', 1)[1])
            if word.startswith('--project-name='):
                hint['project'] = word.split('=', 1)[1]
        return hint
    if len(words) >= 2 and words[1] in ('run', 'start', 'restart'):
        return {'type': 'container', 'ids': []}
    return None


def ensure_collector():
    engine.database().close()
    with (engine.ROOT / 'capture-start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return ensure_collector_unlocked()


def ensure_collector_unlocked():
    metadata = engine.query('SELECT value FROM metadata WHERE key=?', ['capture'], True)
    state = json.loads(metadata['value']) if metadata else {}
    if engine.matches(state.get('pid'), state.get('birth'), 'capture.py'):
        return
    existing = engine.query('SELECT id,project_id,spec FROM services WHERE id=?', [INTERNAL_ID], True)
    if existing:
        if json.loads(existing['spec']).get('source') != 'internal':
            raise RuntimeError('自动采集组件标识已被其他服务占用')
        run = engine.latest(INTERNAL_ID)
        runtime = (run or {}).get('runtime', {})
        if engine.matches(runtime.get('child_pid'), runtime.get('child_birth')):
            return
        # Only reconcile our own internal record; never signal its old PID.
        if run and run['ended'] is None:
            engine.set_run(run['id'], ended=time.time(), state='exited', error='自动采集组件已退出')
        # Stable internal cwd allows either the installed hook or bundled GUI to
        # recover the same collector, even though their code paths are different.
        proj = engine.project(str(engine.ROOT))
        if existing['project_id'] != proj['id']:
            engine.write('UPDATE services SET project_id=? WHERE id=?', [proj['id'], INTERNAL_ID])
            if not engine.query('SELECT id FROM services WHERE project_id=?', [existing['project_id']]):
                engine.write('DELETE FROM projects WHERE id=?', [existing['project_id']])
    entry = Path(__file__).resolve().parents[1] / 'scripts/csm'
    p = subprocess.run([str(entry), '--root', str(engine.ROOT), 'launch',
                        '--id', INTERNAL_ID, '--name', '自动采集组件', '--source', 'internal',
                        '--cwd', str(engine.ROOT), '--task', '--',
                        sys.executable, str(Path(__file__).resolve()), '--root', str(engine.ROOT), 'collect'],
                       capture_output=True, text=True, timeout=12)
    if p.returncode:
        raise RuntimeError('自动采集组件启动失败：' + p.stdout[-500:])


def hook(payload):
    name = payload.get('hook_event_name')
    if payload.get('tool_name') != 'Bash':
        return {}
    key = str(payload.get('session_id', '')) + '\0' + str(payload.get('tool_use_id', ''))
    if not payload.get('tool_use_id'):
        return {}
    event_id = hashlib.sha256(key.encode()).hexdigest()[:32]
    tool_input = payload.get('tool_input') or {}
    command = tool_input.get('command') if isinstance(tool_input, dict) else None
    if name == 'PreToolUse' and isinstance(command, str):
        if ARM_MARKER in command:
            return {}
        ensure_collector()
        event = {'id': event_id, 'session_id': payload.get('session_id'),
                 'tool_use_id': payload.get('tool_use_id'), 'cwd': payload.get('cwd'),
                 'created': time.time(), 'docker_hint': docker_hints(command)}
        import adoption
        recipe = adoption.launch_recipe(command)
        if recipe:
            event['launch_recipe'] = recipe
        save_event(event)
        # Keep the ORIGINAL shell/command text and streams. A final checkpoint
        # captures direct background children before the shell returns; its private
        # variable preserves the original command's exit status. Explicit exec/exit
        # retain their native behaviour and skip the final checkpoint.
        prefix = ' '.join(shlex.quote(w) for w in [sys.executable, str(Path(__file__).resolve()),
                           '--root', str(engine.ROOT), 'arm', '--event', event_id])
        status_var = 'CSM_CAPTURE_STATUS_' + event_id
        rewritten = ARM_MARKER + '\n' + prefix + ' --pid "$$" >/dev/null 2>&1 || :\n' + command + '\n' + \
            status_var + '=$?\n' + prefix + ' --pid "$$" >/dev/null 2>&1 || :\nexit "$' + status_var + '"'
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                      'updatedInput': {'command': rewritten}}}
    if name == 'PostToolUse':
        path = event_dir() / (event_id + '.json')
        if path.exists():
            event = json.loads(path.read_text())
            event['finished'] = time.time()
            if (event.get('docker_hint') or {}).get('type') == 'container':
                output = payload.get('tool_response')
                if isinstance(output, dict):
                    output = output.get('output', output.get('stdout', ''))
                if isinstance(output, str):
                    # Returned container IDs only; never persist general tool output.
                    event['docker_hint']['ids'] = re.findall(r'(?m)^([a-f0-9]{12,64})\s*$', output)
            save_event(event)
    return {}


def arm(event_id, pid):
    if not re.fullmatch(r'[a-f0-9]{32}', event_id):
        raise ValueError('无效启动上下文标识')
    message = json.dumps({'id': event_id, 'pid': os.getppid(),
                          'env': {k: os.environ[k] for k in ('PATH', 'LANG', 'JAVA_HOME') if k in os.environ}}).encode() + b'\n'
    fd = os.open(channel_dir() / 'arm.fifo', os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        os.write(fd, message)
    finally:
        os.close(fd)
    # Establish the root before allowing the original command to run. Only temp
    # files are accessed here; process-table reads happen in the trusted collector.
    ack = channel_dir() / (event_id + '.ack')
    until = time.time() + 2
    while time.time() < until:
        if ack.exists():
            ack.unlink()
            break
        time.sleep(.05)


def managed_processes():
    result = set()
    for row in engine.query('SELECT id,spec FROM services'):
        if json.loads(row['spec']).get('management') == 'observed':
            continue
        runtime = (engine.latest(row['id']) or {}).get('runtime', {})
        if engine.matches(runtime.get('supervisor_pid'), runtime.get('supervisor_birth')):
            result.update(engine.group_members(runtime.get('pgid', 0)))
    return result


def record_process(event, item, ports):
    cwd = process_cwd(item['pid'], event['cwd'])
    # Runtime cwd may be a shared temporary directory: retain authoritative launch project.
    proj = engine.project(event['cwd'])
    fingerprint = '\0'.join([proj['id'], item['name'], cwd, ','.join(map(str, ports))])
    sid = 'observed-' + hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
    current = engine.latest(sid)
    if current and current['ended'] is None and not engine.matches(current['runtime'].get('pid'), current['runtime'].get('birth')):
        engine.set_run(current['id'], ended=time.time(), state='exited')
        current = engine.latest(sid)
    if current and current['ended'] is None and current['runtime'].get('pid') != item['pid']:
        sid += '-' + str(item['pid'])
        current = engine.latest(sid)
    spec = {'cwd': cwd, 'ports': ports, 'source': 'global_hook', 'management': 'observed',
            'session_id': event.get('session_id'), 'tool_use_id': event.get('tool_use_id'),
            'association': '已确认启动 shell 的后代', 'url': None}
    name = item['name'] + (' · ' + ', '.join(map(str, ports)) if ports else ' · 后台任务')
    # Observed metadata may gain ports while the process is running; no launch config exists.
    engine.write('INSERT INTO services(id,project_id,name,kind,spec) VALUES (?,?,?,?,?) '
                 'ON CONFLICT(id) DO UPDATE SET name=excluded.name,spec=excluded.spec',
                 [sid, proj['id'], name, 'process' if ports else 'task', json.dumps(spec)])
    runtime = {'pid': item['pid'], 'birth': item['birth'], 'ports': ports}
    if not current or current['ended'] is not None or current['runtime'].get('birth') != item['birth']:
        if current and current['ended'] is None:
            engine.set_run(current['id'], ended=time.time(), state='exited')
        run_id = engine.new_run(sid, runtime)
    else:
        run_id = current['id']
    engine.set_run(run_id, state='running', runtime=runtime)


def record_container(event, obj):
    cid = obj['Id']
    for row in engine.query('SELECT id,spec FROM services WHERE kind IN (?,?)', ['container', 'compose']):
        spec = json.loads(row['spec'])
        if spec.get('container_id') == cid:
            if spec.get('management') == 'observed':
                engine.reconcile_docker(engine.service(row['id']), [obj])
            return
        labels = obj['Config'].get('Labels') or {}
        if spec.get('management') != 'observed' and spec.get('compose_project') == labels.get('com.docker.compose.project') and spec.get('member') == labels.get('com.docker.compose.service'):
            return
    proj = engine.project(event['cwd'])
    labels = obj['Config'].get('Labels') or {}
    group = labels.get('com.docker.compose.project')
    sid = 'observed-docker-' + hashlib.sha256(cid.encode()).hexdigest()[:24]
    spec = {'cwd': event['cwd'], 'container_id': cid, 'source': 'global_hook', 'management': 'observed',
            'session_id': event.get('session_id'), 'tool_use_id': event.get('tool_use_id'),
            'association': '工具返回容器 ID' if not group else 'Compose 启动配置与实际标签匹配',
            'url': None}
    engine.write('INSERT INTO services(id,project_id,name,kind,spec,group_id) VALUES (?,?,?,?,?,?) '
                 'ON CONFLICT(id) DO UPDATE SET spec=excluded.spec',
                 [sid, proj['id'], obj['Name'].lstrip('/'), 'container', json.dumps(spec),
                  'observed-' + proj['id'] + '-' + group if group else None])
    item = engine.service(sid)
    engine.reconcile_docker(item, [obj])


def collect_containers(events):
    candidates = [e for e in events if e.get('armed') and e.get('docker_hint') and
                  time.time() - e.get('finished', e['armed']) < 120]
    if not candidates:
        return
    objects = []
    ids = engine.docker('ps', '-aq', timeout=3).splitlines()
    if ids:
        objects = engine.container_observation(*ids)
    for event in candidates:
        hint = event['docker_hint']
        for obj in objects:
            if hint['type'] == 'container':
                if any(obj['Id'].startswith(cid) for cid in hint.get('ids', [])):
                    record_container(event, obj)
                continue
            labels = obj['Config'].get('Labels') or {}
            files = {str(Path(f).resolve()) for f in (labels.get('com.docker.compose.project.config_files') or '').split(',') if f}
            if not hint.get('files') or files != set(hint['files']):
                continue
            if hint.get('project') and labels.get('com.docker.compose.project') != hint['project']:
                continue
            started = engine.docker_time(obj['State'].get('StartedAt'))
            if started and started >= event['armed'] - 1:
                record_container(event, obj)


def collect_once(tracked, first_seen):
    events = [e for e in load_events() if e.get('shell')]
    table = process_table()
    by_id = {e['id']: e for e in events}
    # Keep only verified identities; a reused PID cannot inherit the prior association.
    tracked = {pid: info for pid, info in tracked.items() if pid in table and table[pid]['birth'] == info['birth']}
    for event in events:
        shell = event['shell']
        if shell['pid'] in table and table[shell['pid']]['birth'] == shell['birth']:
            tracked.setdefault(shell['pid'], {'birth': shell['birth'], 'event': event['id'], 'root': True})
    changed = True
    while changed:
        changed = False
        for pid, item in table.items():
            if pid not in tracked and item['ppid'] in tracked:
                parent = tracked[item['ppid']]
                tracked[pid] = {'birth': item['birth'], 'event': parent['event'], 'root': False}
                changed = True
    ports = listeners()
    managed = managed_processes()
    parents = {p['ppid'] for p in table.values()}
    for pid, info in tracked.items():
        if pid in managed or pid == os.getpid() or info['event'] not in by_id:
            continue
        key = (pid, info['birth'])
        first_seen.setdefault(key, time.time())
        # Listener is the service unit; portless wrapper/build trees are not duplicated.
        if ports.get(pid) or (pid not in parents and table[pid]['name'] not in ('bash', 'zsh', 'sh') and time.time() - first_seen[key] >= 30):
            if table[pid]['name'] in ('lsof', 'ps'):
                continue
            # Deduplicate the same actual process even when its port set changes.
            rows = engine.query('SELECT s.id,r.runtime FROM services s JOIN runs r ON r.service_id=s.id '
                                'WHERE r.ended IS NULL AND s.spec LIKE ?', ['%"management": "observed"%'])
            existing = next((r for r in rows if json.loads(r['runtime']).get('pid') == pid and
                             json.loads(r['runtime']).get('birth') == info['birth']), None)
            if existing:
                item = engine.service(existing['id']); item['spec']['ports'] = ports.get(pid, [])
                name = table[pid]['name'] + (' · ' + ', '.join(map(str, ports[pid])) if ports.get(pid) else ' · 后台任务')
                engine.write('UPDATE services SET spec=?,name=?,kind=? WHERE id=?',
                             [json.dumps(item['spec']), name, 'process' if ports.get(pid) else 'task', item['id']])
            else:
                with engine.mutation_lock():
                    record_process(by_id[info['event']], table[pid], ports.get(pid, []))
    # Persist verified descendants so collector recovery can follow reparented services.
    saved = {str(pid): value for pid, value in tracked.items()}
    engine.write('INSERT OR REPLACE INTO metadata VALUES (?,?)', ['capture_tracking', json.dumps(saved)])
    # A recipe is attached only after the actual single-service launcher and all
    # descendants are known. Existing explicit configurations are preserved.
    import adoption
    for row in engine.query('SELECT id FROM services WHERE spec LIKE ?', ['%"management": "observed"%']):
        item = engine.service(row['id'])
        run = engine.latest(item['id'])
        if item['kind'] not in ('process', 'task') or not run or run['ended'] is not None or run['state'] != 'running':
            continue
        info = tracked.get(run['runtime'].get('pid')) or {}
        event = by_id.get(info.get('event'), {})
        if run['runtime'].get('launch_members') and (item['spec'].get('argv') or not event.get('launch_recipe')):
            continue
        try:
            with engine.mutation_lock():
                # Re-read under the same lock used by start/configure.
                item = engine.service(row['id'])
                if item['spec'].get('management') == 'observed':
                    plan = adoption.stop_plan(item, table, saved, ports)
                    adoption.remember_scope(item, plan)
                    if not item['spec'].get('argv'):
                        adoption.remember_recipe(item, plan, event)
        except (RuntimeError, ValueError):
            pass
    first_seen = {key: value for key, value in first_seen.items() if key[0] in tracked}
    try:
        collect_containers(events)
        docker_error = None
    except Exception as exc:
        docker_error = str(exc)[-500:]
    # Reconcile ended observed runs even with no UI open.
    for row in engine.query('SELECT id,spec FROM services'):
        if json.loads(row['spec']).get('management') == 'observed':
            engine.observe(row['id'])
    return tracked, first_seen, docker_error


def collect():
    engine.database().close()
    with (engine.ROOT / 'capture.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        metadata = engine.query('SELECT value FROM metadata WHERE key=?', ['capture_tracking'], True)
        tracked = {int(k): v for k, v in json.loads(metadata['value']).items()} if metadata else {}
        first_seen = {}
        own = engine.identity(os.getpid())
        channel = open_channel()
        while True:
            error = None
            try:
                receive_arms(channel, tracked)
                tracked, first_seen, error = collect_once(tracked, first_seen)
            except Exception as exc:
                error = str(exc)[-500:]
            engine.write('INSERT OR REPLACE INTO metadata VALUES (?,?)', ['capture', json.dumps({
                'pid': os.getpid(), 'birth': own['birth'], 'updated': time.time(), 'error': error,
                'tracked': len(tracked), 'event_count': len(load_events())})])
            time.sleep(.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path)
    sub = parser.add_subparsers(dest='mode', required=True)
    sub.add_parser('hook'); sub.add_parser('collect'); sub.add_parser('ensure')
    armed = sub.add_parser('arm'); armed.add_argument('--event', required=True); armed.add_argument('--pid', required=True, type=int)
    args = parser.parse_args()
    if args.root:
        engine.ROOT = args.root.expanduser().resolve()
    if args.mode == 'hook':
        try:
            result = hook(json.load(sys.stdin))
        except Exception as exc:
            result = {'systemMessage': '服务自动采集接入失败：' + str(exc)[-300:]}
        print(json.dumps(result, ensure_ascii=False))
    elif args.mode == 'arm':
        arm(args.event, args.pid)
    elif args.mode == 'ensure':
        ensure_collector()
    else:
        collect()


if __name__ == '__main__':
    main()
