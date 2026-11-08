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
import math
import os
from pathlib import Path
import shlex
import re
import signal
import socket
import sqlite3
import storage
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


SCHEMA_VERSION = 1
BASE_SCHEMA = """
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
"""
SCHEMA_V1 = """
    ALTER TABLE projects ADD COLUMN auto_scan INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE projects ADD COLUMN observation_revision INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE projects ADD COLUMN created_at REAL;
    ALTER TABLE projects ADD COLUMN created_at_source TEXT NOT NULL DEFAULT 'unknown';
    ALTER TABLE projects ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE services ADD COLUMN display_name TEXT;
    ALTER TABLE services ADD COLUMN created_at REAL;
    ALTER TABLE services ADD COLUMN created_at_source TEXT NOT NULL DEFAULT 'unknown';
    ALTER TABLE services ADD COLUMN pending_spec TEXT;
    ALTER TABLE services ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE service_observations(service_id TEXT PRIMARY KEY, checked_at REAL NOT NULL,
      projection TEXT NOT NULL);
    UPDATE services SET created_at=(SELECT MIN(started) FROM runs WHERE service_id=services.id);
    UPDATE projects SET created_at=(SELECT MIN(created_at) FROM services WHERE project_id=projects.id);
    UPDATE services SET created_at_source='estimated' WHERE created_at IS NOT NULL;
    UPDATE projects SET created_at_source='estimated' WHERE created_at IS NOT NULL;
    INSERT OR IGNORE INTO metadata(key,value) VALUES
      ('project_sidebar_preferences','{"sort_key":"name","direction":"asc"}');
"""


def database():
    db = storage.open_database(ROOT)
    try:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version != SCHEMA_VERSION:
            with (ROOT / 'schema.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version == 0:
                    db.execute('PRAGMA journal_mode=WAL')
                    db.executescript('BEGIN IMMEDIATE;\n' + BASE_SCHEMA + SCHEMA_V1
                                     + f'PRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;')
                elif version != SCHEMA_VERSION:
                    raise RuntimeError('状态数据库版本不受当前引擎支持')
        storage.private_file(ROOT / 'manager.sqlite')
        return db
    except BaseException:
        db.close()  # Rolls back an incomplete schema transaction.
        raise


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


class MutationBusy(RuntimeError):
    pass


_mutation = threading.local()


@contextlib.contextmanager
def mutation_lock(wait=0):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / 'mutation.lock').open('a') as lock:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MutationBusy('另一个操作正在进行，请等待完成后重试') from None
                time.sleep(.025)
        previous = getattr(_mutation, 'root', None)
        _mutation.root = ROOT
        try:
            yield
        finally:
            _mutation.root = previous


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
    write("INSERT INTO projects(id,cwd,name,created_at,created_at_source) VALUES (?,?,?,?,'registered') "
          "ON CONFLICT(cwd) DO NOTHING", [item_id, path, name or Path(path).name, timestamp()])
    return query('SELECT * FROM projects WHERE id=?', [item_id], True)


def environment(explicit):
    return {**{k: os.environ[k] for k in ('PATH', 'LANG', 'JAVA_HOME') if k in os.environ},
            **{str(k): str(v) for k, v in (explicit or {}).items()}}


def runtime_env(spec):
    env = {**os.environ, **spec.get('env', {})}
    for key in spec.get('env_unset', ()):
        env.pop(key, None)
    return env


def private_error(spec, error):
    message = str(error)
    values = spec.get('env', {}).values()
    if any(value and value in message for value in values):
        return '服务操作失败，请查看本机日志'
    return message


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
    storage.private_directory(ROOT / 'logs')
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
    write("INSERT INTO services(id,project_id,name,kind,spec,group_id,created_at,created_at_source) "
          "VALUES (?,?,?,?,?,?,?,'registered') "
          'ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,spec=excluded.spec,group_id=excluded.group_id',
          [item_id, proj['id'], name, kind, json.dumps(spec), group, timestamp()])
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
    process = None
    try:
        with open(ROOT / 'supervisor.log', 'ab', buffering=0) as output:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--root', str(ROOT),
                '_supervise', run_id], stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, start_new_session=True, close_fds=True)
    except Exception as exc:
        message = private_error(spec, exc)
        if process is None:
            write("UPDATE runs SET ended=?,state='failed',error=? WHERE id=? AND state='starting' AND ended IS NULL",
                  [timestamp(), message, run_id])
        raise RuntimeError(message) from None

    unconfirmed = None
    until = time.monotonic() + 8
    while time.monotonic() < until:
        run = latest(item['id'])
        if run['state'] != 'starting':
            break
        if process.poll() is not None:
            unconfirmed = '监督进程在启动确认之前退出；无法排除业务进程已启动'
            break
        time.sleep(.05)
    else:
        unconfirmed = '监督器启动确认超时；无法排除业务进程已启动'

    # Re-read after poll/timeout so a completed supervisor write is never overwritten.
    if unconfirmed:
        write("UPDATE runs SET state='unknown',error=? WHERE id=? AND state='starting' AND ended IS NULL",
              [unconfirmed, run_id])
    run = latest(item['id'])
    if run['state'] in ('failed', 'unknown'):
        raise RuntimeError(run['error'] or '启动失败，请查看日志')
    return observe(item['id'])


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
        set_run(run_id, state='running', runtime=runtime, error=None)
        while True:
            # poll() reaps the leader; only signal groups while the leader is still alive, or
            # verified children from the known group remain. No external stale-PID signalling.
            alive = child.poll() is None
            if pending[0]:
                action = pending[0]
                pending[0] = None
                members = group_members(child.pid)
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
                members = group_members(child.pid)
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
                ended=timestamp() if child is None else None, error=private_error(spec, exc))


def container_inspect(cid, *, timeout=8):
    return json.loads(docker('inspect', cid, timeout=timeout))[0]


def container_observation(*ids, timeout=8):
    """Docker observation needs identities/status/ports, never Config.Env or health logs."""
    template = '{"Id":{{json .Id}},"Name":{{json .Name}},"State":{"Running":{{json .State.Running}},' \
        '"Status":{{json .State.Status}},"StartedAt":{{json .State.StartedAt}},"FinishedAt":{{json .State.FinishedAt}},' \
        '"ExitCode":{{json .State.ExitCode}},"Health":{"Status":{{with (index .State "Health")}}{{json .Status}}{{else}}null{{end}}}},' \
        '"NetworkSettings":{"Ports":{{json .NetworkSettings.Ports}}},"Config":{"Labels":{' \
        '"com.docker.compose.project":{{json (index .Config.Labels "com.docker.compose.project")}},' \
        '"com.docker.compose.service":{{json (index .Config.Labels "com.docker.compose.service")}},' \
        '"com.docker.compose.project.config_files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}}}}}'
    return [json.loads(line) for line in docker('inspect', '--format', template, *ids, timeout=timeout).splitlines()]


def compose_command(item, *args, merge_stderr=False, timeout=60):
    spec = item['spec']
    return docker('compose', '-p', spec['compose_project'], '-f', spec['file'], *args,
                  env=runtime_env(spec), cwd=spec['cwd'], timeout=timeout, merge_stderr=merge_stderr)


def compose_config_paths(files):
    return {str(Path(path).resolve()) for path in (files or '').split(',') if path}


def containers_for(item, *, budget=None):
    spec = item['spec']
    deadline = time.monotonic() + budget if budget is not None else None

    def remaining(default):
        if deadline is None:
            return default
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('后台容器检查超时')
        return min(default, left)

    if item['kind'] == 'container':
        if spec.get('management') == 'observed':
            return container_observation(spec['container_id'], timeout=remaining(8))
        return [container_inspect(spec['container_id'], timeout=remaining(8))]
    ids = compose_command(item, 'ps', '-aq', spec['member'], timeout=remaining(60)).splitlines()
    result = [container_inspect(cid, timeout=remaining(8)) for cid in ids]
    for obj in result:
        labels = obj['Config'].get('Labels') or {}
        if labels.get('com.docker.compose.project') != spec['compose_project'] or labels.get('com.docker.compose.service') != spec['member']:
            raise RuntimeError('Compose 成员身份不匹配')
        working = labels.get('com.docker.compose.project.working_dir')
        if working and str(Path(working).resolve()) != str(Path(spec['file']).parent):
            raise RuntimeError('同名 Compose 项目属于其他工作目录')
        files = labels.get('com.docker.compose.project.config_files')
        if files and compose_config_paths(files) != {spec['file']}:
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
    selected = project_record(req['project_id']) if 'project_id' in req else None
    obj = container_inspect(req['container_id'])
    proj = selected or project(req.get('cwd', os.getcwd()))
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


def start_docker(item, objects=None):
    if objects is None:
        objects = containers_for(item)
    if any(o['State']['Running'] for o in objects):
        reconcile_docker(item, objects)
        raise RuntimeError('服务已经运行，拒绝重复启动')
    try:
        if item['kind'] == 'container':
            docker('start', item['spec']['container_id'])
        else:
            preserve = ['--no-recreate'] if item['pending_spec'] else []
            compose_command(item, 'up', '-d', '--no-deps', *preserve, item['spec']['member'])
    except Exception as exc:
        # Record the failed start attempt without falsely declaring a container stopped
        # when the daemon accepted it but the command response was lost.
        try:
            after = containers_for(item)
        except Exception:
            run_id = new_run(item['id'])
            set_run(run_id, state='unknown', error=private_error(item['spec'], exc))
        else:
            if any(o['State']['Running'] for o in after):
                reconcile_docker(item, after)
            else:
                run_id = new_run(item['id'], docker_runtime(after))
                set_run(run_id, ended=timestamp(), state='failed', error=private_error(item['spec'], exc))
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


def pending_settings(item):
    return json.loads(item['pending_spec']) if item['pending_spec'] else {}


def start_registered(item, run):
    local = item['kind'] in ('process', 'task')
    if local and run and run['ended'] is None:
        raise RuntimeError('前一次运行尚未结束，拒绝启动第二个副本')
    pending = pending_settings(item)
    spec = {**item['spec'], **pending}
    if spec.get('management') == 'observed':
        spec['management'] = 'managed'
    objects = None
    if not local:
        objects = containers_for(item)
        if any(obj['State']['Running'] for obj in objects):
            reconcile_docker(item, objects)
            raise RuntimeError('服务已经运行，拒绝重复启动')
        if 'url' in pending:
            bindings = {health_binding(spec['url'], obj['NetworkSettings'].get('Ports') or {}) for obj in objects}
            bindings.discard(None)
            if len(bindings) == 1:
                spec['url_binding_port'] = bindings.pop()
    changed = spec != item['spec']
    if changed:
        write('UPDATE services SET spec=? WHERE id=?', [json.dumps(spec), item['id']])
    try:
        active = {**item, 'spec': spec}
        result = start_process(active) if local else start_docker(active, objects)
        if result['state'] == 'unknown':
            raise RuntimeError(result.get('problem') or '无法确认新运行已启动')
    except Exception as exc:
        attempted = latest(item['id'])
        # A timed-out start may have created a live or unknown run. Keep the
        # active spec used for that run until identity and termination are known.
        if changed and (not attempted or attempted == run or attempted['ended'] is not None):
            write('UPDATE services SET spec=? WHERE id=?', [json.dumps(item['spec']), item['id']])
        message = private_error(spec, exc)
        if message != str(exc):
            raise RuntimeError(message) from None
        raise
    if pending:
        write('UPDATE services SET pending_spec=NULL,config_revision=config_revision+1 '
              'WHERE id=? AND config_revision=?', [item['id'], item['config_revision']])
        return cached_observation(item['id'])
    return result


def operate(service_id, action, trigger='user'):
    operation = uid()
    write('INSERT INTO operations(id,target,action,trigger,started) VALUES (?,?,?,?,?)',
          [operation, service_id, action, trigger, timestamp()])
    try:
        item = service(service_id)
        observed = item['spec'].get('management') == 'observed'
        capability = None
        if observed:
            capability = observe(service_id)
            key = {'stop': 'can_stop', 'force': 'can_force', 'start': 'can_start', 'restart': 'can_restart'}.get(action)
            if not key or not capability.get(key):
                raise RuntimeError(capability.get('capability_reason') or '观察服务当前不具备此操作能力')
        run = latest(service_id)
        if action == 'restart':
            if capability is None or capability['state'] in ACTIVE:
                operate(service_id, 'stop', trigger)
            result = operate(service_id, 'start', trigger)
        elif action == 'start':
            result = start_registered(item, run)
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
        message = private_error(item['spec'], exc) if 'item' in locals() else str(exc)
        write('UPDATE operations SET ended=?,result=?,error=? WHERE id=?', [timestamp(), 'failed', message, operation])
        if message != str(exc):
            raise RuntimeError(message) from None
        raise


def probe_observation(item, run, *, background=False, managed_identities=None):
    objects = None
    changes = None
    problem = None
    can_stop = False
    capability_reason = None
    can_force = False
    observed = item['spec'].get('management') == 'observed'
    if item['kind'] in ('container', 'compose'):
        try:
            objects = containers_for(item, budget=4) if background else containers_for(item)
            can_stop = any(o['State']['Running'] for o in objects)
            state = 'running' if can_stop else 'stopped'
            run = {**(run or {}), 'runtime': docker_runtime(objects)}
        except Exception as exc:
            objects = None
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
                    changes = {'ended': timestamp(), 'state': state, 'error': None}
                    run = {**run, **changes}
                else:
                    state, problem = 'stop_failed', run['error']
            elif launchers:
                state, problem = 'unknown', '监听进程已退出，但已确认启动器或子进程仍在运行，拒绝启动副本'
            else:
                state = 'exited'
                changes = {'ended': timestamp(), 'state': state,
                           'runtime': {**runtime, 'exit_observation': '观察对象已退出或 PID 出生身份已变化；退出码未采集'}}
                run = {**run, **changes}
        except Exception as exc:
            state, problem = 'unknown', str(exc)
    elif run and run['ended'] is None:
        try:
            runtime = run['runtime']
            pid = runtime.get('supervisor_pid')
            birth = runtime.get('supervisor_birth')
            if managed_identities is not None and pid in managed_identities:
                entry = managed_identities[pid]
                verified = bool(entry and entry['birth'] == birth and run['id'] in entry['command'])
            else:
                verified = matches(pid, birth, run['id'])
            if verified:
                state = run['state']
                can_stop = True
            else:
                state = 'unknown'
                problem = (run['error'] if run['state'] == 'unknown' else None) or '监督进程身份无法确认；服务可能仍在运行'
        except Exception as exc:
            state, problem = 'unknown', str(exc)
    else:
        state = run['state'] if run else 'never_started'
    native = (run or {}).get('runtime', {}).get('native_health', [])
    url = observed_url(item['spec'], (run or {}).get('runtime', {}))
    health_state = health(url) if state in ('running', 'stop_failed') else 'not_configured'
    if state in ('running', 'stop_failed') and not url and any(native):
        health_state = 'available' if all(h == 'healthy' for h in native) else 'unavailable' if 'unhealthy' in native else 'checking'
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
    projection = {'effective_url': url, 'state': state, 'health': health_state,
            'problem': problem or ((run or {}).get('error')), 'can_stop': can_stop, 'can_force': can_force,
            'can_start': can_start, 'can_restart': can_restart, 'capability_reason': capability_reason,
            'management': management, 'automatic_end': not observed,
            'can_logs': not observed or item['kind'] in ('container', 'compose')}
    return projection, objects, changes


def public_service(item, observation):
    spec = {k: v for k, v in item['spec'].items() if k not in ('env', 'recipe_candidate')}
    spec['env_keys'] = sorted(item['spec'].get('env', {}))
    spec['url'] = observation.get('effective_url', spec.get('url'))
    public = {k: v for k, v in item.items() if k != 'pending_spec'}
    public.update(observation)
    public['name'] = item['display_name'] or item['name']
    pending = pending_settings(item)
    desired = {**item['spec'], **pending}
    public['has_pending_configuration'] = bool(pending)
    public['configuration'] = {key: desired[key] for key in ('argv', 'cwd', 'url') if key in desired}
    public['configuration']['env_keys'] = sorted(desired.get('env', {}))
    if item['spec'].get('management') == 'observed' and pending.get('argv'):
        if public['state'] not in ACTIVE:
            public.update(can_start=True, can_restart=True, capability_reason=None, management='attached')
        elif public['state'] in ('running', 'stop_failed') and public.get('can_stop'):
            public.update(can_restart=True, capability_reason=None, management='attached')
    public.pop('effective_url', None)
    return {**public, 'spec': spec}


def observation_token(project):
    if project is None:
        return None
    return project['id'], project['created_at'], project['observation_revision']


def observation_row(project_id):
    return query('SELECT id,created_at,observation_revision,auto_scan FROM projects WHERE id=?',
                 [project_id], True)


def cached_observation(service_id):
    item = service(service_id)
    project = observation_row(item['project_id'])
    cached = query('SELECT * FROM service_observations WHERE service_id=?', [service_id], True)
    return cached_service(item, bool(project and not project['auto_scan']), cached)


def cached_service(item, paused, cached):
    projection = json.loads(cached['projection']) if cached else {
        'state': 'unknown', 'health': 'not_configured', 'run': None,
        'can_start': False, 'can_stop': False, 'can_restart': False, 'can_force': False,
        'can_logs': item['spec'].get('management') != 'observed' or item['kind'] in ('container', 'compose'),
    }
    return public_service(item, {**projection, 'observation_paused': paused,
                                 'last_checked_at': cached['checked_at'] if cached else None, 'state_fresh': False})


def prepare_observation(service_id, automatic=False, expected=None, *, background=False,
                        managed_identities=None, started=None):
    if started is None:
        started = timestamp()
    item = service(service_id)
    project = observation_row(item['project_id'])
    token = observation_token(project)
    if automatic and (not project or not project['auto_scan'] or (expected is not None and expected != token)):
        return None
    run = latest(service_id)
    result = (probe_observation(item, run, background=True, managed_identities=managed_identities)
              if background else probe_observation(item, run))
    return item, run, token, started, timestamp(), result


def persist_observation(db, service_id, checked_at, projection):
    db.execute('INSERT INTO service_observations(service_id,checked_at,projection) VALUES (?,?,?) '
               'ON CONFLICT(service_id) DO UPDATE SET checked_at=excluded.checked_at,projection=excluded.projection '
               'WHERE excluded.checked_at>=service_observations.checked_at',
               [service_id, checked_at, json.dumps(projection)])


def check_observation(db, item, run, token, started, automatic):
    row = db.execute('SELECT * FROM services WHERE id=?', [item['id']]).fetchone()
    if row is None:
        raise ValueError('服务不存在：' + item['id'])
    current = dict(row)
    current['spec'] = json.loads(current['spec'])
    row = db.execute('SELECT * FROM projects WHERE id=?', [item['project_id']]).fetchone()
    project = dict(row) if row else None
    if automatic and (not project or not project['auto_scan'] or observation_token(project) != token):
        return None
    row = db.execute('SELECT * FROM runs WHERE service_id=? ORDER BY started DESC LIMIT 1', [item['id']]).fetchone()
    current_run = dict(row) if row else None
    if current_run:
        current_run['runtime'] = json.loads(current_run['runtime'])
    row = db.execute('SELECT checked_at FROM service_observations WHERE service_id=?', [item['id']]).fetchone()
    if current != item or current_run != run or (automatic and row and row['checked_at'] > started):
        if automatic:
            return None
        raise MutationBusy('服务在检查期间发生变化，请重试')
    return project


def finish_observation(service_id, prepared, automatic=False):
    if prepared is None:
        return cached_observation(service_id)
    item, run, token, started, checked_at, result = prepared
    projection, objects, changes = result
    try:
        if objects is None and not changes:
            # A cache-only update shares SQLite's write transaction with the
            # policy switch and deletions, without taking the operation lock.
            with database() as db:
                db.execute('BEGIN IMMEDIATE')
                project = check_observation(db, item, run, token, started, automatic)
                if project is not None:
                    projection['run'] = run
                    persist_observation(db, service_id, checked_at, projection)
        else:
            lock = contextlib.nullcontext() if getattr(_mutation, 'root', None) == ROOT else mutation_lock()
            with lock:
                with database() as db:
                    project = check_observation(db, item, run, token, started, automatic)
                if project is not None:
                    if objects is not None:
                        run = reconcile_docker(item, objects)
                        projection['state'] = 'running' if projection['can_stop'] else (run['state'] if run else 'stopped')
                    else:
                        set_run(run['id'], **changes)
                        run = latest(service_id)
                    projection['run'] = run
                    projection['problem'] = projection['problem'] or (run or {}).get('error')
                    with database() as db:
                        persist_observation(db, service_id, checked_at, projection)
        if project is None:
            return cached_observation(service_id)
        return public_service(item, {**projection, 'observation_paused': not bool(project['auto_scan']),
                                     'last_checked_at': checked_at, 'state_fresh': True})
    except MutationBusy:
        if not automatic:
            raise
        return cached_observation(service_id)


def observe(service_id, *, automatic=False, expected=None):
    # Supervisor updates are independent of the mutation lock. Retry one read if
    # a service finishes naturally while an explicit inspection is in flight.
    for attempt in range(2):
        try:
            return finish_observation(service_id, prepare_observation(service_id, automatic, expected), automatic)
        except MutationBusy:
            if attempt:
                raise

def project_record(project_id):
    item = query('SELECT * FROM projects WHERE id=?', [project_id], True)
    if not item:
        raise ValueError('项目不存在')
    return item


def validated_name(name, label):
    if not isinstance(name, str):
        raise ValueError(label + '名称必须是文字')
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError(label + '名称须为 1–100 个字符')
    if any(unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp') for c in name):
        raise ValueError(label + '名称不能包含控制字符、不可见格式或换行')
    return name


def rename_project(project_id, name):
    project_record(project_id)
    name = validated_name(name, '项目')
    write('UPDATE projects SET name=? WHERE id=?', [name, project_id])
    return project_record(project_id)


def rename_service(service_id, name):
    service(service_id)
    name = validated_name(name, '服务')
    write('UPDATE services SET display_name=?,config_revision=config_revision+1 WHERE id=?', [name, service_id])
    return cached_observation(service_id)


ENV_KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def validated_health_url(value):
    if value is None or value == '':
        return None
    if isinstance(value, str) and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        try:
            endpoint = urllib.parse.urlsplit(value)
            port = endpoint.port
            if endpoint.scheme in ('http', 'https') and endpoint.hostname and (port is None or port > 0):
                return value
        except ValueError:
            pass
    raise ValueError('健康检查地址必须是有效的 HTTP 或 HTTPS URL')


def configure_service(req):
    item = service(req['id'])
    revision = item['config_revision']
    if 'config_revision' in req and (type(req['config_revision']) is not int or req['config_revision'] != revision):
        raise RuntimeError('配置已被修改，请重新打开表单后重试')
    local = item['kind'] in ('process', 'task')
    local_fields = {'argv', 'command', 'cwd', 'env', 'unset_env'}
    allowed = {'action', 'id', 'config_revision', 'name', 'url'}
    if set(req) - (allowed | (local_fields if local else set())):
        raise ValueError('服务类型不支持该配置字段')
    if 'command' in req and 'argv' in req:
        raise ValueError('请只提供一种启动命令格式')
    name = validated_name(req['name'], '服务') if 'name' in req else item['display_name']
    pending = pending_settings(item)
    active = item['spec']
    if 'url' in req:
        pending['url'] = validated_health_url(req['url'])
        if not local:
            ports = ((latest(item['id']) or {}).get('runtime') or {}).get('ports', [])
            matches = {health_binding(pending['url'], mapping) for mapping in ports}
            matches.discard(None)
            pending['url_binding_port'] = matches.pop() if len(matches) == 1 else None
    if 'argv' in req or 'command' in req:
        argv = req.get('argv')
        if 'command' in req:
            if not isinstance(req['command'], str):
                raise ValueError('启动命令必须是文字')
            try:
                argv = shlex.split(req['command'])
            except ValueError:
                raise ValueError('启动命令的引号不完整') from None
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and '\0' not in arg for arg in argv) or not argv[0].strip():
            raise ValueError('请填写单项服务的有效启动命令')
        pending['argv'] = argv
        if active.get('management') == 'observed':
            pending.update(recipe_explicit=True, recipe_automatic=False)
    if 'cwd' in req or 'argv' in req or 'command' in req:
        raw_cwd = req.get('cwd', pending.get('cwd', active.get('cwd')))
        try:
            folder = Path(raw_cwd).expanduser().resolve(strict=True)
            if not folder.is_dir():
                raise ValueError
        except (ValueError, TypeError, OSError, RuntimeError):
            raise ValueError('工作目录不存在或不是文件夹') from None
        pending['cwd'] = str(folder)
    if 'env' in req or 'unset_env' in req:
        additions = req.get('env', {})
        removals = req.get('unset_env', [])
        if (not isinstance(additions, dict) or not isinstance(removals, list) or
                any(not isinstance(key, str) or not ENV_KEY.fullmatch(key)
                    for key in [*additions, *removals]) or
                any(not isinstance(value, str) or '\0' in value for value in additions.values())):
            raise ValueError('环境变量需要有效的键和值')
        if set(additions) & set(removals):
            raise ValueError('同一个环境变量不能同时更新和删除')
        values = dict(pending.get('env', active.get('env', {})))
        unset = set(pending.get('env_unset', active.get('env_unset', ())))
        values.update(additions)
        for key in removals:
            values.pop(key, None)
        pending['env'] = values
        pending['env_unset'] = sorted((unset | set(removals)) - set(additions))
    write('UPDATE services SET display_name=?,pending_spec=?,config_revision=config_revision+1 WHERE id=?',
          [name, json.dumps(pending) if pending else None, item['id']])
    return cached_observation(item['id'])


SORT_KEYS = ('name', 'created_at', 'running_count')
SORT_DIRECTIONS = ('asc', 'desc')


def project_sort_preferences(read=query):
    row = read('SELECT value FROM metadata WHERE key=?', ['project_sidebar_preferences'], True)
    if row:
        try:
            value = json.loads(row['value'])
            if (isinstance(value, dict) and value.get('sort_key') in SORT_KEYS and
                    value.get('direction') in SORT_DIRECTIONS):
                return value
        except ValueError:
            pass
    return {'sort_key': 'name', 'direction': 'asc'}


def sort_projects(projects, preferences):
    sort_key = preferences['sort_key']
    reverse = preferences['direction'] == 'desc'
    def value(project):
        if sort_key == 'name':
            return unicodedata.normalize('NFKC', project['name']).casefold()
        return project[sort_key]

    ordered = []
    stable = sorted(projects, key=lambda project: project['id'])
    for pinned in (True, False):
        group = [project for project in stable if bool(project['pinned']) == pinned]
        known = [project for project in group if value(project) is not None]
        ordered.extend(sorted(known, key=value, reverse=reverse))
        ordered.extend(project for project in group if value(project) is None)
    return ordered


def project_sidebar(observations=None, *, read=query, registrations=None):
    import capture
    disabled = set(capture.disabled_projects(read))
    projects = read('SELECT * FROM projects')
    if registrations is None:
        registrations = read('SELECT id,project_id,spec FROM services')
    occupied = {item['project_id'] for item in registrations}
    visible = [item for item in registrations
               if (item['spec'] if isinstance(item['spec'], dict) else json.loads(item['spec'])).get('source') != 'internal']
    occupied_visible = {item['project_id'] for item in visible}
    projects = [p for p in projects if p['id'] not in occupied or p['id'] in occupied_visible]
    if observations is None:
        observations = []
        for item in visible:
            try:
                observations.append(cached_observation(item['id']))
            except ValueError:
                if query('SELECT id FROM services WHERE id=?', [item['id']], True):
                    raise
    by_id = {project['id']: project for project in projects}
    for project in projects:
        project.update(running_count=0, last_checked_at=None, capture_enabled=project['cwd'] not in disabled)
    for observation in observations:
        project = by_id.get(observation['project_id'])
        if not project:
            continue
        if observation['state'] == 'running':
            project['running_count'] += 1
        checked_at = observation['last_checked_at']
        if checked_at is not None and (project['last_checked_at'] is None or checked_at > project['last_checked_at']):
            project['last_checked_at'] = checked_at
    preferences = project_sort_preferences(read)
    return {'projects': sort_projects(projects, preferences), 'project_sort_preferences': preferences}


def deletion_blocker(item):
    labels = {'starting': '启动中', 'running': '运行中', 'stopping': '停止中',
              'stop_failed': '停止失败', 'unknown': '身份或状态未知'}
    unfinished = query('SELECT id FROM runs WHERE service_id=? AND ended IS NULL LIMIT 1', [item['id']], True)
    if item['state'] in ACTIVE or item['can_stop'] or unfinished:
        state = item['state'] if item['state'] in ACTIVE else 'unknown'
        return {'id': item['id'], 'name': item['name'], 'state': state,
                'reason': item.get('problem') or labels.get(state, '存在未结束的运行记录，需先核对')}
    return None


def service_delete_preview(service_id):
    item = observe(service_id)
    blocker = deletion_blocker(item)
    return {'id': service_id, 'name': item['name'], 'can_delete': blocker is None,
            'blockers': [blocker] if blocker else [],
            'run_count': query('SELECT COUNT(*) AS count FROM runs WHERE service_id=?', [service_id], True)['count'],
            'operation_count': query('SELECT COUNT(*) AS count FROM operations WHERE target=?', [service_id], True)['count']}


def delete_service(service_id):
    preview = service_delete_preview(service_id)
    if not preview['can_delete']:
        raise RuntimeError('不能删除服务：' + preview['blockers'][0]['reason'] + '。请先停止服务或核对其状态。')
    trash_id = uid()
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        archive = {'deleted_at': timestamp(),
                   'service': dict(db.execute('SELECT * FROM services WHERE id=?', [service_id]).fetchone()),
                   'runs': [dict(row) for row in db.execute('SELECT * FROM runs WHERE service_id=?', [service_id])],
                   'operations': [dict(row) for row in db.execute('SELECT * FROM operations WHERE target=?', [service_id])]}
        if any(run['ended'] is None for run in archive['runs']):
            raise RuntimeError('不能删除服务：仍有未结束的运行')
        db.execute('INSERT INTO metadata(key,value) VALUES (?,?)', ['deleted_service:' + trash_id, json.dumps(archive)])
        db.execute('DELETE FROM service_observations WHERE service_id=?', [service_id])
        db.execute('DELETE FROM operations WHERE target=?', [service_id])
        db.execute('DELETE FROM runs WHERE service_id=?', [service_id])
        db.execute('DELETE FROM services WHERE id=?', [service_id])
    return {'id': service_id, 'trash_id': trash_id,
            'removed_runs': preview['run_count'], 'removed_operations': preview['operation_count']}


def project_delete_preview(project_id):
    proj = project_record(project_id)
    ids = query('SELECT id FROM services WHERE project_id=? ORDER BY name', [project_id])
    blockers = []
    for row in ids:
        blocker = deletion_blocker(observe(row['id']))
        if blocker:
            blockers.append(blocker)
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
        db.execute('DELETE FROM service_observations WHERE service_id IN '
                   '(SELECT id FROM services WHERE project_id=?)', [project_id])
        db.execute('DELETE FROM services WHERE project_id=?', [project_id])
        db.execute('DELETE FROM projects WHERE id=?', [project_id])
    return {'id': project_id, 'trash_id': trash_id, 'removed_services': preview['service_count'],
            'removed_runs': preview['run_count'], 'removed_operations': preview['operation_count']}


def deleted_projects(read=query):
    items = []
    for row in read("SELECT key,value FROM metadata WHERE key GLOB 'deleted_project:*'"):
        archive = json.loads(row['value'])
        items.append({'id': row['key'].split(':', 1)[1], 'name': archive['project']['name'],
            'cwd': archive['project']['cwd'], 'deleted_at': archive['deleted_at'],
            'service_count': len(archive['services']), 'run_count': len(archive['runs'])})
    return sorted(items, key=lambda item: item['deleted_at'], reverse=True)


ARCHIVE_COLUMNS = {
    'projects': ('id', 'cwd', 'name', 'manual', 'codex_exit', 'auto_scan',
                 'observation_revision', 'created_at', 'created_at_source', 'pinned'),
    'services': ('id', 'project_id', 'name', 'kind', 'spec', 'keep', 'group_id',
                 'display_name', 'created_at', 'created_at_source', 'pending_spec', 'config_revision'),
    'runs': ('id', 'service_id', 'started', 'ended', 'state', 'exit_code', 'runtime', 'error', 'log'),
    'operations': ('id', 'target', 'action', 'trigger', 'started', 'ended', 'result', 'error'),
}


def restore_archive_rows(db, table, records):
    fields = ARCHIVE_COLUMNS[table]
    sql = 'INSERT INTO ' + table + ' (' + ','.join(fields) + ') VALUES (' + ','.join('?' for _ in fields) + ')'
    db.executemany(sql, [[item[field] for field in fields] for item in records])


def restore_project(trash_id):
    row = query('SELECT value FROM metadata WHERE key=?', ['deleted_project:' + trash_id], True)
    if not row:
        raise ValueError('已删除的项目记录不存在')
    archive = json.loads(row['value'])
    proj = archive['project']
    if query('SELECT id FROM projects WHERE id=? OR cwd=?', [proj['id'], proj['cwd']], True):
        raise RuntimeError('同一目录已有项目登记，无法恢复；不会覆盖现有项目')
    oldest = {}
    for run in archive['runs']:
        sid, started = run['service_id'], run['started']
        oldest[sid] = min(oldest.get(sid, started), started)
    archived_services = {item['id'] for item in archive['services']}
    for item in archive['services']:
        item.setdefault('display_name', None)
        item.setdefault('pending_spec', None)
        item.setdefault('config_revision', 0)
        item.setdefault('created_at', oldest.get(item['id']))
        item.setdefault('created_at_source', 'estimated' if item['created_at'] is not None else 'unknown')
    proj.setdefault('created_at', min((oldest[sid] for sid in archived_services & oldest.keys()), default=None))
    proj.setdefault('created_at_source', 'estimated' if proj['created_at'] is not None else 'unknown')
    proj.setdefault('auto_scan', 1)
    proj.setdefault('observation_revision', 0)
    proj.setdefault('pinned', 0)
    try:
        with database() as db:
            for table in ARCHIVE_COLUMNS:
                restore_archive_rows(db, table, [proj] if table == 'projects' else archive[table])
            db.execute('DELETE FROM metadata WHERE key=?', ['deleted_project:' + trash_id])
    except sqlite3.IntegrityError as exc:
        raise RuntimeError('登记或历史标识已被占用，恢复已取消；现有记录和回收记录保持不变') from exc
    return {'project': project_record(proj['id']), 'restored_services': len(archive['services']), 'restored_runs': len(archive['runs'])}


SNAPSHOT_ROWS = ('SELECT s.*,p.auto_scan AS scan,o.checked_at AS checked_at,o.projection AS projection '
                 'FROM services s LEFT JOIN projects p ON p.id=s.project_id '
                 'LEFT JOIN service_observations o ON o.service_id=s.id ORDER BY s.name')


def snapshot(refresh_project=None):
    if refresh_project not in (None, 'all'):
        project_record(refresh_project)
    automatic = refresh_project is None
    services = []
    probe_ids = []
    for row in query(SNAPSHOT_ROWS):
        row['spec'] = json.loads(row['spec'])
        if row['spec'].get('source') == 'internal':
            continue
        scan = row.pop('scan')
        checked_at, projection = row.pop('checked_at'), row.pop('projection')
        should_probe = bool(scan) if automatic else refresh_project in ('all', row['project_id'])
        if should_probe:
            probe_ids.append(row['id'])
        else:
            cached = {'checked_at': checked_at, 'projection': projection} if projection is not None else None
            services.append(cached_service(row, scan == 0, cached))

    def prepare(sid):
        try:
            return prepare_observation(sid, automatic)
        except ValueError:
            if not query('SELECT id FROM services WHERE id=?', [sid], True):
                return None
            raise

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        pending = list(pool.map(prepare, probe_ids))
    for sid, prepared in zip(probe_ids, pending):
        try:
            services.append(finish_observation(sid, prepared, automatic))
        except MutationBusy:
            services.append(observe(sid, automatic=automatic))
        except ValueError:
            if query('SELECT id FROM services WHERE id=?', [sid], True):
                raise
    return snapshot_payload(services)


def snapshot_payload(services, *, read=query, registrations=None, component_identities=None):
    metadata = {row['key']: json.loads(row['value']) for row in read(
        "SELECT key,value FROM metadata WHERE key IN ('watcher','capture','capture_diagnostic','last_codex_exit')")}
    for key, marker in (('watcher', '--watch'), ('capture', 'capture.py')):
        entry = metadata.get(key)
        if entry:
            identity = (entry.get('pid'), entry.get('birth'))
            recent = timestamp() - entry.get('updated', 0) < 20
            entry['alive'] = bool(recent and (matches(*identity, marker) if component_identities is None
                                             else component_identities.get(key) == identity))
    services.sort(key=lambda item: (item['state'] != 'running', -(item['created_at'] or 0), item['id']))
    return {**project_sidebar(services, read=read, registrations=registrations), 'services': services,
            'operations': read('SELECT * FROM operations ORDER BY started DESC LIMIT 100'),
            **{key: metadata.get(key) for key in ('watcher', 'capture', 'last_codex_exit', 'capture_diagnostic')},
            'deleted_projects': deleted_projects(read), 'generated_at': timestamp(), 'state_dir': str(ROOT)}


def snapshot_cached(db=None, *, visible=True, component_identities=None):
    import observation
    if db is None:
        with contextlib.closing(storage.open_snapshot_database(ROOT)) as db:
            return snapshot_cached(db, visible=visible, component_identities=component_identities)

    def read(sql, params=(), one=False):
        rows = [dict(row) for row in db.execute(sql, params)]
        return (rows[0] if rows else None) if one else rows

    now = timestamp()
    with db:
        db.execute('BEGIN')
        registrations = read(SNAPSHOT_ROWS)
        services = []
        for row in registrations:
            row['spec'] = json.loads(row['spec'])
            if row['spec'].get('source') == 'internal':
                continue
            paused = not row.pop('scan')
            checked_at, projection = row.pop('checked_at'), row.pop('projection')
            cached = {'checked_at': checked_at, 'projection': projection} if projection is not None else None
            item = cached_service(row, paused, cached)
            if paused:
                item['cache_state'] = 'paused'
            elif checked_at is None:
                item['cache_state'] = 'pending'
            else:
                interval = observation.interval(row['kind'], row['spec'], item, visible)
                item['cache_state'] = 'expired' if now - checked_at > interval + 4 else 'recent'
            services.append(item)
        return snapshot_payload(services, read=read, registrations=registrations,
                                component_identities=component_identities or {})


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


def exit_event(req):
    event_id = req.get('event_id', uid())
    at = req.get('event_at', timestamp())
    if not isinstance(event_id, str) or not re.fullmatch(r'[a-f0-9]{32}', event_id):
        raise ValueError('退出事件标识无效')
    if type(at) not in (int, float) or not 0 < at < math.inf:
        raise ValueError('退出事件时间无效')
    return {'event_id': event_id, 'event_at': at}


def save_exit_result(event, status, **fields):
    # Heartbeats and uncertain transport outcomes do not hold the operation lock.
    with contextlib.closing(database()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT value FROM metadata WHERE key='last_codex_exit'").fetchone()
        old = json.loads(row['value']) if row else None
        if old:
            same_event = old['event_id'] == event['event_id']
            if same_event and (status == 'running' or old['status'] == 'completed'):
                return False
            if not same_event and (status == 'completed' or old['event_at'] >= event['event_at']):
                return False
        db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)',
                   ['last_codex_exit', json.dumps({**event, 'status': status, **fields})])
        return True


def run_codex_exit(req):
    event = exit_event(req)
    if not save_exit_result(event, 'running'):
        return []  # A repeated or obsolete notification must not replay service stops.
    results = [{'project_id': p['id'], 'results': end_project({'project_id': p['id']}, 'codex_exit')}
               for p in query('SELECT id FROM projects WHERE codex_exit=1')]
    counts = dict(succeeded=0, skipped=0, failed=0)
    failures = []
    skip_reasons = {}
    for project_result in results:
        for result in project_result['results']:
            counts[result['result']] += 1
            if result['result'] == 'failed':
                item = service(result['id'])
                failures.append({'id': item['id'], 'name': item['display_name'] or item['name'],
                                 'error': private_error(item['spec'], result['error'])})
            elif result['result'] == 'skipped':
                reason = result['reason']
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
    save_exit_result(event, 'completed', **counts, failures=failures,
                     skip_reasons=[{'reason': reason, 'count': count} for reason, count in sorted(skip_reasons.items())])
    return results


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


def update_project_scan_policy(enabled, project_ids=None):
    if type(enabled) is not bool:
        raise ValueError('自动扫描开关必须为布尔值')
    update = ('UPDATE projects SET auto_scan=?,observation_revision=observation_revision+1 '
              'WHERE auto_scan!=?')
    with contextlib.closing(database()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if project_ids is None:
            return db.execute(update, [enabled, enabled]).rowcount
        registered = {row['id'] for row in db.execute('SELECT id FROM projects')}
        if not project_ids <= registered:
            raise ValueError('所选项目已变更，请重新选择')
        return db.executemany(update + ' AND id=?', [(enabled, enabled, project_id)
                                                     for project_id in project_ids]).rowcount


def dispatch(req):
    action = req['action']
    if action == 'snapshot':
        return snapshot()
    if action == 'snapshot_cached':
        return snapshot_cached()
    if action == 'refresh':
        project_id = req.get('project_id', 'all')
        if not isinstance(project_id, str) or not project_id:
            raise ValueError('手动刷新需要有效的项目标识')
        return snapshot(project_id)
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
        if path and (not re.fullmatch(r'[a-f0-9]{32}', run['id']) or path != ROOT / 'logs' / (run['id'] + '.log')):
            raise RuntimeError('日志路径不属于本工具运行记录')
        if not path or not path.exists():
            return ''
        storage.private_file(path)
        with path.open('rb') as log:
            log.seek(max(0, path.stat().st_size - 65536))
            return log.read().decode(errors='replace')
    if action == 'discover':
        return discover()
    if action == 'capture_start':
        import capture
        capture.ensure_collector()
        return True
    if action == 'service_delete_preview':
        return service_delete_preview(req['id'])
    if action in ('service_trash_list', 'service_trash_purge_preview'):
        import service_trash
        return service_trash.listing() if action == 'service_trash_list' else service_trash.purge_preview(req.get('days'))
    if action == 'project_delete_preview':
        return project_delete_preview(req['id'])
    if action == 'watcher_event':
        write('INSERT OR REPLACE INTO metadata VALUES (?,?)', ['watcher', json.dumps(req['watcher'])])
        return True
    if action == 'codex_exit_unconfirmed':
        reason = ('其他操作持续占用，退出策略未开始' if req.get('busy') is True
                  else '管理组件未返回可确认的退出策略结果')
        return save_exit_result(exit_event(req), 'unconfirmed', reason=reason)
    with mutation_lock(wait=1 if action in ('project_scan_policy', 'project_scan_policy_batch') else 0):
        if action == 'capture_policy':
            import capture
            return capture.set_policy(req['cwd'], req['enabled'])
        if action == 'project_scan_policy':
            enabled = req.get('enabled')
            if type(enabled) is not bool:
                raise ValueError('自动扫描开关必须为布尔值')
            project_record(req['id'])
            update_project_scan_policy(enabled, {req['id']})
            return project_record(req['id'])
        if action == 'project_scan_policy_batch':
            scope = req.get('scope')
            ids = req.get('ids')
            if scope == 'all' and 'ids' not in req:
                project_ids = None
            elif scope == 'selected' and isinstance(ids, list) and ids and all(isinstance(i, str) and i for i in ids):
                project_ids = set(ids)
            else:
                raise ValueError('请选择要调整扫描策略的项目')
            return {'changed': update_project_scan_policy(req.get('enabled'), project_ids)}
        if action == 'project_pin':
            pinned = req.get('pinned')
            if type(pinned) is not bool:
                raise ValueError('项目置顶必须为布尔值')
            project_record(req['id'])
            write('UPDATE projects SET pinned=? WHERE id=?', [pinned, req['id']])
            return project_sidebar()
        if action == 'project_sort_preferences':
            sort_key, direction = req.get('sort_key'), req.get('direction')
            if sort_key not in SORT_KEYS or direction not in SORT_DIRECTIONS:
                raise ValueError('项目排序规则或方向无效')
            write('INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)',
                  ['project_sidebar_preferences', json.dumps({'sort_key': sort_key, 'direction': direction})])
            return project_sidebar()
        if action == 'service_delete':
            return delete_service(req['id'])
        if action == 'service_restore':
            import service_trash
            return service_trash.restore(req['id'])
        if action == 'service_trash_purge':
            import service_trash
            return service_trash.purge(req.get('days'), req.get('ids'))
        if action == 'service_configure':
            return configure_service(req)
        if action == 'service_rename':
            return rename_service(req['id'], req.get('name'))
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
            return run_codex_exit(req)
        if action == 'project_policy':
            write('UPDATE projects SET manual=?,codex_exit=? WHERE id=?',
                  [bool(req['manual']), bool(req['codex_exit']), req['id']])
            return True
        if action == 'service_keep':
            write('UPDATE services SET keep=? WHERE id=?', [bool(req['keep']), req['id']])
            return True
        if action == 'cleanup':
            import maintenance
            return maintenance.cleanup(sys.modules[__name__], req.get('days', 30))
        raise ValueError('未知请求：' + action)


def main():
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path)
    parser.add_argument('mode', choices=['rpc', '_supervise', 'snapshot-worker'])
    parser.add_argument('run_id', nargs='?')
    args = parser.parse_args()
    if args.root:
        ROOT = args.root.expanduser().resolve()
    if args.mode == '_supervise':
        supervise(args.run_id)
        return
    if args.mode == 'snapshot-worker':
        import observation
        observation.serve()
        return
    try:
        request = json.load(sys.stdin)
        result = dispatch(request)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
    except Exception as exc:
        response = {'ok': False, 'error': str(exc)}
        if isinstance(exc, MutationBusy):
            response['error_code'] = 'mutation_busy'
        print(json.dumps(response, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    # RPC/supervisor execution must share this configured ROOT with modules
    # importing engine; otherwise a second module silently uses DEFAULT_ROOT.
    sys.modules['engine'] = sys.modules[__name__]
    main()
