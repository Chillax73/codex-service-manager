#!/usr/bin/env python3
"""Companion entry point. All normal starts register cwd automatically."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import engine


def ensure_watcher():
    state = engine.query('SELECT value FROM metadata WHERE key=?', ['watcher'], True)
    state = json.loads(state['value']) if state else {}
    if engine.matches(state.get('pid'), state.get('birth'), '--watch'):
        return
    candidates = [Path(__file__).resolve().parents[1] / 'build/Codex Service Manager.app/Contents/MacOS/ServiceManager',
                  Path(__file__).resolve().parent.parent / 'MacOS/ServiceManager']
    executable = next((p for p in candidates if p.exists()), None)
    if executable:
        with (engine.ROOT / 'watcher.log').open('ab', buffering=0) as output:
            subprocess.Popen([str(executable), '--watch', '--root', str(engine.ROOT), '--engine', str(Path(engine.__file__).resolve())],
                             stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)


def main():
    parser = argparse.ArgumentParser(description='Codex Service Manager 配套入口')
    parser.add_argument('--root', type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    launch = sub.add_parser('launch')
    launch.add_argument('--name', required=True)
    launch.add_argument('--id')
    launch.add_argument('--cwd', default=os.getcwd())
    launch.add_argument('--url')
    launch.add_argument('--port', type=int, action='append', default=[])
    launch.add_argument('--task', action='store_true')
    launch.add_argument('--env', action='append', default=[])
    launch.add_argument('--source', choices=['companion', 'internal'], default='companion')
    launch.add_argument('argv', nargs=argparse.REMAINDER)
    container = sub.add_parser('container')
    container.add_argument('container_id')
    container.add_argument('--cwd', default=os.getcwd())
    container.add_argument('--name')
    container.add_argument('--url')
    compose = sub.add_parser('compose')
    compose.add_argument('--file', required=True)
    compose.add_argument('--project', dest='compose_project')
    compose.add_argument('--cwd', default=os.getcwd())
    compose.add_argument('--env', action='append', default=[])
    compose.add_argument('--register-only', action='store_true')
    sub.add_parser('list')
    sub.add_parser('discover')
    sub.add_parser('capture-start')
    capture_policy = sub.add_parser('capture-policy', help='按规范化工作目录控制后续 hook 采集；不停止已有服务')
    capture_policy.add_argument('--cwd', default=os.getcwd())
    capture_policy.add_argument('--enabled', choices=['on', 'off'], required=True)
    configure = sub.add_parser('configure-observed', help='补充观察服务的单项启动配置，不停止或启动服务')
    configure.add_argument('id')
    configure.add_argument('--cwd')
    configure.add_argument('--env', action='append', default=[])
    configure.add_argument('argv', nargs='+')
    for action in ('start', 'stop', 'restart', 'force', 'history', 'logs'):
        sub.add_parser(action).add_argument('id')
    policy = sub.add_parser('policy')
    policy.add_argument('project_id')
    policy.add_argument('--manual', choices=['on', 'off'], required=True)
    policy.add_argument('--codex-exit', choices=['on', 'off'], required=True)
    rename = sub.add_parser('rename-project', help='修改项目显示名称')
    rename.add_argument('project_id')
    rename.add_argument('name')
    remove = sub.add_parser('delete-project', help='仅删除已无活动/未知服务的项目登记')
    remove.add_argument('project_id')
    remove.add_argument('--confirm', action='store_true', required=True, help='确认删除登记和历史记录')
    restore = sub.add_parser('restore-project', help='恢复已移除项目的登记与历史，不启动服务')
    restore.add_argument('trash_id')
    finish = sub.add_parser('end')
    finish.add_argument('project_id')
    args = parser.parse_args()
    if args.root:
        engine.ROOT = args.root.expanduser().resolve()
    if args.action == 'capture-start':
        import capture
        capture.ensure_collector()
        print(json.dumps({'ok': True, 'result': '自动采集组件已启动'}, ensure_ascii=False))
        return
    req = vars(args).copy()
    req.pop('root', None)
    if args.action == 'launch':
        req['argv'] = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
        req['kind'] = 'task' if args.task else 'process'
        req['ports'] = args.port
    if 'env' in req:
        req['env'] = dict(value.split('=', 1) for value in req['env'])
    if args.action == 'container':
        req['action'] = 'register_container'
    elif args.action == 'configure-observed':
        req['action'] = 'configure_observed'
        req['argv'] = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    elif args.action == 'capture-policy':
        req.update(action='capture_policy', enabled=args.enabled == 'on')
    elif args.action == 'compose':
        req['action'] = 'register_compose'
        req['start'] = not args.register_only
    elif args.action == 'list':
        req['action'] = 'snapshot'
    elif args.action == 'policy':
        req.update(action='project_policy', id=args.project_id, manual=args.manual == 'on', codex_exit=args.codex_exit == 'on')
    elif args.action == 'rename-project':
        req.update(action='project_rename', id=args.project_id)
    elif args.action == 'delete-project':
        req.update(action='project_delete', id=args.project_id)
        req.pop('confirm', None)
    elif args.action == 'restore-project':
        req.update(action='project_restore', id=args.trash_id)
    elif args.action == 'end':
        req['action'] = 'end_project'
    try:
        engine.database().close()
        ensure_watcher()
        result = engine.dispatch(req)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    main()
