---
audience: [dev, ops]
type: explanation
since: v0.15.17
status: stable
last_reviewed: 2026-10-19
---

# 部署总览

> 这一页是部署文档的分流入口：按你要做的事选对子文档，不重复内容。

## 先选形态

| 你要做的                                   | 去哪                                                   |
| ------------------------------------------ | ------------------------------------------------------ |
| 在本机跑起来做开发 / 联调                  | [开发部署（本地）](/ops/deploy/development)            |
| 搬到 staging / 生产                        | [生产部署](/ops/deploy/docker-compose)                 |
| 复用现有基础设施，另建局域网生产数据和账号 | [局域网生产部署](/ops/deploy/lan-production)           |
| 把平台 / API 暴露给团队或远程 SDK 调用方   | [端口暴露与网络安全](/ops/deploy/network-security)     |
| 升级版本 / 跑迁移 / rebuild 还是 restart   | [升级指南](/ops/upgrade-guide)                         |
| 配置超管页面的一键重建 ML 服务             | [本机 ML 服务维护](/ops/deploy/ml-backend-maintenance) |

## 开发态 vs 生产态：一句话区别

`docker-compose.yml` 只定义**基础设施 + Celery**（api/web 不在内）。两态差异：

- **开发态**：API / Web 跑宿主机进程（`uvicorn --reload` + `vite`），源码热更新，端口绑本机，宽松 CORS / 测试后门开着。
- **生产态**：叠加 `docker-compose.prod.yml` 把 api/web 进容器，外层反代终结 TLS，`ENVIRONMENT=production` 收紧所有安全断言。

差异背后的代码行为（`ENVIRONMENT` 如何改变断言、谁进容器、profile）详见[运行环境形态](/dev/concepts/runtime-environments)；物理机器怎么摆（单机 / 分离 GPU）见[部署拓扑](/dev/concepts/deployment-topology)。

## 严谨性递进

开发态图省事、生产态收紧——核心红线就一条：**生产对公网只暴露反代（HTTPS），后端端口一律内网**。这条容易踩坑（Docker 会绕过 ufw），单独成篇见[端口暴露与网络安全](/ops/deploy/network-security)。

## 只读站点 Agent

[独立 Agent 组件与当前接入范围](./site-agents.md)说明出站 HTTPS、受控根和本地恢复边界；中心配对与数据集接入尚未开放。
