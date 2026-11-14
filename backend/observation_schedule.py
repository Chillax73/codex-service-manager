"""Additive, indexed observation deadlines, compatible with existing v1 writers.

SQLite triggers keep deadlines current even for supervisors loaded before this
module was installed. No process is started or stopped here.
"""
import contextlib
import threading

import engine

_initialized = {}
_lock = threading.Lock()


def _projection_sql():
    active = ','.join("'" + state + "'" for state in sorted(engine.ACTIVE))
    changed = """(json_extract(o.projection,'$.run.id') IS NOT r.id
        OR json_extract(o.projection,'$.run.state') IS NOT r.state
        OR json_extract(o.projection,'$.run.ended') IS NOT r.ended)"""
    def deadline(visible):
        unknown, docker, terminal, health, live = (16, 7, 120, 5, 3.5) if visible else (32, 20, 240, 15, 12)
        return f"""CASE WHEN o.projection IS NULL OR {changed} THEN 0 ELSE o.checked_at + CASE
            WHEN json_extract(o.projection,'$.state')='unknown' THEN {unknown}
            WHEN s.kind IN ('container','compose') THEN {docker}
            WHEN json_extract(o.projection,'$.state') NOT IN ({active}) THEN {terminal}
            WHEN COALESCE(json_extract(s.spec,'$.url'),'')!='' THEN {health}
            ELSE {live} END END"""
    return f"""SELECT s.id,s.project_id,
        p.auto_scan=1 AND COALESCE(json_extract(s.spec,'$.source'),'')!='internal' AS eligible,
        COALESCE(json_extract(s.spec,'$.management'),'')='observed' AS observed,
        {changed} AS urgent,
        s.kind IN ('container','compose') OR COALESCE(r.state IN ({active}),0) AS live,
        {deadline(True)} AS next_visible,{deadline(False)} AS next_hidden
        FROM services s JOIN projects p ON p.id=s.project_id
        LEFT JOIN service_observations o ON o.service_id=s.id
        LEFT JOIN runs r ON r.id=(SELECT id FROM runs WHERE service_id=s.id ORDER BY started DESC LIMIT 1)"""


def _refresh(where, invalidate=False):
    # The view's aliases permit targeted recomputation in triggers without
    # reparsing unrelated service specifications in Python.
    select = ('service_id,project_id,eligible,observed,1,live,0,0' if invalidate else '*')
    return (f'DELETE FROM observation_schedule WHERE {where}; '
            f'INSERT INTO observation_schedule SELECT {select} FROM observation_deadlines WHERE {where}; ')


def ensure():
    path = engine.ROOT / 'manager.sqlite'
    stat = path.stat()
    key = (stat.st_dev, stat.st_ino, getattr(stat, 'st_birthtime', stat.st_ctime_ns))
    with _lock:
        if _initialized.get(str(path)) == key:
            return
        with contextlib.closing(engine.database()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observation_schedule'").fetchone()
                if not exists:
                    db.execute('''CREATE TABLE observation_schedule(
                        service_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,
                        eligible INTEGER NOT NULL,observed INTEGER NOT NULL,
                        urgent INTEGER NOT NULL,live INTEGER NOT NULL,
                        next_visible REAL NOT NULL,next_hidden REAL NOT NULL)''')
                    for mode in ('visible', 'hidden'):
                        db.execute(f'CREATE INDEX observation_due_{mode} ON observation_schedule(next_{mode}) WHERE eligible=1')
                    db.execute('CREATE INDEX observation_project ON observation_schedule(project_id)')
                    db.execute('CREATE VIEW observation_deadlines AS ' + _projection_sql().replace('SELECT s.id,', 'SELECT s.id AS service_id,', 1))
                    triggers = []
                    for table, column in (('services', 'id'), ('runs', 'service_id'), ('service_observations', 'service_id')):
                        for action in ('INSERT', 'UPDATE', 'DELETE'):
                            where = (f'service_id IN (OLD.{column},NEW.{column})' if action == 'UPDATE'
                                     else f"service_id={'OLD' if action == 'DELETE' else 'NEW'}.{column}")
                            body = _refresh(where, table != 'service_observations' or action == 'DELETE')
                            triggers.append((f'{table}_{action.lower()}', f'AFTER {action} ON {table}', body))
                    for action in ('INSERT', 'UPDATE OF auto_scan,observation_revision,created_at', 'DELETE'):
                        alias = 'OLD' if action == 'DELETE' else 'NEW'
                        triggers.append(('projects_' + action.split()[0].lower(), f'AFTER {action} ON projects',
                                         _refresh(f'project_id={alias}.id', True)))
                    for name, event, body in triggers:
                        db.execute(f'CREATE TRIGGER observation_{name} {event} BEGIN {body} END')
                    db.execute('INSERT INTO observation_schedule SELECT * FROM observation_deadlines')
                db.commit()
            except BaseException:
                db.rollback()
                raise
        _initialized[str(path)] = key


def due_rows(visible, observed_only, priority_project, now, limit):
    ensure()
    deadline = 'next_visible' if visible else 'next_hidden'
    # Indexed deadline selection happens before specification/runtime decoding.
    # Priority sorting only concerns due rows, never all retained history.
    return engine.query(f'''SELECT s.id,s.kind,s.spec,s.project_id,p.created_at AS project_created_at,
        p.observation_revision,r.runtime,r.ended AS run_ended
        FROM (SELECT service_id FROM observation_schedule
              WHERE eligible=1 AND {deadline}<=? {'AND observed=1' if observed_only else ''}
              ORDER BY project_id IS NOT ?,urgent DESC,live DESC,{deadline},service_id LIMIT ?) q
        JOIN services s ON s.id=q.service_id JOIN projects p ON p.id=s.project_id
        LEFT JOIN runs r ON r.id=(SELECT id FROM runs WHERE service_id=s.id ORDER BY started DESC LIMIT 1)''',
                        [now, priority_project, limit])
