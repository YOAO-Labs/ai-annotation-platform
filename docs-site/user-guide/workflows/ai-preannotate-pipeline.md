---
title: AI 预标注流水线
audience: [project_admin, super_admin]
type: tutorial
since: v0.9.0
status: stable
last_reviewed: 2026-10-20
---

# AI 预标注流水线

本文描述 AI 预标注的完整流程：从全局注册 ML Backend → 项目启用 → 触发预标注 Job → 标注员采用结果。

## 前提条件

- 已有可用 ML Backend（自部署或使用平台内置 Grounded-SAM-2）
- 超管已在 **模型市场**（`/model-market`）的**全局注册表**完成 ML Backend 注册（env 配置的 backend 启动后自动注册）
- 项目已创建并上传数据

## 流程概览

```
超管全局注册 ML Backend → 项目启用 backend（设主后端） → 触发批量 Job → Job 完成 → 标注员采用 Prediction
```

## Step 1：全局注册 ML Backend（超管）

1. 主导航 → **模型市场**（`/model-market`）→ **注册管理** tab
2. 点「注册 backend」，填写服务 URL（如 `http://sam-backend:8001`，全局唯一）并点击**测试连接** — 返回 200 且协议版本匹配即可
3. 保存后该 backend 进入全局注册表，可在**模型目录**中看到具体模型；**协议能力**解释支持的操作

详见 [ML Backend 协议](/dev/reference/ml-backend-protocol)。

## Step 2：项目启用 backend（项目管理员操作）

1. 项目详情 → **设置** → **ML 模型** → 点「管理 backend」在全局 backend 启用清单里**勾选启用**（推理阈值在运行时跑批配置里调，不在此预设）
2. 在 **项目主后端** 下拉里选一个已启用的 backend 并保存（不影响现有 Task 和 Prediction）

## 使用已发布的训练模型

Ultralytics 图片检测训练完成后，在项目「模型与发布」注册成功尝试、绑定类别、加载试用并发布。随后可从「模型市场 → 模型目录 → 用于预标注 / 管线」进入项目配置，或在「项目检测模型」中选择当前发布／具体已发布版本，无需增加一个 YOLO 服务。

管线的整图根阶段和父框裁剪检测阶段均可独立选择已发布模型，同一 YOLO 实例可执行多个阶段。原生类别过滤使用各自权重的索引；父框类别使用直接父阶段返回的项目类名。下游失败时，系统保存原配置和部分结果；失败项重试会重跑这一题的完整原管线，完整成功后替代旧部分候选。视频、交互和其他训练任务不属于这条回流链路。

## Step 3：触发批量预标注

![AI 预标注项目详情面板](../images/projects/ai-pre-config-panel.png)

1. 主导航 → **AI 预标**（`/ai-pre`）→ 选择项目卡片，进入 ProjectDetailPanel
2. 勾选一个或多个 `active` 批次
3. 选择幂等模式（`跳过已预标` / `覆盖` / `追加`，默认跳过已预标）
4. 点击**跑预标**（单批次）或选择串行 / 并行后批量触发
5. 系统为每个批次创建一个 Job（状态 `pending` → `running`）；Celery Worker 调用 ML Backend，逐 Task 写入 Prediction
6. 所有 Task 处理完毕后 Job 状态变为 `completed`

可在 `/ai-pre/jobs` 或右上角后台任务铃实时查看 Job 进度；失败任务在 `/ai-pre/jobs?status=failed` 查看并重试。

## Step 4：标注员采用预测结果

预标注完成后，标注员打开工作台：

- **紫色候选框** = AI 预测（Prediction）
- 点击选中某个 Prediction 框后：按 `A` 采纳选中框 / 按 `D` 驳回选中框
- 也可在右侧 AI 面板逐条点击接受 / 拒绝按钮

> **注意**：`A` / `D` 快捷键仅对**当前选中**的 Prediction 生效，不会操作全部候选框。

采纳的 Prediction 变为正式 Annotation（蓝色），可继续编辑。

## 错误处理

| 情况                            | 原因                           | 处理                                                                                                                                                       |
| ------------------------------- | ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Job 停在 `running` 超过 10 分钟 | Worker 崩溃或 Backend 超时     | 查 Celery Worker 日志（`docker logs ai-annotation-platform-celery-worker-1 --tail 100`）；参考 [Runbook: ML Backend 不可用](/ops/runbooks/ml-backend-down) |
| 部分 Task 无 Prediction         | Backend 返回空结果（置信度低） | 检查 Backend 日志，调整阈值配置                                                                                                                            |
| 连接 Backend 失败               | URL 配置错误或网络隔离         | 参考 [容器网络排查](/dev/troubleshooting/container-networking)                                                                                             |
