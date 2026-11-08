"""Bounded background observations shared by the desktop worker and collector."""
import concurrent.futures
import contextlib
import fcntl
import json
import subprocess
import sys
import threading
import time

import engine
import storage


BATCH_SIZE = 12
WORKERS = 6


def interval(kind, spec, projection, visible):
    if projection['state'] == 'unknown':
        return 16 if visible else 32
    if kind in ('container', 'compose'):
        return 7 if visible else 20
    if projection['state'] not in engine.ACTIVE:
        return 120 if visible else 240
    if spec.get('url'):
        return 5 if visible else 15
    return 3.5 if visible else 12


def due_services(visible, observed_only, priority_project, now):
    candidates = []
    rows = engine.query("""SELECT s.id,s.kind,s.spec,s.project_id,p.created_at AS project_created_at,
        p.observation_revision,r.id AS run_id,r.state AS run_state,r.ended AS run_ended,r.runtime,
        o.checked_at,o.projection
        FROM services s JOIN projects p ON p.id=s.project_id AND p.auto_scan=1
        LEFT JOIN service_observations o ON o.service_id=s.id
        LEFT JOIN runs r ON r.id=(SELECT id FROM runs WHERE service_id=s.id ORDER BY started DESC LIMIT 1)""")
    for row in rows:
        spec = json.loads(row['spec'])
        if spec.get('source') == 'internal' or (observed_only and spec.get('management') != 'observed'):
            continue
        projection = json.loads(row['projection']) if row['projection'] else None
        run = (projection or {}).get('run') or {}
        changed = (run.get('id'), run.get('state'), run.get('ended')) != (
            row['run_id'], row['run_state'], row['run_ended'])
        checked = row['checked_at']
        due_at = checked + interval(row['kind'], spec, projection, visible) if projection else 0
        if changed or due_at <= now:
            live = row['kind'] in ('container', 'compose') or row['run_state'] in engine.ACTIVE
            priority = (row['project_id'] != priority_project, not changed, not live, due_at, row['id'])
            token = (row['project_id'], row['project_created_at'], row['observation_revision'])
            pid = None
            if (row['kind'] in ('task', 'process') and spec.get('management') != 'observed'
                    and row['runtime'] and row['run_ended'] is None):
                pid = json.loads(row['runtime']).get('supervisor_pid')
            candidates.append((priority, row['id'], token, pid))
    return [(sid, token, pid) for _, sid, token, pid in sorted(candidates)[:BATCH_SIZE]]


def managed_identities(pids):
    if not pids:
        return {}
    try:
        result = subprocess.run(['/bin/ps', '-ww', '-p', ','.join(map(str, sorted(pids))),
                                 '-o', 'pid=', '-o', 'lstart=', '-o', 'stat=', '-o', 'command='],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return {}  # Fall back to existing per-process diagnostics.
    if result.returncode not in (0, 1) or result.stderr:
        return {}
    identities = dict.fromkeys(pids)
    for line in result.stdout.splitlines():
        fields = line.split(None, 7)
        if len(fields) == 8 and fields[0].isdigit() and 'Z' not in fields[6]:
            pid = int(fields[0])
            if pid in identities:
                identities[pid] = {'birth': ' '.join(fields[1:6]), 'command': fields[7]}
    return identities


def observe_due(*, visible=False, observed_only=False, priority_project=None):
    """Re-evaluate due work while holding a lock independent of explicit controls."""
    with (engine.ROOT / 'observation.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        candidates = due_services(visible, observed_only, priority_project, engine.timestamp())
        if not candidates:
            return 0
        started = engine.timestamp()
        identities = managed_identities({pid for _, _, pid in candidates if type(pid) is int and pid > 0})
        checked = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as workers:
            pending = {workers.submit(engine.prepare_observation, sid, True, token, background=True,
                                      managed_identities=identities, started=started): sid
                       for sid, token, _ in candidates}
            for future in concurrent.futures.as_completed(pending):
                sid = pending[future]
                try:
                    prepared = future.result()
                    if prepared is not None:
                        result = engine.finish_observation(sid, prepared, automatic=True)
                        checked += bool(result['state_fresh'])
                except ValueError:
                    if engine.query('SELECT id FROM services WHERE id=?', [sid], True):
                        raise
        return checked


def verify_components():
    identities = {}
    rows = engine.query("SELECT key,value FROM metadata WHERE key IN ('watcher','capture')")
    for row in rows:
        data = json.loads(row['value'])
        marker = '--watch' if row['key'] == 'watcher' else 'capture.py'
        pair = (data.get('pid'), data.get('birth'))
        if engine.timestamp() - data.get('updated', 0) < 20 and engine.matches(*pair, marker):
            identities[row['key']] = pair
    return identities


class DesktopObserver:
    def __init__(self):
        self.visible = True
        self.priority = None
        self.identities = {}
        self.available = True
        self.wake = threading.Event()
        self.finished = threading.Event()
        self.mode_lock = threading.Lock()

    def update(self, visible, priority):
        with self.mode_lock:
            changed = self.visible != visible
            self.visible = visible
            if priority is not None:
                self.priority = priority
            if changed or priority is not None:
                self.wake.set()

    def run(self):
        checked_components_at = 0
        while not self.finished.is_set():
            with self.mode_lock:
                self.wake.clear()
                visible, priority = self.visible, self.priority
                self.priority = None
            try:
                now = time.monotonic()
                if now >= checked_components_at:
                    self.identities = verify_components()
                    checked_components_at = now + 8
                observe_due(visible=visible, priority_project=priority)
                self.available = True
            except Exception:
                self.identities = {}
                self.available = False
            self.wake.wait((.8 if visible else 2) if self.available else 4)


def serve(stdin=sys.stdin, stdout=sys.stdout):
    observer = DesktopObserver()
    with contextlib.closing(engine.database()), contextlib.closing(storage.open_snapshot_database(engine.ROOT)) as db:
        thread = threading.Thread(target=observer.run, name='csm-observer', daemon=True)
        thread.start()
        try:
            for line in stdin:
                try:
                    request = json.loads(line)
                    visible = request.get('visible')
                    priority = request.get('priority_project')
                    if (request.get('action') != 'snapshot_cached' or type(visible) is not bool
                            or (priority is not None and not isinstance(priority, str))):
                        raise ValueError('无效的缓存快照请求')
                    result = engine.snapshot_cached(db, visible=visible, component_identities=observer.identities)
                    result['observation_available'] = observer.available
                    observer.update(visible, priority)
                    response = {'ok': True, 'result': result}
                except (ValueError, TypeError, AttributeError):
                    response = {'ok': False, 'error': '无效的缓存快照请求'}
                except Exception:
                    response = {'ok': False, 'error': '暂时无法读取状态缓存'}
                print(json.dumps(response, ensure_ascii=False), file=stdout, flush=True)
        finally:
            observer.finished.set()
            observer.wake.set()
