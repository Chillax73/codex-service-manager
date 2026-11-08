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
import storage

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


def event_project(event, projects=None):
    cwd = event.get('cwd')
    if not cwd:
        return None
    canonical = str(Path(cwd).expanduser().resolve())
    if projects is not None:
        return projects.get(canonical)
    return engine.query('SELECT * FROM projects WHERE cwd=?', [canonical], True)


def scan_allowed(current, previous):
    if current and not current['auto_scan']:
        return False
    if previous is None:
        # A new project may have been registered by a different event in this cycle.
        return current is None or current['observation_revision'] == 0
    return engine.observation_token(current) == engine.observation_token(previous)


def extend_tracking(table, tracked):
    changed = True
    while changed:
        changed = False
        for pid, item in table.items():
            if pid not in tracked and item['ppid'] in tracked:
                parent = tracked[item['ppid']]
                tracked[pid] = {'birth': item['birth'], 'event': parent['event'], 'root': False}
                changed = True


def save_tracking(tracked):
    saved = {str(pid): value for pid, value in tracked.items()}
    engine.write('INSERT OR REPLACE INTO metadata VALUES (?,?)', ['capture_tracking', json.dumps(saved)])
    return saved


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
            storage.private_file(path)
            event = json.loads(path.read_text())
            previous = event_project(event)
            if previous and not previous['auto_scan']:
                (channel_dir() / (event_id + '.ack')).touch(mode=0o600)
                continue
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
            canonical = str(Path(event['cwd']).expanduser().resolve())
            with engine.database() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM projects WHERE cwd=?', [canonical]).fetchone()
                if scan_allowed(dict(row) if row else None, previous):
                    # Seed children before the shell can exit and reparent them.
                    branch = {child: info for child, info in tracked.items()
                              if info['event'] == event_id and child in table and
                              info['birth'] == table[child]['birth']}
                    branch[pid] = {'birth': table[pid]['birth'], 'event': event_id, 'root': True}
                    extend_tracking(table, branch)
                    for child, info in branch.items():
                        if child not in tracked or tracked[child]['birth'] != info['birth']:
                            tracked[child] = info
                    tracked[pid] = branch[pid]
                    save_event(event)
                    db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                               ['capture_tracking', json.dumps({str(k): v for k, v in tracked.items()})])
            (channel_dir() / (event_id + '.ack')).touch(mode=0o600)
        except (ValueError, KeyError, OSError, RuntimeError):
            continue


def event_dir():
    return storage.private_directory(engine.ROOT / 'capture-events')


def save_event(event):
    path = event_dir() / (event['id'] + '.json')
    storage.save_json(path, event)


def load_events():
    result = []
    for path in event_dir().glob('*.json'):
        try:
            storage.private_file(path)
            result.append(json.loads(path.read_text()))
        except (OSError, ValueError, RuntimeError):
            continue
    return result


def disabled_projects():
    row = engine.query('SELECT value FROM metadata WHERE key=?', ['capture_disabled_cwds'], True)
    return json.loads(row['value']) if row else []


def set_policy(cwd, enabled):
    if type(enabled) is not bool:
        raise ValueError('采集开关必须是布尔值')
    path = Path(cwd).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError('工作目录不是文件夹')
    disabled = set(disabled_projects())
    disabled.discard(str(path)) if enabled else disabled.add(str(path))
    engine.write('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                 ['capture_disabled_cwds', json.dumps(sorted(disabled))])
    return {'cwd': str(path), 'enabled': enabled}


def record_failure(stage, exc):
    # Exception text can include argv, URLs or subprocess output. Never persist it.
    diagnostic = {'stage': stage, 'type': type(exc).__name__, 'at': time.time()}
    try:
        engine.write('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                     ['capture_diagnostic', json.dumps(diagnostic)])
    except Exception:
        pass  # A broken state directory must not prevent the user's shell command.
    return '服务自动采集失败（' + stage + ' / ' + type(exc).__name__ + '）；请检查采集状态或改用显式登记'


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
        if str(Path(payload['cwd']).expanduser().resolve()) in disabled_projects():
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
                                      'updatedInput': {**tool_input, 'command': rewritten}}}
    if name == 'PostToolUse':
        path = event_dir() / (event_id + '.json')
        if path.exists():
            storage.private_file(path)
            event = json.loads(path.read_text())
            event['finished'] = time.time()
            project = event_project(event)
            if not event.get('armed') and (not project or project['auto_scan']):
                record_failure('checkpoint', TimeoutError())
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
    for row in engine.query('SELECT s.id,s.spec FROM services s JOIN projects p ON p.id=s.project_id WHERE p.auto_scan=1'):
        if json.loads(row['spec']).get('management') == 'observed':
            continue
        runtime = (engine.latest(row['id']) or {}).get('runtime', {})
        if engine.matches(runtime.get('supervisor_pid'), runtime.get('supervisor_birth')):
            result.update(engine.group_members(runtime.get('pgid', 0)))
    return result


def record_process(event, item, ports, cwd=None, table=None):
    if cwd is None:
        cwd = process_cwd(item['pid'], event['cwd'])
    # Runtime cwd may be a shared temporary directory: retain authoritative launch project.
    proj = engine.project(event['cwd'])
    fingerprint = '\0'.join([proj['id'], item['name'], cwd, ','.join(map(str, ports))])
    sid = 'observed-' + hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
    current = engine.latest(sid)
    def still_live(runtime):
        if table is None:
            return engine.matches(runtime.get('pid'), runtime.get('birth'))
        entry = table.get(runtime.get('pid'))
        return bool(entry and entry['birth'] == runtime.get('birth'))

    if current and current['ended'] is None and not still_live(current['runtime']):
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
    engine.write("INSERT INTO services(id,project_id,name,kind,spec,created_at,created_at_source) "
                 "VALUES (?,?,?,?,?,?,'registered') "
                 'ON CONFLICT(id) DO UPDATE SET name=excluded.name,spec=excluded.spec',
                 [sid, proj['id'], name, 'process' if ports else 'task', json.dumps(spec), engine.timestamp()])
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
    for row in engine.query('SELECT s.id,s.spec,p.auto_scan,p.cwd FROM services s JOIN projects p '
                            'ON p.id=s.project_id WHERE kind IN (?,?)', ['container', 'compose']):
        spec = json.loads(row['spec'])
        if spec.get('container_id') == cid:
            if row['auto_scan'] and row['cwd'] == str(Path(event['cwd']).resolve()) and spec.get('management') == 'observed':
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
    engine.write("INSERT INTO services(id,project_id,name,kind,spec,group_id,created_at,created_at_source) "
                 "VALUES (?,?,?,?,?,?,?,'registered') "
                 'ON CONFLICT(id) DO UPDATE SET spec=excluded.spec',
                 [sid, proj['id'], obj['Name'].lstrip('/'), 'container', json.dumps(spec),
                  'observed-' + proj['id'] + '-' + group if group else None, engine.timestamp()])
    item = engine.service(sid)
    engine.reconcile_docker(item, [obj])


def collect_containers(events, policies):
    candidates = [e for e in events if e.get('armed') and e.get('docker_hint') and
                  time.time() - e.get('finished', e['armed']) < 120]
    if not candidates:
        return
    paused_ids = set()
    for row in engine.query('SELECT s.id,s.spec FROM services s JOIN projects p ON p.id=s.project_id '
                            "WHERE p.auto_scan=0 AND s.kind IN ('container','compose')"):
        spec = json.loads(row['spec'])
        if spec.get('container_id'):
            paused_ids.add(spec['container_id'])
        paused_ids.update(((engine.latest(row['id']) or {}).get('runtime') or {}).get('container_ids', []))
    for event in candidates:
        previous = policies[event['id']]
        if not scan_allowed(event_project(event), previous):
            continue
        hint = event['docker_hint']
        if hint['type'] == 'container':
            ids = hint.get('ids', [])
        else:
            files = hint.get('files', [])
            if not files:
                continue
            ids = engine.docker('ps', '-aq', '--filter',
                                'label=com.docker.compose.project.config_files=' + ','.join(files), timeout=3).splitlines()
        ids = [cid for cid in ids if not any(known.startswith(cid) for known in paused_ids)]
        for obj in engine.container_observation(*ids) if ids else []:
            if hint['type'] != 'container':
                labels = obj['Config'].get('Labels') or {}
                files = {str(Path(f).resolve()) for f in (labels.get('com.docker.compose.project.config_files') or '').split(',') if f}
                if files != set(hint['files']):
                    continue
                if hint.get('project') and labels.get('com.docker.compose.project') != hint['project']:
                    continue
                started = engine.docker_time(obj['State'].get('StartedAt'))
                if not started or started < event['armed'] - 1:
                    continue
            if not obj['State']['Running']:
                # A historical event is not evidence of a currently discoverable service.
                continue
            try:
                with engine.mutation_lock():
                    if scan_allowed(event_project(event), previous):
                        record_container(event, obj)
            except engine.MutationBusy:
                continue


def update_observed_process(event, previous, item, ports, table):
    pid = item['pid']
    cwd = process_cwd(pid, event['cwd'])
    with engine.mutation_lock():
        if not scan_allowed(event_project(event), previous):
            return
        rows = engine.query('SELECT s.id,r.runtime,p.auto_scan FROM services s JOIN runs r ON r.service_id=s.id '
                            'JOIN projects p ON p.id=s.project_id '
                            'WHERE r.ended IS NULL AND s.spec LIKE ?', ['%"management": "observed"%'])
        existing = next((row for row in rows if json.loads(row['runtime']).get('pid') == pid and
                         json.loads(row['runtime']).get('birth') == item['birth']), None)
        if existing is None:
            record_process(event, item, ports, cwd, table)
        elif existing['auto_scan']:
            registered = engine.service(existing['id'])
            # A duplicate event must not mutate a service owned by a different project.
            owner = engine.observation_row(registered['project_id'])
            if previous is not None and engine.observation_token(owner) != engine.observation_token(previous):
                return
            if previous is None and event_project(event) is None:
                return
            registered['spec']['ports'] = ports
            name = item['name'] + (' · ' + ', '.join(map(str, ports)) if ports else ' · 后台任务')
            engine.write('UPDATE services SET spec=?,name=?,kind=? WHERE id=?',
                         [json.dumps(registered['spec']), name, 'process' if ports else 'task', registered['id']])


def collect_once(tracked, first_seen):
    events = [e for e in load_events() if e.get('shell')]
    projects = {p['cwd']: p for p in engine.query('SELECT * FROM projects')}
    policies = {e['id']: event_project(e, projects) for e in events}
    by_id = {e['id']: e for e in events
             if policies[e['id']] is None or policies[e['id']]['auto_scan']}
    enabled_observed = engine.query(
        'SELECT s.id,s.spec,s.project_id FROM services s JOIN projects p ON p.id=s.project_id '
        'WHERE p.auto_scan=1 AND s.spec LIKE ?', ['%"management": "observed"%'])
    by_project = {p['id']: p for p in projects.values()}
    prior = tracked
    dormant = {pid: info for pid, info in tracked.items()
               if info['event'] in policies and info['event'] not in by_id}
    tracked = {}
    table, ports = {}, {}
    if by_id:
        table = process_table()
        # Keep paused lineage dormant so recovery can verify the same births on resume.
        tracked = {pid: info for pid, info in prior.items()
                   if info['event'] in by_id and pid in table and table[pid]['birth'] == info['birth']}
        for event in by_id.values():
            shell = event['shell']
            if shell['pid'] in table and table[shell['pid']]['birth'] == shell['birth']:
                tracked.setdefault(shell['pid'], {'birth': shell['birth'], 'event': event['id'], 'root': True})
        extend_tracking(table, tracked)
        if tracked:
            ports = listeners()
            managed = managed_processes()
            parents = {p['ppid'] for p in table.values()}
            for pid, info in tracked.items():
                if pid in managed or pid == os.getpid() or table[pid]['name'] in ('lsof', 'ps'):
                    continue
                key = (pid, info['birth'])
                first_seen.setdefault(key, time.time())
                long_lived = pid not in parents and table[pid]['name'] not in ('bash', 'zsh', 'sh') and time.time() - first_seen[key] >= 30
                if ports.get(pid) or long_lived:
                    try:
                        update_observed_process(by_id[info['event']], policies[info['event']],
                                                table[pid], ports.get(pid, []), table)
                    except engine.MutationBusy:
                        continue
    try:
        with engine.mutation_lock():
            allowed = {eid for eid, event in by_id.items() if scan_allowed(event_project(event), policies[eid])}
            dormant.update({pid: info for pid, info in prior.items()
                            if info['event'] in by_id and info['event'] not in allowed})
            tracked = {**dormant, **{pid: info for pid, info in tracked.items() if info['event'] in allowed}}
            saved = save_tracking(tracked)
    except engine.MutationBusy:
        return prior, first_seen, None

    # Persist launch scope and verified recipes only against the same project and run.
    import adoption
    for row in enabled_observed:
        try:
            item = engine.service(row['id'])
        except ValueError:
            continue  # It may have been deleted while collecting other services.
        run = engine.latest(item['id'])
        if item['kind'] not in ('process', 'task') or not run or run['ended'] is not None or run['state'] != 'running':
            continue
        info = tracked.get(run['runtime'].get('pid')) or {}
        event = by_id.get(info.get('event'))
        if not event or (run['runtime'].get('launch_members') and (item['spec'].get('argv') or not event.get('launch_recipe'))):
            continue
        previous = by_project.get(row['project_id'])
        if not scan_allowed(engine.observation_row(row['project_id']), previous):
            continue
        try:
            plan = adoption.stop_plan(item, table, saved, ports)
            with engine.mutation_lock():
                if (scan_allowed(engine.observation_row(row['project_id']), previous) and
                        scan_allowed(event_project(event), policies[event['id']]) and
                        engine.service(item['id']) == item and engine.latest(item['id']) == run):
                    adoption.remember_scope(item, plan)
                    if not item['spec'].get('argv'):
                        adoption.remember_recipe(item, plan, event)
        except (RuntimeError, ValueError):
            pass

    first_seen = {key: value for key, value in first_seen.items() if key[0] in tracked}
    try:
        collect_containers(by_id.values(), policies)
        docker_error = None
    except Exception as exc:
        docker_error = record_failure('docker', exc)
    for row in enabled_observed:
        if json.loads(row['spec']).get('management') == 'observed':
            try:
                expected = engine.observation_token(by_project.get(row['project_id']))
                engine.observe(row['id'], automatic=True, expected=expected)
            except ValueError:
                if engine.query('SELECT id FROM services WHERE id=?', [row['id']], True):
                    raise
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
                error = record_failure('collector', exc)
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
            result = {'systemMessage': record_failure('hook', exc)}
        print(json.dumps(result, ensure_ascii=False))
    elif args.mode == 'arm':
        arm(args.event, args.pid)
    elif args.mode == 'ensure':
        ensure_collector()
    else:
        collect()


if __name__ == '__main__':
    main()
