---
title: Runbook：模型服务不可用
audience: [ops]
type: how-to
status: stable
last_reviewed: 2026-07-11
---

# Runbook：模型服务不可用

## 症状

- 超级管理员在 **模型市场 → 注册管理** 看到实例状态异常
- AI 预标注 Job 失败，错误信息包含 `ConnectionError` 或 `502`
- 工作台 SAM 工具无响应

## 快速诊断

```bash
# 1. 检查模型服务容器（以已部署的 SAM 为例）
docker ps | grep sam

# 2. 手动 health check
curl -f http://localhost:8001/health || echo "Backend 不可达"

# 3. 查看日志
docker logs ai-annotation-platform-grounded-sam2-backend-1 --tail 100
```

## 处理步骤

### 情况 A：容器已退出（OOM 或异常）

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml \
  -f docker-compose.ml-gsam2.yml --profile gpu up -d grounded-sam2-backend

# 观察启动日志（GPU 加载通常需要 30–60 秒）
docker logs -f ai-annotation-platform-grounded-sam2-backend-1
```

如果反复因 OOM 退出：

- 检查 GPU 显存（`nvidia-smi`）
- 换用更小的 SAM 变体（`SAM_VARIANT=tiny`）

### 情况 B：容器运行但 health check 失败

```bash
# 检查端口映射
docker port ai-annotation-platform-grounded-sam2-backend-1

# 查看详细错误
curl -v http://localhost:8001/health
docker logs ai-annotation-platform-grounded-sam2-backend-1 --tail 200
```

常见原因：模型权重下载未完成 / CUDA 初始化失败。

### 情况 C：外部自部署 Backend 不可达

1. 确认 Backend 服务在目标机器上正常运行
2. 检查网络/防火墙：运行平台 API 的进程是否能访问 Backend URL。
3. 开发态 API 跑在宿主机，直接从宿主机测试：
   ```bash
   curl -fsS http://<backend-host>:<port>/health
   ```
   生产叠加 compose 时，使用 API 容器：
   ```bash
   docker compose --env-file .env.production \
     -f docker-compose.yml -f docker-compose.prod.yml exec api \
     curl -fsS http://<backend-host>:<port>/health
   ```
4. 参考 [容器网络排查](/dev/troubleshooting/container-networking)

### 情况 D：协议版本不兼容

```bash
curl http://localhost:8001/health
# 检查返回的 protocol_version 字段是否与平台要求一致
```

对照 [ML Backend 协议](/dev/reference/ml-backend-protocol) 检查 Backend 实现。

### 情况 E：GPU 资源配置或影子决策异常

1. 在模型市场「注册管理」查看 backend 的 `gpu_resource_id`、显存预算和配置诊断。
2. 确认 `gpu_resource_id` 精确匹配 `GPU_ARBITER_RESOURCES_JSON` 的 key；不同主机的
   `index:0` 必须使用不同 resource domain。
3. 只信任 `connected` 且 3 分钟内的 health。`compute=cpu` 不等于显存已空；
   还要检查 `residency.gpu_loaded`、builder 与 borrower。
4. 设置 `GPU_ARBITER_MODE=observe` 时，检索
   `gpu_arbiter_shadow_decision`。`would-reject` 只是非权威预演，不会阻断当前请求；
   若业务已失败，仍需继续排查 backend OOM、网络或权重加载错误。
5. 检查 `/api/v1/admin/ml-integrations/gpu-resources` 的 `rollout` 与 `runtime`。只有持久
   `state=enforcing`、`effective_mode=enforce` 且 `runtime.ready=true` 时才已就绪。
   `gpu_rollout_not_ready` 需先修复其 `blocked_reason`；`gpu_rollout_active_while_disabled`
   表示在持久过渡尚未 demotion 时错误关闭了 release latch，不要清表或清 Redis。

## 维护窗口 / 避免告警

`MLBackendDown` 由 Prometheus `up{job="ml-backends"} == 0` 持续 5m 触发，target 由 http_sd 从 `ml_backends` 表生成，**仅** `state="disconnected"` 的 backend 被排除。换言之 `state="error"`（health 探活失败）仍在 target 列表中，会按设计触发告警。

停机更新模型或更换权重前，先在 **模型市场 → 注册管理** 断开该实例；否则健康检查连续失败五分钟后会触发 `MLBackendDown`。

## 影响范围评估

| 受影响功能         | 影响级别                                    |
| ------------------ | ------------------------------------------- |
| SAM 工具（工作台） | 功能降级，标注员手动框选仍可用              |
| AI 预标注（批量）  | 新 Job 会失败；已完成的 Prediction 不受影响 |
| 已有 Annotation    | 无影响                                      |

## 相关文档

- [AI 预标注流水线](/user-guide/workflows/ai-preannotate-pipeline)
- [失败预测恢复流程](/user-guide/workflows/failed-prediction-recovery)
- [AI 模型集成](/dev/concepts/ai-models)
- [ML Backend 协议](/dev/reference/ml-backend-protocol)
- [ML Backend API](/api/guides/ml-backend)
- [Runbook: Celery Worker 卡死](/ops/runbooks/celery-worker-stuck)
