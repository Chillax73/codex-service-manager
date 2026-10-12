#!/usr/bin/env python3
"""Install/remove a narrow project instruction without overwriting user instructions."""
import argparse
import json
from pathlib import Path
import shlex

BEGIN = '<!-- codex-service-manager:begin -->'
END = '<!-- codex-service-manager:end -->'
ROOT = Path(__file__).resolve().parents[1]


def integrate(project, remove=False):
    path = Path(project).expanduser().resolve(strict=True) / 'AGENTS.md'
    content = path.read_text() if path.exists() else ''
    if BEGIN in content:
        first = content.index(BEGIN)
        if END not in content[first:]:
            raise ValueError('现有接入段落缺少结束标记，拒绝覆盖')
        last = content.index(END, first) + len(END)
        content = content[:first] + content[last:]
    if not remove:
        cli = shlex.quote(str(ROOT / 'scripts/csm'))
        content = content.rstrip() + '\n\n' + BEGIN + f'''
## 本机服务管理接入

本项目开发时，新启动的长期本机服务、后台任务、Docker/Compose 必须通过配套入口登记，自动使用当前项目 cwd。不得用未登记的后台启动方式代替。

- 本机服务：`{cli} launch --name <稳定名称> --url <健康URL> --port <监听端口> -- <原启动命令及参数>`。不要改写原命令的业务行为；没有 URL/端口时省略。启动参数使用原工作目录。
- 后台任务：相同入口加 `--task`；非后台的一次性检查不需要登记。
- Docker：先使用原命令创建容器，再立即 `{cli} container <返回的容器ID> --cwd <项目目录>`；由 Codex 完成登记，不要求用户补填。
- Compose：`{cli} compose --file <文件绝对路径> --cwd <项目目录>`。已有 Compose 项目明确提供 `--project <原项目名>`，避免创建副本。
- 必要环境显式 `--env KEY=VALUE`；不要读取或保存完整环境、聊天或凭据文件。
- 状态/控制：`{cli} list`、`stop <serviceID>`、`restart <serviceID>`。不要用旧 PID 直接 kill。
- 用户明确说结束该项目工作时使用 `end <projectID>`。聊天回复结束、切换/归档聊天不是应用退出，禁止调用 codex_exit。

服务通过此入口脱离 Codex 工具进程存活，原生管理界面自动出现记录。普通未经入口启动的进程仅能发现，不能假定全部已接管。
''' + END + '\n'
    if path.exists():
        backup = path.with_name('AGENTS.md.csm-backup')
        if not backup.exists():
            backup.write_text(path.read_text())
    path.write_text(content.lstrip('\n'))
    return {'path':str(path),'installed':not remove}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('project',type=Path)
    parser.add_argument('--remove',action='store_true')
    args = parser.parse_args()
    print(json.dumps(integrate(args.project,args.remove),ensure_ascii=False))
