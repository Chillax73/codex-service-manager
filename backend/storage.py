"""SQLite schema and private local files; no process or lifecycle decisions."""
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('状态目录身份无效')
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
    return path


def private_file(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('状态文件身份无效')
    if stat.S_IMODE(info.st_mode) != 0o600:
        path.chmod(0o600)


def save_json(path, value):
    path = Path(path)
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def database(root):
    root = private_directory(root)
    for name in ('logs', 'capture-events'):
        if (root / name).exists() or (root / name).is_symlink():
            private_directory(root / name)
    for name in ('manager.sqlite', 'manager.sqlite-wal', 'manager.sqlite-shm'):
        private_file(root / name)
    db = sqlite3.connect(str(root / 'manager.sqlite'), timeout=30)
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
    private_file(root / 'manager.sqlite')
    return db
