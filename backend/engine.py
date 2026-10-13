#!/usr/bin/env python3
"""Local service engine. JSON RPC is shared by the native app and companion CLI."""
import argparse
import concurrent.futures
import contextlib
import datetime
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import unicodedata
import urllib.parse
import uuid

os.umask(0o077)
DEFAULT_ROOT = Path.home() / 'Library/Application Support/Codex Service Manager'
ROOT = Path(os.environ.get('CSM_STATE_DIR', str(DEFAULT_ROOT))).expanduser().resolve()
LOG_LIMIT = 1024 * 1024
ACTIVE = ('starting', 'running', 'stopping', 'stop_failed', 'unknown')


def timestamp():
    return time.time()


def uid():
    return uuid.uuid4().hex


def database():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(str(ROOT / 'manager.sqlite'), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, cwd TEXT UNIQUE NOT NULL,
      name TEXT NOT NULL, manual INTEGER NOT NULL DEFAULT 1, codex_exit INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS services(id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
      name TEXT NOT NULL, kind TEXT NOT NULL, spec TEXT NOT NULL, keep INTEGER NOT NULL DEFAULT 0,
      group_id TEXT);
    CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, service_id TEXT NOT NULL,
      started REAL NOT NULL, ended REAL, state TEXT NOT NULL, exit_code INTEGER,
      runtime TEXT NOT NULL DEFAULT '{}', error TEXT, log TEXT);
    CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, target TEXT NOT NULL,
      action TEXT NOT NULL, trigger TEXT NOT NULL, started REAL NOT NULL, ended REAL,
      result TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS runs_service ON runs(service_id, started DESC);
    ''')
    return db


def query(sql, values=(), one=False):
    with database() as db:
        rows = [dict(r) for r in db.execute(sql, values)]
    return (rows[0] if rows else None) if one else rows


def write(sql, values=()):
    with database() as db:
        db.execute(sql, values)


def set_run(run_id, **fields):
    if 'runtime' in fields:
        fields['runtime'] = json.dumps(fields['runtime'])
    write('UPDATE runs SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
          list(fields.values()) + [run_id])


def latest(service_id):
    item = query('SELECT * FROM runs WHERE service_id=? ORDER BY started DESC LIMIT 1', [service_id], True)
    if item:
        item['runtime'] = json.loads(item['runtime'])
    return item


def service(service_id):
    item = query('SELECT * FROM services WHERE id=?', [service_id], True)
    if not item:
        raise ValueError('服务不存在：' + service_id)
    item['spec'] = json.loads(item['spec'])
    return item


@contextlib.contextmanager
def mutation_lock():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / 'mutation.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('另一个操作正在进行，请等待完成后重试')
        yield


def identity(pid):
    if not pid:
        return None
    p = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'stat=', '-o', 'command='],
                       capture_output=True, text=True, timeout=3)
    fields = p.stdout.strip().split(None, 6)
    if p.returncode or len(fields) < 7 or 'Z' in fields[5]:
        return None
    return {'birth': ' '.join(fields[:5]), 'command': fields[6]}


def matches(pid, birth, token=None):
    item = identity(pid)
    return bool(item and item['birth'] == birth and (token is None or token in item['command']))


def group_members(pgid):
    p = subprocess.run(['/bin/ps', '-axo', 'pid=,pgid=,stat='], capture_output=True, text=True, timeout=3)
    if p.returncode:
        raise RuntimeError('无法核对进程组')
    return [int(f[0]) for line in p.stdout.splitlines() for f in [line.split()]
            if len(f) == 3 and int(f[1]) == pgid and 'Z' not in f[2]]


def command(argv, env=None, cwd=None, timeout=30, merge_stderr=False):
    p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                       text=True, errors='replace', timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or '命令失败').strip()[-4000:])
    return p.stdout.strip()


def docker(*args, env=None, cwd=None, timeout=30, merge_stderr=False):
    executable = '/usr/local/bin/docker'
    if not Path(executable).exists():
        executable = 'docker'
    return command([executable, *args], env=env, cwd=cwd, timeout=timeout, merge_stderr=merge_stderr)


def project(cwd, name=None):
    path = str(Path(cwd).expanduser().resolve(strict=True))
    if not Path(path).is_dir():
        raise ValueError('工作目录不是文件夹')
    found = query('SELECT * FROM projects WHERE cwd=?', [path], True)
    if found:
        return found
    item_id = hashlib.sha256(path.encode()).hexdigest()[:24]
    write('INSERT INTO projects(id,cwd,name) VALUES (?,?,?)', [item_id, path, name or Path(path).name])
    return query('SELECT * FROM projects WHERE id=?', [item_id], True)


def environment(explicit):
    return {**{k: os.environ[k] for k in ('PATH', 'LANG', 'JAVA_HOME') if k in os.environ},
            **{str(k): str(v) for k, v in (explicit or {}).items()}}


def runtime_env(spec):
    return {**os.environ, **spec.get('env', {})}


def health(url):
    if not url:
        return 'not_configured'
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return 'available' if 200 <= response.status < 400 else 'unavailable'
    except Exception:
        return 'unavailable'


def new_run(service_id, runtime=None, started=None):
    run_id = uid()
    log = str(ROOT / 'logs' / (run_id + '.log'))
    (ROOT / 'logs').mkdir(exist_ok=True, mode=0o700)
    write('INSERT INTO runs(id,service_id,started,state,runtime,log) VALUES (?,?,?,?,?,?)',
          [run_id, service_id, started if started is not None else timestamp(), 'starting', json.dumps(runtime or {}), log])
    return run_id


def record_spec(item_id, proj, name, kind, spec, group=None):
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', item_id):
        raise ValueError('服务 ID 仅允许字母、数字、点、下划线和连字符')
    prior = query('SELECT * FROM services WHERE id=?', [item_id], True)
    if prior and prior['project_id'] != proj['id']:
        raise ValueError('服务 ID 已属于其他项目')
    if prior and latest(item_id) and latest(item_id)['ended'] is None:
        if json.loads(prior['spec']) != spec:
            raise ValueError('运行期间不能覆盖启动配置，请先停止')
        return service(item_id)
    write('INSERT INTO services(id,project_id,name,kind,spec,group_id) VALUES (?,?,?,?,?,?) '
          'ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,spec=excluded.spec,group_id=excluded.group_id',
          [item_id, proj['id'], name, kind, json.dumps(spec), group])
    return service(item_id)


def launch(req):
    proj = project(req.get('cwd', os.getcwd()), req.get('project_name'))
    argv = req.get('argv')
    if not argv and req.get('command'):
        argv = shlex.split(req['command'])
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise ValueError('启动配置必须包含非空 argv 数组')
    kind = req.get('kind', 'process')
    if kind not in ('process', 'task'):
        raise ValueError('本机启动类型必须为 process/task')
    name = req.get('name') or Path(argv[0]).name
    item_id = req.get('id') or hashlib.sha256((proj['id'] + '\0' + name).encode()).hexdigest()[:24]
    spec = {'argv': argv, 'cwd': proj['cwd'], 'env': environment(req.get('env')), 'url': req.get('url'),
            'ports': [int(port) for port in req.get('ports', [])], 'source': req.get('source', 'companion')}
    record_spec(item_id, proj, name, kind, spec)
    return operate(item_id, 'start', 'launch')


def port_free(port):
    # macOS permits some SO_REUSEADDR wildcard/specific-address combinations;
    # a successful loopback bind is not evidence that no listener exists.
    probe = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:' + str(int(port)), '-sTCP:LISTEN', '-t'],
                           capture_output=True, text=True, timeout=5)
    if probe.stdout.strip() or probe.returncode not in (0, 1):
        return False
    for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
        try:
            with socket.socket(family) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((address, port))
                sock.listen(1)
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL):
                continue
            return False
    return True


def start_process(item):
    spec = item['spec']
    occupied = [p for p in spec.get('ports', []) if not port_free(p)]
    run_id = new_run(item['id'])
    if occupied:
        set_run(run_id, ended=timestamp(), state='failed', error='端口已占用：' + str(occupied))
        raise RuntimeError('端口已占用：' + str(occupied))
    with open(ROOT / 'supervisor.log', 'ab', buffering=0) as output:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--root', str(ROOT),
            '_supervise', run_id], stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, start_new_session=True, close_fds=True)
    until = timestamp() + 8
    while timestamp() < until:
        run = latest(item['id'])
        if run['state'] != 'starting':
            if run['state'] == 'failed':
                raise RuntimeError(run['error'] or '启动失败，请查看日志')
            return observe(item['id'])
        if process.poll() is not None:
            set_run(run_id, ended=timestamp(), state='failed', error='监督进程在启动确认之前退出')
            raise RuntimeError('监督进程启动失败')
        time.sleep(.05)
    raise TimeoutError('监督器启动超时，记录保留；请检查状态')


def stop_process(item, force=False):
    run = latest(item['id'])
    if not run or run['ended'] is not None:
        return observe(item['id'])
    runtime = run['runtime']
    if not matches(runtime.get('supervisor_pid'), runtime.get('supervisor_birth'), run['id']):
        raise RuntimeError('监督进程身份无法确认，拒绝向旧 PID 发送信号')
    set_run(run['id'], state='stopping')
    os.kill(runtime['supervisor_pid'], signal.SIGUSR1 if force else signal.SIGTERM)
    until = timestamp() + 9
    while timestamp() < until:
        current = latest(item['id'])
        if current['ended'] is not None:
            return observe(item['id'])
        if current['state'] == 'stop_failed':
            raise RuntimeError(current['error'])
        time.sleep(.08)
    raise TimeoutError('停止超时；未向身份不明的对象发送信号')


def rotate_write(path, data):
    if path.exists() and path.stat().st_size + len(data) > LOG_LIMIT:
        oldest = Path(str(path) + '.2')
        oldest.unlink(missing_ok=True)
        previous = Path(str(path) + '.1')
        if previous.exists():
            previous.replace(oldest)
        path.replace(previous)
    with path.open('ab', buffering=0) as output:
        output.write(data)


def supervise(run_id):
    run = query('SELECT * FROM runs WHERE id=?', [run_id], True)
    item = service(run['service_id'])
    spec = item['spec']
    pending = [None]
    def handle(signum, frame):
        pending[0] = 'force' if signum == signal.SIGUSR1 else 'stop'
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
        signal.signal(sig, handle)
    child = None
    stopped = False
    try:
        own = identity(os.getpid())
        # Supervision owns Popen until wait(), so the group leader cannot be reused while we signal it.
        child = subprocess.Popen(spec['argv'], cwd=spec['cwd'], env=runtime_env(spec), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        born = identity(child.pid)
        runtime = {'supervisor_pid': os.getpid(), 'supervisor_birth': own['birth'],
                   'child_pid': child.pid, 'child_birth': born['birth'] if born else None, 'pgid': child.pid}
        def capture():
            while True:
                data = child.stdout.read1(8192)
                if not data:
                    break
                rotate_write(Path(run['log']), data)
        reader = threading.Thread(target=capture, daemon=True)
        reader.start()
        set_run(run_id, state='running', runtime=runtime)
        while True:
            # poll() reaps the leader; only signal groups while the leader is still alive, or
            # verified children from the known group remain. No external stale-PID signalling.
            alive = child.poll() is None
            members = group_members(child.pid)
            if pending[0]:
                action = pending[0]
                pending[0] = None
                if members:
                    os.killpg(child.pid, signal.SIGKILL if action == 'force' else signal.SIGTERM)
                until = timestamp() + 5
                while timestamp() < until and group_members(child.pid):
                    time.sleep(.05)
                if group_members(child.pid):
                    set_run(run_id, state='stop_failed', error='正常停止超时，服务仍在运行；可以选择强制停止')
                else:
                    stopped = True
                    break
            elif not alive:
                if members:
                    os.killpg(child.pid, signal.SIGTERM)
                    until = timestamp() + 5
                    while timestamp() < until and group_members(child.pid):
                        time.sleep(.05)
                    if group_members(child.pid):
                        set_run(run_id, state='stop_failed', error='主进程退出但子进程仍在运行，可显式强制停止')
                        while not pending[0]:
                            time.sleep(.1)
                        continue
                break
            time.sleep(.1)
        code = child.wait(timeout=2)
        reader.join(timeout=2)
        state = ('cancelled' if item['kind'] == 'task' else 'stopped') if stopped else (
            'succeeded' if code == 0 and item['kind'] == 'task' else 'exited' if code == 0 else 'failed')
        set_run(run_id, state=state, ended=timestamp(), exit_code=code, error=None)
    except Exception as exc:
        set_run(run_id, state='failed' if child is None else 'unknown',
                ended=timestamp() if child is None else None, error=str(exc))


def container_inspect(cid):
    return json.loads(docker('inspect', cid, timeout=8))[0]


def container_observation(*ids):
    """Docker observation needs identities/status/ports, never Config.Env or health logs."""
    template = '{"Id":{{json .Id}},"Name":{{json .Name}},"State":{"Running":{{json .State.Running}},' \
        '"Status":{{json .State.Status}},"StartedAt":{{json .State.StartedAt}},"FinishedAt":{{json .State.FinishedAt}},' \
        '"ExitCode":{{json .State.ExitCode}},"Health":{"Status":{{with (index .State "Health")}}{{json .Status}}{{else}}null{{end}}}},' \
        '"NetworkSettings":{"Ports":{{json .NetworkSettings.Ports}}},"Config":{"Labels":{' \
        '"com.docker.compose.project":{{json (index .Config.Labels "com.docker.compose.project")}},' \
        '"com.docker.compose.service":{{json (index .Config.Labels "com.docker.compose.service")}},' \
        '"com.docker.compose.project.config_files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}}}}}'
    return [json.loads(line) for line in docker('inspect', '--format', template, *ids, timeout=8).splitlines()]


def compose_command(item, *args, merge_stderr=False):
    spec = item['spec']
    return docker('compose', '-p', spec['compose_project'], '-f', spec['file'], *args,
                  env=runtime_env(spec), cwd=spec['cwd'], timeout=60, merge_stderr=merge_stderr)


def containers_for(item):
    spec = item['spec']
    if item['kind'] == 'container':
        if spec.get('management') == 'observed':
            return container_observation(spec['container_id'])
        return [container_inspect(spec['container_id'])]
    ids = compose_command(item, 'ps', '-aq', spec['member']).splitlines()
    result = [container_inspect(cid) for cid in ids]
    for obj in result:
        labels = obj['Config'].get('Labels') or {}
        if labels.get('com.docker.compose.project') != spec['compose_project'] or labels.get('com.docker.compose.service') != spec['member']:
            raise RuntimeError('Compose 成员身份不匹配')
        working = labels.get('com.docker.compose.project.working_dir')
        if working and str(Path(working).resolve()) != str(Path(spec['file']).parent):
            raise RuntimeError('同名 Compose 项目属于其他工作目录')
        files = labels.get('com.docker.compose.project.config_files')
        if files and {str(Path(path).resolve()) for path in files.split(',')} != {spec['file']}:
            raise RuntimeError('同名 Compose 项目使用其他配置文件，拒绝接管')
    return result


def docker_runtime(objects):
    return {'container_ids': [o['Id'] for o in objects],
            'started_at': [o['State'].get('StartedAt') for o in objects],
            'states': [o['State']['Status'] for o in objects],
            'native_health': [o['State'].get('Health', {}).get('Status') for o in objects],
            'ports': [o['NetworkSettings'].get('Ports') or {} for o in objects]}


def docker_time(value):
    if not value or value.startswith('0001-'):
        return None
    try:
        value = re.sub(r'(\.\d{6})\d+', r'\1', value).replace('Z', '+00:00')
        parsed = datetime.datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def docker_boundary(objects, key, earliest=False):
    values = [value for obj in objects for value in [docker_time(obj['State'].get(key))] if value is not None]
    return (min(values) if earliest else max(values)) if values else timestamp()


def health_binding(url, ports):
    """Only map a configured loopback URL that matches an actual published TCP port."""
    if not url:
        return None
    endpoint = urllib.parse.urlsplit(url)
    if endpoint.scheme not in ('http', 'https') or endpoint.hostname not in ('127.0.0.1', 'localhost', '::1'):
        return None
    host_port = endpoint.port or (443 if endpoint.scheme == 'https' else 80)
    matches = [port for port, bindings in ports.items() if port.endswith('/tcp')
               and any(b.get('HostPort') == str(host_port) for b in bindings or [])]
    return matches[0] if len(matches) == 1 else None


def observed_url(spec, runtime):
    url = spec.get('url')
    binding = spec.get('url_binding_port')
    if not url or not binding:
        return url
    endpoint = urllib.parse.urlsplit(url)
    for ports in runtime.get('ports', []):
        for published in ports.get(binding) or []:
            if published.get('HostPort') and published.get('HostIp') in ('127.0.0.1', '0.0.0.0', '::1', '::'):
                host = '[' + endpoint.hostname + ']' if ':' in endpoint.hostname else endpoint.hostname
                credentials = endpoint.netloc.rpartition('@')[0] + '@' if '@' in endpoint.netloc else ''
                return urllib.parse.urlunsplit((endpoint.scheme, credentials + host + ':' + published['HostPort'],
                                               endpoint.path, endpoint.query, endpoint.fragment))
    return url


def reconcile_docker_unlocked(item, objects):
    runtime = docker_runtime(objects)
    run = latest(item['id'])
    running = bool(objects) and any(o['State']['Running'] for o in objects)
    if running:
        if not run or run['ended'] is not None or (run['runtime'].get('started_at') and
                                                   run['runtime']['started_at'] != runtime['started_at']):
            if run and run['ended'] is None:
                set_run(run['id'], ended=timestamp(), state='exited')
            run_id = new_run(item['id'], runtime, started=docker_boundary(objects, 'StartedAt', earliest=True))
            set_run(run_id, state='running')
        else:
            set_run(run['id'], state='running', runtime=runtime)
    elif objects and (not run or (run['ended'] is not None and run['runtime'].get('started_at') != runtime['started_at'])) and any(
            started and not started.startswith('0001-') for started in runtime['started_at']):
        # A container may have exited before the first snapshot, or between two snapshots.
        # Keep that actual run, even when we never saw a running state.
        code = next((o['State'].get('ExitCode') for o in objects if o['State'].get('ExitCode')), objects[0]['State'].get('ExitCode'))
        run_id = new_run(item['id'], runtime, started=docker_boundary(objects, 'StartedAt', earliest=True))
        error = '; '.join(o['State'].get('Error', '') for o in objects if o['State'].get('Error'))
        set_run(run_id, ended=docker_boundary(objects, 'FinishedAt'), state='exited' if code == 0 else 'failed', exit_code=code,
                error=error or ('容器退出，退出码 ' + str(code) if code else None))
    elif run and run['ended'] is None:
        code = objects[0]['State'].get('ExitCode') if objects else None
        set_run(run['id'], ended=docker_boundary(objects, 'FinishedAt'), state='stopped' if code == 0 else 'failed',
                exit_code=code, runtime=runtime)
    return latest(item['id'])



def reconcile_docker(item, objects):
    # Snapshot requests can overlap across UI and CLI. Serialize each container history.
    with (ROOT / ('reconcile-' + item['id'] + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return reconcile_docker_unlocked(item, objects)


def register_container(req):
    obj = container_inspect(req['container_id'])
    proj = project(req.get('cwd', os.getcwd()))
    item_id = req.get('id') or hashlib.sha256(obj['Id'].encode()).hexdigest()[:24]
    existing = query('SELECT id FROM services WHERE kind=?', ['container'])
    for row in existing:
        prior = service(row['id'])
        if prior['spec']['container_id'] == obj['Id'] and prior['id'] != item_id:
            raise ValueError('该容器已登记，不能重复归属')
    spec = {'container_id': obj['Id'], 'cwd': proj['cwd'], 'url': req.get('url'), 'env': {}, 'source': 'explicit_adoption'}
    binding = health_binding(spec['url'], obj['NetworkSettings'].get('Ports') or {})
    if binding:
        spec['url_binding_port'] = binding
    item = record_spec(item_id, proj, req.get('name') or obj['Name'].lstrip('/'), 'container', spec)
    reconcile_docker(item, [obj])
    return observe(item_id)


def register_compose(req):
    proj = project(req.get('cwd', os.getcwd()))
    compose_file = str(Path(req['file']).expanduser().resolve(strict=True))
    group = req.get('group_id') or hashlib.sha256((proj['id'] + '\0' + compose_file).encode()).hexdigest()[:24]
    spec = {'cwd': proj['cwd'], 'file': compose_file, 'compose_project': req.get('compose_project') or ('csm-' + group[:12]),
            'env': environment(req.get('env')), 'source': 'companion'}
    probe = {'kind': 'compose', 'spec': spec}
    members = compose_command(probe, 'config', '--services').splitlines()
    if not members:
        raise ValueError('Compose 配置没有服务')
    # Validate every existing member before persisting an adopted group.
    for member in members:
        containers_for({'kind': 'compose', 'spec': {**spec, 'member': member}})
    items = []
    for member in members:
        item_id = hashlib.sha256((group + '\0' + member).encode()).hexdigest()[:24]
        item = record_spec(item_id, proj, member, 'compose', {**spec, 'member': member}, group)
        items.append(item_id)
    return {'group_id': group, 'members': items, 'results': batch({'ids': items, 'operation': 'start'}, 'launch')
            if req.get('start', True) else []}


def start_docker(item):
    objects = containers_for(item)
    if any(o['State']['Running'] for o in objects):
        reconcile_docker(item, objects)
        raise RuntimeError('服务已经运行，拒绝重复启动')
    try:
        if item['kind'] == 'container':
            docker('start', item['spec']['container_id'])
        else:
            compose_command(item, 'up', '-d', '--no-deps', item['spec']['member'])
    except Exception as exc:
        # Record the failed start attempt without falsely declaring a container stopped
        # when the daemon accepted it but the command response was lost.
        try:
            after = containers_for(item)
        except Exception:
            run_id = new_run(item['id'])
            set_run(run_id, state='unknown', error=str(exc))
        else:
            if any(o['State']['Running'] for o in after):
                reconcile_docker(item, after)
            else:
                run_id = new_run(item['id'], docker_runtime(after))
                set_run(run_id, ended=timestamp(), state='failed', error=str(exc))
        raise
    reconcile_docker(item, containers_for(item))
    result = observe(item['id'])
    if result['state'] == 'failed':
        raise RuntimeError(result['problem'] or '容器启动后退出，请查看日志')
    return result


def stop_docker(item, force=False):
    objects = containers_for(item)
    if not objects:
        return observe(item['id'])
    for obj in objects:
        if obj['State']['Running']:
            docker(*(['kill', obj['Id']] if force else ['stop', '-t', '5', obj['Id']]), timeout=20)
    run = latest(item['id'])
    if run and run['ended'] is None:
        after = containers_for(item)
        if any(o['State']['Running'] for o in after):
            raise RuntimeError('容器仍在运行')
        set_run(run['id'], ended=docker_boundary(after, 'FinishedAt'), state='stopped', runtime=docker_runtime(after))
    return observe(item['id'])


def operate(service_id, action, trigger='user'):
    operation = uid()
    write('INSERT INTO operations(id,target,action,trigger,started) VALUES (?,?,?,?,?)',
          [operation, service_id, action, trigger, timestamp()])
    try:
        item = service(service_id)
        observed = item['spec'].get('management') == 'observed'
        if observed:
            capability = observe(service_id)
            key = {'stop': 'can_stop', 'force': 'can_force', 'start': 'can_start', 'restart': 'can_restart'}.get(action)
            if not key or not capability.get(key):
                raise RuntimeError(capability.get('capability_reason') or '观察服务当前不具备此操作能力')
        run = latest(service_id)
        if action == 'restart':
            operate(service_id, 'stop', trigger)
            item = service(service_id)
            result = operate(service_id, 'start', trigger)
        elif action == 'start':
            if observed:
                item['spec']['management'] = 'managed'
                write('UPDATE services SET spec=? WHERE id=?', [json.dumps(item['spec']), service_id])
            if item['kind'] in ('process', 'task'):
                if run and run['ended'] is None:
                    raise RuntimeError('前一次运行尚未结束，拒绝启动第二个副本')
                result = start_process(item)
            else:
                result = start_docker(item)
        elif action in ('stop', 'force'):
            if observed and item['kind'] in ('process', 'task'):
                import adoption
                result = adoption.stop(item, action == 'force')
            else:
                result = stop_process(item, action == 'force') if item['kind'] in ('process', 'task') else stop_docker(item, action == 'force')
        else:
            raise ValueError('未知操作')
        write('UPDATE operations SET ended=?,result=? WHERE id=?', [timestamp(), 'succeeded', operation])
        return result
    except Exception as exc:
        write('UPDATE operations SET ended=?,result=?,error=? WHERE id=?', [timestamp(), 'failed', str(exc), operation])
        raise


def observe(service_id):
    item = service(service_id)
    run = latest(service_id)
    problem = None
    can_stop = False
    capability_reason = None
    can_force = False
    observed = item['spec'].get('management') == 'observed'
    if item['kind'] in ('container', 'compose'):
        try:
            objects = containers_for(item)
            run = reconcile_docker(item, objects)
            state = 'running' if any(o['State']['Running'] for o in objects) else (run['state'] if run else 'stopped')
            can_stop = any(o['State']['Running'] for o in objects)
        except Exception as exc:
            state, problem = 'unknown', str(exc)
    elif observed and run and run['ended'] is None:
        runtime = run['runtime']
        try:
            current = identity(runtime.get('pid'))
            remaining = [m for m in runtime.get('control_members', []) if matches(m['pid'], m['birth'])]
            launchers = [m for m in runtime.get('launch_members', []) if matches(m['pid'], m['birth'])]
            if current and current['birth'] == runtime.get('birth') or remaining:
                state = run['state'] if run['state'] in ('stop_failed', 'stopping') else 'running'
            elif run['state'] == 'stop_failed':
                if runtime.get('control_members') and all(port_free(p) for p in item['spec'].get('ports', [])):
                    state = 'cancelled' if item['kind'] == 'task' else 'stopped'
                    set_run(run['id'], ended=timestamp(), state=state, error=None)
                    run = latest(service_id)
                else:
                    state, problem = 'stop_failed', run['error']
            elif launchers:
                state, problem = 'unknown', '监听进程已退出，但已确认启动器或子进程仍在运行，拒绝启动副本'
            else:
                state = 'exited'
                set_run(run['id'], ended=timestamp(), state=state,
                        runtime={**runtime, 'exit_observation': '观察对象已退出或 PID 出生身份已变化；退出码未采集'})
                run = latest(service_id)
        except Exception as exc:
            state, problem = 'unknown', str(exc)
    elif run and run['ended'] is None:
        try:
            runtime = run['runtime']
            if matches(runtime.get('supervisor_pid'), runtime.get('supervisor_birth'), run['id']):
                state = run['state']
                can_stop = True
            else:
                state, problem = 'unknown', '监督进程身份无法确认；服务可能仍在运行'
        except Exception as exc:
            state, problem = 'unknown', str(exc)
    else:
        state = run['state'] if run else 'never_started'
    native = (run or {}).get('runtime', {}).get('native_health', [])
    url = observed_url(item['spec'], (run or {}).get('runtime', {}))
    health_state = health(url) if state in ('running', 'stop_failed') else 'not_configured'
    if state in ('running', 'stop_failed') and not url and any(native):
        health_state = 'available' if all(h == 'healthy' for h in native) else 'unavailable' if 'unhealthy' in native else 'checking'
    # Secrets remain usable for restart but never leave the engine in UI snapshots.
    spec = {k: v for k, v in item['spec'].items() if k != 'env'}
    spec['url'] = url
    spec['env_keys'] = sorted(item['spec'].get('env', {}))
    can_start = state not in ACTIVE and not observed
    can_restart = state != 'unknown' and not observed
    management = 'managed'
    if observed:
        management = 'observed'
        if item['kind'] in ('container', 'compose'):
            can_start = state not in ACTIVE
            can_restart = state not in ('unknown', 'starting', 'stopping')
            can_force = can_stop
        else:
            if state in ('running', 'stop_failed'):
                try:
                    import adoption
                    plan = adoption.stop_plan(item)
                    can_stop = True
                    can_force = state == 'stop_failed'
                    if item['spec'].get('recipe_automatic') and not adoption.recipe_matches(item['spec'], plan):
                        capability_reason = '启动配置与当前启动器不匹配，请补充单项启动配置'
                    else:
                        can_restart = bool(item['spec'].get('argv'))
                except Exception as exc:
                    can_stop = False
                    capability_reason = str(exc)
            can_start = state not in ACTIVE and bool(item['spec'].get('argv'))
            if state not in ACTIVE:
                can_restart = can_start
            if not item['spec'].get('argv'):
                capability_reason = capability_reason or '缺少单项服务启动配置；可补充配置以启用启动和重启'
        if can_stop or can_start or can_restart:
            management = 'attached'
    else:
        can_force = state == 'stop_failed' and can_stop
    spec.pop('recipe_candidate', None)
    return {**item, 'spec': spec, 'run': run, 'state': state, 'health': health_state,
            'problem': problem or ((run or {}).get('error')), 'can_stop': can_stop, 'can_force': can_force,
            'can_start': can_start, 'can_restart': can_restart, 'capability_reason': capability_reason,
            'management': management, 'automatic_end': not observed,
            'can_logs': not observed or item['kind'] in ('container', 'compose')}


def project_record(project_id):
    item = query('SELECT * FROM projects WHERE id=?', [project_id], True)
    if not item:
        raise ValueError('项目不存在')
    return item


def rename_project(project_id, name):
    project_record(project_id)
    if not isinstance(name, str):
        raise ValueError('项目名称必须是文字')
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError('项目名称须为 1–100 个字符')
    if any(unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp') for c in name):
        raise ValueError('项目名称不能包含控制字符、不可见格式或换行')
    write('UPDATE projects SET name=? WHERE id=?', [name, project_id])
    return project_record(project_id)


def project_delete_preview(project_id):
    proj = project_record(project_id)
    ids = query('SELECT id FROM services WHERE project_id=? ORDER BY name', [project_id])
    blockers = []
    labels = {'starting': '启动中', 'running': '运行中', 'stopping': '停止中',
              'stop_failed': '停止失败', 'unknown': '身份或状态未知'}
    for row in ids:
        item = observe(row['id'])
        unfinished = query('SELECT id FROM runs WHERE service_id=? AND ended IS NULL LIMIT 1', [item['id']], True)
        if item['state'] in ACTIVE or item['can_stop'] or unfinished:
            state = item['state'] if item['state'] in ACTIVE else 'unknown'
            blockers.append({'id': item['id'], 'name': item['name'], 'state': state,
                'reason': item.get('problem') or labels.get(state, '存在未结束的运行记录，需先核对')})
    return {'project': proj, 'service_count': len(ids),
        'run_count': query('SELECT COUNT(*) AS count FROM runs WHERE service_id IN (SELECT id FROM services WHERE project_id=?)', [project_id], True)['count'],
        'operation_count': query('SELECT COUNT(*) AS count FROM operations WHERE target=? OR target IN (SELECT id FROM services WHERE project_id=?)', [project_id, project_id], True)['count'],
        'blockers': blockers, 'can_delete': not blockers}


def delete_project(project_id):
    preview = project_delete_preview(project_id)
    if preview['blockers']:
        detail = '；'.join(item['name'] + '：' + item['reason'] for item in preview['blockers'][:5])
        raise RuntimeError('不能删除项目：' + detail + '。请先手动处理这些服务，再重新检查。')
    trash_id = uid()
    with database() as db:
        archive = {'deleted_at': timestamp(), 'project': preview['project'],
            'services': [dict(row) for row in db.execute('SELECT * FROM services WHERE project_id=?', [project_id])],
            'runs': [dict(row) for row in db.execute('SELECT * FROM runs WHERE service_id IN (SELECT id FROM services WHERE project_id=?)', [project_id])],
            'operations': [dict(row) for row in db.execute('SELECT * FROM operations WHERE target=? OR target IN (SELECT id FROM services WHERE project_id=?)', [project_id, project_id])]}
        db.execute('INSERT INTO metadata VALUES (?,?)', ['deleted_project:' + trash_id, json.dumps(archive)])
        db.execute('DELETE FROM operations WHERE target=? OR target IN (SELECT id FROM services WHERE project_id=?)', [project_id, project_id])
        db.execute('DELETE FROM runs WHERE service_id IN (SELECT id FROM services WHERE project_id=?)', [project_id])
        db.execute('DELETE FROM services WHERE project_id=?', [project_id])
        db.execute('DELETE FROM projects WHERE id=?', [project_id])
    return {'id': project_id, 'trash_id': trash_id, 'removed_services': preview['service_count'],
            'removed_runs': preview['run_count'], 'removed_operations': preview['operation_count']}


def deleted_projects():
    items = []
    for row in query("SELECT key,value FROM metadata WHERE key GLOB 'deleted_project:*'"):
        archive = json.loads(row['value'])
        items.append({'id': row['key'].split(':', 1)[1], 'name': archive['project']['name'],
            'cwd': archive['project']['cwd'], 'deleted_at': archive['deleted_at'],
            'service_count': len(archive['services']), 'run_count': len(archive['runs'])})
    return sorted(items, key=lambda item: item['deleted_at'], reverse=True)


def restore_project(trash_id):
    row = query('SELECT value FROM metadata WHERE key=?', ['deleted_project:' + trash_id], True)
    if not row:
        raise ValueError('已删除的项目记录不存在')
    archive = json.loads(row['value'])
    proj = archive['project']
    if query('SELECT id FROM projects WHERE id=? OR cwd=?', [proj['id'], proj['cwd']], True):
        raise RuntimeError('同一目录已有项目登记，无法恢复；不会覆盖现有项目')
    columns = {'projects': ('id', 'cwd', 'name', 'manual', 'codex_exit'),
        'services': ('id', 'project_id', 'name', 'kind', 'spec', 'keep', 'group_id'),
        'runs': ('id', 'service_id', 'started', 'ended', 'state', 'exit_code', 'runtime', 'error', 'log'),
        'operations': ('id', 'target', 'action', 'trigger', 'started', 'ended', 'result', 'error')}
    try:
        with database() as db:
            for table, fields in columns.items():
                records = [proj] if table == 'projects' else archive[table]
                for item in records:
                    db.execute('INSERT INTO ' + table + ' (' + ','.join(fields) + ') VALUES (' + ','.join('?' for _ in fields) + ')',
                               [item[field] for field in fields])
            db.execute('DELETE FROM metadata WHERE key=?', ['deleted_project:' + trash_id])
    except sqlite3.IntegrityError as exc:
        raise RuntimeError('登记或历史标识已被占用，恢复已取消；现有记录和回收记录保持不变') from exc
    return {'project': project_record(proj['id']), 'restored_services': len(archive['services']), 'restored_runs': len(archive['runs'])}


def snapshot():
    ids = [row['id'] for row in query('SELECT id,spec FROM services ORDER BY name')
           if json.loads(row['spec']).get('source') != 'internal']
    def observe_existing(sid):
        try:
            return observe(sid)
        except ValueError:
            if not query('SELECT id FROM services WHERE id=?', [sid], True):
                return None  # A concurrent project deletion completed after the ID read.
            raise
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        services = [item for item in pool.map(observe_existing, ids) if item is not None]
    watcher = query('SELECT value FROM metadata WHERE key=?', ['watcher'], True)
    watcher = json.loads(watcher['value']) if watcher else None
    if watcher:
        watcher['alive'] = matches(watcher.get('pid'), watcher.get('birth'), '--watch') and timestamp() - watcher.get('updated', 0) < 20
    capture = query('SELECT value FROM metadata WHERE key=?', ['capture'], True)
    capture = json.loads(capture['value']) if capture else None
    if capture:
        capture['alive'] = matches(capture.get('pid'), capture.get('birth'), 'capture.py') and timestamp() - capture.get('updated', 0) < 20
    projects = [p for p in query('SELECT * FROM projects ORDER BY name')
                if not query('SELECT id FROM services WHERE project_id=?', [p['id']]) or
                any(json.loads(s['spec']).get('source') != 'internal' for s in query('SELECT spec FROM services WHERE project_id=?', [p['id']]))]
    return {'projects': projects, 'services': services,
            'operations': query('SELECT * FROM operations ORDER BY started DESC LIMIT 100'),
            'watcher': watcher, 'capture': capture, 'deleted_projects': deleted_projects(), 'generated_at': timestamp(), 'state_dir': str(ROOT)}


def batch(req, trigger='user'):
    ids = set(req.get('ids', []))
    if req.get('group_id'):
        ids.update(r['id'] for r in query('SELECT id FROM services WHERE group_id=?', [req['group_id']]))
    if req.get('project_id'):
        ids.update(r['id'] for r in query('SELECT id FROM services WHERE project_id=?', [req['project_id']]))
    results = []
    for sid in sorted(ids):
        try:
            registered = query('SELECT spec FROM services WHERE id=?', [sid], True)
            spec = json.loads(registered['spec']) if registered else {}
            if (spec.get('management') == 'observed' and trigger in ('manual', 'codex_exit')) or spec.get('source') == 'internal':
                results.append({'id': sid, 'result': 'skipped', 'reason': '自动观察服务或内部采集组件不参与批量控制'})
                continue
            if trigger in ('manual', 'codex_exit') and service(sid)['keep']:
                results.append({'id': sid, 'result': 'skipped', 'reason': '持续运行例外'})
                continue
            result = operate(sid, req.get('operation', 'stop'), trigger)
            results.append({'id': sid, 'result': 'succeeded', 'state': result['state']})
        except Exception as exc:
            results.append({'id': sid, 'result': 'failed', 'error': str(exc)})
    return results


def end_project(req, trigger='manual'):
    proj = query('SELECT * FROM projects WHERE id=?', [req['project_id']], True)
    if not proj:
        raise ValueError('项目不存在')
    if trigger not in ('manual', 'codex_exit'):
        return []
    if not proj[trigger]:
        return [{'result': 'skipped', 'reason': '项目未开启该结束触发'}]
    return batch({'project_id': proj['id'], 'operation': 'stop'}, trigger)


def discover():
    containers, process_items, errors = [], [], []
    known = set()
    for row in query('SELECT id FROM services WHERE kind IN (?,?)', ['container', 'compose']):
        try:
            known.update(o['Id'] for o in containers_for(service(row['id'])))
        except Exception:
            pass
    try:
        for cid in docker('ps', '-aq').splitlines():
            obj = container_inspect(cid)
            if obj['Id'] not in known:
                containers.append({'id': obj['Id'], 'name': obj['Name'].lstrip('/'), 'state': obj['State']['Status']})
    except Exception as exc:
        errors.append(str(exc))
    try:
        output = command(['/usr/sbin/lsof', '-nP', '-iTCP', '-sTCP:LISTEN', '-Fpcn'], timeout=8)
        current = None
        for line in output.splitlines():
            if line.startswith('p'):
                current = {'pid': int(line[1:]), 'ports': []}
                process_items.append(current)
            elif current and line.startswith('c'):
                current['name'] = line[1:]
            elif current and line.startswith('n'):
                current['ports'].append(line[1:])
        managed_services = snapshot()['services']
        for item in process_items:
            item['identity'] = identity(item['pid'])
            item['managed'] = any(s.get('run') and item['pid'] in (
                s['run']['runtime'].get('child_pid'), s['run']['runtime'].get('supervisor_pid'))
                for s in managed_services)
    except Exception as exc:
        errors.append(str(exc))
    return {'containers': containers, 'processes': process_items, 'errors': errors}


def dispatch(req):
    action = req['action']
    if action == 'snapshot':
        return snapshot()
    if action == 'history':
        rows = query('SELECT * FROM runs WHERE service_id=? ORDER BY started DESC', [req['id']])
        for row in rows:
            row['runtime'] = json.loads(row['runtime'])
        return rows
    if action == 'logs':
        item = service(req['id'])
        if item['kind'] in ('container', 'compose'):
            if item['kind'] == 'container':
                return docker('logs', '--tail', '200', item['spec']['container_id'], merge_stderr=True)[-65536:]
            return compose_command(item, 'logs', '--no-color', '--tail', '200', item['spec']['member'], merge_stderr=True)[-65536:]
        run = latest(req['id']) if not req.get('run_id') else query('SELECT * FROM runs WHERE id=? AND service_id=?', [req['run_id'], req['id']], True)
        path = Path(run['log']) if run and run.get('log') else None
        if not path or not path.exists():
            return ''
        with path.open('rb') as log:
            log.seek(max(0, path.stat().st_size - 65536))
            return log.read().decode(errors='replace')
    if action == 'discover':
        return discover()
    if action == 'capture_start':
        import capture
        capture.ensure_collector()
        return True
    if action == 'project_delete_preview':
        return project_delete_preview(req['id'])
    with mutation_lock():
        if action == 'project_rename':
            return rename_project(req['id'], req.get('name'))
        if action == 'project_delete':
            return delete_project(req['id'])
        if action == 'project_restore':
            return restore_project(req['id'])
        if action == 'launch':
            return launch(req)
        if action == 'configure_observed':
            import adoption
            return adoption.configure(req)
        if action == 'register_container':
            return register_container(req)
        if action == 'register_compose':
            return register_compose(req)
        if action in ('start', 'stop', 'restart', 'force'):
            return operate(req['id'], action)
        if action == 'batch':
            return batch(req)
        if action == 'end_project':
            return end_project(req)
        if action == 'codex_exit':
            return [{'project_id': p['id'], 'results': end_project({'project_id': p['id']}, 'codex_exit')}
                    for p in query('SELECT * FROM projects WHERE codex_exit=1')]
        if action == 'project_policy':
            write('UPDATE projects SET manual=?,codex_exit=? WHERE id=?',
                  [bool(req['manual']), bool(req['codex_exit']), req['id']])
            return True
        if action == 'service_keep':
            write('UPDATE services SET keep=? WHERE id=?', [bool(req['keep']), req['id']])
            return True
        if action == 'watcher_event':
            write('INSERT OR REPLACE INTO metadata VALUES (?,?)', ['watcher', json.dumps(req['watcher'])])
            return True
        if action == 'cleanup':
            before = timestamp() - int(req.get('days', 30)) * 86400
            rows = query('SELECT id,log FROM runs WHERE ended IS NOT NULL AND ended<?', [before])
            for row in rows:
                if row['log']:
                    for suffix in ('', '.1', '.2'):
                        Path(row['log'] + suffix).unlink(missing_ok=True)
            write('DELETE FROM runs WHERE ended IS NOT NULL AND ended<?', [before])
            write('DELETE FROM operations WHERE ended IS NOT NULL AND ended<?', [before])
            return {'removed_runs': len(rows)}
        raise ValueError('未知请求：' + action)


def main():
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path)
    parser.add_argument('mode', choices=['rpc', '_supervise'])
    parser.add_argument('run_id', nargs='?')
    args = parser.parse_args()
    if args.root:
        ROOT = args.root.expanduser().resolve()
    if args.mode == '_supervise':
        supervise(args.run_id)
        return
    try:
        request = json.load(sys.stdin)
        result = dispatch(request)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    # RPC/supervisor execution must share this configured ROOT with modules
    # importing engine; otherwise a second module silently uses DEFAULT_ROOT.
    sys.modules['engine'] = sys.modules[__name__]
    main()
