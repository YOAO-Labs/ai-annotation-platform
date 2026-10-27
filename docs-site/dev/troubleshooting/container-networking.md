---
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-19
---

# 容器网络与 loopback 限制

## 症状

注册 ML Backend 时填了 `http://localhost:9090` 或 `http://127.0.0.1:9090`，前端报 422 校验失败：

```
URL must not point to loopback (localhost / 127.0.0.1 / 0.0.0.0 / ::1).
Use the Docker bridge IP (172.17.0.1) or service DNS (e.g. grounded-sam2-backend) instead.
```

或：早期未拦截时注册成功，但 Celery worker 真正调用时连不上，日志显示 `Connection refused`。

## 根因

`localhost` 在容器内指向**容器自身**，不是宿主机。当 ML Backend 跑在宿主机或另一个 compose service 上时：

- 容器 A 的 `localhost:9090` ≠ 宿主机 `localhost:9090`
- 跨容器调用必须通过 Docker 网络（service DNS 或 bridge IP）

后端在 `MLBackendCreate.url` / `MLBackendUpdate.url` 上通过 `field_validator` 直接拒绝 loopback，避免后期才在 worker 调用时抛出 `ConnectionRefused`。

## 修复 / 规避

| 场景                                     | 应填 URL                                                                                      |
| ---------------------------------------- | --------------------------------------------------------------------------------------------- |
| ML Backend 跑在**同一个 docker-compose** | `http://<service-name>:<port>`（例 `http://grounded-sam2-backend:8001`）                      |
| ML Backend 跑在**宿主机本地**进程        | `http://172.17.0.1:<port>`（Linux 默认 bridge 网关；macOS/Windows 用 `host.docker.internal`） |
| ML Backend 跑在**另一台机器**            | 该机器的 LAN IP / 公网域名                                                                    |

**自检命令：**

```bash
# 从 worker 容器内 ping 目标 URL
docker exec ai-annotation-platform-celery-worker-1 \
  curl -sf http://172.17.0.1:8001/health
```

## 相关存储 host 配置

SAM 工作流里 worker 把 task 文件路径转换成 presigned URL 给 ML Backend 拉取。内部对象存储 URL 默认使用 `MINIO_ENDPOINT`，但 ML Backend 在另一个网络命名空间访问该 URL 时也面临同样的 loopback 问题，因此支持配置：

```bash
ML_BACKEND_STORAGE_HOST=172.17.0.1:9000
```

worker 在签名前使用这个 Host 与内部端点的协议生成模型访问 URL，不替换已签名链接。留空时模型直接访问内部端点；不能把浏览器同源 `/minio` 填在这里。配置在 `.env`，参考 `.env.example` 及[部署签名契约](/ops/deploy/docker-compose#对象存储访问与签名契约)。

## 相关

- commit: `d41236b` URL validator 拒绝 loopback
- commit: `c5eaf94` `ML_BACKEND_STORAGE_HOST` 引入
- 代码：`apps/api/app/schemas/ml_backend.py`（`field_validator`）
