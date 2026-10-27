# 来源与第三方依赖

本次公开快照来自本项目的 Codex 辅助开发源码；检查范围为全部 Git 跟踪源码、测试夹具、文档和锁文件。未发现 vendored 第三方源码、第三方品牌图标、复制的图片资源或预编译第三方库。新贡献必须补充来源信息；该检查不等于外部权属审计。

产品 Python 后端仅使用标准库；原生界面使用 macOS 自带 SwiftUI/AppKit/Foundation 和 SF Symbols，系统框架/符号并未作为独立资源复制进仓库，使用受 Apple 平台条款约束。本项目不使用 OpenAI 官方 logo；名称描述使用场景，不表示官方授权或关联。

| 依赖/用途 | 许可与来源 | 分发方式 |
|---|---|---|
| Vite 7.3.7 / 前端测试夹具 | [MIT](https://github.com/vitejs/vite/blob/v7.3.7/LICENSE) | 只提交 package.json/锁文件，npm ci 安装。 |
| npm 锁文件间接依赖 | 62 个 MIT、picocolors 为 ISC、source-map-js 为 BSD-3-Clause（含可选平台包） | 各依赖保留 npm 包内原 LICENSE，未随源码打包。 |
| Spring Boot 3.5.5 / Java 测试夹具 | [Apache-2.0](https://github.com/spring-projects/spring-boot/blob/v3.5.5/LICENSE.txt) | 只提交 POM/自编测试源码，Maven 安装。 |
| Spring、Jackson、Tomcat、Micrometer 等 Java 间接依赖 | 核对 30 个运行组件 POM/父 POM，见 [逐项清单](docs/results/java-dependencies.json)；主要 Apache-2.0，Logback EPL-1.0/LGPL 双许可，SLF4J MIT，Jakarta 注解 EPL-2.0/GPL-2.0 with Classpath Exception 双许可 | 不提交 JAR/依赖源码；采用其适用原许可，MIT 不覆盖第三方依赖。 |
| python:3.12-slim / Docker 测试镜像 | [官方镜像来源与许可](https://hub.docker.com/_/python)（Python PSF 与基础系统组件各自许可） | 从官方 registry 拉取；镜像不随 release 分发。 |
| actions/checkout / CI | [MIT](https://github.com/actions/checkout/blob/main/LICENSE) | 固定官方提交引用，未 vendoring。 |
| docs/images/app-preview.png | 本工具真实窗口，临时测试数据；[复核记录](docs/images/README.md) | 项目 MIT；未复制其他应用或品牌图片。 |

源码发行包不包含 node_modules、Maven target、Docker 镜像或 macOS 框架。如以后分发含依赖的安装包，应重新生成完整组件清单、保留各 LICENSE/NOTICE，并核对 Python 运行时和系统平台许可；当前源码预览不把这些组件重新声明为 MIT。

2026-10-27 审查修复：新增/拆分的存储、清理、采集契约、基线与版本检查代码均为本项目实现，仅使用 Python 标准库及已有 macOS 系统框架；未新增第三方运行依赖、vendored 代码或图片资源，现有许可与分发范围保持适用。
