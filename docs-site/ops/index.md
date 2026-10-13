---
pageClass: docs-hub-page
aside: false
audience: [ops]
type: explanation
status: stable
last_reviewed: 2026-07-12
---

# 部署与运维

面向将本平台部署到生产环境的工程师和 SRE。

## 主入口

先选择部署形态，再按升级、健康检查或故障症状进入对应手册：

<div class="doc-card-grid cols-3">
  <DocLinkCard title="部署总览" desc="比较开发、生产和局域网部署形态" href="/ops/deploy/" />
  <DocLinkCard title="开发部署" desc="让基础设施进容器，保留 API 和 Web 的热更新" href="/ops/deploy/development" />
  <DocLinkCard title="生产部署" desc="使用 Docker Compose、反向代理和备份恢复" href="/ops/deploy/docker-compose" />
  <DocLinkCard title="局域网生产部署" desc="复用基础设施并隔离业务数据库和存储" href="/ops/deploy/lan-production" />
</div>

## 部署

- [部署总览](/ops/deploy/) — 开发 / 生产分流入口，两态差异速览
- [开发部署（本地）](/ops/deploy/development) — 基础设施进容器、API/Web 跑宿主机热更新
- [生产部署](/ops/deploy/docker-compose) — Docker Compose 生产部署、环境变量、反向代理
- [局域网生产部署](/ops/deploy/lan-production) — 复用基础设施、隔离业务数据和首次信任证书
- [端口暴露与网络安全](/ops/deploy/network-security) — 端口该不该对外、Docker 绕 ufw、远程 SDK 安全访问

## 升级与健康检查

- [升级指南](/ops/upgrade-guide) — 镜像 rebuild / restart、数据库迁移与版本升级检查
- [生产部署：健康检查端点](/ops/deploy/docker-compose#_7-健康检查端点) — 验证 API、Worker、数据库和依赖服务

## 可观测性

- [监控与告警](/ops/observability/) — Prometheus / Grafana 集成、Celery 任务监控、性能 HUD

## 安全

- [安全模型](/ops/security/) — JWT 认证、CSP 策略、权限边界

## Runbooks（应急手册）

- [Celery Worker 卡死](/ops/runbooks/celery-worker-stuck) — worker 卡住、队列堆积、任务无法消费
- [GPU 显存仲裁验收](/ops/runbooks/gpu-arbitration-acceptance) — 验收 GPU 资源仲裁、驱逐和恢复分支
- [ML Backend 不可用](/ops/runbooks/ml-backend-down) — 模型服务不可达、健康检查失败或 GPU 加载异常
- [视频帧服务排障](/ops/runbooks/video-frame-service) — frame cache、chunk smart-copy、视频 tracker job
- [图片金字塔](/ops/runbooks/image-pyramid) — 大图派生、回填、告警和故障码处理
- [PG 连接池耗尽](/ops/runbooks/postgres-connection-pool-exhausted) — 连接池打满、请求排队或超时
- [员工项目角色迁移与回滚](/ops/runbooks/project-role-migration) — 0173/0174 协调切换、审计、回滚边界与已签发 URL 限制
