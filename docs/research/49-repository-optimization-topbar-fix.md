# 仓库优化 P9 修复：工作台顶部栏中等宽度重叠

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P9 工作包 / P10 验证通道）
> 基线根：`36bd3c7c0`（`feat/codebase_opt260920`，已接受的优化根）· 修复分支：`worktree-agent-opt-p9-topbar-fix`
> 冻结候选：`dc972be08a42855c836023da178487469aae2fc9`（`Topbar.tsx` 与 `workbench-topbar.spec.ts` 同基线根逐字节一致）
> 自有一次性环境：`aap_wt_c2820af87ec94679_e2e`（checkout `c2820af87ec94679`）
> 证据图例：**[V]** 本工作树/自有一次性环境实测；**[M]** 变异或负向探针；**[EXT]** 外部只读事实；**[GAP]** 未执行
>
> **P10 收尾（2026-09-21）**：视频分段选择器探针只覆盖了 `collaboration_enabled`（其余为真实 video/claim API），不能称为完全未 mock；最终门禁见 [43](./43-repository-optimization-p9-shadow-comparison.md)。

## 0. 结论

1. **根因（真实 CSS 宽度语义，非测量缺陷）[V]**：中段容器 `flex-1 min-w-0` 允许被压缩，而其中的任务标识组 `flex items-center min-w-0` 的子元素（`task.display_id`、位置徽章）均为 `shrink-0`。中等宽度下中段可用宽度小于其内容宽度时，标识组被压到内容以下（实测 800px：`clientWidth=155.8` / `scrollWidth=214`），`shrink-0` 子元素向右溢出，压到相邻的「上一个任务 / 提交」等按钮。P7 把共享 `seed.reset()` 迁移到 per-test `seed.owned()` 后，任务 `display_id` 变为命名空间化的长形态（`T-E2E-{12}` + 6 位序号，最长 24 字符），把潜伏的分层断点缺口在约 700–1200px 区间暴露出来。
2. **修复 [V]**（`apps/web/src/pages/Workbench/shell/Topbar.tsx`，最小生产改动，无重设计）：
   - 中段由 `min-w-0` 改为 `min-w-max`：永不压缩到内容以下；空间不足时改为收缩左侧项目名（其项目名本就是 `min-w-0 truncate`，可安全让位），消除「压缩后溢出」这一路径。
   - 新增容器 `<1000px` 分层：状态相关主操作（提交 / 通过 / 退回 / 撤回 / 继续编辑 / 跳过）收起为保留 `aria-label`/`title` 的图标按钮；位置徽章在 `<900px` 与前后的文件名同步省略；快捷键、智能切题、主题、版本更新、Bug 反馈、设置收进「更多工具」（更多菜单由 `<700px` 提前到 `<1000px` 出现）。
   - 视频分段选择器同样在 `<1000px` 收起：该元素在 DOM 中仍渲染两处（标识组内联 + 「更多工具」页脚），`<1000px` 时仅「更多工具」页脚中的实例可见，避免同时出现两个可见控件。
3. **验收 [V]**：在新构建产物上，既有 `workbench-topbar.spec.ts` 在 6 个宽度（1440/1280/1024/800/640/375）`--retries=0` **1 passed**（JSON `expected=1 / unexpected=0 / flaky=0`）；追加的边界探针覆盖断点 ±1px 与最长 24 字符任务 ID，`overlaps=[]`、`outside=[]`；审核模式与任务位置可达性同样通过。被复核指出的窄宽度视频分段选择器（真实视频任务 + 真实分段）在 800px / 375px 实测：DOM 中仍有两处实例，`<1000px` 时只有「更多工具」门户中的一处可见，且选择分段会更新 `activeVideoSegmentId`。

## 1. 复现与证据

**装置 [V]**：自有一次性 e2e 模式 `aap_wt_c2820af87ec94679_e2e`；临时配置 `apps/web/playwright.preview.e2e.config.ts`（从冻结候选 `dc972be08` 取出，仅覆盖 webServer：同一自有 API 命令 + `vite preview` 指定 `--mode e2e` 的 dist；projects/testDir/testMatch/grepInvert/retries/reporter 全部沿用真实配置）；产物为 P9 既有共享构建
`/tmp/opencode/web-e2e-dist-253b54a03c4f0fc663b26cd9038913bce188e6b7f772de67dad1a5ec616015e1.tar.gz`（sha256 `754d61b9…` 与 `SHA256SUMS` 一致，`dist/index.html` sha256 `f31c3ecb…`；其 app 源码的 `Topbar.tsx` 与本基线逐字节一致）。

- **复现（修复前，实际构建产物，`--retries=0`）[V]**：`workbench-topbar.spec.ts` **1 failed**，`overlap at 800px`，`result.overlaps = ["1 / 5 / ", "1 / 5 / 提交"]`。
- **DOM 度量（修复前，800px）[V]**：标识组 `clientWidth 155.8` / `scrollWidth 214`；`T-E2E-…` 主标识宽 164；位置徽章 `1 / 5` 位于 `x=260.0..300.4`，而标识组右边界 `241.8`；「上一个任务」`x=254.8..282.8`、「提交」`x=288.8..356.8`——徽章同时压过两者。
- **失败宽度带（修复前，375–1455px 每 10px 扫描）[V]**：`705–845`、`905–915`、`1105–1205`。三处分别对应容器断点 700 / 900 / 1100 的两侧落差：右栏在 `>900px` 重新出现版本号、左栏在 `>1100px` 重新出现项目名与「返回」文字，而中段压缩层级未同步。

## 2. 修复后的断点几何（实际构建产物）[V]

探针使用 12 字符命名空间 `a1b2c3d4e5f6`，任务 `display_id = T-E2E-a1b2c3d4e5f6000001`（24 字符，列上限最大值）。每行取 `bar.getBoundingClientRect()` 与全部可见控件矩形的并集；`meta=clientWidth/scrollWidth`。

| 宽度 | 控件最左 | 控件最右 | 中段宽 | meta    | navGap | filenameGap | 高度 | overlaps/outside |
| ---- | -------- | -------- | ------ | ------- | ------ | ----------- | ---- | ---------------- |
| 1440 | 16       | 1424     | 744    | 391/391 | 23     | 10          | 49   | 0 / 0            |
| 1101 | 16       | 1085     | 579    | 289/289 | 13     | 10          | 49   | 0 / 0            |
| 1099 | 16       | 1083     | 684    | 289/289 | 13     | 10          | 49   | 0 / 0            |
| 1024 | 16       | 1008     | 609    | 289/289 | 13     | 10          | 49   | 0 / 0            |
| 1001 | 16       | 985      | 586    | 289/289 | 13     | 10          | 49   | 0 / 0            |
| 1000 | 16       | 984      | 585    | 289/289 | 13     | 10          | 49   | 0 / 0            |
| 999  | 16       | 983      | 713    | 289/289 | 13     | 10          | 49   | 0 / 0            |
| 901  | 16       | 885      | 615    | 289/289 | 13     | 10          | 49   | 0 / 0            |
| 900  | 16       | 884      | 614    | 289/289 | 13     | 10          | 49   | 0 / 0            |
| 899  | 16       | 883      | 613    | 169/169 | 13     | 0           | 49   | 0 / 0            |
| 800  | 16       | 784      | 514    | 169/169 | 13     | 0           | 49   | 0 / 0            |
| 700  | 16       | 684      | 414    | 169/169 | 13     | 0           | 49   | 0 / 0            |
| 699  | 8        | 691      | 465    | 0/0     | 0      | 0           | 49   | 0 / 0            |
| 375  | 8        | 367      | 141    | 0/0     | 0      | 0           | 49   | 0 / 0            |

- **无外溢**：所有宽度控件最左 ≥ 16（`<700px` 为 8，`px-2`），最右 ≤ 宽度 −16（`<700px` 为 −8），即 `outside=[]`，**不存在** `min-w-max` 把重叠换成外溢的情况。
- **标识组不再压缩**：所有宽度 `clientWidth == scrollWidth`；`<900px` 时位置徽章省略（169），`<700px` 标识组整体省略（0）。
- **断点匹配为严格小于**：`@max-[1000px]` 在 999 生效、1000 不生效；`@max-[900px]` 在 899 生效、900 不生效；`@max-[700px]` 在 699 生效、700 不生效。两侧均无重叠。
- **既有断言全部保持**：`height ≤ 52`（实测 49）、`navigationGap ≤ 24`（13）、`filenameGap ≤ 12`（10），`overlaps=[]`、`outside=[]`。

## 3. 审核模式与动作可达性 [V]

- **审核模式**（真实 reviewer 登录 `/projects/:id/review?task=…`；annotator 真实标注 + 提交后进入）：在 1280 / 1001 / 1000 / 800 / 375 各宽度 `overlaps=[]`、`outside=[]`；「通过」「退回」始终可见且具 `aria-label`（`通过` / `退回`），`<1000px` 收起为 28px 图标按钮（1001px 为 68/70px 文字按钮）。`>1100px` 的 79px 高度来自既有 `ReviewerMiniPanel`（对照用「更多工具」不变），与本次水平修复无关。
- **任务位置仍可达**：`<900px` 顶部位置徽章省略后，同一位置仍显示在任务队列面板标题（`TaskQueuePanel.tsx:429` 的 `{taskIdx + 1} / {tasks.length}`）；800px 浏览器探针确认顶部徽章不可见时页面仍有可见的 `1 / 5`。
- **次要入口可达**：800px 打开「更多工具」，菜单项为 `下一未标注 / 下一最不确定 / 快捷键 / 切到夜间 / 本次更新 / 报告 Bug / 工作台设置`，均为原有入口。

## 4. 变更文件

| 文件                                                     | 变更                                                                                                                                                                             |
| -------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apps/web/src/pages/Workbench/shell/Topbar.tsx`          | 中段 `min-w-0`→`min-w-max`；主操作按钮压缩与位置徽章、更多菜单的分层提前到 `<1000px` / `<900px`；视频分段选择器 `<1000px` 时仅菜单中一个可见实例（元素仍渲染两处）；更新入口注释 |
| `docs-site/user-guide/workbench/index.md`                | 顶部栏窄宽度行为说明：不重叠、按钮收图标、次要入口收「更多工具」、任务位置仍见任务队列                                                                                           |
| `CHANGELOG.md`                                           | Unreleased · Fixed 一条用户可见说明                                                                                                                                              |
| `docs/research/49-repository-optimization-topbar-fix.md` | 本文                                                                                                                                                                             |

未改动：`workbench-topbar.spec.ts`（保留原 6 宽度与全部断言）、`measureTopbar`、Markdown 所有者文件、共享清单、Markdown/Projects 用户指南。

## 5. 验证

- **构建 [V]**：`OPENAPI_URL=<本 checkout>/apps/api/openapi.snapshot.json pnpm --filter @anno/web build --mode e2e`，退出码 0。首次修复构建指纹 `023c84d48afd719c9272d1052713f272647b8d13f75f7a8dd7c224918403416a`；该产物清理后，本次复核重新构建，源码+模式指纹 `cc9c2a9eed64c9eeb341ea09c2b3b6eda2b96fab230ade40b8d8ecbb0a828e99`（仅注释变更），`dist/index.html` sha256 `0840132e3a251f2393a8172618acd5687e21bad9eb2cbb41a3e61f90bcf94ac4`（与首次一致）。指纹 = `git HEAD` + `apps/web/{src,e2e,public,index.html,package.json,vite.config.ts,playwright.config.ts}` 文件哈希 + `apps/api/openapi.snapshot.json` 的 sha256 再哈希；**下述所有复核结果均来自这次重建产物，而非 P9 旧共享产物 `253b54a0`（它只用于修复前复现）**。codegen 未产生额外的生成类型改动。
- **既有 e2e（重建产物，`--retries=0`）[V]**：`workbench-topbar.spec.ts` **1 passed**（JSON `expected=1 / unexpected=0 / flaky=0`）。
- **边界回归探针（重建产物，24 字符 ID）[V]**：18 个宽度（含断点 ±1px）全部 `overlaps=[] / outside=[]`。
- **窄宽度视频分段选择器（重建产物，800px / 375px，真实视频任务 + 真实 `/video/segments` 分段）[V]**：两处都 `inlineDisplay="none"`；打开「更多工具」前 `total=1 / visible=0`（内联副本不可见），打开后 `total=2 / visible=1`（仅门户页脚副本可见）；选择分段后识别为真实认领，`activeVideoSegmentId` 更新为所选分段 id，服务端该分段 `locked_by` 为当前标注员。探针经 `page.route` 把真实响应的 `collaboration_enabled` 单字段改为 true（其余字段/分段均来自真实后端），原因见 §6。
- **静态检查 [V]**：`apps/web` `Topbar.test.tsx` 9 passed（含「更多工具」菜单页脚原生 select 的键盘焦点语义）；`tsc --noEmit` 通过；`eslint Topbar.tsx` 通过；`prettier --check` 通过；`pnpm --filter @anno/web lint:css-tokens` 通过。

## 6. 边界与限制

- 仅覆盖并验证图片 `annotate` 与 `review` 两种顶部栏状态；点云 / 视频的其它工具条不在本次范围。视频分段选择器已在重建产物的 800px / 375px 实测：元素在 DOM 中渲染两处（标识组内联 + 「更多工具」门户页脚），`<1000px` 时仅门户实例可见。该探针使用真实视频任务与真实分段，仅把真实响应中的 `collaboration_enabled` 单字段改为 true —— 因为 `seed.owned()` 夹具项目是 image-det，现有 seed 助手与公共项目更新都无法为它开启 `video_collaboration`（该开关要求空的 video-track 项目），两个视频 seed 路由又都要求项目已有批次 + 任务；其余字段、分段与认领接口均为真实后端。视频变体的其它长内容组合仍未专门扫描。
- 未运行远端 CI（未 push）；结论来自本工作树自有一次性环境与真实构建产物。
- 不改动 `workbench-topbar.spec.ts` 的宽度集合与断言强度；边界证据以本文件记录。
- 审核模式 `>1100px` 的 79px 高度为既有 `ReviewerMiniPanel` 行为，本次不涉及。

## 7. 复发防护口径

- **几何回归**：既有 800px 用例锁定根因（标识组溢出压到提交按钮）；本次新增的边界证据覆盖断点 1000/900/700 两侧与 24 字符 ID 上限。
- **视频选择器回归**：`<1000px` 内联副本保持隐藏、仅「更多工具」门户实例可见且选择后更新状态；`≥1000px` 继续使用内联实例。
- **可达性**：主操作在收起为图标后仍保留 `aria-label` / `title`；位置徽章收起后，任务队列标题继续显示当前位置。
- **修法约束**：不使用截断任务 ID、不隐藏必需动作、不弱化既有断言；中段用 `min-w-max` 明确「优先让项目名收缩」，避免再次出现「压缩后溢出」。
