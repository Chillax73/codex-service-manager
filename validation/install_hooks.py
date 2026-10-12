"""Install this project's prepared metadata-only hooks; never grant hook trust."""
from pathlib import Path
import json
import shlex

base = Path(__file__).resolve().parent
source = base / 'hooks.json'
command = '/usr/bin/python3 ' + shlex.quote(str(base / 'hook_recorder.py'))
events = ('SessionStart', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionEnd')
payload = {'description': '本项目服务管理第一轮验证；仅记录事件元数据，不操作服务。',
           'hooks': {event: [{'hooks': [{'type': 'command', 'command': command,
                       'timeout': 3 if event == 'SessionEnd' else 5,
                       'statusMessage': '记录服务管理验证事件'}]}] for event in events}}
prepared = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
target = base.parent / '.codex/hooks.json'
if target.exists():
    if target.read_text() != prepared:
        raise SystemExit('Existing hooks differ; refusing to overwrite.')
else:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x') as output:
        output.write(prepared)
source.write_text(prepared)
print('Installed metadata-only hooks: ' + str(target))
print('Hook trust was not changed. Review/trust is still required in Codex.')
