"""Real Docker/Compose acceptance with synthetic hook inputs, isolated state.

Only uniquely labelled fixture containers are created/removed. Desktop callback
coverage is verified separately; these tests never claim to prove that coverage.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend'))
import capture
import engine
from support import stop_test_watcher


def main():
    temporary = tempfile.TemporaryDirectory(prefix='csm-capture-docker-', dir=str(BASE / '.test-state'))
    root = Path(temporary.name); project = root / 'project'; project.mkdir()
    ownership = root / 'ownership'; ownership.mkdir()
    engine.ROOT = root
    tag = 'csm-capture-' + engine.uid()[:12]
    created = []; checks = []
    before_ids = engine.docker('ps', '-aq').splitlines()
    before = engine.container_observation(*before_ids) if before_ids else []
    image = engine.docker('image', 'inspect', 'python:3.12-slim', '--format', '{{.Id}}')
    capture.ensure_collector()

    def submit(command):
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'session_id': tag,
                   'tool_use_id': engine.uid(), 'cwd': str(project), 'tool_input': {'command': command}}
        rewritten = capture.hook(payload)['hookSpecificOutput']['updatedInput']['command']
        result = subprocess.run(['/bin/zsh', '-lc', rewritten], cwd=project, capture_output=True, text=True, timeout=25)
        if result.returncode:
            raise RuntimeError(result.stderr)
        payload.update(hook_event_name='PostToolUse', tool_response={'output': result.stdout})
        capture.hook(payload)
        return result.stdout.strip()

    def register_owned(cid):
        # Satisfy the project-wide Docker registration convention in a separate
        # test ownership registry, without masking the observer under test.
        result = subprocess.run([str(BASE / 'scripts/csm'), '--root', str(ownership), 'container', cid,
                                 '--cwd', str(project)], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stdout)

    def wait_count(count):
        until = time.time() + 15
        while time.time() < until:
            items = [s for s in engine.snapshot()['services'] if s['management'] == 'observed' and s['state'] == 'running']
            if len(items) == count:
                return items
            time.sleep(.2)
        raise RuntimeError('容器自动观察超时：' + json.dumps(items, ensure_ascii=False))

    try:
        # No image pulls or starts of pre-existing containers.
        cid = submit('docker run -d --pull=never --label csm.capture.test=' + tag + ' --name ' + tag +
                     ' ' + image + ' python -m http.server 8000')
        created.append(cid); register_owned(cid)
        items = wait_count(1)
        item = items[0]
        assert item['spec']['container_id'] == cid and not item['can_stop'] and not item['can_restart']
        checks.append('单容器工具返回 ID、归属、只读能力')
        first_run = item['run']['id']
        submit('docker restart ' + cid)
        until = time.time() + 10
        while time.time() < until and engine.observe(item['id'])['run']['id'] == first_run:
            time.sleep(.2)
        assert engine.observe(item['id'])['run']['id'] != first_run
        checks.append('实际容器重启产生独立运行历史')
        file = project / 'compose.yaml'
        file.write_text('services:\n  one:\n    image: ' + image + '\n    pull_policy: never\n'
                        '    command: [python, -m, http.server, "8000"]\n    labels: {csm.capture.test: ' + tag + '}\n'
                        '  two:\n    image: ' + image + '\n    pull_policy: never\n'
                        '    command: [python, -m, http.server, "8000"]\n    labels: {csm.capture.test: ' + tag + '}\n')
        submit('docker compose -p ' + tag + '-compose -f compose.yaml up -d')
        compose_ids = engine.docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + tag + '-compose').splitlines()
        created.extend(compose_ids)
        for cid in compose_ids:
            register_owned(cid)
        items = wait_count(3)
        assert len({s['id'] for s in items}) == 3
        assert len({s['group_id'] for s in items if s['group_id']}) == 1
        assert all(s['project_id'] == engine.project(str(project))['id'] for s in items)
        checks.append('Compose 两成员、服务组与真实标签归属')
        time.sleep(2)
        assert len(wait_count(3)) == 3
        checks.append('重复采集不重复登记')
        observation = engine.container_observation(created[0])[0]
        assert 'Env' not in observation['Config']
        checks.append('观察字段白名单，不读取容器环境或健康日志')
    finally:
        exact = engine.docker('ps', '-aq', '--filter', 'label=csm.capture.test=' + tag).splitlines()
        if exact:
            engine.docker('rm', '-f', *exact)
        if (project / 'compose.yaml').exists():
            engine.docker('compose', '-p', tag + '-compose', '-f', str(project / 'compose.yaml'), 'down')
        engine.dispatch({'action': 'stop', 'id': capture.INTERNAL_ID})
        stop_test_watcher(root)
        stop_test_watcher(ownership)
        after = engine.container_observation(*before_ids) if before_ids else []
        assert before == after, '原有容器身份、状态或启动时间发生变化'
        checks.append('仅清理测试资源，原有容器状态不变')
        temporary._finalizer.detach()
    evidence = {'generated_at': time.time(), 'passed': True, 'checks': checks, 'state_root': str(root),
                'hook_input': 'synthetic; actual Docker and Compose', 'desktop_callback_verified_by_this_test': False}
    (BASE / 'docs/evidence/capture-docker-tests.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': True, 'checks': checks}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
