"""Explicit retention of manager-owned data, never arbitrary recorded paths."""
import json
from pathlib import Path
import re
import stat


def cleanup(engine, days):
    if type(days) is not int or not 1 <= days <= 36500:
        raise ValueError('保留天数须为 1–36500 的整数')
    before = engine.timestamp() - days * 86400
    # Keep the last state even for stopped services; deleting it changes capabilities.
    rows = engine.query('''SELECT id,log FROM runs r WHERE ended IS NOT NULL AND ended<?
        AND id NOT IN (SELECT id FROM runs newer WHERE newer.service_id=r.service_id
                      ORDER BY started DESC, rowid DESC LIMIT 1)''', [before])
    removed_logs = skipped_logs = 0
    logs = engine.ROOT / 'logs'
    for row in rows:
        if not row['log']:
            continue
        expected = logs / (row['id'] + '.log')
        if not re.fullmatch(r'[a-f0-9]{32}', row['id']) or Path(row['log']) != expected or logs.is_symlink():
            skipped_logs += 1
            continue
        for suffix in ('', '.1', '.2'):
            path = Path(str(expected) + suffix)
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    skipped_logs += 1
                    continue
                path.unlink()
                removed_logs += 1
            except FileNotFoundError:
                pass
    with engine.database() as db:
        db.executemany('DELETE FROM runs WHERE id=?', [(row['id'],) for row in rows])
        db.execute('DELETE FROM operations WHERE ended IS NOT NULL AND ended<?', [before])
    # A live descendant may need an old event after collector recovery.
    row = engine.query('SELECT value FROM metadata WHERE key=?', ['capture_tracking'], True)
    active_events = {info['event'] for info in json.loads(row['value']).values()} if row else set()
    events = engine.ROOT / 'capture-events'
    removed_events = 0
    if not events.is_symlink():
        for path in events.glob('*.json'):
            if not re.fullmatch(r'[a-f0-9]{32}', path.stem) or path.stem in active_events:
                continue
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                event = json.loads(path.read_text())
                if event.get('finished', event.get('created', engine.timestamp())) < before:
                    path.unlink()
                    removed_events += 1
            except (OSError, ValueError, TypeError):
                continue
    return {'removed_runs': len(rows), 'removed_logs': removed_logs,
            'skipped_logs': skipped_logs, 'removed_events': removed_events}
