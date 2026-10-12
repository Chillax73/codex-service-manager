"""Local-only test directories and configurable executable lookup."""
import os
from pathlib import Path
import shutil

BASE = Path(__file__).resolve().parents[1]


def prepare_workspace(base=BASE):
    for relative in ('.test-state', 'docs/evidence'):
        (base / relative).mkdir(parents=True, exist_ok=True)


def tool(name, variable):
    configured = os.environ.get(variable)
    executable = shutil.which(configured or name)
    if not executable:
        raise RuntimeError(f'找不到 {name}：安装并加入 PATH，或设置 {variable}=可执行文件路径')
    return executable


def stop_test_watcher(root):
    """Stop only the identity-verified watcher of a test-owned state directory."""
    import json
    import signal
    import time
    import engine
    root = Path(root).resolve()
    root.relative_to((BASE / '.test-state').resolve())
    previous = engine.ROOT
    engine.ROOT = root
    try:
        row = engine.query('SELECT value FROM metadata WHERE key=?', ['watcher'], True)
        watcher = json.loads(row['value']) if row else {}
        identity = engine.identity(watcher.get('pid'))
        if not identity:
            return
        if not engine.matches(watcher['pid'], watcher.get('birth'), '--watch') or str(root) not in identity['command']:
            raise RuntimeError('测试观察器身份不符，拒绝清理')
        os.kill(watcher['pid'], signal.SIGTERM)
        until = time.time() + 5
        while engine.matches(watcher['pid'], watcher['birth'], '--watch') and time.time() < until:
            time.sleep(.05)
        if engine.matches(watcher['pid'], watcher['birth'], '--watch'):
            raise RuntimeError('测试观察器未结束')
    finally:
        engine.ROOT = previous
