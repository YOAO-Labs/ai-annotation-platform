# 0046 — 全局 Pipeline 库:持久化命名编排模板 + 三档作用域

- **Status:** Accepted
- **Date:** 2026-07-07（回填；实现于 v0.21.0 / PR #50，迁移 `0112`）
- **Deciders:** core team
- **Supersedes:** —（在 [ADR-0043](./0043-staged-preannotation-pipeline.md) 运行时编排 / [ADR-0044](./0044-global-ml-backend-registry-and-project-enablement.md) 全局注册表之上做「持久化 + 作用域」的加法，不推翻）

## Context

[ADR-0043](./0043-staged-preannotation-pipeline.md) 把跨 backend 流水线从 backend 内部上提到平台层，但那套 `pipeline_stages` 是**运行时临时编排**:它只作为 `PreannotateRequest.pipeline_stages` 出现在预标请求体里，跑完即弃（拓扑仅为追溯落 `PredictionMeta.extra`）。项目侧「记住这条编排」的唯一落点是 `Project.preannotate_pipeline` 单条 JSONB 列——**一项目一条、无名、不可跨项目复用**。

[ADR-0044](./0044-global-ml-backend-registry-and-project-enablement.md) 已把 backend 注册表全局化,能力池是全局的;但「用这些全局能力搭出来的编排」还锁在单项目的 JSONB 列里——层次不对称:**能力全局、编排却每项目一份私货**。

由此的痛点:

1. **编排不能复用**:A 项目搭好的「detect → 车辆属性」DAG,B 项目要用只能照着重搭。多个同构项目间反复手搭同一条编排。
2. **一项目一条**:项目只能持有「当前那一条」,存不下多套备选(如白天检测密集档 / 夜间轻量档),换一套就覆盖掉上一套。
3. **无治理层次**:没有「组织内共享」「平台公共模板」的概念。人人各搭一套,无沉淀、无策展、无复用热度可见。

需要把编排从「Project 的一条 JSONB」升级为**可命名、可复用、有作用域**的一等资源。

| 选项                                                                                                                    | 主要卖点                                                                                                                 | 主要劣势                                                                                     |
| ----------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| **A. 独立 `project_pipelines` 表 + 三档作用域(private / organization / public) + apply copy-on-write**                  | 命名 / 多条 / 跨项目复用;作用域天然表达 private→org→public 治理层次;项目侧仍以一条 `is_default` 维持「当前编排」语义不破 | 新表 + 数据迁移;权限要按档分层;与旧 `preannotate_pipeline` 列并存一段                        |
| B. 继续用 `Project.preannotate_pipeline`,加「从别的项目导入」                                                           | 改动最小                                                                                                                 | 仍是每项目一份拷贝,无命名无作用域,治理缺失——治标                                             |
| C. 编排直接挂到 [ADR-0044](./0044-global-ml-backend-registry-and-project-enablement.md) 的全局 backend 注册表上 | 复用现成全局层                                                                                                           | 编排是「能力的组合」而非「能力本身」,概念错层;且套用要项目上下文(类别 / 属性 schema)才能落地 |

## Decision

**采方案 A:新建 `project_pipelines` 表,把编排升级为可命名、可复用、copy-on-write、带三档作用域的一等资源。** stages 结构直接复用 [ADR-0043](./0043-staged-preannotation-pipeline.md) 的 `pipeline_stages`(存储态与运行态同构),本 ADR 只加「持久化 + 作用域 + 应用」这一层,不碰执行路径。

### 1. 数据模型(`project_pipelines`,迁移 `0112`)

`apps/api/app/db/models/project_pipeline.py:19`,关键列:

- `scope` `VARCHAR(20)`(`private` | `organization` | `public`,默认 `private`)
- `project_id` / `organization_id`:两个可空 owner 外键(均 `ondelete=CASCADE`),按 scope 二选一
- `name` / `stages`(JSONB)/ `is_default` / `created_by` / `usage_count`

约束在 **DB 层与 Pydantic 层双写**(迁移 `0112` 的四条约束对应 `apps/api/app/schemas/project_pipeline.py`):

- `ck_project_pipelines_scope` — scope 枚举 ↔ `PipelineScope = Literal[...]`(`project_pipeline.py:12`)
- `ck_project_pipelines_scope_owner` — owner 配对(见下)↔ `_validate_scope_owner`(`project_pipeline.py:15`)
- `ck_project_pipelines_default_private` — `scope='private' OR is_default=false`(只有项目私有编排能设默认)
- `uq_project_pipelines_default_per_project` — `project_id` 上 `WHERE is_default=true` 的**部分唯一索引**:每项目至多一条默认

### 2. 三档作用域 + owner 配对规则

| scope          | 语义                                      | `project_id` | `organization_id` | 可 `is_default` |
| -------------- | ----------------------------------------- | ------------ | ----------------- | --------------- |
| `private`      | 项目私有(即「项目当前 / 默认编排」的载体) | **必填**     | 必空              | 是              |
| `organization` | 组织内共享                                | 必空         | **必填**          | 否              |
| `public`       | 平台公共模板                              | 必空         | 必空              | 否              |

配对是硬约束:`_validate_scope_owner`(`apps/api/app/schemas/project_pipeline.py:15`)与 DB CHECK `ck_project_pipelines_scope_owner` 逐条对应,任一侧违规即拒(schema 层 422 / DB 层写入失败),不给「scope=public 却带 project_id」这类脏行留缝。

### 3. apply-to-project = copy-on-write 快照实例化

`POST /projects/{id}/pipelines/apply`(`apps/api/app/api/v1/projects.py:721`),入参 `{ pipeline_id, set_default }`。语义**不是活引用,是深拷贝快照**:

1. `require_project_owner` 守卫 + `assert_pipeline_visible` 校验来源模板对当前用户可见;
2. `_validate_saved_pipeline` 结构校验 + `unenabled_backend_ids`(`services/pipeline_template.py:87`)校验编排引用的每个 backend **已在当前项目启用**——直接复用 [ADR-0044](./0044-global-ml-backend-registry-and-project-enablement.md) 的项目级启用门控,未启用则 422 回带 `unenabled_backends` 列表;
3. `copy_pipeline_stages`(deepcopy)把模板 stages 拷成一条**新的 `scope=private` 项目副本**(`project_id=当前项目`,`created_by=当前用户`,`projects.py:743`);
4. 源模板 `usage_count += 1`(复用热度埋点,前端列表显示「已套用 N 次」);
5. `set_default=true` 时先 `switch_project_default_pipeline` 清掉该项目原默认,再把新副本置默认。

**copy-on-write 的后果是解耦**:套用后项目改这条副本不回灌源模板;源模板日后改动也不追灌已套用的项目。全局/组织模板是「出厂样板」,项目拿到的是自己的一份可改快照。

### 4. 「项目当前编排」= 该项目的默认 private pipeline

历史的 `Project.preannotate_pipeline`(一项目一条 JSONB)被升级为「project 名下多条 `scope=private` 记录 + 一条 `is_default=true` 标记谁是当前」。`switch_project_default_pipeline`(`services/pipeline_template.py:68`)做「先清零旧默认、再置新默认」的原子切换,`uq_project_pipelines_default_per_project` 从 DB 侧兜底「每项目至多一条默认」。旧列不立即拆(见 Consequences 迁移影响)。

### 5. 与 [ADR-0043](./0043-staged-preannotation-pipeline.md) 的关系:存储层 vs 执行层

- **stages 同构**:模板 `stages` 就是 0043 的 `pipeline_stages`。`_validate_saved_pipeline`(`projects.py:1465`)直接构造 `PreannotateRequest(pipeline_stages=stages)` 复用 0043 同款树形校验——存储态即运行态,零翻译。
- **执行仍走 0043**:本 ADR 只负责「把 stages 存下来 / 分作用域 / 拷进项目」;真正跑预标时,项目默认编排的 stages 仍沿 0043 的 `PreannotateRequest.pipeline_stages` → worker 路径执行。0046 是 0043 的**持久化与复用层**,不是新执行引擎。

### 6. 权限模型

- **创建**(`project_pipelines.py:91` + `assert_can_create_pipeline_scope`,`services/pipeline_template.py:48`):仅 `PROJECT_ADMIN` / `SUPER_ADMIN`;`public` 档**仅** `SUPER_ADMIN`;`organization` 档须指定 `organization_id` 且创建者属于该组织(超管例外)。
- **可见**(`list_pipelines` + `assert_pipeline_visible`):超管全可见;否则 `public` 人人可见、`private` 仅 `created_by` 本人、`organization` 仅该组织成员。**看不到即 404**(隐藏存在性,不泄露「有这条但你无权」)。
- **编辑 / 删除**(`can_edit_pipeline`,`services/pipeline_template.py:35`):超管、创建者本人、或组织编排的同组织成员。

## Consequences

正向:

- **编排一次搭建、跨项目复用**:全局库搭一条公共 / 组织模板,任意项目一键 apply,不再重搭;`usage_count` 让「哪条模板最常被套用」可见,便于沉淀策展。
- **治理层次归位**:private→organization→public 三档对齐平台既有的项目 / 组织 / 超管权限分层,与 [ADR-0044](./0044-global-ml-backend-registry-and-project-enablement.md) 的全局能力池层次对称。
- **「当前编排」语义不破且增强**:项目仍有唯一「默认编排」(`is_default` + 部分唯一索引),但同时能持有多条备选 private 编排,切换即改默认。
- **copy-on-write 隔离**:套用是快照,项目改副本与源模板互不干扰,避免「改了公共模板波及所有已用项目」的连锁事故。
- **约束双写、脏编排早拒**:owner 配对 / 默认唯一在 DB 与 schema 双层保证;结构非法 / backend 未启用在 apply 时即 422,不留到执行期才炸。

负向:

- **旧列并存的过渡债(迁移影响核心)**:迁移 `0112` 把每个项目非空的 `preannotate_pipeline` 回填成一条 `scope=private, is_default=true, name='项目默认编排'` 记录(`created_by=owner_id`),但 `Project.preannotate_pipeline` 列**保留一版读兼容**——`PUT /projects` 仍写它(`projects.py:674`)、`_compute_pipeline_capability_warnings` 仍读它(`projects.py:711`)。即当前是**新表 + 旧列双轨**期,存在「项目默认编排」两处真值需保持一致的窗口,后续须专门收口(废弃旧列 / 统一读表)。
- **迁移方向单向**:回填 + 建约束在 upgrade 稳健;downgrade 仅 drop 表(回填数据不逆),采 forward-only 姿态。同一迁移还顺手移除了 `projects.ml_backend_id` 外键约束(默认编排源阶段成为主 backend 派生来源,`0112` upgrade 末尾)。
- **organization 档前端未完备**:后端校验 / 权限已就绪,但全局库首版(`GlobalPipelineLibraryPage.tsx`)未提供组织选择 UI——选 `organization` 时前端仍传 `organization_id=null`,会被 `assert_can_create_pipeline_scope` 以 400 拒;该档目前实际只能经 API 直连指定 `organization_id` 使用(前端已用 hint 标注此限制)。
- **权限分层的边界用例**:公共模板由超管策展,组织模板由组织成员共管;删除 / 改动语义需随治理规则演进(如源模板删除不影响已 copy-on-write 的项目副本,反之亦然——这是隔离带来的预期行为,但需在文档中讲清)。

## Alternatives Considered（详）

**方案 B(沿用 `Project.preannotate_pipeline` + 跨项目导入)**:在单条 JSONB 上加「从项目 X 复制到项目 Y」。否决——仍是每项目一份无名拷贝,存不下多条、无作用域、无治理,只是少敲几次;与「命名复用 + 组织 / 公共共享」诉求正交,治标不治本。

**方案 C(编排挂到 ADR-0044 全局 backend 注册表)**:把编排作为注册表的子资源。否决——编排是「多个能力按 DAG 的组合」,不是「某个 backend 的固有能力」,挂上去概念错层;且套用一条编排需要项目上下文(项目类别 / 属性 schema)才能落成可执行配置,天然是「模板 → apply 到项目」的两步,而非注册表的一步。独立成表 + apply copy-on-write 才贴合语义。

## Notes

- **实现代码位置**:
  - 数据模型:`apps/api/app/db/models/project_pipeline.py`
  - Schema / 作用域校验:`apps/api/app/schemas/project_pipeline.py`(`PipelineScope` / `_validate_scope_owner`)
  - 编排库端点:`apps/api/app/api/v1/project_pipelines.py`(list / create / update / delete + 可见性过滤)
  - apply 端点 + 结构校验:`apps/api/app/api/v1/projects.py`(`apply_project_pipeline:721` / `_validate_saved_pipeline:1465`)
  - 权限与应用辅助:`apps/api/app/services/pipeline_template.py`（`assert_pipeline_visible` / `can_edit_pipeline` / `assert_can_create_pipeline_scope` / `switch_project_default_pipeline` / `copy_pipeline_stages` / `unenabled_backend_ids`）
  - 前端:`apps/web/src/pages/AIPreAnnotate/GlobalPipelineLibraryPage.tsx`(路由 `/pipelines`,全局库 UI;与 `ProjectDetailPanel` 共用 `usePipelineComposer`)、`apps/web/src/api/projectPipelines.ts`、`apps/web/src/hooks/useProjectPipelines.ts`
- **相关 alembic**:`0112_project_pipelines.py`(建表 + 四约束 + 回填 + 移除 `projects.ml_backend_id` FK)
- **相关 ADR**:
  - [ADR-0043](./0043-staged-preannotation-pipeline.md)(多阶段运行时编排,本 ADR 的持久化 / 复用层建于其上,stages 结构复用)
  - [ADR-0044](./0044-global-ml-backend-registry-and-project-enablement.md)(全局 backend 注册表 + 项目级启用,apply 时的 backend 启用门控复用其能力)
  - [ADR-0023](./0023-project-template-vs-clone.md)(项目模板 vs 克隆:copy-on-write 思路同源)
- **后续演进 / 触发条件**:
  - **旧列收口**:`Project.preannotate_pipeline` 读兼容为过渡态,待读写路径统一到 `project_pipelines` 后废弃该列(与本 ADR 迁移影响呼应)。
  - **organization 档前端补全**:补组织选择器,让 `organization` 编排可从全局库直接创建,去掉当前「需 API 直连」限制。
  - **默认编排唯一真值统一**:`is_default` 部分唯一索引已从 DB 侧兜底,收口旧列后「项目当前编排」单一真值即为默认 private 记录。
