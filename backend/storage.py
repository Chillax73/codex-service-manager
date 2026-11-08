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


def open_database(root):
    root = private_directory(root)
    for name in ('logs', 'capture-events'):
        if (root / name).exists() or (root / name).is_symlink():
            private_directory(root / name)
    for name in ('manager.sqlite', 'manager.sqlite-wal', 'manager.sqlite-shm'):
        private_file(root / name)
    db = sqlite3.connect(str(root / 'manager.sqlite'), timeout=30)
    db.row_factory = sqlite3.Row
    return db


def open_snapshot_database(root):
    path = root / 'manager.sqlite'
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('状态文件身份无效')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    return db
