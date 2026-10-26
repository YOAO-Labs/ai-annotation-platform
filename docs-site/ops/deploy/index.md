---
title: 部署总览
audience: [dev, ops]
type: explanation
since: v0.15.17
status: stable
last_reviewed: 2026-10-26
---

# 部署总览

> 这一页是部署文档的分流入口：按你要做的事选对子文档，不重复内容。

## 先选形态

| 你要做的                                   | 去哪                                                          |
| ------------------------------------------ | ------------------------------------------------------------- |
| 在本机跑起来做开发 / 联调                  | [开发部署（本地）](/ops/deploy/development)                   |
| 搬到 staging / 生产                        | [生产部署](/ops/deploy/docker-compose)                        |
| 复用现有基础设施，另建局域网生产数据和账号 | [局域网生产部署](/ops/deploy/lan-production)                  |
| 把平台 / API 暴露给团队或远程 SDK 调用方   | [端口暴露与网络安全](/ops/deploy/network-security)            |
| 升级版本 / 跑迁移 / rebuild 还是 restart   | [升级指南](/ops/upgrade-guide)                                |
| 配置超管页面的一键重建 ML 服务             | [本机 ML 服务维护](/ops/deploy/ml-backend-maintenance)        |
| 对象存储迁移：清单、复制、封存、切换与退役 | [对象存储迁移与切换](/ops/deploy/storage-migration-inventory) |
| 验证一致备份、九用途恢复映射与对象归属顺序 | [一致备份与隔离恢复演练](/ops/deploy/storage-recovery)        |
| 隔离验证候选对象存储构建与真实签名兼容性   | [对象存储候选本地验证](/ops/deploy/storage-candidate-poc)     |
| 接入无法入站的内网数据服务器只读目录       | [只读站点 Agent](/ops/deploy/site-agents)                     |

## 开发态 vs 生产态：一句话区别

`docker-compose.yml` 只定义**基础设施 + Celery**（api/web 不在内）。两态差异：

- **开发态**：API / Web 跑宿主机进程（`uvicorn --reload` + `vite`），源码热更新，端口绑本机，宽松 CORS / 测试后门开着。
- **生产态**：叠加 `docker-compose.prod.yml` 把 api/web 进容器，外层反代终结 TLS，`ENVIRONMENT=production` 收紧所有安全断言。

差异背后的代码行为（`ENVIRONMENT` 如何改变断言、谁进容器、profile）详见[运行环境形态](/dev/concepts/runtime-environments)；物理机器怎么摆（单机 / 分离 GPU）见[部署拓扑](/dev/concepts/deployment-topology)。

## 严谨性递进

开发态图省事、生产态收紧——核心红线就一条：**生产对公网只暴露反代（HTTPS），后端端口一律内网**。这条容易踩坑（Docker 会绕过 ufw），单独成篇见[端口暴露与网络安全](/ops/deploy/network-security)。

## 只读站点 Agent

[只读站点 Agent](./site-agents.md)已接通中心配对与出站 HTTPS 传输链路：超级管理员可登记配对后的受控只读根，支持目录浏览、来源观测扫描、预检与确认原地登记、完整核验原图快照、单区间 Range 读取以及同内容引用样本的显式备用来源关联。
