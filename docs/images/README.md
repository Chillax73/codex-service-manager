# 图片来源与隐私复核

app-preview.png 于 2026-10-12 从实际运行的 Codex Service Manager 原生测试窗口捕获。测试服务由 scripts/csm 登记，使用独立临时状态和 demo-project，HTTP 仅绑定 loopback；不是 UI 绘制/合成图，也不替代正式桌面验收。

已人工查看全部画面，仅有 Demo Web/Demo Background Task、测试临时目录和 loopback URL；没有真实项目、个人路径/邮箱、凭据、聊天、环境值或其他应用内容。PNG 无文本元数据块，EXIF 仅含图像尺寸，未对截图做图像编辑。图片 SHA-256：`f4e1ddefea7ffa62320ab2576334b8cea5f283d7f820a9198138fd1980a45139`。

仅此内容哈希被 scripts/check_publish.py 接受。换图需重新捕获仅测试数据的真实窗口、人工查看、检查元数据和更新白名单；不要以通用“允许 PNG”代替审查。
