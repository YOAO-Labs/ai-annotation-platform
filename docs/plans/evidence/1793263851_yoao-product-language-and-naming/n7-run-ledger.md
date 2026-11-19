# N7 产品语言定向验收账本

## 2026-11-19：CLI 导入与清理回执

从 `feat/backend-opt@2df1c6286` 分出 `feat/product-language-n7`；没有更改 T1／T2 的冻结候选 `3d45da957`。范围是 `yoao predictions import`、`yoao annotations import` 和 `yoao predictions purge` 的帮助与文本回执，不改变服务端写入、API、确认、JSON 或退出码。

- **SDK / CLI：** `sdk.xml` 与 `annotation-preview.xml` 合计 14 个不同用例通过，无失败、无跳过。覆盖预览与实际计数、任务范围、未确认时不发请求、逐项错误和完整 JSON 回执；SDK 调用与 OpenAPI 快照匹配。HTTP 响应由 SDK 契约测试提供，不是本轮实库写入。
- **终端：** 独立工作树安装的 `yoao` 实际启动三个命令的 `--help`。另用阶段 C 的 `image-import-readback.json` 在本地回放同一组真实预测预览／确认回执；命令分别显示「预计写入 1 条待审预测」和「已写入 1 条待审预测」。这是当前客户端对历史回执的读取，不是重做数据导入。
- **指南：** 在实际 VitePress 页面从[预测导入指南](../../../../docs-site/user-guide/datasets/prediction-import-export.md)点击进入 [CLI 参考](../../../../docs-site/dev/sdk/cli.md#yoao-predictions)。两页均检查明暗主题及 1280／390 像素宽度，共 8 张截图，无页面横向溢出或浏览器脚本异常。开发预览的 `/favicon.ico` 返回 404；首次只准备文档依赖时，预生成缺少 Web 的 TypeScript，使用本树冻结依赖后预览成功。

本地 JUnit、命令输出、回放来源摘要、浏览器记录和截图位于本工作树 `.artifacts/n7-cli-receipts/`。临时回放服务、浏览器和文档预览均已停止；没有启动后端验收环境，也不计入 J1、N6 环境采用或发布资格。
