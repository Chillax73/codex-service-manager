#!/usr/bin/env python3
"""Export only allowlisted validation facts; never copy raw runtime metadata."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
RAW = BASE / 'docs/evidence'
DESTINATION = BASE / 'docs/results/validation-summary.json'


def read(name):
    return json.loads((RAW / name).read_text())


def export():
    groups = []
    for name, label in [
        ('process-tests.json', '本机进程集成'),
        ('project-tests.json', '真实前端/Java/Python 项目'),
        ('docker-tests.json', 'Docker/Compose 集成'),
        ('docker-failures.json', '真实容器失败场景'),
        ('endpoint-tests.json', '端点/时间/日志契约'),
        ('ui-tests.json', '实际原生界面'),
    ]:
        source = read(name)
        checks = source.get('checks')
        passed = (bool(checks) and all(c['passed'] is True for c in checks)) if checks is not None else source['passed'] is True
        assert passed, label + ' 尚未通过'
        groups.append({'name': label, 'checks_passed': len(checks) if checks is not None else source['tests_run'],
                       'passed': True, 'local_source': name})

    quit_result = read('real-codex-quit.json')
    assert quit_result['passed'] and quit_result['status'] == 'actual_quit_verified'
    before, after = quit_result['before'], quit_result['after']
    for sid in quit_result['eligible']:
        assert after['services'][sid]['state'] == 'stopped'
        assert before['services'][sid]['run']['id'] == after['services'][sid]['run']['id']
        ops = [op for op in after['operations'] if op['target'] == sid and
               op['trigger'] == 'codex_exit' and op['started'] >= before['at']]
        assert len(ops) == 1 and ops[0]['result'] == 'succeeded'
    for sid in quit_result['retained']:
        assert after['services'][sid]['state'] == 'running'
        assert before['services'][sid]['run']['id'] == after['services'][sid]['run']['id']
    assert after['watcher']['alive'] and quit_result.get('human_observation')
    cleanup = read('ui-cleanup.json')
    assert all(cleanup[k] for k in ('passed', 'processes_stopped', 'containers_removed',
                                  'volume_removed', 'existing_containers_unchanged', 'docker_restored_off'))
    assert read('ui-final-state.json')['passed']

    result = {
        'schema_version': 1, 'validated_on': '2026-10-12',
        'environment': {'os': 'macOS 15.7.3', 'architecture': 'Apple Silicon', 'build_target': 'macOS 13+'},
        'acceptance': {'passed': ['A-%02d' % n for n in range(1, 17)],
                       'scope': '项目配套入口登记、本机服务/任务、Docker/Compose；远程服务不在范围内'},
        'test_groups': groups,
        'actual_codex_quit': {'passed': True, 'source': '实际进程终止与独立 NSWorkspace 观察器，未合成事件',
                             'eligible_stopped': len(quit_result['eligible']),
                             'retained_same_run': len(quit_result['retained']), 'watcher_survived': True},
        'human_observation': {'codex_window_close_keeps_service': True, 'status_menu_detail_available': True,
                             'status_menu_each_control_repeated': False},
        'cleanup': {'passed': True, 'test_services_removed': len(cleanup['owned_service_ids']),
                    'test_containers_removed': len(cleanup['owned_container_ids']), 'test_volume_removed': True,
                    'existing_containers_unchanged': True, 'docker_restored_off': True},
        'limitations': ['未经配套入口启动的普通进程只发现，不承诺自动接管',
                        '其他 macOS 版本和 Intel 未实测', '系统关机不保证停止完成',
                        '系统菜单栏详情由人工确认，快捷控制未逐项重复人工点击'],
        'privacy': {'method': 'allowlist summary; no runtime metadata copied',
                    'excluded': ['用户名/邮箱/绝对路径', '聊天/会话标识', '进程和容器标识',
                                 '环境变量值与日志内容', '原始截图与本机配置']},
    }
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return DESTINATION


if __name__ == '__main__':
    print(export().relative_to(BASE))
