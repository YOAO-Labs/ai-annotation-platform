---
title: Agent 接入与操作
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-17
---

# Agent 接入与操作

Agent 可以使用 Python SDK 或 `aap` CLI 完成发现、读取、预检和明确提交。机器入口沿用现有凭据与权限，不需要额外服务。全量 API 台账区分已覆盖、主动排除和尚未接入；接口存在不代表当前账号可调用，也不代表当前执行器在线。

## 按需发现能力与请求

```bash
aap capabilities --status covered --limit 20 --offset 0 --json
aap capabilities --tag training-runs --json
aap describe training_runs.create --json
aap describe learning.runs.create --json
```

能力列表有界分页，支持按 tag 和状态筛选；`describe` 展开一个具体操作。SDK 方法名为规范标识，对应 CLI 的点分路径可作为别名。返回内容包括 SDK/CLI 参数、关联 API、请求 schema 与额外约束，读取时同时核对限制、幂等和确认行为。

Python 核心包也可离线读取：

```python
from ai_annotation import list_capabilities, describe_operation

page = list_capabilities(status="covered", limit=20, offset=0)
operation = describe_operation("training_runs.create")
print(page)
print(operation)
```

生成的目录随 wheel 分发，来源是 OpenAPI、覆盖分类及实际 SDK/CLI 声明；请求 schema 复用安装包里的模型。`covered` 表示有实现，`excluded` 表示明确的范围决定，`uncovered` 表示 API 已存在但 SDK 尚未接入。描述不会把 Python callback 或不透明 kwargs 伪装成可自动执行的 JSON 工具。

## 保持可核对的操作流程

1. 用 `describe` 读取参数、请求 schema、限制和操作级要求。
2. 确认项目权限、冻结数据与精确模型/执行身份；按需调用预检、资格或恢复检查。
3. 明确输入预算、范围、任务与模型，不补算未知证据。
4. 预检通过后带回原摘要，在发送前保存稳定 `request_id` 和完整请求。结果不确定时复用原请求，不重新生成 ID。
5. 读取资源和当前 attempt 的状态，使用有界轮询、分页与游标；超时保留资源 ID，不隐式取消、恢复或重建。
6. 显式完成评估、部署、人工决策和发布，并验证最终下载文件。

创建训练需要 `preflight_digest`，参数搜索创建需要 `preview_digest`；共享的预检请求 schema 不一定能表达创建阶段的全部要求。配置选用 `configuration` 时不能同时显式提交旧 `parameters`。

`dataset_id` 在训练请求里是冻结版本 UUID，不是上传素材数据集或任务版本流 ID。策略、配方和发布分别保留各自的 revision/generation。`cancelling` 与 `lost` 不表示 GPU 已释放，`202` 不表示完成；执行与 Job 状态分别读取。

## 机器输入与错误输出

```bash
aap --error-format json learning runs preflight --project "$PROJECT_ID" --body run-draft.json --json
aap --error-format json learning runs create --project "$PROJECT_ID" --body - --json < run-request.json
```

`--body -` 从 stdin 读取 UTF-8 JSON 对象，和文件使用相同校验。根选项 `--error-format json` 必须放在命令前；它让失败以结构化 JSON 写到 stderr，成功 stdout 和原有退出码约定不变。未选择该模式时仍输出文本错误。

区分认证、权限、资源不存在、陈旧预检/修订冲突、参数错误、限流和执行不可用。服务端错误码不存在时保持未知，不猜测下一步；网络失败后的写请求不能因存在 retry-after 信息就重新生成请求 ID 重发。字段错误只包含经过处理的位置与类型，不回显请求输入或凭据。

Job 失败或等待超时的错误保留 `job_id`，用它继续读取现有任务；失败状态通过 `job_status` 提供，不输出原始 Job payload。

删除、取消、重试、恢复、部署、发布及其他受保护操作仍需明确确认；JSON 模式使用 `--yes`。确认不会绕过服务器的权限、预算或冲突保护。某些训练列表 GET 会协调失联状态，不能仅按 HTTP 方法推断无服务端副作用。

## 控制返回量与导出范围

先读取短列表和状态，再展开必要详情。使用 `limit`/`offset` 或操作自身的 cursor；不需要冻结清单时用 `include_manifest=False`。日志不能同时指定 `after` 与 `before`，切换 attempt 后重新读取游标。

视频导出范围和点云选项写入 `ExportRequestBody` 的 JSON body，异步导出仍返回 Job。VOC 返回同步 ZIP，使用专用 `download_voc` 方法或 CLI 同步下载路径，不解析为 Job ID。流式下载校验已有的摘要/大小后原子替换目标，中断或失败保留原文件。

完整参数与示例见 [Python SDK 参考](./python-client)、[CLI 参考](./cli) 和 [Cookbook](./cookbook)。安装包内还提供英文 agent 操作说明：

```python
from importlib.resources import files

guide = files("ai_annotation").joinpath("AGENT_GUIDE.md").read_text(encoding="utf-8")
print(guide)
```
