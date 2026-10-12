"""Observe a human-performed Codex quit. Never synthesize a lifecycle event.

Run through scripts/csm --task in the UI fixture's non-exit project.
This recorder reads process identities and the real watcher's operations only.
"""
import json
from pathlib import Path
import sys
import time

BASE = Path(__file__).resolve().parents[1]
from support import prepare_workspace
prepare_workspace()
sys.path.insert(0, str(BASE / 'backend'))
import engine

REPORT = BASE / 'docs/evidence/real-codex-quit.json'
ui = json.loads((BASE / 'docs/evidence/ui-fixtures.json').read_text())
docker = json.loads((BASE / 'docs/evidence/ui-docker-fixtures.json').read_text())
eligible = [ui['services']['web']['id'], docker['service_id'], *docker['group']['members']]
retained = [ui['services']['keep']['id'], ui['services']['other']['id']]


def capture():
    snapshot = engine.snapshot()
    return {
        'at': time.time(), 'watcher': snapshot['watcher'],
        'services': {sid: engine.observe(sid) for sid in eligible + retained},
        'operations': [op for op in snapshot['operations'] if op['target'] in eligible + retained],
        'projects': [p for p in snapshot['projects'] if p['id'] in
                     {ui['services']['web']['project_id'], ui['services']['other']['project_id']}],
    }


def save(report):
    # Atomic replacement lets the next session read a complete snapshot.
    temporary = REPORT.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(REPORT)


before = capture()
assert before['watcher']['alive'], '正式退出观察器不可用'
assert before['watcher']['bundle_ids'] == ['com.openai.codex']
assert all(before['services'][sid]['state'] == 'running' for sid in eligible + retained)
assert before['services'][retained[0]]['keep'] == 1
policies = {p['id']: p for p in before['projects']}
assert policies[ui['services']['web']['project_id']]['codex_exit'] == 1
assert policies[ui['services']['other']['project_id']]['codex_exit'] == 0
targets = {pid: engine.identity(pid) for pid in before['watcher']['instances']}
assert targets and all(targets.values()), '未找到实际 Codex 进程'
report = {'status': 'waiting_for_actual_quit', 'source': 'actual OS process identities and independent NSWorkspace watcher',
          'target_processes': targets, 'before': before, 'eligible': eligible, 'retained': retained,
          'window_close': 'pending human observation; process checks cannot prove a window was closed',
          'menu_bar': 'pending human observation; native tool cannot expose the status menu'}
save(report)
print('READY: 实际退出记录程序已就绪', flush=True)

deadline = time.monotonic() + 24 * 3600
while time.monotonic() < deadline:
    if all(engine.identity(pid) != birth for pid, birth in targets.items()):
        report['target_termination_observed_at'] = time.time()
        break
    time.sleep(2)
else:
    report.update(status='timed_out', passed=False)
    save(report)
    sys.exit(1)

until = time.monotonic() + 120
while time.monotonic() < until:
    after = capture()
    stopped = all(after['services'][sid]['state'] == 'stopped' and
                  after['services'][sid]['run']['id'] == before['services'][sid]['run']['id']
                  for sid in eligible)
    untouched = all(after['services'][sid]['state'] == 'running' and
                    after['services'][sid]['run']['id'] == before['services'][sid]['run']['id']
                    for sid in retained)
    recorded = all(any(op['target'] == sid and op['trigger'] == 'codex_exit' and
                       op['started'] >= before['at'] and op['result'] == 'succeeded'
                       for op in after['operations']) for sid in eligible)
    if stopped and untouched and recorded and after['watcher']['alive']:
        report.update(status='actual_quit_verified', passed=True, after=after)
        save(report)
        print('PASS: 实际 Codex 终止、四项停止、保留例外、其他项目隔离、观察器继续运行', flush=True)
        break
    time.sleep(2)
else:
    report.update(status='actual_quit_checks_failed', passed=False, after=capture())
    save(report)
    sys.exit(1)
