"""Record only lifecycle metadata; no transcript, environment or general tool output."""
import fcntl
import json
from pathlib import Path
import sys
import time

root = Path(__file__).resolve().parent / '.state'
root.mkdir(parents=True, exist_ok=True)
try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(1)
record = {key: payload.get(key) for key in ('hook_event_name', 'session_id', 'turn_id', 'cwd', 'tool_name', 'reason')}
record['received_at'] = time.time()
record['source'] = 'codex_hook'
tool_input = payload.get('tool_input', {})
command = tool_input.get('command', tool_input.get('cmd')) if isinstance(tool_input, dict) else None
# Persist the command only for explicitly marked PoC commands, never arbitrary user commands.
if isinstance(command, str) and 'CSM_POC_MARKER' in command:
    record['poc_command'] = command
with (root / 'hook-events.jsonl').open('a') as output:
    fcntl.flock(output, fcntl.LOCK_EX)
    output.write(json.dumps(record, ensure_ascii=False) + '\n')
