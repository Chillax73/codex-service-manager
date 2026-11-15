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


def due_services(visible, observed_only, priority_project, now, observed_local=None):
    from observation_schedule import due_rows
    candidates = []
    for row in due_rows(visible, observed_only, priority_project, now, BATCH_SIZE):
        spec = json.loads(row['spec'])
        if (observed_local is not None and row['kind'] in ('task', 'process')
                and spec.get('management') == 'observed' and row['runtime'] and row['run_ended'] is None):
            observed_local.append(row['id'])
        token = (row['project_id'], row['project_created_at'], row['observation_revision'])
        pid = None
        if (row['kind'] in ('task', 'process') and spec.get('management') != 'observed'
                and row['runtime'] and row['run_ended'] is None):
            pid = json.loads(row['runtime']).get('supervisor_pid')
        candidates.append((row['id'], token, pid))
    return candidates


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


def local_probe_context(service_ids):
    """A single immutable observation batch, never passed to actual controls."""
    if not service_ids:
        return None
    import adoption
    import capture
    try:
        with engine.read_context():
            versions = {}
            for sid in service_ids:
                try:
                    versions[sid] = (engine.service(sid), engine.latest(sid))
                except ValueError:
                    continue
            table = capture.process_table()
            pids = set()
            for item, run in versions.values():
                runtime = (run or {}).get('runtime', {})
                roots = [runtime.get('pid')] + [m['pid'] for key in ('control_members', 'launch_members')
                                                for m in runtime.get(key, [])]
                for pid in roots:
                    if pid in table:
                        pids.update(adoption.descendants(table, pid))
            row = engine.query("SELECT value FROM metadata WHERE key='capture_tracking'", one=True)
            return {'versions': versions, 'table': table,
                    'tracked': json.loads(row['value']) if row else {},
                    'listeners': capture.listeners(pids), 'runs': engine.live_runtimes()}
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None  # Existing individual probes retain their conservative failures.


def observe_due(*, visible=False, observed_only=False, priority_project=None):
    """Re-evaluate due work while holding a lock independent of explicit controls."""
    with (engine.ROOT / 'observation.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        observed_local = []
        candidates = due_services(visible, observed_only, priority_project, engine.timestamp(), observed_local)
        if not candidates:
            return 0
        started = engine.timestamp()
        identities = managed_identities({pid for _, _, pid in candidates if type(pid) is int and pid > 0})
        process_context = local_probe_context(observed_local)
        checked = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as workers:
            pending = {workers.submit(engine.prepare_observation, sid, True, token, background=True,
                                      managed_identities=identities, started=started,
                                      process_context=process_context): sid
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
                    result = engine.snapshot_cached(db, visible=visible, component_identities=observer.identities,
                                                    snapshot_format=request.get('snapshot_format', 'full'))
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
