# N7 产品语言定向验收账本

## 2026-11-19：CLI 导入与清理回执

从 `feat/backend-opt@2df1c6286` 分出 `feat/product-language-n7`；没有更改 T1／T2 的冻结候选 `3d45da957`。范围是 `yoao predictions import`、`yoao annotations import` 和 `yoao predictions purge` 的帮助与文本回执，不改变服务端写入、API、确认、JSON 或退出码。

- **SDK / CLI：** `sdk.xml` 与 `annotation-preview.xml` 合计 14 个不同用例通过，无失败、无跳过。覆盖预览与实际计数、任务范围、未确认时不发请求、逐项错误和完整 JSON 回执；SDK 调用与 OpenAPI 快照匹配。HTTP 响应由 SDK 契约测试提供，不是本轮实库写入。
- **终端：** 独立工作树安装的 `yoao` 实际启动三个命令的 `--help`。另用阶段 C 的 `image-import-readback.json` 在本地回放同一组真实预测预览／确认回执；命令分别显示「预计写入 1 条待审预测」和「已写入 1 条待审预测」。这是当前客户端对历史回执的读取，不是重做数据导入。
- **指南：** 在实际 VitePress 页面从[预测导入指南](../../../../docs-site/user-guide/datasets/prediction-import-export.md)点击进入 [CLI 参考](../../../../docs-site/dev/sdk/cli.md#yoao-predictions)。两页均检查明暗主题及 1280／390 像素宽度，共 8 张截图，无页面横向溢出或浏览器脚本异常。开发预览的 `/favicon.ico` 返回 404；首次只准备文档依赖时，预生成缺少 Web 的 TypeScript，使用本树冻结依赖后预览成功。

本地 JUnit、命令输出、回放来源摘要、浏览器记录和截图位于本工作树 `.artifacts/n7-cli-receipts/`。临时回放服务、浏览器和文档预览均已停止；没有启动后端验收环境，也不计入 J1、N6 环境采用或发布资格。

## 2026-11-19：原生 JSON 导入说明与诊断

在 `feat/product-language-n7@aeb066a78` 上继续。原接口将当前文件称为旧格式，且预测与标注两条路径遇到结构错误均会回显上传值及校验器链接。修正两条 API 的说明、离线 CLI 目录、用户指南和错误提示，没有改选择器、文件身份、权限、必填条件、回执字段或状态码。

- **实库行为：** 用已提交的项目和操作者，通过真实 ASGI 路由分别上传同一份含 2 条预测和 1 条正式标注的文件。预览回执分别为 2／1，另开数据库连接读回均为 0；实际导入后读回 2／1。单个坏文件两条接口均返回 422，指出 `tasks[0].task_match` 而不带测试备注；坏文件与有效文件一起预览时，预测回执为预计写入 2、跳过 1，并指出坏文件名。
- **读取与契约：** 两条导入服务与 Mask 暂存预检定位到相同字段。UTF-8、语法行列、旧／新版本身份、跨字段约束、前三个位置与超出数量均有定向记录。实际 `/openapi.json` 与版本化快照及文档镜像一致；相对基线，只有两个导入操作的 `summary`、`description` 改变。两个 `yoao describe` 输出与快照一致，离线目录同时补齐上个切片三个命令的参数帮助。
- **文档：** 从[用户指南](../../../../docs-site/user-guide/datasets/prediction-import-export.md)点击到[导入响应](../../../../docs-site/api/guides/import.md#响应)，实际检查两页在 1280／390 像素及明暗主题下的 8 张截图，无页面横向溢出。开发预览的 `/favicon.ico` 仍为 404，没有脚本异常。
- **定向回归：** 18 个 HTTP、11 个文件身份／Mask 预检及 3 个 SDK 目录用例通过，共 32 个，无跳过。首次只启 PostgreSQL 的调用超时，未计入结果；单独定位到已有身份测试在同一事务内连续提交坏文件，真实回滚令下一次请求失去测试账号（401）。改为每个身份组合独立建夹具，保留全部 200／422 断言；没有修改鉴权或回滚逻辑。独立 PostgreSQL、Redis 就绪后，最终 HTTP 组用时 15.50 秒。

JUnit、首次失败、实库读回、快照差异及截图保存在 `.artifacts/n7-native-import/`。一次性数据库、Redis、文档预览、浏览器和额外的契约生成虚拟环境已退役。没有触碰 T1／T2 的冻结环境、日常数据库或生产资源。
