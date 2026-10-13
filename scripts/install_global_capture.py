#!/usr/bin/env python3
"""Install/remove only our global hooks. Never writes Codex trust state."""
import argparse
import json
import os
from pathlib import Path
import shlex

os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'Codex Service Manager 自动观察'
INSTRUCTIONS_BEGIN = '<!-- csm-global-capture:begin -->'
INSTRUCTIONS_END = '<!-- csm-global-capture:end -->'


def prepare_instructions(content, remove=False):
    if INSTRUCTIONS_BEGIN in content:
        first = content.index(INSTRUCTIONS_BEGIN)
        if INSTRUCTIONS_END not in content[first:]:
            raise ValueError('现有全局采集说明缺少结束标记，拒绝覆盖')
        last = content.index(INSTRUCTIONS_END, first) + len(INSTRUCTIONS_END)
        content = content[:first] + content[last:]
    if not remove:
        content = content.rstrip() + '\n\n' + INSTRUCTIONS_BEGIN + '''
## 本机服务自动观察

本机已安装 Codex Service Manager 用户级采集组件。正常审核并启用的 PreToolUse/PostToolUse 会在 shell 命令前后自动添加 capture.py arm 检查点；该记录会保存本机启动身份、项目归属及运行状态，是服务启动的正常配套采集行为。它不保存原始命令、完整环境、工具输出或聊天正文，也不自动停止业务服务。

申请服务启动的端口/进程权限时，说明已启用的采集检查点及必要的本机元数据写入；不要为绕过审批而删掉前缀，不使用信任或沙箱绕过选项。若审批仍拒绝，报告具体原因并正常取得所需授权。启动业务命令的授权不扩大到其他服务、凭据或外部通信。
''' + INSTRUCTIONS_END + '\n'
    return content.lstrip('\n')


def prepare(existing, remove=False):
    result = json.loads(json.dumps(existing))
    hooks = result.setdefault('hooks', {})
    for event in ('PreToolUse', 'PostToolUse'):
        groups = []
        for group in hooks.get(event, []):
            handlers = [h for h in group.get('hooks', []) if h.get('statusMessage') != LABEL]
            if handlers:
                groups.append({**group, 'hooks': handlers})
        if not remove:
            groups.append({'matcher': '^Bash$', 'hooks': [{'type': 'command',
                'command': '/usr/bin/python3 ' + shlex.quote(str(ROOT / 'backend/capture.py')) + ' hook',
                'timeout': 20, 'statusMessage': LABEL}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    return result


def main():
    parser = argparse.ArgumentParser(description='一次安装任意本机聊天的服务自动观察 hooks')
    parser.add_argument('--codex-dir', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--remove', action='store_true')
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--instructions', action='store_true', help='同时维护全局 AGENTS.md 中本工具的正常采集说明')
    args = parser.parse_args()
    path = args.codex_dir.expanduser().resolve() / 'hooks.json'
    prior = path.read_text() if path.exists() else '{}'
    prepared = prepare(json.loads(prior), args.remove)
    if args.preview:
        print(json.dumps(prepared, ensure_ascii=False, indent=2))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name('hooks.json.csm-backup')
        if not backup.exists():
            backup.write_text(prior)
    path.write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)
    if args.instructions:
        instructions = path.with_name('AGENTS.md')
        content = instructions.read_text() if instructions.exists() else ''
        updated = prepare_instructions(content, args.remove)
        if instructions.exists():
            backup = instructions.with_name('AGENTS.md.global-capture-backup')
            if not backup.exists():
                backup.write_text(content)
        instructions.write_text(updated)
    print(json.dumps({'path': str(path), 'installed': not args.remove, 'trust_modified': False,
                      'next': '通过 Codex /hooks 正常审核两个 hook，并在新聊天验证实际事件'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
