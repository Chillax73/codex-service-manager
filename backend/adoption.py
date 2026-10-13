"""Control verified hook descendants without signalling shared process groups."""
import json
import os
from pathlib import Path
import re
import shlex
import signal
import time

import engine

ENV_KEYS = ('PATH', 'LANG', 'JAVA_HOME')
SHELLS = ('sh', 'bash', 'zsh', 'fish')


def launch_recipe(command):
    # Shell expressions cannot be reconstructed as argv. In particular do not
    # replay a multi-service command or persist arbitrary shell text/secrets.
    if any(c in command for c in '$`\n\r;|&<>*?~{}()\\#'):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if argv[:1] == ['exec']:
        argv = argv[1:]
    if not argv or re.search(r'(?i)(password|passwd|secret|token|credential|api[-_]?key|authorization|bearer|://)', ' '.join(argv)):
        return None
    name = Path(argv[0]).name
    supported = (
        name.startswith('python') and argv[1:3] == ['-m', 'http.server'] or
        name == 'node' and len(argv) > 1 and argv[1].endswith(('.js', '.mjs', '.cjs')) or
        name == 'npm' and argv[1:3] in (['run', 'dev'], ['run', 'start']) or
        name == 'npm' and argv[1:2] == ['start'] or
        name == 'npx' and argv[1:2] == ['vite'] or
        name == 'java' and any(w in ('-jar', '-cp', '-classpath') for w in argv[1:]) or
        name in ('mvn', 'mvnw') and 'spring-boot:run' in argv[1:])
    return {'argv': argv} if supported else None


def descendants(table, root):
    depths = {root: 0}
    changed = True
    while changed:
        changed = False
        for pid, info in table.items():
            if pid not in depths and info['ppid'] in depths:
                depths[pid] = depths[info['ppid']] + 1
                changed = True
    return depths


def exclusive_scope(item, members, table):
    for row in engine.query('SELECT id FROM services WHERE id<>?', [item['id']]):
        run = engine.latest(row['id'])
        if not run or run['ended'] is not None:
            continue
        runtime = run['runtime']
        for pid_key, birth_key in (('pid', 'birth'), ('child_pid', 'child_birth'), ('supervisor_pid', 'supervisor_birth')):
            pid = runtime.get(pid_key)
            if pid in members and table[pid]['birth'] == runtime.get(birth_key):
                raise RuntimeError('启动器与其他已登记服务共用，拒绝扩大停止范围')


def stop_plan(item, table=None, tracked=None, listeners=None):
    import capture
    run = engine.latest(item['id']) or {}
    runtime = run.get('runtime', {})
    table = capture.process_table() if table is None else table
    pid = runtime.get('pid')
    if runtime.get('control_members') and run.get('state') == 'stop_failed':
        members = []
        for m in runtime['control_members']:
            if m['pid'] in table:
                if table[m['pid']]['birth'] != m['birth']:
                    raise RuntimeError('停止范围内 PID 已复用，拒绝控制')
                members.append(m)
        if not members:
            raise RuntimeError('原进程已退出，端口占用者不属于可控制范围')
        allowed = {m['pid'] for m in members}
        for m in members:
            if not set(descendants(table, m['pid'])).issubset(allowed):
                raise RuntimeError('停止失败后出现新的未知子进程，拒绝扩大控制范围')
        exclusive_scope(item, allowed, table)
        return {'root': members[0]['pid'], 'members': members}
    if pid not in table or table[pid]['birth'] != runtime.get('birth'):
        raise RuntimeError('观察进程出生身份无法确认，拒绝控制旧 PID')
    if tracked is None:
        row = engine.query('SELECT value FROM metadata WHERE key=?', ['capture_tracking'], True)
        tracked = json.loads(row['value']) if row else {}
    saved = {str(m['pid']): m for m in runtime.get('control_members', [])}
    def verified(p):
        info = tracked.get(str(p)) or saved.get(str(p))
        return info if info and p in table and info['birth'] == table[p]['birth'] else None
    origin = verified(pid)
    if not origin:
        raise RuntimeError('缺少已确认的启动关联，暂时只能观察')
    event = origin.get('event')
    root = pid
    while not (verified(root) or {}).get('root'):
        parent = table[root]['ppid']
        if parent <= 1:
            break  # A previously verified service has been reparented.
        info = verified(parent)
        if info and info.get('root') and table[parent]['name'] in SHELLS:
            break  # Never signal the tool/interactive shell.
        if not info or info.get('event') != event:
            # A live unverified launcher may respawn this process.
            raise RuntimeError('启动器归属无法确认，不能单独停止可能被重新拉起的子进程')
        root = parent
    if table[root]['name'] in SHELLS and (verified(root) or {}).get('root'):
        raise RuntimeError('不能接管启动 shell')
    depths = descendants(table, root)
    for member in depths:
        info = verified(member)
        if not info or info.get('event') != event:
            raise RuntimeError('服务树包含尚未确认身份的子进程，请等待采集后重试')
    listeners = capture.listeners() if listeners is None else listeners
    if len([p for p in depths if listeners.get(p)]) > 1:
        raise RuntimeError('启动器包含多个监听服务，需分别配置独立控制范围')
    exclusive_scope(item, depths, table)
    members = [{**table[p], **verified(p), 'pid': p, 'depth': depths[p]} for p in depths]
    return {'root': root, 'members': sorted(members, key=lambda m: m['depth'])}


def recipe_matches(recipe, plan):
    executable = Path(recipe['argv'][0]).name.lower()
    actual = plan['members'][0]['name'].lower()
    # Node's process.title changes ps comm to e.g. "npm run dev".
    if executable in ('npm', 'npx') and actual.split()[:1] == ['npm']:
        return True
    if executable.startswith('python'):
        return actual.startswith('python')
    allowed = {'npm': ('node', 'npm'), 'npx': ('node', 'npx'),
               'mvn': ('java', 'mvn', 'sh'), 'mvnw': ('java', 'mvnw', 'sh')}
    return actual in allowed.get(executable, (executable,))


def configure(req):
    item = engine.service(req['id'])
    if item['spec'].get('management') != 'observed' or item['kind'] not in ('process', 'task'):
        raise ValueError('只有观察中的本机服务需要补充启动配置')
    argv = req.get('argv') or shlex.split(req.get('command', ''))
    if not isinstance(argv, list) or not argv or not all(isinstance(w, str) and w for w in argv):
        raise ValueError('请填写单项服务的有效启动命令')
    cwd = str(Path(req.get('cwd') or item['spec']['cwd']).expanduser().resolve(strict=True))
    if not Path(cwd).is_dir():
        raise ValueError('工作目录不是文件夹')
    env = {**engine.environment({}), **item['spec'].get('env', {}),
           **{str(k): str(v) for k, v in (req.get('env') or {}).items()}}
    spec = {**item['spec'], 'argv': argv, 'cwd': cwd, 'env': env,
            'recipe_explicit': True}
    spec.pop('recipe_candidate', None)
    engine.write('UPDATE services SET spec=? WHERE id=?', [json.dumps(spec), item['id']])
    return engine.observe(item['id'])


def remember_recipe(item, plan, event):
    recipe = event.get('launch_recipe')
    if not recipe or not event.get('launch_env', {}).get('PATH') or not recipe_matches(recipe, plan):
        return
    spec = {**item['spec'], 'argv': recipe['argv'], 'cwd': event['cwd'],
            'env': {k: v for k, v in event.get('launch_env', {}).items() if k in ENV_KEYS},
            'recipe_automatic': True}
    engine.write('UPDATE services SET spec=? WHERE id=?', [json.dumps(spec), item['id']])


def remember_scope(item, plan):
    run = engine.latest(item['id'])
    if run and run['ended'] is None:
        scope = [{k: m[k] for k in ('pid', 'birth', 'event', 'root') if k in m} for m in plan['members']]
        engine.set_run(run['id'], runtime={**run['runtime'], 'launch_members': scope})


def stop(item, force=False):
    run = engine.latest(item['id'])
    if not run or run['ended'] is not None:
        return engine.observe(item['id'])
    plan = stop_plan(item)
    runtime = {**run['runtime'], 'control_members': plan['members']}
    engine.set_run(run['id'], state='stopping', runtime=runtime)
    try:
        # Stop the launcher first to prevent it recreating children. Each child
        # is still verified independently, even after being reparented.
        for member in plan['members']:
            current = engine.identity(member['pid'])
            if current is None:
                continue
            if current['birth'] != member['birth']:
                raise RuntimeError('停止期间进程身份变化，拒绝向复用 PID 发送信号')
            os.kill(member['pid'], signal.SIGKILL if force else signal.SIGTERM)
        until = time.time() + 5
        while time.time() < until:
            remaining = [m for m in plan['members'] if engine.matches(m['pid'], m['birth'])]
            if not remaining:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('正常停止超时，服务仍在运行；可选择强制停止')
        busy = [p for p in item['spec'].get('ports', []) if not engine.port_free(p)]
        if busy:
            raise RuntimeError('已确认进程退出，但端口仍占用，拒绝启动副本：' + str(busy))
        engine.set_run(run['id'], ended=engine.timestamp(), state='cancelled' if item['kind'] == 'task' else 'stopped',
                       runtime=runtime, error=None)
    except Exception as exc:
        engine.set_run(run['id'], state='stop_failed', runtime=runtime, error=str(exc))
        raise
    return engine.observe(item['id'])
