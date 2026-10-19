---
title: Mask 字节接口
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# Mask 字节接口

`apps/assets-mask-content` 是内部字节服务，协议为 `aap-assets-mask-content/v1`。公开的任务、标注、预测和追踪 Mask 内容端点继续使用现有身份与权限检查，通过本协议读写对象。浏览器仍获取原有 JSON 与 SHA ETag。

## 角色与操作

每个进程只有一个角色、一份 bearer token 和一个受限 S3 身份。请求使用 `Authorization: Bearer <token>`，操作均为 `POST /v1/mask-content/{operation}`。

| 操作     | 角色    | 输入与结果                                               |
| -------- | ------- | -------------------------------------------------------- |
| `put`    | content | 规范 RLE 与 `storage_encoding` 偏好；返回内容地址引用    |
| `get`    | content | 已有 Mask 引用；返回同一引用和验证后的规范 RLE           |
| `list`   | cleanup | 不透明游标和至多 1000 的页大小；返回对象事实及后续游标   |
| `delete` | cleanup | 精确内容键、观测到的 ETag 与修改时间；返回当前键是否删除 |

成功响应为 `{ "schema": "aap-assets-mask-content/v1", "ok": true, "data": ... }`；错误为同一 schema、`ok: false` 与封闭的 `error.code`。HTTP 与 JSON 错误都经过字节预算和响应范围校验。服务错误不包含对象存储凭据、URL 或供应商异常正文。

普通内容进程不能列举或删除，清理进程不能写入 Mask 内容；它仅可在独立日志前缀写入操作与回执。两个进程均不接收用户、Task、Annotation、QC 或 repair 的 SQL 连接，也不判定业务引用是否存活。配置与部署范围见[独立部署台账](/dev/concepts/module-deployment)。

## 内容与兼容

规范内容仍为 `{encoding: "coco_rle", size: [height,width], counts: [...]}` 的紧凑 JSON。引用继续包含 `encoding`、`size`、`object_key`、`sha256`、`runs` 和 `bytes`，gzip 引用另含 `storage_encoding: "gzip"`；旧 `coco_rle_gzip` 引用仍可读取。

对象键固定为 `raster-masks/sha256/{sha前两位}/{接下两位}/{sha}.json[.gz]`。SHA 与 `bytes` 均描述未压缩规范 JSON。读取前先验证键与引用；读取后复核摘要、长度、尺寸与 runs。重复写入使用条件创建；已有对象必须验证一致，损坏对象不会被静默覆盖。

规范对象上限 4 MiB，压缩对象上限 8 MiB，gzip 展开倍率上限 20，最多 100 万 runs；gzip 超过可读倍率时写入 identity 对象。请求与响应预算为 4 MiB 加 16 KiB envelope 空间。内容结构的绝对限制保留；具体 Task 的尺寸、帧号、前景与图像写入开关由 Labeling 检查。

每个角色进程同时接收一个内容操作，在读取和解析正文前限制并发；忙时返回 503，由调用方按业务规则重试。请求取消后，正在执行的存储操作结束前仍占用该名额。扩大吞吐量需要增加副本并验证容量。

## 引用、回收与失败

上传预约、每任务配额、标注和预测引用、候选、审核、QC、修复及回退窗口继续属于 Labeling。字节服务检查观测到的对象身份，条件不符时拒绝删除；它不持有业务回收授权。

区域审核范围在关联 QC 问题删除后仍保留其区域掩码，直到范围本身退役。跟踪输入的来源标注、提示与修正掩码按原生输入保留规则继续保护，即使当前标注或提示已变化；保留查询异常时继续保留内容。

业务回收仍在本地装配执行。远程 cleanup 只接入 dry-run 清单查询；配置远程地址时，执行删除的业务回收明确失败。HTTP 超时后删除可能继续执行，释放请求内 SQL 锁不足以阻止迟到的删除影响同内容重传。远程删除接入依赖 Labeling 的持久每键删除栅栏与结果恢复协议。

成功重传相同内容会续期该任务的上传预约，即使不可变对象的修改时间很旧；已关联的预约保留关联状态与配额语义。本地回收先保护宽限期内的预约，再移除旧预约并提交，重新加锁复查预约与当前引用后才删除。字节适配器通过 HEAD 核对 ETag 与修改时间，再以 `If-Match` 条件删除当前键；时间按 HEAD 的整秒精度比较。

`delete` 删除当前逻辑键；版本化桶中历史对象版本的物理回收仍依赖桶生命周期或独立清理责任。普通调用者不能通过本协议删除历史版本。

显式远程配置失败时保持失败，不切回本地 S3。调用方可按既有业务规则重试；重复 put 校验同一内容，重复 delete 对已不存在的键返回未删除。公开上传在写入失败时回滚预约与配额，不把未提交预约视为成功上传。

这条协议提供 Mask 字节边界；完整 Labeling 业务写入、预约与 GC 协调运行时仍需各自完成模块协议。

## 持久回收协议

隔离的回收实验使用 `aap-assets-mask-gc/v1`，提供 cleanup 专用的 `POST /v1/mask-gc/scan`、`execute` 和 `status`。每页最多 1000 项，消息至多 1 MiB；游标是最后访问的物理键，包含被跳过的旧格式文件名。删除请求固定操作 UUID、内容键、ETag 和观测修改时间。

`raster-mask-gc/v1/<operation_id>/request.json` 通过条件创建声明唯一执行权；相同操作只读取回执，不再次删除。`receipt.json` 保存明确的 `deleted`、`missing` 或 `content_changed` 终态。两类日志不可变且不能独立过期。SDK 删除只尝试一次；丢失回包、崩溃、回执保存失败都保留待定状态，不能根据后续 HEAD 不存在而补造成功回执。

Labeling 的 `aap-labeling-mask-gc/v1` 以原生每键代次、删除意图与完整引用根管理业务授权。内容预检先读取代次，最终写入检查原代次；已开始删除、待定或代次变化会返回可重试的 409。只有匹配当前执行凭据的明确终态回执可以解除围栏，代次本身继续保留。独立协调运行时不持有 S3 凭据。

该协议已有本地数据库与真实对象存储的故障验收。现有 Celery 回收器尚未切换，远程业务删除仍受保护；新运行时镜像、普通批处理恢复和部署容量是后续验收项。
