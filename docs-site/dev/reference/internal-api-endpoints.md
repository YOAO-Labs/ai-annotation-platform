---
audience: [dev, ops]
type: reference
since: v0.11.19
status: stable
last_reviewed: 2026-05-28
---

# 内部 API 端点

`/api/v1/internal/*` 是只给内网消费方 (Prometheus / 内部脚本) 调用的端点, **不进 OpenAPI 公开 schema** (`include_in_schema=False`), 同时在 `infra/docker/nginx.conf` 显式 `deny all + return 404`, 与根路径 `/metrics` 一样靠"反向代理不转发"做网段隔离。直连 api 容器 (例如同 docker network 内的 prometheus) 才能访问。

可选的全局 bearer token: env `METRICS_SD_TOKEN` 非空时, 所有 `/internal/*` 端点强制校验 `Authorization: Bearer <env>` (常量时间比较, 不匹配返回 401); 为空则免鉴权。

## GET /api/v1/internal/metrics-targets

Prometheus [`http_sd_config`](https://prometheus.io/docs/prometheus/latest/http_sd/) 期望的 JSON 服务发现端点, 从 `ml_backends` 表动态生成 ML backend 的 scrape target 列表。

**消费方**: `infra/prometheus/prometheus.yml` 的 `ml-backends` job。新 backend 在超管「模型市场」注册即被自动纳入抓取, 无需手改 prometheus.yml。

**过滤规则**: `state != "disconnected"` 的 backend 进入 target 列表。这意味着 `state="error"` (health 探活失败) 仍在列, 会被 `MLBackendDown` 告警捕获 —— 这是设计意图。运维带外维护时请先把 backend 置 `disconnected`, 否则 5m 后触发告警。

**去重**: `ml_backends` 是 project-scoped, 同一物理 backend 在不同 project 下可能多条记录, 按 `host:port` 去重 (首条胜出)。

**实现**: [apps/api/app/api/v1/internal.py](https://github.com/yyq19990828/ai-annotation-platform/blob/main/apps/api/app/api/v1/internal.py)

### 响应 schema

返回 JSON 数组, 每项是 Prometheus http_sd 原生格式:

```json
[
  {
    "targets": ["grounded-sam2-backend:8080"],
    "labels": {
      "service": "grounded-sam2",
      "backend_id": "1",
      "project_id": "1"
    }
  }
]
```

| 字段                | 类型   | 含义                                                                                                                                                                                                                            |
| ------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `targets[0]`        | string | `host:port`, 由 `ml_backends.url` 剥出 scheme / path 得到                                                                                                                                                                       |
| `labels.service`    | string | `ml_backends.name`。**未加 unique 约束**, 同名 backend 会撞 label; 在 Prometheus 内靠 http_sd 自动注入的 `instance` (= `host:port`) 二次定位 (见 `infra/prometheus/alerts.yml` / `infra/grafana/dashboards/ml-backends.json`)。 |
| `labels.backend_id` | string | `ml_backends.id` (字符串化)                                                                                                                                                                                                     |
| `labels.project_id` | string | `ml_backends.project_id` (字符串化)                                                                                                                                                                                             |

### 鉴权

| 场景                                                              | 行为                        |
| ----------------------------------------------------------------- | --------------------------- |
| `METRICS_SD_TOKEN=""` (默认)                                      | 免鉴权, 仍受 nginx 隔离保护 |
| `METRICS_SD_TOKEN=<token>` + 正确 `Authorization: Bearer <token>` | 200                         |
| `METRICS_SD_TOKEN=<token>` + 缺失/错配 `Authorization`            | 401                         |

生产环境若 internal 端点会被运维网段以外的容器访问, 强烈建议显式设置 `METRICS_SD_TOKEN` 作为第二道闸。

### 示例 curl

```bash
# 默认免鉴权
curl http://anno-api:8000/api/v1/internal/metrics-targets

# 配了 token
curl -H "Authorization: Bearer $METRICS_SD_TOKEN" \
  http://anno-api:8000/api/v1/internal/metrics-targets
```

## 训练执行器内部端点 (/api/v1/internal/training/\*)

独立训练执行器（`apps/training-executor`）与平台 API 之间的拉取式通信协议端点，不包含在 OpenAPI 公开 schema 中，由 Nginx/Caddy 网络隔离并在应用层通过共享密钥严格鉴权。

### 鉴权机制

- 请求头：`X-Training-Executor-Token: <token>`
- Token 来源：环境变量 `TRAINING_EXECUTOR_TOKEN`（生产环境通过 Docker Secret `/run/secrets/training_executor_token` 挂载，至少 32 字符的随机字符串）。
- API 启动时读取同一 token，使用常量时间比较验证请求头；未配置 token 或验证失败返回 401。

### 端点列表

| 方法   | 路径                                                           | 行为与用途                                                                                            |
| ------ | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `POST` | `/api/v1/internal/training/claim`                              | 执行器根据自身节点与 GPU UUID 认领排队中的训练实验，建立 `TrainingResourceLease` 与 `TrainingAttempt` |
| `POST` | `/api/v1/internal/training/attempts/{attempt_id}/heartbeat`    | 周期性上报执行器心跳，延长租约有效期                                                                  |
| `POST` | `/api/v1/internal/training/attempts/{attempt_id}/uploads`      | 请求产物上传的短期对象存储预签名 PUT URL（训练包、检查点、权重、日志与评估结果）                      |
| `POST` | `/api/v1/internal/training/attempts/{attempt_id}/finish`       | 提交不可变产物清单与最终执行结果；平台验证文件大小、SHA-256 与评估报告无误后转为终态                  |
| `POST` | `/api/v1/internal/training/attempts/{attempt_id}/recover`      | 执行器重启后核对原租约分配并尝试恢复未完成的 Attempt                                                  |
| `POST` | `/api/v1/internal/training/attempts/{attempt_id}/observations` | 批量上报训练遥测采样（CPU/GPU/显存/吞吐）与脱敏日志块，幂等游标推进                                   |
| `POST` | `/api/v1/internal/training/previews/claim`                     | 认领原生数据增强预览任务，独占节点 CPU 槽                                                             |
| `POST` | `/api/v1/internal/training/previews/{preview_id}/heartbeat`    | 增强预览心跳上报                                                                                      |
| `POST` | `/api/v1/internal/training/previews/{preview_id}/uploads`      | 获取预览图片与元数据的短期上传 URL                                                                    |
| `POST` | `/api/v1/internal/training/previews/{preview_id}/finish`       | 提交预览完成收据与生成的 PNG 校验摘要                                                                 |

### 隔离与安全性

- **网段与反代**：Nginx / Caddy 默认拦截对 `/api/v1/internal/*` 的外部公网访问；训练执行器容器仅能在内部容器网络通过 `TRAINING_API_URL` 访问。
- **租约与防护**：所有回报接口均要求同时匹配当前执行器标识、boot ID、有效 GPU 租约与当前 Attempt。已取消或已退出的旧执行 Attempt 无法覆盖新执行的状态。

## 关联文档

- [可观测性总览](/ops/observability/) — Prometheus / Grafana 接入指南
- [环境变量参考 · METRICS_SD_TOKEN](/dev/reference/env-vars#服务发现鉴权)
- [环境变量参考 · TRAINING_EXECUTOR_TOKEN](/dev/reference/env-vars)
- [在线学习与训练体系](/dev/concepts/training-datasets) — 训练数据版本、执行器与模型闭环
- [Runbook · ML Backend 不可用](/ops/runbooks/ml-backend-down)
