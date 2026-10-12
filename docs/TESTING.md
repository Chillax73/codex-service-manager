# 复现与 CI

## 基础回归（无需第三方 Python 包）

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_support.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_endpoints.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_engine.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_projects.py
./scripts/build.sh
```

入口自动创建 ignored `.test-state/` 与 `docs/evidence/`。进程验收启动真实测试服务，需要本机进程与端口权限，只清理自己的服务，保留本机诊断。原始诊断不上传。

## 真实 Vite / Spring / Python 项目

Vite 7.3.7 要求 Node `^20.19.0 || >=22.12.0`（见锁文件）；Java 17+、Maven 3.6.3+、npm 必须可用。可按自己的包管理器安装 Node/Maven/JDK，设置 JAVA_HOME；`/usr/bin/python3 scripts/doctor.py --projects` 检查可执行文件。系统 PATH 优先，也可指定 `CSM_NPM`、`CSM_MAVEN` 的可执行文件绝对路径（空格无需改写源码）。

```sh
/usr/bin/python3 scripts/prepare_fixtures.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_projects.py
```

准备入口运行锁定的 npm ci 和 Maven package，不启动服务；初次 Maven 启动可能继续下载插件依赖。项目测试通过 scripts/csm 登记，默认新建独立状态目录，停止并释放自己的监听端口。`--root <测试目录>` 与 `--keep` 仅用于明确需要保留测试服务的桌面验收，禁止指向正式状态目录。

## Docker 独立复验

```sh
docker pull python:3.12-slim
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_docker.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_docker_failures.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_project_docker.py
```

Docker/Compose 可用，测试以唯一标签隔离对象并核对原有容器状态，删除仅测试拥有的容器/卷。镜像版本标签可能更新，结果须同时保留本机具体镜像 ID。

`.github/workflows/ci.yml` 在 push/PR 上运行 macOS 构建、签名验证、14 组真实进程、9 组端点、3 组目录/工具契约、13 组项目管理契约和隐私扫描，并在独立 Linux job 检查锁定 npm 夹具依赖的高危公告。`.github/workflows/docker.yml` 在独立 Linux runner 手动执行 Docker/Compose 、真实失败场景及项目改名/删除/恢复。这不表示应用支持 Linux。工作流无写权限、不上传原始证据。

真实主窗口、菜单栏与实际 Codex 退出继续使用 [验收索引](../specs/001-service-manager/acceptance.md) 在解锁桌面操作；引擎中的合成策略测试只能验证规则，不是实际退出证据。
