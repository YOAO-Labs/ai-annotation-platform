---
title: Notifications 运行时
audience: [dev, ops]
type: reference
status: experimental
last_reviewed: 2026-10-25
---

# Notifications 运行时

`apps/notifications` 提供语言无关合同、固定 PostgreSQL 数据能力、即时发布、持久
publisher 与 WebSocket owner。现有 `/notifications` REST 和 `/ws/notifications`
公开地址继续由 API 提供。API 保留认证、公开响应和调用方事务的提交／回滚；
Notifications 数据能力不自行提交业务事务。

## 数据与调用边界

偏好、普通追加、终态追加、已读、删除和收件箱通过 `aap_notifications_v1` 的固定
函数执行；按 ID 读取和投递授权也使用固定能力。业务数据调用使用十五项运行函数，
兼容持久 relay 另使用既有三项投递函数，不直接访问通知私表或 `notification_facts`。
owner 使用 Identity 账号事实、Workspace
项目权限和 Jobs 通知范围的公开能力，不读取其他模块私表。

JSONB payload 保留对象、数组、标量与 null。API 和 Python SDK 不将其强制转换为
字典。收件箱筛选与投递授权各自保留原有 payload 解析规则。

普通通知继续采用尽力发布；讨论通知在调用方提交后发布，普通通知不会自动进入
持久 outbox。Identity 注销通知沿用同事务 outbox：先提交领取租约，再发布 Redis，
成功后确认；进程丢失或确认失败留下租约供重投，通知 ID 保持稳定，允许重复送达。

## 进程与配置

| 进程          | 入口／部署文件                                                              | 凭据与用途                                                                                                                                                                    |
| ------------- | --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Publisher     | `aap-notifications-publisher`；`docker-compose.notifications-publisher.yml` | `NOTIFICATION_PUBLISH_DATABASE_URL(_FILE)` 与发布专用 `NOTIFICATION_PUBLISH_REDIS_URL(_FILE)`                                                                                 |
| 即时发布 HTTP | 同一 publisher；私网端口 8181                                               | 设置 `NOTIFICATION_PUBLISH_API_TOKEN(_FILE)` 启用；不设置时保持 durable-only                                                                                                  |
| WebSocket     | `aap-notifications-socket`；`docker-compose.notifications.yml`              | `NOTIFICATION_SOCKET_DATABASE_URL(_FILE)`、订阅专用 `NOTIFICATION_SOCKET_REDIS_URL(_FILE)`、`NOTIFICATION_SOCKET_IDENTITY_URL` 和 `NOTIFICATION_SOCKET_IDENTITY_TOKEN(_FILE)` |
| API 绑定      | 原公开 REST／WebSocket                                                      | `NOTIFICATION_PUBLICATION_URL`、`NOTIFICATION_PUBLICATION_TOKEN(_FILE)`、`NOTIFICATION_SOCKET_URL`                                                                            |

直接启动时，每对 secret 值／文件恰好选择一项。Compose 使用文件，两个 Notifications
overlay 均不发布宿主机端口，应与 API 和 Identity 加入同一选定私网。组合启动时
显式选择 `identity-admission notifications-socket notifications-publisher`，替换前先停止
兼容 `identity-notification-publisher`，不对整个组合无差别执行 `up`。独立镜像使用
锁定依赖和非 root 用户；`/health/live` 与 `/health/ready` 提供存活和就绪检查。
API 的 URL 留空时使用内嵌 owner；显式远程绑定故障保留原错误处理，不回退本地传输。

socket 通过 Identity 的准入与 `/v1/identity/actor-status` 核验当前账号，并逐帧重新
认证、逐消息授权。Identity 另持有 actor-status 专用只读 LOGIN，配置为
`IDENTITY_ACTOR_STATUS_DATABASE_URL(_FILE)`；该身份只执行既有
`aap_identity_v1.read_actor_facts(uuid)`。专用 `IDENTITY_ADMISSION_TOKEN(_FILE)`
只能调用准入和状态读取，不能调用其他账户管理能力。Identity Compose 对应宿主
文件为 `IDENTITY_RUNTIME_ACTOR_STATUS_DATABASE_URL_FILE` 和
`IDENTITY_RUNTIME_ADMISSION_TOKEN_FILE`，socket 不持有一般管理 bearer 或签名密钥。

## 迁移与权限采用

从现有 `0306` 升级须保留到 `0325` 的完整线性历史，其中含 Workspace、Audit 和视频
输入保留迁移。`0313` 改变资产垃圾领取围栏，应与最新功能合并及资产变更范围一起
评审；不能挑选、重编号或改写历史迁移。源码采用候选尚未应用到日常数据库。

迁移后，先按 `infra/docker/module-data-grants.sql` 安装既有 NOLOGIN owner 与公开
依赖，再通过普通 LAN／worktree 授权入口设置应用身份。两个入口撤销通知私表、
列和事实视图权限，开放十五项业务运行函数，并检查继承权限是否仍产生旁路；
LAN 兼容 relay 的既有三项投递授权保留，独立 publisher 使用自己的三函数 LOGIN。
owner 和迁移身份不作为 API／worker 的运行凭据。

独立 socket 的七项只读能力由
`infra/docker/notification-delivery-read-grants.sql` 授给专用 LOGIN；Identity 的状态
读取由 `infra/docker/identity-actor-status-grants.sql` 单独授权。publisher 继续使用
既有持久投递专用授权，不共享 socket 或 API 的凭据。各脚本头部列出所需角色参数，
运行前须核对目标数据库与角色。`notifications-read-grants.sql` 是历史事实视图授权，
当前函数专用配置不得在最终收权后再使用它恢复 API 对事实视图的访问。

主工作树源码采用、数据库迁移、独立运行验收和日常拓扑切换分别记录。模块实现范围
已冻结；已有本地发布／组合／恢复证据保留，当前候选按模块集中验收。完整替代语言
提供方与生产容量尚未验证，Go 保持独立实验。
