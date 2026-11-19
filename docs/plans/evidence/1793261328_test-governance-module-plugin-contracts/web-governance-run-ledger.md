# Web 测试治理运行账本

原始日志与 JUnit 位于当前工作树 `.artifacts/test-results/web-governance/`；本账本只记录不依赖 T1／T2 的本地证据。

## 2026-11-19：录制与媒体工具的 Linux CI 入口

- 基线 `fef2aace8`：三个 `screenshots:*:test` 命令可手动运行，但 `Frontend verification` 的 Vitest 排除了这十个 Node 测试文件；仓库／文档脚本 CI 也不选择它们。包体积和精确帧基准测试原本由 Vitest 收集，不迁移或重复运行。
- 在 Linux、Node 22.23.1、FFmpeg／ffprobe 7.0.2 下实跑 `pnpm --filter web screenshots:test`，通过原生 `spec` 和 `junit` reporter 同时输出。`media-tools.log` 列出实际选择的十个文件，`media-tools.xml` 有 **86 个通过 case**，无失败、取消、skip 或 todo。编码、剪辑和像素断言使用临时媒体，不是实际采集会话。
- 负对照：Node 22 的未命中 glob 原本退出 0；新入口在空目录退出 1，在嵌套目录新增的故意失败用例也退出 1 且 JUnit 包含该失败（`empty-media-selection.log`、`nested-media-rejection.xml`）。同一 CI 报告检查接受真实报告，拒绝空报告，也拒绝一个通过、一个 skip、一个 todo 的真实 Node 报告（`skipped-media-probe.xml`）。无需固定历史 case 数。
- 本地验证了工作流命令和报告检查，未运行远端 GitHub Actions、真实录制、产品 E2E 或 GPU 资格检查。

## 2026-11-19：存储绑定恢复与撤权

- 三种用途各有两条原风险：预览时目标已删除，可以换目标；已取得预览后候选访问被撤销，不能保留内容或继续确认。原六条用例替换为 `StorageBindingRecovery.test.tsx` 的六条 Vitest/MSW 场景；其他用途及会话切换、过期、陈旧响应和显式确认的原用例保留。
- 四个绑定测试文件实际执行 **87 通过、无跳过或失败**（`storage-bindings-final.xml`）。新场景从按钮发起请求，核对两次预览的 generation、目标 ID、不同修订和最终确认状态；撤权从刷新按钮触发，不依赖 React Query 的私有缓存键。调整卸载与会话重置顺序后，新六项另行通过（`binding-cleanup.xml`），没有新增未包裹的 React 更新警告。旧组件用例的警告未据此关闭。
- 负对照只在 Vite 加载时将报告附件、审计归档两种 `*_binding_target_missing` 替换成头像码，不改磁盘上的产品代码。六项中恰好这两种用途的恢复失败：目标删除被误显示为管理权限丢失；头像恢复和三项撤权仍通过（`binding-mutant.xml`）。负对照不计作产品通过项。
- 初轮限 worker 参数冲突，零项执行（`storage-bindings.xml`）；上述成功来自显式设置最小／最大 worker 后的真实运行。替身在 HTTP 响应边界，不证明后端权限、真实存储、浏览器布局或 T1／T2。

## 2026-11-19：筛选控件的静态归属与交互

证据目录：`.artifacts/test-results/web-filter-ownership/`。

- 在一次性副本中用空的 `<div>` 代替数据集页面，只在注释里留下共享组件的导入路径和 `<FilterGroup>`。旧 Vitest 仍然 **28/28 通过**（`before-comment-only.xml`）；它证明的是文字匹配，不是筛选行为。
- `pnpm --filter web lint:filters` 现在核对 28 个界面的 JSX 标签是否绑定到指定的共享导入，已接入 Web lint。实际源码通过（`current.log`）；仅注释、同名本地组件、遮蔽导入三个对照分别退出 1，且只报告数据集页；具名导入改名和通过相对路径限定的命名空间导入通过（`static-controls.json`）。这 28 项不再计入 Vitest。
- `FilterControls`、`FilterPanel` 与真实 `DatasetsPage` 的 Vitest/jsdom 选集分别为 **5、2、8 项通过**，无跳过（`filter-interactions.xml`）。页面场景覆盖恢复已归档筛选和第 40 项偏移后，清除筛选回到起始页。该证据不代表 28 个页面各自的运行时交互、真实浏览器或后端查询资格。
