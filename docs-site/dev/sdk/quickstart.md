---
title: Python SDK / CLI 快速上手
audience: [dev]
type: tutorial
status: beta
last_reviewed: 2026-10-19
---

# Python SDK / CLI 快速上手

10 分钟跑通一条完整链路:创建数据集 → 上传数据 → 创建项目并关联 → 触发导出并下载。CLI 与 Python 两条路径任选其一。

处理待办、讨论反馈或执行模型推理时，继续阅读 [通知、反馈与预标注](./workflows)。

按项目 schema 筛选任务／对象／轨迹、复用视图及处理选中任务，见 [Data Explorer 与保存视图](./data-explorer)。

当前账户、会话、用户生命周期、连接器及诊断操作，见 [账户、管理与运维](./account-and-operations)。

项目／批次维护、数据运维、绩效、搜索与导入，见 [项目、批次与数据运维](./project-workflows)。

> **版本与稳定性**：SDK、CLI 与 TUI 使用独立于平台的 SemVer。`aap --version` 同时显示 SDK 版本与
> 完成测试/OpenAPI 对账的 AAP target；target 是已验证基线，不是对其他平台版本的兼容承诺。公开 API
> 以 [Python SDK 参考](./python-client)列出的命名空间、模型和异常为准。

讨论、历史、缺陷报告、Mask 导入／修复与点云评估，见 [讨论、缺陷报告与质量处理](./annotation-quality)。

任务级读取、媒体、租约、点云与几何修改见 [任务工作流](./task-workflows)；六条 WebSocket 的离线发现、typed 事件、鉴权与有界 NDJSON 监听见 [公开事件流](./streams)。REST 已覆盖全部 558 项采用接口，十项执行器内部协议保持排除。

Agent 自动化调用、离线能力发现与结构化错误，见 [Agent 接入与操作](./agents)；在线学习、数据版本冻结与模型发布，见 [在线学习与模型训练](../../api/guides/training-datasets) 及 `aap learning` CLI 命令组。

## 安装

要求 Python >= 3.11。

```bash
pip install ai-annotation-sdk            # 核心:仅 httpx + pydantic
pip install 'ai-annotation-sdk[cli]'     # + aap 命令行 (typer + rich)
pip install 'ai-annotation-sdk[tui]'     # + aap tui 终端面板 (textual, 隐含 cli 依赖)
```

三层 extras 按需选装:只在脚本里调 API 装核心包即可;要用 `aap` 命令装 `[cli]`;要用 [TUI 监控面板](./tui)装 `[tui]`。

## 准备凭据

SDK / CLI 用 `Authorization: Bearer <api_key>` 认证,接受 `ak_` 开头的平台 API key 或 JWT(SDK 不区分)。API key 可在平台 Web 端创建,也可以用已有凭据通过 SDK 的 `client.api_keys.create(...)` 创建。

可分别读取 SDK 版本和已验证的平台基线：

```python
from ai_annotation import __aap_target_version__, __version__

print(__version__)
print(__aap_target_version__)
```

```bash
aap --version  # aap <sdk-version>@AAP<aap-target-version>
```

## 路径 A:CLI

首次使用先登录。`aap login` 会先用给定凭据调一次轻量接口验证连通,成功后写入 `~/.config/ai-annotation/config.toml`(权限 0600):

```bash
aap login --url http://localhost:8000 --api-key ak_...
# 省略 --api-key 则交互式隐藏输入
```

然后串起整条链路:

```bash
# 1. 创建数据集并上传(目录逐文件上传,带进度条;ZIP 整包加 --zip)
aap datasets create --name demo-ds
aap datasets upload <dataset-id> ./imgs

# 2. 创建项目并关联数据集(异步建任务时自动等待 job 完成)
aap projects create --name demo --type image     # image|video|lidar
aap datasets link <dataset-id> <project-id>

# 3. 创建批次并添加成员
aap batches create <project-id> --name round-1 --dataset-id <dataset-id>
aap members add <project-id> --user-id <user-id> --role annotator

# 4. 触发导出 → 等待完成 → 下载,一条命令全流程
aap export project <project-id> --target aap_json --out ./export.zip
```

所有命令支持 `--json`(输出裸 JSON、退出码非 0 表示失败),供 CI / 脚本使用。删除、解绑和移除成员等破坏性命令在 JSON 模式下必须显式传 `--yes`。详见 [CLI 参考](./cli)。

## 路径 B:Python

```python
from ai_annotation import Client

# base_url / api_key 也可走环境变量 AAP_BASE_URL / AAP_API_KEY,
# 或 ~/.config/ai-annotation/config.toml(显式参数优先)
with Client(base_url="http://localhost:8000", api_key="ak_...") as client:
    # 1. 创建数据集并上传
    dataset = client.datasets.create(name="demo-ds", data_type="image")
    client.datasets.upload_files(dataset.id, ["./imgs/a.jpg", "./imgs/b.jpg"])

    # 2. 创建项目并关联数据集
    project = client.projects.create(name="demo", data_type="image")
    link = client.datasets.link_project(dataset.id, project.id)
    if link.async_job_id is not None:        # 大数据集走异步建任务
        client.jobs.wait(link.async_job_id)

    # 3. 创建批次
    client.batches.create(project.id, "round-1", dataset_id=dataset.id)

    # 4. 异步导出:创建 → 等待 → 下载
    job_id = client.exports.create(project.id, targets=["aap_json"])
    job = client.exports.wait(job_id, timeout=600)
    client.exports.download(job, "./export.zip")
```

错误统一抛 `ai_annotation` 下的异常(`AuthenticationError` / `NotFoundError` / `JobFailedError` 等),响应模型 `extra="allow"` 容忍服务端新增字段 —— 详见 [Python SDK 参考](./python-client)。

## 下一步

Agent 可先使用 `aap capabilities --status covered --json` 与 `aap describe learning.runs.create --json` 读取安装包内的能力和 schema，根选项 `--error-format json` 让失败写入结构化 stderr。详见 [Agent 接入与操作](./agents)。

已有项目标注完成后，可通过 `client.task_datasets` 查看任务版本流，用 `client.training_datasets` 冻结数据，再用 `client.training_runs` 预检并提交实验。`dataset_id` 此时指冻结训练版本 UUID，和上传数据集 ID 不同。训练流程需要项目管理权限及对应读写 scope；全局训练目录另需平台管理角色。参考 [冻结数据并提交训练](./cookbook#_6-冻结数据并提交训练) 和 [学习命令](./cli#aap-learning)。

- [Python SDK 参考](./python-client) — Client 构造、公开资源命名空间、模型与异常层级
- [CLI 参考](./cli) — 全部命令、`--json` 可脚本化契约
- [Agent 接入与操作](./agents) — 离线发现、请求约束、stdin 与结构化错误
- [TUI 监控面板](./tui) — `aap tui` 项目、数据集、任务、模型与绩效监控
- [Cookbook](./cookbook) — 可直接拷贝的完整脚本片段
