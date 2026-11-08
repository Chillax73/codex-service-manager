"""Restore or explicitly purge individual service registrations and their local history."""
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import engine


PREFIX = 'deleted_service:'
HEX_ID = re.compile(r'[a-f0-9]{32}')


def decode(row):
    archive = json.loads(row['value'])
    if not isinstance(archive, dict) or not isinstance(archive.get('service'), dict):
        raise ValueError('回收记录缺少服务登记')
    deleted_at = archive.get('deleted_at')
    if type(deleted_at) not in (int, float) or not 0 < deleted_at < float('inf'):
        raise ValueError('回收记录缺少有效删除时间')
    for name in ('runs', 'operations'):
        records = archive.get(name)
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError('回收历史记录无效')
    if any(run.get('ended') is None for run in archive['runs']):
        raise ValueError('回收记录含未结束的运行')
    return archive


def archives():
    return engine.query("SELECT key,value FROM metadata WHERE key GLOB 'deleted_service:*'")


def listing():
    projects = {p['id']: p['name'] for p in engine.query('SELECT id,name FROM projects')}
    result = []
    for row in archives():
        sid = row['key'][len(PREFIX):]
        try:
            archive = decode(row)
            service = archive['service']
            project_id = service['project_id']
            result.append({'id': sid, 'name': service.get('display_name') or service['name'],
                           'project': projects.get(project_id), 'deleted_at': archive['deleted_at'],
                           'run_count': len(archive['runs']), 'status': 'purging' if archive.get('purging') else 'recoverable'})
        except (ValueError, KeyError, TypeError):
            result.append({'id': sid, 'name': '无法读取的服务归档', 'status': 'invalid'})
    return sorted(result, key=lambda item: (item.get('deleted_at', 0), item['id']), reverse=True)


def docker_identity(item, run):
    spec = json.loads(item['spec'])
    if item['kind'] == 'container':
        cid = spec.get('container_id')
        return None, {cid} if cid else set()
    member = tuple(spec.get(key) for key in ('compose_project', 'member', 'file'))
    runtime = json.loads(run['runtime']) if run else {}
    return member if all(member) else None, set(runtime.get('container_ids', []))


def check_docker_conflict(db, service, runs):
    if service['kind'] not in ('container', 'compose'):
        return
    latest = max(runs, key=lambda run: run['started'], default=None)
    member, ids = docker_identity(service, latest)
    for row in db.execute("SELECT * FROM services WHERE kind IN ('container','compose')"):
        run = db.execute('SELECT runtime FROM runs WHERE service_id=? ORDER BY started DESC LIMIT 1',
                         [row['id']]).fetchone()
        registered, registered_ids = docker_identity(row, run)
        if (member is not None and registered == member) or ids.intersection(registered_ids):
            raise RuntimeError('容器或 Compose 成员已登记，无法恢复')


def restore(trash_id):
    if not isinstance(trash_id, str) or not HEX_ID.fullmatch(trash_id):
        raise ValueError('回收记录标识无效')
    key = PREFIX + trash_id
    try:
        with closing(engine.database()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM metadata WHERE key=?', [key]).fetchone()
            if not row:
                raise ValueError('已删除的服务记录不存在')
            archive = decode(row)
            if archive.get('purging'):
                raise RuntimeError('回收记录已开始清理，不能恢复')
            item = archive['service']
            if not db.execute('SELECT id FROM projects WHERE id=?', [item['project_id']]).fetchone():
                raise RuntimeError('原项目不存在，恢复原项目后再试')
            if db.execute('SELECT id FROM services WHERE id=?', [item['id']]).fetchone():
                raise RuntimeError('服务已重新登记，无法恢复；现有登记不会被覆盖')
            check_docker_conflict(db, item, archive['runs'])
            engine.restore_archive_rows(db, 'services', [item])
            engine.restore_archive_rows(db, 'runs', archive['runs'])
            engine.restore_archive_rows(db, 'operations', archive['operations'])
            db.execute('DELETE FROM metadata WHERE key=?', [key])
    except sqlite3.IntegrityError:
        raise RuntimeError('登记或历史标识冲突，恢复已回滚') from None
    except (KeyError, TypeError):
        raise ValueError('服务回收记录格式无效') from None
    return {'id': item['id'], 'project_id': item['project_id'], 'restored_runs': len(archive['runs'])}


def purge_candidates(days):
    if type(days) is not int or not 1 <= days <= 36500:
        raise ValueError('保留天数须为 1–36500 的整数')
    cutoff = engine.timestamp() - days * 86400
    eligible, invalid = {}, 0
    for row in archives():
        sid = row['key'][len(PREFIX):]
        try:
            if not HEX_ID.fullmatch(sid):
                raise ValueError('回收记录标识无效')
            archive = decode(row)
            if archive.get('purging') or archive['deleted_at'] < cutoff:
                eligible[sid] = archive
        except (ValueError, TypeError):
            invalid += 1
    return eligible, invalid


def purge_preview(days):
    candidates, invalid = purge_candidates(days)
    return {'ids': sorted(candidates), 'count': len(candidates), 'invalid': invalid,
            'retry_count': sum(bool(item.get('purging')) for item in candidates.values())}


def log_references():
    """Index live and recoverable archive references, including project archives."""
    references = {}
    def add(owner, records):
        for run in records:
            if not isinstance(run, dict):
                continue
            for value in (run.get('id'), run.get('log')):
                if isinstance(value, str) and value:
                    references.setdefault(value, set()).add(owner)

    add('', engine.query('SELECT id,log FROM runs'))
    for row in engine.query("SELECT key,value FROM metadata WHERE key GLOB 'deleted_service:*' OR key GLOB 'deleted_project:*'"):
        try:
            value = json.loads(row['value'])
            if isinstance(value, dict) and isinstance(value.get('runs'), list):
                add(row['key'], value['runs'])
        except ValueError:
            continue
    return references


def remove_logs(archive, key, references, counts):
    logs = engine.ROOT / 'logs'
    try:
        fd = os.open(logs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        fd = None
    try:
        for run in archive['runs']:
            stored = run.get('log')
            if not stored:
                continue
            rid = run.get('id')
            if not isinstance(rid, str) or not HEX_ID.fullmatch(rid):
                counts['skipped_logs'] += 1
                continue
            name = rid + '.log'
            expected = str(logs / name)
            if stored != expected or (references.get(rid, set()) | references.get(expected, set())) - {key}:
                counts['skipped_logs'] += 1
                continue
            if fd is None:
                continue
            for suffix in ('', '.1', '.2'):
                filename = name + suffix
                try:
                    info = os.stat(filename, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                        counts['skipped_logs'] += 1
                        continue
                    os.unlink(filename, dir_fd=fd)
                    counts['removed_logs'] += 1
                except FileNotFoundError:
                    continue
    finally:
        if fd is not None:
            os.close(fd)


def purge(days, ids):
    if not isinstance(ids, list) or not ids or any(not isinstance(sid, str) for sid in ids):
        raise ValueError('请先预览并确认要清理的服务归档')
    candidates, _ = purge_candidates(days)
    if not set(ids) <= candidates.keys():
        raise ValueError('回收记录已变化，请重新预览')
    counts = dict(removed_archives=0, removed_logs=0, skipped_logs=0, failed_archives=0)
    references = log_references()
    for sid in sorted(set(ids)):
        key = PREFIX + sid
        archive = candidates[sid]
        if not archive.get('purging'):
            archive['purging'] = True
            engine.write('UPDATE metadata SET value=? WHERE key=?', [json.dumps(archive), key])
        try:
            remove_logs(archive, key, references, counts)
        except OSError:
            counts['failed_archives'] += 1
            continue
        engine.write('DELETE FROM metadata WHERE key=?', [key])
        for run in archive['runs']:
            for value in (run.get('id'), run.get('log')):
                if isinstance(value, str) and value in references:
                    references[value].discard(key)
        counts['removed_archives'] += 1
    return counts
