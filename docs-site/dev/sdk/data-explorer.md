---
title: Data Explorer 与保存视图
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-17
---

# Data Explorer 与保存视图

Data Explorer 为脚本与 Agent 提供项目 schema、任务／对象／轨迹查询、保存视图，以及明确选择任务后的分配和导出。所有查询沿用当前账号的可见任务范围；项目 ID 不代表可以读取项目内全部任务。先读取 schema，再构造筛选和选择需要处理的资源。

```bash
aap data-manager schema --project "$PROJECT_ID" --entity-scope tasks --json
aap describe task_views.query --json
aap describe data_manager.query_objects --json
aap describe data_manager.assignment_apply --json
```

## 按项目 schema 构造查询

`schema` 返回当前粒度的字段、类型、操作符、选项、列、默认列和排序字段。动态属性键由项目工具与 attribute schema 决定，不是跨项目通用的固定词表。属性键可以含点号；不要截断键名或展平嵌套条件组。同一个 AND 中的对象条件具有同对象匹配语义，改写分组可能改变结果。

```python
from ai_annotation import Client, ProjectTaskQueryRequest, DataManagerEntityQueryRequest

PROJECT_ID = "<project-uuid>"

with Client() as client:
    task_schema = client.data_manager.schema(PROJECT_ID)
    tasks = client.task_views.query(PROJECT_ID, ProjectTaskQueryRequest(
        filter_json={}, columns_json=task_schema.default_columns, limit=20, offset=0,
    ))
    object_schema = client.data_manager.schema(PROJECT_ID, entity_scope="objects")
    objects = client.data_manager.query_objects(PROJECT_ID, DataManagerEntityQueryRequest(
        filter_json={}, columns_json=object_schema.default_columns, limit=20,
    ))
    print(tasks.total, objects.total, objects.next_cursor)
```

筛选使用 `{}`、`and`／`or` 分组或 `{field, op, value}` 叶节点，字段和值仍由服务器按当前 schema 校验。树深最多 32 层、节点最多 4096 个，单个 `in` 最多 200 项。CLI 文件／stdin 拒绝 NaN、Infinity 和溢出成无限值的数字；未知字段与无效操作符不会变成原始 SQL 或在客户端执行。

| 粒度   | 分页                          | 返回口径                                             |
| ------ | ----------------------------- | ---------------------------------------------------- |
| task   | `limit`／`offset`，每页 1–200 | 一行一个可见任务，`total` 为完整匹配任务量           |
| object | `limit`／不透明 `cursor`      | 一行一个合格 active 对象，facet 针对完整匹配对象集合 |
| track  | `limit`／不透明 `cursor`      | 一行一条逻辑轨迹，不能按 task／关键帧重复解释        |

任务查询没有 cursor、revision 或 ETag。显式任务列清单可减少昂贵投影；空列清单保留较宽的兼容行为。解释指标时核对请求列，未请求的昂贵指标可能保留兼容默认值，不能据此断言真实数量为零。对象／轨迹当前只使用第一项排序；其 columns_json 用于白名单校验，当前不会裁剪返回行字段。改变筛选或排序后重置实体游标，不解析或跨查询复用它。

`summary` 是完整匹配任务范围的聚合，与当前页无关；请求中的 dimensions 当前不提供选择性返回。`matches` 为单个可见任务返回命中摘要，annotation、prediction shape 和 tracker job 共用一个 offset/limit 窗口，不返回 raw geometry。

```bash
aap data-manager summary --project "$PROJECT_ID" --body summary.json --json
aap task-views query --project "$PROJECT_ID" --body task-query.json --json
aap data-manager query-objects --project "$PROJECT_ID" --body object-query.json --json
aap data-manager query-tracks --project "$PROJECT_ID" --body track-query.json --json
```

这些 POST 是读取，不需要 `--yes`。对象详情返回 `{item: ...}`，定位接口直接返回 location。轨迹必须使用查询返回的 `track_ref`，不能用业务 `track_id` 替代；SDK 会编码路径参数。轨迹详情返回全部可见成员，当前没有成员分页，宜先用短列表确定需要展开的轨迹。

## 保存和复用视图

```bash
aap task-views list --project "$PROJECT_ID" --scope tasks --json
aap task-views create --project "$PROJECT_ID" --body view.json --json
aap task-views get "$VIEW_ID" --project "$PROJECT_ID" --json
aap task-views tasks "$VIEW_ID" --project "$PROJECT_ID" --limit 20 --offset 0 --json
aap task-views copy "$VIEW_ID" --project "$PROJECT_ID" --body copy.json --json
aap task-views delete "$VIEW_ID" --project "$PROJECT_ID" --yes --json
```

`ProjectTaskViewCreate` 保存 name、entity_scope、visibility、filter_json、sort_json 和 columns_json。私有视图属于当前账号；共享视图对有项目访问权的成员可读，写入要求项目管理权。复制默认沿用来源 visibility，普通成员复制共享视图时应明确提供 `{"visibility":"private"}`；copy 请求 body 必须存在，即使使用 `{}`。

列表包含内置与保存视图，没有服务端分页。内置项 `id=null`、以 key 标识，不能传给保存视图的 UUID 操作；可将其配置交给普通任务查询。`task-views tasks` 仅支持已保存的 task 粒度视图，对象／轨迹视图应使用对应实体查询。

项目配置变化后，视图可能保留 invalid_fields 与 null 计数；null 不等于零。更新省略字段或给 null 都表示不修改，清空 filter/sort/columns 应用 `{}`／`[]`；entity_scope 不能通过更新改变。名称冲突返回 409，跨项目或不可见视图返回 404。删除只删除配置，不删除任务、对象或轨迹。

## 预览并应用选中任务的分配

```python
from ai_annotation import (
    Client, DataManagerTaskAssignmentRequest, DataManagerTaskAssignmentApplyRequest,
)

PROJECT_ID = "<project-uuid>"
TASK_ID = "<selected-task-uuid>"

with Client() as client:
    draft = DataManagerTaskAssignmentRequest(task_ids=[TASK_ID], annotator_id=None)
    preview = client.data_manager.assignment_preview(PROJECT_ID, draft)
    print(preview.items, preview.eligible_count, preview.skipped_count)
    # 核对有效人员和逐项原因，明确确认后再提交。
    result = client.data_manager.assignment_apply(PROJECT_ID,
        DataManagerTaskAssignmentApplyRequest(
            task_ids=draft.task_ids, annotator_id=None,
            preview_version=preview.preview_version,
        ),
    )
    print(result.succeeded, result.skipped_count, result.failed_count)
```

选择 1–200 个任务 UUID，模型会排序并去重。annotator_id／reviewer_id 至少明确给一个：省略保留原 task override，null 清除 override 后继承批次人员，不能把 null 理解为始终无人负责。预览显示原值、目标值和 effective 人员，可解释批次继承。

应用需同一选择、同一显式目标和返回的 preview_version。服务器在行锁下重新计算；状态、锁或分配变化可能产生 409，需重新预览并作出决定。SDK 不自动刷新预览或重试应用。完成／锁定／归档等不合格任务会按实际规则跳过或失败，HTTP 成功不代表全部任务已修改。

```bash
aap data-manager assignment-preview --project "$PROJECT_ID" --body selection.json --json
aap --error-format json data-manager assignment-apply --project "$PROJECT_ID" --body apply.json --yes --json
```

## 仅导出明确选中的任务

```python
from ai_annotation import Client, DataManagerTaskExportRequest

PROJECT_ID = "<project-uuid>"
TASK_ID = "<selected-task-uuid>"
IDEMPOTENCY_KEY = "<caller-persisted-key>"

with Client() as client:
    request = DataManagerTaskExportRequest(task_ids=[TASK_ID], targets=["aap_json"])
    accepted = client.data_manager.export_tasks(
        PROJECT_ID, request, idempotency_key=IDEMPOTENCY_KEY,
    )
    job = client.jobs.wait(accepted.job_id, timeout=600)
    client.exports.download(job, "./selected-tasks.zip")
```

该接口不会回退到全项目导出。服务器要求导出能力并检查每个任务仍可见；选择最多 200 个任务，不能同时提供视频 scope。当前选中任务导出不支持 VOC、coco-multicamera、nuscenes、kitti、pointmask；失败后保持原选择和范围核对原因。

```bash
aap data-manager export-tasks --project "$PROJECT_ID" --body export.json --idempotency-key "$IDEMPOTENCY_KEY" --yes --json
aap jobs get "$JOB_ID" --json
```

可选 Idempotency-Key 在服务器 trim 后要求 1–128 字符，按操作、项目和账号限定。先保存完整请求与原键；同键的不同 canonical 请求返回 409，不生成替代键或自动重试 POST。HTTP 202 返回 Job ID 与实际状态，重放可能返回已有终态；CLI 不隐式等待或下载。Python 示例中的等待与下载是独立显式调用，下载沿用既有流式落盘、原子替换与凭据隔离保护。短期签名 URL 不是可持久保存的文件路径。

权限和资源关系由服务器当前状态决定。API key 作用域不能代替项目成员／管理权、任务可见性或导出能力。完整边界见 [查询与校验契约](/dev/reference/data-manager-query)、[Agent 接入与操作](./agents) 和 [CLI 参考](./cli)。
