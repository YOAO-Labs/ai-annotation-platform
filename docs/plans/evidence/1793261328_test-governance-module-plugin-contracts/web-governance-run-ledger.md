# Web 测试治理运行账本

原始日志与 JUnit 位于当前工作树 `.artifacts/test-results/web-governance/`；本账本只记录不依赖 T1／T2 的本地证据。

## 2026-11-19：录制与媒体工具的 Linux CI 入口

- 基线 `fef2aace8`：三个 `screenshots:*:test` 命令可手动运行，但 `Frontend verification` 的 Vitest 排除了这十个 Node 测试文件；仓库／文档脚本 CI 也不选择它们。包体积和精确帧基准测试原本由 Vitest 收集，不迁移或重复运行。
- 在 Linux、Node 22.23.1、FFmpeg／ffprobe 7.0.2 下实跑 `pnpm --filter web screenshots:test`，通过原生 `spec` 和 `junit` reporter 同时输出。`media-tools.log` 列出实际选择的十个文件，`media-tools.xml` 有 **86 个通过 case**，无失败、取消、skip 或 todo。编码、剪辑和像素断言使用临时媒体，不是实际采集会话。
- 负对照：Node 22 的未命中 glob 原本退出 0；新入口在空目录退出 1，在嵌套目录新增的故意失败用例也退出 1 且 JUnit 包含该失败（`empty-media-selection.log`、`nested-media-rejection.xml`）。同一 CI 报告检查接受真实报告，拒绝空报告，也拒绝一个通过、一个 skip、一个 todo 的真实 Node 报告（`skipped-media-probe.xml`）。无需固定历史 case 数。
- 本地验证了工作流命令和报告检查，未运行远端 GitHub Actions、真实录制、产品 E2E 或 GPU 资格检查。
