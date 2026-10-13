# 仓库优化 P9 Markdown 表格单元格图片重试：根因、修复与验收

> 完成日期：2026-09-20 · 隶属 P9 同候选全域执行的 markdown 缺陷通道
> 分支：`worktree-agent-opt-p9-markdown-fix`（自冻结候选 `dc972be08` 建立；原 doc46 分支 `worktree-agent-opt-p9-domains` 保留）
> 状态：**根因已证实；产品修复提交 `69960028d` 已通过生产预览产物验收**。本文件按最终结论重写，早期尝试与失败完整保留在 §5 历史表。
> 证据图例：**[V]** 已核对；**[HYP]** 代码一致的假设（凡已被后续证据取代者均标注）
>
> **P10 收尾（2026-09-21）**：目标用例 `:889` 的删除测试最终由 `default-one` 覆盖（旧 `19 passed` 属**修正前**测试修订）；最终门禁见 [43](./43-repository-optimization-p9-shadow-comparison.md)。

## 1. 失败事实 **[V]**

来源：`/tmp/opencode/p9-default-1-preview.json`（expected 79 / unexpected 2 / duration 445.6s）与 `/tmp/opencode/p9-default-1-preview.log`。

- 目标用例：`apps/web/e2e/tests/markdown-authoring.spec.ts:777`「a failed table-cell image upload retries inside the same cell」。
- 失败点：点击错误面板「重试」后，`targetCell.getByRole("img", { name: "表格重试.png" })` 在 15s 内 **not found**（不是可见性超时后的错误提示，而是元素不存在）。
- 该次失败发生在**另一 worktree**（trace / error-context / screenshot 指向 `ai-annotation-platform-worktree-agent-opt-p7`，该目录当前已无对应产物），因此当时没有可用的 DOM 快照；未臆造证据。
- 同 run 的另一失败（`employee-project-roles.spec.ts`）属 P9 owner 的 fixture，未触碰。

## 2. 代码路径 **[V]**

`apps/web/src/components/markdown/MarkdownEditor.tsx`：

1. `retryUpload(id)` 先尝试「原位重试」，条件为 `existing.failed && existing.nodeKey && existingEditor && rootRef.contains(existingRoot)` 且在该编辑器状态中 `$getNodeByKey(nodeKey)` 仍是 src 匹配的 ImageNode。
2. 条件不成立时走「取消并重插」分支：新建 pending 并调用 `insertPlaceholderAtAnchor`。
3. `insertPlaceholderAtAnchor` 依赖 `pending.anchor.before/after` 在**根 markdown** 中定位插入点；两段文本都找不到时**追加到文档末尾**。
4. `anchor` 在错误发生时由根 markdown 惰性推导（`handleEditorChange` → `pendingImageAnchor`）。
5. 表格单元格是独立嵌套 Lexical 编辑器，编辑经 `TABLE_CELL_DRAFT_SYNC_DELAY_MS = 250` 的 debounce 才导出到根 markdown。
6. MDXEditor 的 `PASTE_COMMAND` / `DROP_COMMAND` 图片处理器通过 `createActiveEditorSubscription$` 注册在「最近一次发布 `SELECTION_CHANGE_COMMAND` 的编辑器」上。

**早期机制假设（已被 §8 取代）[HYP]**：曾认为重试时持有的 `editor/nodeKey` 已非活体，于是落入取消重插分支、anchor 缺失导致占位符追加到表格之外。§7 的复现只观察到了「图片在表格之外」这一结果，并未证明是重试造成的；§8 的探针证明图片在重试之前就已经落到表格之外。

## 3. 最终范围与约束 **[V]**

- 产品改动：仅 `apps/web/src/components/markdown/MarkdownEditor.tsx`（提交 `69960028d`，之后保持不变）。
- 测试新增：`markdown-authoring.spec.ts:837`（同一任务内 `focus()` + 粘贴的归属竞态）与 `:889`（删除失败待重试图片后取消重传）。
- 文档：本文件、`docs-site/user-guide/projects/index.md`、`CHANGELOG.md`。
- 约束：不新增 sleep / 超时 / 重试兜底，不放宽断言，不改 skip / retry 策略，不触碰其他 worker 的共享产物，不 push。

## 4. 本地已执行 **[V]**

- `pnpm vitest run src/components/markdown/MarkdownEditor.test.tsx` → **7 passed**。
- `tsc --noEmit` 干净；`pnpm lint` 干净（仅仓库既有 warning）；`pnpm openapi:check` 通过（快照与当前路由一致）。

## 5. 历史尝试与证据表 **[V]**

按时间顺序保留全部尝试；「产物指纹」为 dist 内容哈希（`find dist -type f | sort | xargs sha256sum | sha256sum`），tar 为归档文件的 sha256。

| 阶段                     | 源码 / 提交                             | 产物指纹                                      | 运行                                                        | 结果                                                                                                                                                                                               |
| ------------------------ | --------------------------------------- | --------------------------------------------- | ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A 初始复现               | 旧共享产物 `253b54a0`（SHA `754d61b9`） | `253b54a0…`                                   | `:777` 预览配置 `--retries=0 --repeat-each=3`               | **2 passed / 1 failed**；失败快照显示图片位于表格之外                                                                                                                                              |
| B flush 重试修复         | `0d433b429`                             | `8f20a72d…`                                   | 同上（生产预览产物）                                        | **3/3 failed**                                                                                                                                                                                     |
| C 无锚点捷径（已被拒绝） | `f5b5bf049`                             | `94727c15…`（tar `99dd7541…`）                | 同上                                                        | 记录 **3/3 passed**，但含 `\|\| !existing.anchor`；root 以「无锚点不等于活体节点、可能重传已删除节点」拒绝。本次以等价的重复运行时序观察到 **1/3–2/3 失败**，该次通过不具决定性                    |
| D 探针诊断               | 最终源码 + 临时探针                     | `9009c617…`（另有两个探针构建未单独记指纹）   | 同上                                                        | 失败 run `MD_BEGIN_UPLOAD {activeIsCell:false}` → `MD_RECONCILE_OWNER {isCell:false,parentTag:"DIV"}`；通过 run `{activeIsCell:true}` → `{isCell:true,parentTag:"TD"}`；`retryInPlace:true` 恒成立 |
| E 负向对照               | 停用归属修复后重建                      | 未单独记指纹（内容同 `f425d402…` 减一处调用） | 新用例 `:837` `--repeat-each=3`                             | **3/3 failed**                                                                                                                                                                                     |
| F 最终修复与验收         | `69960028d`                             | `f425d402…`                                   | `:777` `--repeat-each=3`；整份 `markdown-authoring.spec.ts` | **3 passed / 19 passed**（测试修订之前）                                                                                                                                                           |
| G 测试 / 文档修订        | 本文件所在提交（紧随 `69960028d`）      | 同 `f425d402…`（仅测试与文档）                | 由 root 指派的最终 default-1 合并套件执行                   | **待办（见 §10）**                                                                                                                                                                                 |

## 6. 边界

无性能 / 资格声明；未做宽泛单测、后端或文档全量复跑；未 push；未触碰其他 worker 的共享产物与临时目录。证据运行在一次性自有 e2e 模式（`de57dbd75d11794b:e2e`，已销毁）与软渲染无头 Chromium；未在远程 CI 观察。

## 7. 历史尝试 A：flush 重试修复（已被取代，非最终结果）**[V]**

**复现 [V]**：在自有 e2e 模式（一次性库、旧共享产物 `253b54a0`、预览配置）上以 `--repeat-each=3 --retries=0` 运行单测用例，**2 passed / 1 failed**（第 3 次，18.2s）；失败时的 error-context DOM 快照显示 `img "表格重试.png"` 确实存在，但位于目标表格行 / 单元格**之外**。当时把该现象归因为重试落入取消重插分支、anchor 缺失导致追加到文档末尾；§8 的探针证据推翻了这一归因。

**当时的修复 [V]**（`retryUpload` 内先调用 `flushActiveTableCell()`）：在生产预览产物上**未通过**（§5 阶段 B：3/3 failed），因此不是最终结果。

**边界 [GAP]**：dev server 上连续 9/9 通过（非验收）；预览产物必须重建后才能作为证据。

## 8. 最终根因：粘贴 / 拖放的编辑器归属竞态 **[V]**

图片粘贴 / 拖放发生在表格单元格刚获得焦点、但浏览器 `selectionchange` 尚未把「活动编辑器」归属给嵌套单元格的窗口内。MDXEditor 的 PASTE / DROP 处理器注册在「最近一次发布 `SELECTION_CHANGE_COMMAND` 的编辑器」上；此刻它仍是根编辑器，于是 `ImageNode` 被插入根编辑器，直接渲染在表格之外。

- 失败 run 探针：`MD_BEGIN_UPLOAD {activeIsCell:false, activeParentTag:"DIV"}` → `MD_RECONCILE_OWNER {isCell:false, parentTag:"DIV", nodeKey:"11"}`；即待定图片**从未注册到嵌套单元格编辑器**，而是注册在根编辑器。
- 通过 run 探针：`MD_BEGIN_UPLOAD {activeIsCell:true}` → `MD_RECONCILE_OWNER {isCell:true, parentTag:"TD"}`。
- 所有 run 的 `retryInPlace` 均为 `true`：**重试判定本身一直正确**，越界在粘贴落点时就已确定。
- 仅调用 `editor.focus()` 不能修复：对未变更的 range selection，Lexical 不会重新派发 `SELECTION_CHANGE_COMMAND`（探针显示 `focus()` 后活动编辑器仍为根编辑器）。

**已证实**：根编辑器在粘贴时仍是活动编辑器、ImageNode 插入根编辑器、重试判定正确、捕获阶段归属修复使失败用例稳定通过。
**仍属假设（未单独验证）**：同类的 `drop` 路径共用同一修复，但本通道只用粘贴复现；删除 / 取消 / 源码模式切换语义按既有用例保持（§10）。

## 9. 最终修复 **[V]**

`apps/web/src/components/markdown/MarkdownEditor.tsx`（提交 `69960028d`）：

1. 在根容器**捕获阶段**监听 `paste` / `drop`，用 Lexical 的 `getNearestEditorFromDOMNode(event.target)` 找到事件目标真正所属的编辑器；若它是本编辑器内、且尚未成为活动编辑器的嵌套表格单元格，则 `dispatchCommand(SELECTION_CHANGE_COMMAND)`，让 MDXEditor 的 active-editor 订阅在它处理粘贴 / 拖放之前先完成归属。
2. 保留一处最小重试稳健性：当记录的 `editor/nodeKey` 已失效、但当前文档仍含该上传标记时，原位复用现有 pending，而不是按锚点重插。被 root 拒绝的「无锚点即原位」捷径保持移除。

未新增 sleep、未放宽断言、未改动跳过 / 超时 / 重试策略。

## 10. 最终验收、产物身份与待办 **[V]**

**产物身份（显式区分，勿混为同一归档）**：

- 源码 / 内容指纹：`f425d4022ecb9a734df99499e028c1bbb7eae18c9265241306d44ff231df741d`（`pnpm build --mode e2e`，`OPENAPI_URL=apps/api/openapi.snapshot.json`，`BUILD_EXIT=0`；恢复修复后重建可复现同一指纹）。
- 归档 `final-dist.tar.gz` → sha256 `35cf2671…`；归档 `final2-dist.tar.gz` → sha256 `12cf53a7…`（恢复修复后的重建）；归档 `final3-dist.tar.gz` → sha256 `0d322304…`（prettier 重排后的重建，仍仅空白差异）。三个 gzip 归档因归档元数据不同而哈希不同，但内容指纹相同（均为 `f425d402…`）；**它们是不同的归档文件，不是同一个**。
- 历史产物：`253b54a0…`（旧共享产物）、`8f20a72d…`（flush 修复）、`94727c15…`（无锚点捷径，tar `99dd7541…`）、`9009c617…`（探针诊断）。

**验收结果（测试修订之前，仍然有效）[V]**：

- 负向对照：停用归属修复后重建，`markdown-authoring.spec.ts:837` `--retries=0 --repeat-each=3` → **3/3 failed**；恢复后 → **3/3 passed**。
- 目标用例：`markdown-authoring.spec.ts:777` 经 `playwright.preview.e2e.config.ts`、`--retries=0`、`--repeat-each=3` → **3 passed，退出码 0**。
- 整份 `markdown-authoring.spec.ts`（19 用例）→ **19 passed，退出码 0**。

**该 19/19 实际覆盖的既有相关用例**（非笼统声称）：`:173` 打开 / 预览 / 源码切换不写入原文；`:395` 源码模式重试保留编辑；`:438` 离开源码模式后新表格编辑随上传完成保留；`:500` 撤销上传不发布内部 pending source；`:525` 破坏排队图片标记后取消上传；`:721` 表格单元格粘贴图片在重载后保留；`:777` 目标重试用例；以及本次新增 `:837`。删除语义由新增 `:889`（测试修订前 3/3 通过）覆盖。**文档切换（`documentId` 变更 / epoch）路径本次没有单独新增用例，既有套件也未覆盖它，故不作声明。**

**待办 [PENDING]**：本修订删除了 `:889` 中的固定 `waitForTimeout(300)`，改为「失败上传计数为 1 → 删除占位 → 真实保存 + 读回 → 断言不再有额外 `upload-init`、无 pending 标记与无残留资源」。该修订后用例与最终 default-1 合并套件的执行由 root 指派，**尚未运行**；不得把修订前的 19/19 记作修订后的结果。
