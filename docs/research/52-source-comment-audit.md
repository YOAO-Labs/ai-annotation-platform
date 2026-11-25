# 源码注释：基线、逐条去向与全量迁移

## 全量迁移结果

首批与全量清理合计移除 **23,089 个**解释性注释 token；清理使**源码减少 28,333 个物理行**（毛减少，不抵扣新增 ADR、证据、lint 和测试）。其中本轮在 3,060 个有改动的文件减少 28,202 行，首批减少 131 行。行尾注释通常不减行；清掉独立 JSX 空表达式的花括号、与注释一同失效的空白分隔行，也会减行。因此实测值与首次词法估算不同。这不是可执行语句或运行耗时的减少。

| 本轮处置                              |  token |
| ------------------------------------- | -----: |
| R 删除重复实现或断言的叙述            | 11,054 |
| S 删除失效说明                        |    321 |
| E 有真实 issue 后移除实现借口         |     79 |
| D 移交有证据的决策                    | 11,526 |
| P 保留 ESLint 消费的既有空 catch 输入 |     96 |
| F 补认已发布的 SQL 迁移工件           |      1 |

以上覆盖原词法范围剩余的 23,069 个待审 token，以及补入的 8 个工作流内嵌 Bash 注释。冻结历史从未为减行改写，包含包根被 `bo0453` 读取的 SQL。完成后，**非冻结路径中的可删解释性注释为零**；冻结历史另有 1,842 个解释性 token，占 1,839 个独立物理行。`.env.example`、工具指令、法律文本等是真实输入，不是待清的余额。

[全量处置清单](./data/comment-retirement-e8a4f595.tsv) 使用基线源码的原始行号，每个 token 一项；R/S 表示删除，E 指向实际 issue，D 指向决策编号，P/F 指明保留原因。工作流中的真实 Shell 注释也使用原始 YAML 行号。决策编号的前四位对应 [工作台](../adr/archive/0181-workbench-interaction-and-rendering-contracts.md)、[Web 与浏览器验证](../adr/archive/0182-web-client-and-browser-verification-contracts.md)、[API 装配](../adr/archive/0183-api-orchestration-and-storage-invariants.md)、[独立服务与客户端](../adr/archive/0184-service-boundaries-and-contract-verification.md) 或[交付工具](../adr/archive/0185-delivery-and-tooling-input-contracts.md)。各记录附按源码位置索引的约束、理由及已有领域 ADR。首批已结项使用下表和原清单，不重复计数。

全量审查新增 [#160](https://github.com/YOAO-Labs/ai-annotation-platform/issues/160) 至 [#186](https://github.com/YOAO-Labs/ai-annotation-platform/issues/186) 共 27 项；加上首批 [#156](https://github.com/YOAO-Labs/ai-annotation-platform/issues/156) 至 [#159](https://github.com/YOAO-Labs/ai-annotation-platform/issues/159)，本次回修范围共 **31 项**。清单中的 E 编号永久指向当时的权宜实现，不随修复或关单改号。清理行数仅计注释迁移，不把后续实现和回归测试相抵；删掉注释、合入修复、通过 E2E 和部署是四件不同的事。

## 首次盘点（历史快照）

[逐条分类与处置清单](./data/comment-audit-e8a4f595.tsv) 对应提交 `e8a4f595b93773bd284cf052224d3aa3fe91e356`。扫描 8,153 个自维护源码、测试和配置文件，其中 3,891 个有注释。分类依据是词法 token、相邻实现和用途，不是出现 `TODO`、`shortcut` 等字样的次数。未迁移项为语义初分，不是已确认缺陷，也不是删除授权。

| 类别                                     | 基线 token | 首批已处置 token | 删除物理行 |
| ---------------------------------------- | ---------: | ---------------: | ---------: |
| R 赘述                                   |      5,328 |               21 |         21 |
| S 过时                                   |        330 |               20 |         30 |
| E 借口／权宜实现                         |         38 |                7 |         19 |
| D 决策记录                               |     19,323 |               61 |         61 |
| P 受保护的功能性输入，不作解释性注释处理 |      7,659 |                0 |          0 |

首次词法估算：基线有 29,007 个只含解释性注释的物理行，当时识别出的冻结历史为 1,838 行，估计可减少 27,169 行。首批减少 131 行，全量清理前的估算余量为 27,038 行。共享 JSX 花括号等情况不计作独立注释行；最终实测见上表。

## 怎样读清单

TSV 每行一个原文件，`classifications` 列按词法顺序给出 `类别起始行[-结束行]`。没有后缀表示尚未迁移或受保护；`>del` 表示已核对并删除，`>#编号` 表示已为真实实现建单，`>ADR文件名` 指已承接决策的完整文件名（省略 `.md`）。同一行可以有多条记录。定位、保护原因和原文可用 `pnpm --silent lint:comments --inventory --jsonl --revision e8a4f595 <路径>` 复查。`bootstrap_fingerprints` 是未迁移解释性 token 按文件保留的 BLAKE2s-128 指纹（CRLF 归一为 LF，重复项计数）；`-` 表示没有额度。它只用于第一次引入门禁，不是以后更新的白名单。

统计排除第三方、生成物、锁文件及 Markdown 正文。Python docstring、字符串、正则、heredoc 数据和 SQL dollar-quoted 字符串不是宿主语言的注释；不递归统计其中可能嵌入的代码。保留 SQL 分句标记、`.env.example` 文档生成输入、Excalidraw 元数据、工具指令和法律声明。历史迁移仍受新增门禁约束，不为删行回写既有工件。

## 首批去向

- 完整处理 `apps/training-executor/` 的 77 个解释性 token。训练与预览之间的资源约束、日志、受控初始化和模型回传等理由进入原有 [0077](../adr/archive/0077-native-augmentation-preview-cpu-ownership.md)、[0078](../adr/archive/0078-attempt-observability-and-redacted-logs.md)、[0081](../adr/archive/0081-controlled-training-initialization.md)、[0082](../adr/archive/0082-budgeted-training-studies.md)、[0084](../adr/archive/0084-qualified-libreyolo-model-return.md)、[0188](../adr/archive/0188-task-scoped-native-instance-learning.md)、[0189](../adr/archive/0189-versioned-yolo-identities-and-content-assets.md)。
- 已建单：[固定 batch 依赖私有 OOM 计数器](https://github.com/YOAO-Labs/ai-annotation-platform/issues/156)、[验证字体在训练时复制改名](https://github.com/YOAO-Labs/ai-annotation-platform/issues/157)、[成员综合分使用固定活跃值](https://github.com/YOAO-Labs/ai-annotation-platform/issues/158)、[图片性能基准的尺寸和密度标签不控制实际 fixture](https://github.com/YOAO-Labs/ai-annotation-platform/issues/159)。移除解释并不等于修复了实现。
- 数据集关联弹窗已经使用 `useLinkProject`；受跟踪 OpenAPI 快照已经包含 `keypoint_schema`、`rendering_config`、`default_variants` 等字段。相反的旧说明已删除，没有为已不存在的绕路建单。性能 smoke 的说明也不再计入它没有执行的交互。

后续运行 `/delete-comment <目录>` 重新核对当前源码；[门禁用法](https://yoao-labs.github.io/YOAO-Docs/dev/testing#源码注释门禁)。不要按历史行号盲删。
