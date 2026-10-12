"""Round-one process supervisor. Stdlib only; not the production application."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

DEFAULT_ROOT = Path(__file__).resolve().parent / '.state'


def now():
    return time.time()


def connect(root):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(root / 'services.sqlite'), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS services (id TEXT PRIMARY KEY, spec TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, service_id TEXT NOT NULL, started REAL NOT NULL,
        ended REAL, state TEXT NOT NULL, exit_code INTEGER, supervisor_pid INTEGER,
        supervisor_birth TEXT, child_pid INTEGER, child_birth TEXT, stop_requested INTEGER DEFAULT 0,
        error TEXT, log TEXT NOT NULL);
    ''')
    return db


def identity(pid):
    if not pid:
        return None
    p = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'stat=', '-o', 'command='],
                       capture_output=True, text=True)
    parts = p.stdout.strip().split(None, 6)
    if p.returncode or len(parts) < 7 or 'Z' in parts[5]:
        return None
    return {'birth': ' '.join(parts[:5]), 'command': parts[6]}


def matches(pid, birth, token=None):
    observed = identity(pid)
    return bool(observed and observed['birth'] == birth and
                (token is None or token in observed['command']))


def update(root, run_id, **values):
    with connect(root) as db:
        db.execute('UPDATE runs SET ' + ', '.join(k + '=?' for k in values) + ' WHERE id=?',
                   list(values.values()) + [run_id])


def latest(root, service_id):
    with connect(root) as db:
        row = db.execute('SELECT * FROM runs WHERE service_id=? ORDER BY started DESC LIMIT 1',
                         [service_id]).fetchone()
    return dict(row) if row else None


def spec_for(root, service_id):
    with connect(root) as db:
        row = db.execute('SELECT spec FROM services WHERE id=?', [service_id]).fetchone()
    if row is None:
        raise ValueError('unknown service: ' + service_id)
    return json.loads(row[0])


def healthy(url):
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=.5) as response:
            return response.status == 200
    except Exception:
        return False


def status(root, service_id):
    spec = spec_for(root, service_id)
    run = latest(root, service_id)
    live = bool(run and run['ended'] is None and
                matches(run['supervisor_pid'], run['supervisor_birth'], run['id']))
    return {'service_id': service_id, 'project': spec['cwd'], 'spec': spec,
            'run': run, 'supervisor_alive': live,
            'child_alive': bool(run and run['ended'] is None and matches(run['child_pid'], run['child_birth'])),
            'health': healthy(spec.get('url')) if live else None,
            'observed_state': ('unknown' if run and run['ended'] is None and not live else
                               run['state'] if run else 'never_started')}


@contextlib.contextmanager
def lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'manager.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def start(root, service_id, spec=None):
    with lock(root):
        prior = latest(root, service_id)
        if prior and prior['ended'] is None:
            raise ValueError('previous run unresolved; refuse duplicate start')
        if spec is not None:
            with connect(root) as db:
                db.execute('INSERT OR REPLACE INTO services VALUES (?, ?)', [service_id, json.dumps(spec)])
        else:
            spec = spec_for(root, service_id)
        run_id = uuid.uuid4().hex
        log = str(root / (run_id + '.log'))
        with connect(root) as db:
            db.execute('INSERT INTO runs (id,service_id,started,state,log) VALUES (?,?,?,?,?)',
                       [run_id, service_id, now(), 'starting', log])
        with open(log, 'ab', buffering=0) as output:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--root', str(root),
                              '_supervise', run_id], stdin=subprocess.DEVNULL,
                             stdout=output, stderr=output, start_new_session=True, close_fds=True)
        until = now() + 5
        while now() < until:
            run = latest(root, service_id)
            if run['state'] != 'starting':
                return status(root, service_id)
            time.sleep(.05)
        raise TimeoutError('supervisor startup timed out; inspect recorded run')


def stop(root, service_id):
    with lock(root):
        run = latest(root, service_id)
        if not run or run['ended'] is not None:
            return status(root, service_id)
        if not matches(run['supervisor_pid'], run['supervisor_birth'], run['id']):
            raise RuntimeError('supervisor identity unavailable; refusing to signal a stale PID')
        update(root, run['id'], stop_requested=1)
        try:
            os.kill(run['supervisor_pid'], signal.SIGTERM)
        except ProcessLookupError:
            pass
        until = now() + 8
        while now() < until:
            current = latest(root, service_id)
            if current['ended'] is not None:
                return status(root, service_id)
            time.sleep(.05)
        raise TimeoutError('stop did not finish; no unrelated processes signalled')


def group_members(group_id):
    observed = subprocess.run(['/bin/ps', '-axo', 'pid=,pgid=,stat='], capture_output=True, text=True)
    if observed.returncode:
        raise RuntimeError('cannot inspect process group')
    members = []
    for line in observed.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and int(fields[1]) == group_id and 'Z' not in fields[2]:
            members.append(int(fields[0]))
    return members


def terminate_group(child):
    if not group_members(child.pid):
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    until = now() + 2
    while now() < until:
        child.poll()
        if not group_members(child.pid):
            return
        time.sleep(.05)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def supervise(root, run_id):
    with connect(root) as db:
        run = dict(db.execute('SELECT * FROM runs WHERE id=?', [run_id]).fetchone())
    spec = spec_for(root, run['service_id'])
    requested = [False]
    def handle_stop(signum, frame):
        requested[0] = True
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    child = None
    try:
        env = dict(os.environ)
        env.update(spec.get('env', {}))
        child = subprocess.Popen(spec['argv'], cwd=spec['cwd'], env=env,
                                 stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        own = identity(os.getpid())
        born = identity(child.pid)
        update(root, run_id, state='running', supervisor_pid=os.getpid(),
               supervisor_birth=own['birth'], child_pid=child.pid,
               child_birth=born['birth'] if born else None)
        while child.poll() is None and not requested[0]:
            time.sleep(.05)
        terminate_group(child)
        code = child.wait(timeout=3)
        with connect(root) as db:
            requested_in_db = db.execute('SELECT stop_requested FROM runs WHERE id=?', [run_id]).fetchone()[0]
        state = ('stopped' if requested[0] or requested_in_db else
                 'completed' if code == 0 and spec['kind'] == 'task' else
                 'exited' if code == 0 else 'failed')
        update(root, run_id, ended=now(), state=state, exit_code=code)
    except Exception as exc:
        if child:
            try:
                terminate_group(child)
            except Exception as cleanup_error:
                exc = RuntimeError(str(exc) + '; cleanup: ' + str(cleanup_error))
        update(root, run_id, ended=now(), state='failed', error=str(exc))


def end_project(root, project, trigger):
    if trigger not in ('manual', 'codex_exit'):
        return []
    accepted = ('manual', 'both') if trigger == 'manual' else ('codex_exit', 'both')
    with connect(root) as db:
        services = [(row['id'], json.loads(row['spec'])) for row in db.execute('SELECT * FROM services')]
    return [stop(root, sid) for sid, spec in services
            if spec['cwd'] == str(Path(project).resolve()) and spec['policy'] in accepted]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest='action', required=True)
    sp = sub.add_parser('start')
    sp.add_argument('id')
    sp.add_argument('--cwd', default=str(Path.cwd()))
    sp.add_argument('--url')
    sp.add_argument('--kind', choices=['service', 'task'], default='service')
    sp.add_argument('--policy', choices=['manual', 'codex_exit', 'both', 'keep'], default='manual')
    sp.add_argument('--env', action='append', default=[])
    sp.add_argument('--argv', nargs=argparse.REMAINDER, required=True)
    for action in ('status', 'stop', 'restart', 'history'):
        sub.add_parser(action).add_argument('id')
    ep = sub.add_parser('end-project')
    ep.add_argument('project')
    ep.add_argument('--trigger', required=True)
    sub.add_parser('_supervise').add_argument('run_id')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == '_supervise':
        supervise(root, args.run_id)
        return
    if args.action == 'start':
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'JAVA_HOME') if key in os.environ}
        env.update(dict(item.split('=', 1) for item in args.env))
        if not args.argv:
            parser.error('--argv needs an executable')
        result = start(root, args.id, {'cwd': str(Path(args.cwd).resolve()), 'argv': args.argv,
                    'url': args.url, 'kind': args.kind, 'policy': args.policy, 'env': env})
    elif args.action == 'status':
        result = status(root, args.id)
    elif args.action == 'stop':
        result = stop(root, args.id)
    elif args.action == 'restart':
        stop(root, args.id)
        result = start(root, args.id)
    elif args.action == 'end-project':
        result = end_project(root, args.project, args.trigger)
    else:
        with connect(root) as db:
            result = [dict(row) for row in db.execute('SELECT * FROM runs WHERE service_id=? ORDER BY started', [args.id])]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
