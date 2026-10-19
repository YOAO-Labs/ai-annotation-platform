---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# 故障排查 / 踩坑手册

收录在实际开发与 CI 中踩到的“非显然 BUG”，以**症状 → 根因 → 修复**的方式归档，便于快速对症。每篇均标注关联 commit / ADR 与代码位置，可顺藤往代码层定位。

> 写作约定：当你修复了一个非显然的问题，在本目录补充或更新排查指南，比只写在 commit message 中更易复用。

## 速查表

| 症状                                                               | 看哪篇                                                    |
| ------------------------------------------------------------------ | --------------------------------------------------------- |
| 改了 Celery task 代码不生效 / `TypeError` 出现新参数找不到         | [Docker rebuild vs restart](./docker-rebuild-vs-restart)  |
| ML Backend 注册时报 URL 不可达 / `localhost` 被拒                  | [容器网络与 loopback 限制](./container-networking)        |
| AI 预标注完成但前端工作台看不到候选框                              | [Prediction Schema 适配器陷阱](./schema-adapter-pitfalls) |
| 开发库出现 `@e2e.test` 用户或 E2E 项目                             | [E2E 测试数据污染与隔离](./dev-data-preservation)         |
| `useState(...)` 报 `Cannot access 'X' before initialization`       | [React useState TDZ 陷阱](./react-tdz-trap)               |
| 容器内 `parents[3]` 越界 / `.env` 路径报错                         | [环境变量与 config 路径](./env-and-config-paths)          |
| GitHub Actions `services:` 启动 MinIO 失败 / FastAPI lifespan 卡死 | [CI 服务依赖踩坑](./ci-flaky-services)                    |
| Raster Mask WebGPU 未命中 / adapter unavailable / 回退 CPU         | [Raster Mask WebGPU 故障排查](./raster-mask-webgpu)       |

## 写一篇新踩坑文档

```markdown
# <一句话标题，尽量复用症状关键词>

## 症状

<复现路径或日志片段>

## 复现

<最小复现步骤；能挂个测试就挂>

## 根因

<指向具体代码 / 库行为 / 平台限制>

## 修复 / 规避

<给出补丁或工作流变更，附验证命令>

## 相关

- commit: `<hash>` <subject>
- ADR: <若有>
- 代码：`path/to/file.py:line`
```
