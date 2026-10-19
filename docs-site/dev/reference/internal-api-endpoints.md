---
title: 内部 API 端点
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# 内部 API 端点

`/api/v1/internal/*` 面向 Prometheus 服务发现和独立训练执行器。部署反向代理拦截外部访问，消费方通过受控内网直连 API；跨主机执行器需要单独规划可达路径。监控发现端点不进入 OpenAPI Schema，训练执行器路由保留在快照中。是否公开 Schema 不代表是否允许匿名调用，各端点仍有独立的认证规则。

---

## 1. Prometheus 服务发现：`GET /api/v1/internal/metrics-targets`

供 Prometheus [`http_sd_config`](https://prometheus.io/docs/prometheus/latest/http_sd/) 周期轮询的 JSON 服务发现端点，从全局 `ml_backend_registry` 表（`MLBackendRegistry`）动态生成 ML Backend 的抓取目标（scrape targets）列表。

- **消费方**：`infra/prometheus/prometheus.yml` 中的 `ml-backends` 抓取任务。新推理后端在超级管理员「模型市场 → 注册管理」中注册后即自动纳入监控，无需手改 Prometheus 配置。
- **过滤规则**：所有 `state != "disconnected"` 的全局注册实例均进入抓取列表。这意味着处于 `state="error"`（健康探活失败）的实例仍保留在列表中，从而触发 `MLBackendDown` 告警——这是有意设计的监控行为。运维带外停机维护时，请先将对应实例置为 `disconnected`，否则 5 分钟后会触发告警。
- **去重规则**：按解析出的 `host:port` 去重（按 `MLBackendRegistry.id` 升序首条胜出）。
- **代码真源**：`apps/api/app/api/v1/internal.py`

### 响应 Schema

返回 Prometheus `http_sd` 原生格式的 JSON 数组：

```json
[
  {
    "targets": ["grounded-sam2-backend:8080"],
    "labels": {
      "service": "grounded-sam2",
      "backend_id": "2c8a4b10-91e4-4d2a-8b15-3a91c08d4211"
    }
  }
]
```

| 字段                | 类型   | 含义说明                                                                                                                                                           |
| ------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `targets[0]`        | string | `host:port`，由 `ml_backend_registry.url` 剥离 scheme 与路径得到（缺省端口按 http=80 / https=443 补齐）                                                            |
| `labels.service`    | string | `ml_backend_registry.name`。若存在同名实例，可在 Prometheus 内结合自动注入的 `instance`（`host:port`）或 `backend_id` 唯一定位（见 `infra/prometheus/alerts.yml`） |
| `labels.backend_id` | string | `ml_backend_registry.id`（UUID 字符串）                                                                                                                            |

### 鉴权机制（`METRICS_SD_TOKEN`）

可选全局 Bearer Token：当环境变量 `METRICS_SD_TOKEN` 非空时，访问该端点必须携带 `Authorization: Bearer <METRICS_SD_TOKEN>`（采用 `hmac.compare_digest` 常量时间比较，缺失或不匹配返回 `401`）；未配置或为空字符串时免鉴权（仍受反向代理网段隔离保护）。

```bash
# 默认免鉴权（容器内网直连）
curl http://anno-api:8000/api/v1/internal/metrics-targets

# 启用了 METRICS_SD_TOKEN 时
curl -H "Authorization: Bearer $METRICS_SD_TOKEN" \
  http://anno-api:8000/api/v1/internal/metrics-targets
```

---

## 2. 训练执行器内部端点（`/api/v1/internal/training/*`）

独立训练执行器通过这些端点拉取实验并上报结果。路由位于 `apps/api/app/api/v1/training_runs.py`、`training_previews.py` 和 `training_observability.py`，请求模型位于相应 `app/schemas/` 模块；训练配置共享契约位于 `apps/_shared/training_contract/`。

### 鉴权与租约防护

- **请求头**：`X-Training-Executor-Token: <token>`
- **凭据来源**：API 配置 `TRAINING_EXECUTOR_TOKEN`，执行器通过独立私有文件读取相同值；Compose 挂载路径为 `/run/secrets/training_executor_token`。至少需要 32 个字符。训练未启用、API 凭据太短、请求缺失或错配均返回 `403`。这份凭据与 Backend 访问 Token 分别管理，不能互换。
- **围栏校验（Lease Fencing）**：所有上报接口均要求同时匹配当前执行器标识（`executor_id`）、进程启动标识（`boot_id`）、有效 `TrainingResourceLease` 与当前活跃 `TrainingAttempt`。已取消、已超时或被新 Attempt 取代的旧执行进程无法覆盖平台状态。

### 训练实验生命周期端点

| 方法   | 路径                                                           | 请求/响应要点与职责说明                                                                                                                                 |
| ------ | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST` | `/api/v1/internal/training/claim`                              | 上报 `executor_id`、`boot_id`、`claim_id`、`recover_only`、`resource_id` 和 `runtime`；平台核对目录与执行环境后认领实验，返回尝试、租约及冻结配置       |
| `POST` | `/api/v1/internal/training/attempts/{attempt_id}/heartbeat`    | 周期性上报执行器心跳与当前阶段（如 `preparing`、`training`、`evaluating`、`uploading`），延长租约有效期并返回平台侧是否已发起取消（`cancel_requested`） |
| `POST` | `/api/v1/internal/training/attempts/{attempt_id}/observations` | 批量上报 epoch 级训练指标、系统遥测采样（CPU/GPU 利用率、显存、吞吐）与经脱敏的日志块（`seq` 幂等游标推进）                                             |
| `POST` | `/api/v1/internal/training/attempts/{attempt_id}/uploads`      | 申请产物上传的短期对象存储预签名 PUT URL（涵盖最优权重 `best_weights`、末次权重 `last_weights`、混淆矩阵、PR 曲线与验证集预测清单）                     |
| `POST` | `/api/v1/internal/training/attempts/{attempt_id}/finish`       | 提交最终结果与不可变产物清单；平台核验产物和摘要后记录 `succeeded` / `failed` / `cancelled`，候选模型仍由负责人选择成功尝试后另行注册                   |
| `POST` | `/api/v1/internal/training/attempts/{attempt_id}/recover`      | 执行器进程重启后核对原租约分配，尝试恢复或安全收尾未完成的 Attempt                                                                                      |

### 数据增强预览端点

| 方法   | 路径                                                        | 请求/响应要点与职责说明                                                            |
| ------ | ----------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `POST` | `/api/v1/internal/training/previews/claim`                  | 认领排队中的原生数据增强预览任务（`TrainingAugmentationPreview`），独占节点 CPU 槽 |
| `POST` | `/api/v1/internal/training/previews/{preview_id}/heartbeat` | 增强预览任务心跳续租                                                               |
| `POST` | `/api/v1/internal/training/previews/{preview_id}/uploads`   | 申请预览生成图片（PNG）与变换元数据 JSON 的短期预签名上传 URL                      |
| `POST` | `/api/v1/internal/training/previews/{preview_id}/finish`    | 提交预览完成收据与生成的 PNG SHA-256 校验摘要，供前端配方工作台实时展示            |

---

## 关联文档

- [在线学习与训练体系](/dev/concepts/training-datasets) — 训练数据版本、执行器拉取式协议与自训模型回流闭环
- [MinIO 存储桶布局](/dev/reference/storage-buckets) — `training` 桶的对象前缀结构与备份策略
- [可观测性总览](/ops/observability/) — Prometheus / Grafana 接入指南
- [环境变量参考](/dev/reference/env-vars) — `METRICS_SD_TOKEN` 与 `TRAINING_EXECUTOR_TOKEN` 配置说明
- [Runbook · ML Backend 不可用](/ops/runbooks/ml-backend-down)
