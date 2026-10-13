---
audience: [project_admin]
type: how-to
since: v0.1.0
status: stable
last_reviewed: 2026-07-29
---

# 批次与分配

批次（Batch）是项目内一组任务的业务分组，也是分配、推进、审核和归档的基本单位。

常见用途：

- 按数据来源分批：不同供应商、采集批次、数据域
- 按运营节奏分批：周批、月批、活动批
- 按难度或人员分批：新手批、高优先级批、复审批

## 创建批次与分包

关联数据集后，尚未划分到批次的任务称为**未归类任务**，会在批次页顶部以横带提示。**非 scene 模式项目**把未归类任务分到批次有三种入口：

- 批次页右上角 **创建批次**（走「创建批次」对话框）
- 未归类任务横带上的 **去分包**（走「创建批次」对话框）
- 未归类任务横带上的 **一键全量建包**：跳过对话框，直接把全部未归类任务注入单个新批次。大数据集导入后用它一步让任务进入工作流。

> scene 模式项目分包只能按 scene：上述随机/顺序入口会统一替换为单个 **按 scene 建包** 按钮（横带与右上角「创建批次」均直接按 scene 分包，不弹对话框）。详见下方 [按 scene 分包](#按-scene-分包)。

> 横带上的 **浏览未归类** 可分页查看当前未归类任务清单。
>
> 关联数据集时，条目数超过当前同步阈值才转后台建任务，界面会显示进度条；建完后未归类任务才出现在横带。阈值默认 2000，超级管理员可在系统设置中调整；等于阈值仍同步，设为 0 时所有非空数据集都转后台。

前两个入口的对话框参数：

对话框参数：

- **批次数量**：把未归类任务切分到几个批次（1–100）。
  - 填 **1** 时，等于「把全部未归类任务注入一个新批次」，此时第二个输入是该批次的**完整名称**。
  - 填 **≥2** 时，按数量平均切分，第二个输入是各批次的**名称前缀**（生成 `前缀 1`、`前缀 2` …）。
- **顺序 / 打乱**：
  - **顺序切分**：按任务导入顺序分配，不打乱（结果稳定、可预期）。
  - **打乱切分**：随机打乱后再切分。
- **优先级**：新批次的优先级（0–100）。

切分只作用于**未归类任务**，不会动已经归属其它批次的任务。新建批次落在 `draft`，需再激活才进入工作流。

> 注意：批次创建后目前没有「往已有批次追加任务」的入口；要把未归类任务放进批次，请通过上面的切分流程（可一次只切 1 个批次）。

### 按 scene 分包

scene 模式项目在关联数据集时（经新建项目向导）会**自动**按 scene 分包，无需手动操作。该策略会把同一个 scene 的所有帧放进同一个批次，避免连续帧被分给不同标注员或进入不同审核批次。

若之后又出现未归类任务（例如删除批次后任务回归、或事后追加 scene 数据集），横带与右上角「创建批次」会提供唯一入口 **按 scene 建包**，按同一策略一键重分；scene 模式下不提供随机/顺序切分。

按 scene 分包的结果：

- 一个 scene 生成一个批次。
- 批次内任务按 `frame_index` 升序排列。
- 任务的 `sequence_order` 会写成对应帧号，便于顺序派题。
- 没有 scene 信息的未归类任务会进入单独的“无 scene”批次，便于管理员发现并补数据。

如果项目不是 scene 模式，通常继续使用顺序/随机切分。

## 批次状态机

![批次列表各状态彩色徽标](../images/projects/batch-status-list.png)

列表的「操作」列默认只显示图标，将光标移到某个按钮上时会展开该按钮的名称，移开后收起。使用 Tab 键聚焦按钮也能显示名称；触屏设备直接显示名称。停留在按钮上可查看详细提示，包括按钮不可用的原因。

批次状态机如下：

<ExcalidrawDiagram
  src="/diagrams/shared/state-machines/batch-lifecycle.svg"
  alt="Batch 从草稿激活，可经 AI 预标或人工标注进入审核，审核后通过、退回或归档，并支持 owner 逆向恢复"
  caption="Batch 完整状态机：自动推进、人工操作与 owner 恢复路径"
/>

说明：

- `active → pre_annotated` 由 AI 批量预标成功结束后自动触发
- `active → annotating`、`pre_annotated → annotating` 有自动路径，不需要管理员手工点状态
- `annotating → reviewing` 既可能由“全量完成”自动触发，也可能由标注员主动整批送审触发；整批送审会先把批次内所有 `pending / in_progress` 任务提交质检，再推进批次状态
- owner 可以对批次执行 admin lock / unlock。admin lock 是软暂停：阻止新派单并冻结自动状态推进，但不会把已有任务变成只读。
- `reset → draft` 是正常迁移图之外的管理性重置，可以从任意状态触发，并会同步重置 task、锁和 AI 预标产物

## 状态语义

| 状态            | 含义                                               | 典型进入方式                                                                         | 下一步                                                                             |
| --------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------- |
| `draft`         | 批次已建好，但还未正式投入生产                     | 新建批次、`reset → draft`                                                            | owner 激活                                                                         |
| `active`        | 已准备好，可开始分派或开始标注，但尚未进入“进行中” | `draft → active`、`archived → active`、`rejected → active`、`pre_annotated → active` | 标注员开始后自动转 `annotating`，或 owner 归档，或 AI 预标完成后转 `pre_annotated` |
| `pre_annotated` | AI 预标已跑完，等待人工接管                        | AI 文本批量预标完成                                                                  | 标注员接管后自动转 `annotating`，owner 也可丢弃预标结果退回 `active`               |
| `annotating`    | 批次处于标注进行中                                 | 任一 task 进入 `in_progress` 或 `rejected`                                           | 全部任务完成后进 `reviewing`，或 owner 归档                                        |
| `reviewing`     | 审核员开始整批复核                                 | 标注员送审或系统自动推进                                                             | reviewer 通过到 `approved`，退回到 `rejected`                                      |
| `approved`      | 审核完成，业务上通过                               | reviewer approve                                                                     | owner 可归档，也可重开审核                                                         |
| `rejected`      | 审核退回，批次需要返工                             | reviewer reject                                                                      | 标注员重做，或 owner 直接重新激活 / 重开审核 / 归档                                |
| `archived`      | 批次结束，不再作为工作中的生产批次使用             | owner archive                                                                        | owner 可撤销归档回 `active`                                                        |

## 自动迁移与手工迁移

### 自动迁移

以下状态变化由系统自动驱动：

- `active → pre_annotated`
  触发条件：AI 批量预标成功结束且不是全量失败
- `active → annotating`
  触发条件：批次内有任务进入 `in_progress` 或 `rejected`
- `pre_annotated → annotating`
  触发条件：人工接管后，批次内有任务进入 `in_progress` 或 `rejected`
- `annotating → reviewing`
  触发条件：批次内不再存在 `pending / in_progress / rejected` 任务

### 手工迁移

以下状态变化依赖用户操作：

- owner：`draft → active`
- 标注员：整批送审，把自己批次内未送审的 `pending / in_progress` 任务提交质检并进入 `reviewing`
- reviewer：`reviewing → approved`；整批退回可对 `active / annotating / reviewing` 批次执行（部分送审的批次也能打回重做）
- owner 重新激活返工：`rejected → active`
- owner 需填写理由的逆向迁移：
  `archived → active`、`approved → reviewing`、`rejected → reviewing`、`pre_annotated → active`
- owner 可从 `active / pre_annotated / annotating / approved / rejected` 归档；`draft / reviewing` 不能直接归档

## 角色分工

| 角色                     | 主要动作                                         |
| ------------------------ | ------------------------------------------------ |
| 项目 owner / super_admin | 创建批次、分配人、激活、归档、逆向迁移、终极重置 |
| 标注员                   | 在自己负责的批次上开始标注，把批次送审           |
| 审核员                   | 审核 `reviewing` 批次，决定通过或退回            |

补充：

- owner 是批次状态机的最终兜底角色
- 标注员不能直接把批次改成 `approved` / `archived`
- reviewer 不负责激活、归档或终极重置

## 分配人员

当前批次采用“一批次一标注员 + 一审核员”的单值语义：

- `annotator_id`：该批次的主标注员
- `reviewer_id`：该批次的主审核员

批次创建后通常先做两件事：

1. 指定标注员 / 审核员
2. 从 `draft` 激活到 `active`

如果只创建批次、不激活，它不会进入正常工作流。

单批次分派、多选后的「重新分配」和「按项目分派批次」都会先展示预览：逐批次的当前与目标负责人、实际任务数、每位接收人的新增待办及已有待办。项目分派默认只填补未分派职责；选择「覆盖全部批次」后会列出已有负责人被替换的影响。单批次清除负责人也会先预览。

标注待办包括待开始、进行中和退回任务，审核待办只计入审核中任务，已通过任务不计入待办。已有待办汇总该成员各项目的分派；这些数字只表示条数，不代表媒体难度或工时。确认时重新校验预览，批次或负载改变后需重新预览；大批量明细可展开查看全部。

## 批量操作

<DocsVideo
  src="/media/projects/batch-bulk-actions.mp4"
  poster="/media/projects/batch-bulk-actions-poster.webp"
  alt="多选批次后显示批量通过和批量驳回工具栏"
  caption="勾选多个批次后，批量操作栏会显示可用动作，并在执行后保留结果摘要。"
/>

已实现的多选批量操作是：

- 激活（owner）
- 归档（owner）
- 重新分配（owner）
- 删除（owner）
- **批量通过**（reviewer）：把多个 `reviewing` 批次一次性置为 `approved`
- **批量驳回**（reviewer）：把多个 `reviewing` 批次一次性置为 `rejected`，同时填写驳回反馈

说明：

- 批量激活只适用于 `draft` 批次，且批次须**已分配标注员**且**有任务**（`total_tasks > 0`）；不满足条件的批次会在结果摘要中标记为失败（`no annotator assigned` / `batch has no tasks`）
- 批量通过 / 批量驳回只适用于 `reviewing` 批次，操作角色为 reviewer（含 super_admin）
- 批量归档、删除、改派都受当前状态和权限约束，部分批次可能成功，部分批次会被跳过或失败

## 删除批次

**默认保护**：删除一个含**非 `pending` task**（进行中 / 审核中 / 已完成）或**被 AI 预标过的 task** 的批次会被拒绝，提示你改用「归档」或确认**强制删除**。纯草稿 / 全 `pending` 且无预标的批次可直接删除。

确认强制删除（或删除无成果批次）时，批次内 task 的处理：

- **所有非 `pending` task 重置回 `pending`**，再解绑（task 变为「未归类」，可重新分包）。
- **人工标注保留**：`source=manual` 的标注不丢，task 回到待标注入口可继续。
- **AI 预标清除**：该批次 task 上的 AI 预测结果与已采纳的 AI 标注（`source=prediction_based`）会被清掉，`total_predictions` 归零。这样重新分包并再次预标时不会与旧预测叠加出重复标注。
- **审核结论丢失**：`review` / `completed`（已通过）/ `rejected` 的 task 一并回到 `pending`——删除意味着这批工作要重新走流程，但已标注的几何不丢。

> 因此删除批次不会留下看不见的孤儿任务，也不会污染后续新批次的计数。批量删除时，含成果的批次会被跳过（标记「未删除」），可对它们单独强制删除。`B-DEFAULT` 默认批次不可删除。

## Admin lock / unlock

<!-- history: ADR-0008 implemented the current soft-hold semantics. -->

owner 可以在批次行上点击锁定，为批次写入锁定原因。锁定后：

- scheduler 不会再从该批次派出新任务；
- 自动状态推进会跳过该批次；
- 批次卡片显示锁定状态与原因；
- 已经可见的任务与已有 task lock 不会被强制撤销；
- 解锁后恢复正常派题与自动推进。

这不是严格只读冻结。需要彻底重来时，用「重置到草稿」；需要结束生产时，用「归档」。

## 批次导出

<!-- TODO(v0.14.18) IMAGE_CHECKLIST: images/projects/batch-export-dialog.png — 导出格式选择面板 [manual] -->

项目管理员可以按批次导出当前批次内的任务标注。支持格式按项目数据类型如下：

| 数据类型 | 可选导出格式                                                                                                                                                       |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 图片     | `coco`、`yolo`（= `yolo-det`）、`yolo-obb`（旋转框）、`yolo-seg`（实例分割）、`aap_json`、`voc`（仅单独导出，同步下载）                                            |
| 视频     | `video_json`、`aap_json`、`mot`、`kitti`（tracking label）、`yolo-frames-det`、`yolo-frames-seg`、`coco-frames-seg`、`davis`（palette PNG）、`youtube-vos`、`mots` |
| 3D 点云  | `aap_json`、`coco-multicamera`（合并人工相机 bbox）、`kitti`（3D label）、`nuscenes`、`pointmask`                                                                  |

点云 `aap_json` 支持 `axis_frame` 参数（`iso`（默认，平台内部坐标系）或 `source`（数据集原始传感器坐标系））。Multi-camera COCO 会合并批次内全部相机 role 的持久化人工 bbox，并保留无框图作为负样本；媒体、bbox 与 SceneTrack 关系不可信时会在入队前拒绝。KITTI 必须显式选择相机通道，并通过逐帧标定与图像尺寸预检。nuScenes 只接受由 nuScenes 以 ego 坐标导入、包含完整 Scene 且保留原始媒体、地图、位姿和传感器合同的数据；批次只覆盖部分 Scene 时会在入队前拒绝。

视频轨迹批次支持两种帧模式：

- **关键帧**：默认模式，只导出人工 / 预测关键帧。
- **所有帧**：按关键帧插值展开逐帧 bbox。

如果要把视频标注导成传统检测训练集，选择 **YOLO 逐帧**：系统会按项目采样网格生成 label，并附 `fetch_frames.py` 用本地 ffmpeg 抽出对应帧图。要导成分割训练集则选 **YOLO 逐帧分割**：同样按采样网格抽帧，但保留多边形顶点（bbox / polyline 跳过）。若下游用 COCO 工具链（pycocotools / Detectron2 / MMDetection），选 **COCO 逐帧分割**：同样按采样网格抽帧、保留多边形顶点，但产出标准 COCO `annotations.json`。

如果关闭“包含属性”，导出文件不会包含项目属性 schema，也不会包含标注属性。

> 大项目 / 大批次导出（十万级任务）由后台 worker 分块读取、ZIP 落盘后流式上传，内存占用与任务量解耦，不会因体量过大而内存超限失败。导出方式与小项目一致：异步生成、完成后给下载链接。

## 退回与重做

整批退回把批次置为 `rejected` 并同步影响任务：

- 接受 `active / annotating / reviewing` 三种来源状态：审核页列出的部分送审批次（还有任务未送审）也能整批打回
- 仅 `review` / `completed` 任务会被回退为 `pending`
- 现有 annotation 历史会保留，不会被硬删除
- `review_feedback` 会保留在批次上，供标注员查看退回原因

这意味着“批次被退回”更接近“软重开”，而不是“整批清空重做”。通用 `transition` 的 `reviewing → rejected` 仍然只改批次状态；只有专用的整批退回入口会写反馈并重置任务。

## 终极重置到 Draft

owner 可以把任意状态批次 `reset → draft`。这是一个比普通逆向迁移更重的兜底动作。

它会做这些事：

- 批次状态回到 `draft`
- 批次下 task 状态统一回到 `pending`
- 删除 task locks，释放现场锁
- 清理批次 review 元数据
- 对 AI 预标相关数据做级联清理

适用场景：

- 批次切错了，想彻底重新来一遍
- AI 预标结果要整批作废
- 运营流程需要把批次回滚到“未启动”阶段

## 项目管理员日常操作建议

分派和改派时，接收账号必须启用，并在当前项目中具有对应的标注员或审核员职责。确认时会重新检查资格；若账号已停用、角色变化或成员关系被移除，请刷新成员列表后重新选择。

推荐节奏：

1. 先按业务维度切出批次
2. 再做标注员 / 审核员分派
3. 确认有任务后再激活
4. 标注完成进入审核
5. 通过后归档

不推荐的做法：

- 把所有任务都塞进一个超大批次再靠任务列表硬筛
- 在 `annotating` 中频繁手工做状态折返
- 把“归档”当成“临时暂停”使用

## 当前未实现能力

以下能力在 ADR / Roadmap 中有规划，但尚未上线：

- 批量锁定 / 批量解锁
- 锁定后严格禁止已持锁用户继续编辑
- 批次级共识 / IAA 与智能切批
