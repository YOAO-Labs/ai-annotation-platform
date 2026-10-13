# 仓库优化 P9 工作台根边停靠与保留分组：原因、修复与验收

> 完成日期：2026-09-21 · 隶属 P9 最终候选（冻结 `8985054699ac203c1ed2b5ebe68e6d6897bda724`）的组合式后续修复
> 分支：`worktree-agent-opt-p9-layout-diag`（自冻结 SHA 建立；产品源码仅改工作台布局所有者）
> 状态：**两个耦合问题均已复现、定位并修复；目标用例、完整 workbench-layout、layout-stress、default-four 全绿**。原始冻结 campaign 的失败保持不变。
> 证据图例：**[V]** 已核对；**[HYP]** 假设已被后续证据取代
>
> **P10 收尾（2026-09-21）**：本修复即集成根产品提交 `13115f71f`；最终 composed 门禁见 [43](./43-repository-optimization-p9-shadow-comparison.md)，台账关闭见 [45](./45-repository-optimization-final-acceptance.md)。

## 1. 两个耦合的问题

1. **原生标签拖拽的命中测试**：画布面板内容渲染在 `.dv-render-overlay`（Dockview 的 always-render 覆盖层，`.dv-shell` 的直接子节点，与 group 网格同级）。Dockview 8.2.0 虽把 overlay 的 DnD 转发给 group，但在本布局下转发目标未激活，于是把标签拖到画布上时 group 的 drop target 收不到 `dragover/drop`，停靠被取消（frozen 构建下画布方向的面板停靠成为 no-op）。
2. **根边停靠后保留分组的几何**：当最后一次“停靠到左侧”落在 Dockview 的**根边带**（`dndEdges.activationSize = 10px`，目标是画布左缘 +8px）时，`dockToLayoutEdge` 会重构网格，并把保留的隐藏 `parking` 组重新暴露为一个 **100px 的可见网格列**。`rememberGridSizes()` 一向把 parking 排除在尺寸恢复之外，于是旧的可见列目标尺寸之和 1920 超出可用宽度，Dockview 把新列压到 256，严格 1px 容差校验抛错，`onDidMutateLayout` 的 catch 调用 `owner.failRestore()`，回退到标准预设（即观察到的 inspector/discussion 被拆开、class-palette 落回停靠）。

## 2. 证据链 **[V]**

| 阶段                                                                                                | 结果                                                                                                                                                                                                                                   |
| --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 冻结 `898505469` 产物 `b3fe3e8c`，`workbench-layout.spec.ts:185` 预览 `--retries=0`                 | baseline **FAIL**：拖到画布底部无 `.dv-drop-target`、无 `drop`，`dragend` 取消                                                                                                                                                         |
| 冻结产物 + 仅浏览器 CSS `.dv-render-overlay{pointer-events:none}`（诊断正对照，仅原生标签拖拽期间） | **PASS**：`dragenter/drop` 命中 `div.dv-content-container`，`.dv-drop-target` 出现                                                                                                                                                     |
| 仅 passthrough、未加所有者几何修复（`fix3`）                                                        | `:185` PASS；`@stress` 单例 **1F**：最终树回退标准预设                                                                                                                                                                                 |
| 诊断构建 DIAG1/2（`3ff71bcd` / `fccdaa8e`）                                                         | 观测到**原始异常** `Parking must remain hidden and docked`：解析器把序列化的 parking 叶子视为 visible（缺少隐藏标记），该时刻的 live 可见性**未测量**；经 `onDidMutateLayout` catch → `owner.failRestore()`                            |
| 诊断构建 DIAG8（`cbd1b939`）                                                                        | 引入输出归一化后测得 `parking_state liveVisible=false`（live-hidden），同时序列化缺 flag；说明该缺 flag 是序列化层面，原始异常时刻的 live 状态未由此证明                                                                               |
| 诊断构建 DIAG10（`15227880`）                                                                       | 精确定量：`tolerance_fail id=22 expected 288 actual 256`，`parkingW=100 parkingH=1031`（live hidden 仍占 100px 网格列），`rawSnapshot/restore` 抛 `Workspace replay exceeded one pixel tolerance`（`workbenchLayoutExecutor.ts:1128`） |
| 干净产物 `dabedc89`（本修复）                                                                       | `:185` **PASS**；`@stress` 单例 **PASS**；完整 `workbench-layout.spec.ts` **6 passed**；`layout-stress` **6 passed**；`default-four`（`--shard=4/4`）**68 passed**                                                                     |

诊断证据的边界：DIAG1/2 只观测到解析器对**序列化** parking 叶子抛出 `Parking must remain hidden and docked`（叶子缺少隐藏标记，被当作 visible）；该时刻的 **live 可见性未测量**。DIAG8 测得后续构建中 `parking liveVisible=false`（live-hidden，仅序列化缺 flag）；DIAG10 定量到 100px 的 live 几何占用与 1px 容差异常。早期 `getGroup` 非空断言假设被 DIAG8/DIAG10 取代。**[HYP] 已退休**。

## 3. 最终修复（仅工作台布局所有者）

1. `WorkbenchDockWorkspace.tsx`：原生 HTML5 标签/组拖拽开始时，在 workspace host 上临时加 `dragPassthrough`，使 `.dv-render-overlay` 对该次拖拽透明；`dragend/drop/pointerup` 与 effect 卸载时清除。仅 HTML5（`nativeEvent instanceof DragEvent`）且非 Shift（Shift+指针是 Dockview 的浮动手势），普通画布指针输入与画布内部 DnD 不受影响。
2. `workbenchLayoutExecutor.ts`：新增 `reassertReservedGroups()`，在尺寸恢复前把仍在网格中的保留 `parking` 组重新 `setVisible(false)`，使隐藏保留组不再占用网格空间；调用点位于既有的 `restore` try 内，失败仍 fail-closed。

**被拒绝的变体**（逐项区分处置）：

- **已实现并用证据否定**：在 `rawSnapshot` 里改写序列化输出把 parking 标记为 hidden（`normalizeParking`）——DIAG8/DIAG10 显示它只掩盖 live 几何失配（parking 仍占 100px），已删除，改用 live 所有者纠正。
- **静态否定、未实现**：对 `position !== center` 的拖拽跳过尺寸恢复——会同时跳过普通边缘分屏的既有尺寸保持，属过度放宽，未实现。
- **未被接受、已回退**：全局 `html.dragPassthrough` 与调整 `onDidLayoutChange` 订阅位置——无证据支持，回退到被接受的 R2 作用域与原订阅顺序。

## 4. 验收 **[V]**

- `workbench-layout.spec.ts:185`：1 passed（干净产物）。
- 新增回归 `workbench-layout.spec.ts:366`「a root-edge queue dock keeps the reserved group collapsed and the saved tree」：逐命令断言队列确实停靠到画布右侧、再下方、画布到达工作区左缘（根边前置条件），最后左停靠后 merge/float 保留、停靠组覆盖工作区边缘（无 100px 空白保留列）、保存/重载后渲染一致；末尾原生标签拖拽 + Esc 取消，断言 passthrough 类先出现后复位、画布仍响应真实指针点击。**诚实说明**：该短回归在干净产物与旧冻结产物上均 PASS（旧产物并非全为 no-op），因此它是正向覆盖而非旧构建失败的证明；失败/修复证据由上面的诊断链与 54 步 `@stress` 提供（仅 passthrough、未加几何修复时 1F）。
- 完整 `workbench-layout.spec.ts`：6 passed。`layout-stress`（预览配置，`--retries=0`）：6 passed。`default-four`（`--shard=4/4`，`CI=true --retries=0`）：68 passed（含冻结 898 中 1F 的用例与 53 个未运行用例，本产物全部通过）。
- 相关单元测试（精确作用域，非“全量 web”声明）：`pnpm vitest run src/pages/Workbench/layout/workbenchLayoutExecutor.test.ts src/pages/Workbench/layout/WorkbenchDockWorkspace.test.tsx` → **2 files / 97 tests passed**（命令与结果一致；已接受的完整 web 套件规模另有记录，此处不据 97 或 1289 声称全站）。`pnpm lint:css-tokens`、eslint（仅改动文件）、`tsc --noEmit`、`git diff --check` 干净。
- **补充测试修订（post-68）**：`default-four`（68 passed）与完整 `workbench-layout.spec.ts`（6 passed）跑的是 `:366` 较早的测试体；此后该用例增加了“拖拽后 passthrough 必须清除”的断言，以及真实原生标签拖拽 + Esc 取消后画布指针命中恢复的探针。最终测试体单独复跑一次（补充修订）：**1 passed（23.8s）**；未重跑整套 68，符合“仅测试体增长时单跑该用例”的约定。

## 5. 产物与来源映射 **[V]**

- 修正产物：dist 内容指纹 `dabedc89cc1da5a25e6bffc18f9a74196e58f04e5ec8846a4f9038453c426a42`，`dist/index.html` `449aac28b78036c270cb1f35884403401dccc25457868d202c94908aced446c0`，`BUILD_EXIT=0`；归档 `/tmp/opencode/web-e2e-dist-dabedc89….tar.gz`。
- 后续状态产物（供 P9 组合最终门）：`/tmp/opencode/p9-followup-status/layout-stress.json`、`/tmp/opencode/p9-followup-status/default-four.json`，原始 JSON/日志在 `/tmp/opencode/p9-followup-*`（上述路径已在 P10 收尾时移除；结果计数见 [43]/[45]）。
- 该产物是基于冻结 `898505469` + 本修复的**本 lane 构建**（非共享冻结产物）；冻结 898 的原始失败 campaign 不变。

## 6. 边界

视觉主题套件未在新 SHA 重跑（CSS 只影响拖拽活动期的 overlay 命中测试，主题/预设样式不变；由完整 stress 与布局回归覆盖）。无远程 CI / push；headless Chromium。原生取消验收以真实标签拖拽 + Esc 完成，未用合成 `dispatchEvent`。
