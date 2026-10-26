---
title: Mask 字节接口
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-20
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

业务回收在本地或独立 HTTP 服务中装配同一个持久协调器，字节操作统一使用下述 `scan/execute/status` 协议。原生意图先建立持久围栏并提交，再执行远程删除；HTTP 超时后围栏继续保留，避免迟到删除影响新引用。

成功重传相同内容会续期该任务的上传预约，即使不可变对象的修改时间很旧；已关联的预约保留关联状态与配额语义。原生 owner 同时检查对象年龄、近期生产／上传事实、当前引用根和活动删除意图。内容预检与最终连接使用同一代次，已失效或正在回收的内容返回可重试冲突。

`delete` 删除当前逻辑键；版本化桶中历史对象版本的物理回收仍依赖桶生命周期或独立清理责任。普通调用者不能通过本协议删除历史版本。
清理身份的规范 S3 策略对内容与回收日志前缀显式拒绝
`s3:DeleteObjectVersion`，不能仅依赖没有列出该动作；较宽父身份下仍须保留这条拒绝。
更新 `scripts/mask_content_s3_policy.py` 生成的策略后，需同步更新实际 IAM 身份。
普通当前键删除仍可执行，保留版本不得随该身份被物理删除。

显式远程配置失败时保持失败，不切回本地 S3。调用方可按既有业务规则重试；重复 put 校验同一内容，重复 delete 对已不存在的键返回未删除。公开上传在写入失败时回滚预约与配额，不把未提交预约视为成功上传。

这条协议提供 Mask 字节边界；完整 Labeling 业务写入及其他入口仍须完成各自模块协议。

## 持久回收协议

持久字节回收使用 `aap-assets-mask-gc/v1`，提供 cleanup 专用的 `POST /v1/mask-gc/scan`、`execute` 和 `status`。每页最多 1000 项，消息至多 1 MiB；游标是最后访问的物理键，包含被跳过的旧格式文件名。删除请求固定操作 UUID、内容键、ETag 和观测修改时间。兼容的 `mask-content/delete` 不参与业务回收协调。

`raster-mask-gc/v1/<operation_id>/request.json` 通过条件创建声明唯一执行权；相同操作只读取回执，不再次删除。`receipt.json` 保存明确的 `deleted`、`missing` 或 `content_changed` 终态。两类日志不可变且不能独立过期。SDK 删除只尝试一次；丢失回包、崩溃、回执保存失败都保留待定状态，不能根据后续 HEAD 不存在而补造成功回执。

Labeling 的 `aap-labeling-mask-gc/v1` 以原生每键代次、删除意图与完整引用根管理业务授权。内容预检先读取代次，最终写入检查原代次；已开始删除、待定或代次变化会返回可重试的 409。只有匹配当前执行凭据的明确终态回执可以解除围栏，代次本身继续保留。独立协调运行时不持有 S3 凭据。

`apps/labeling-mask-gc` 的独立运行时以 bearer token 提供 `POST /v1/mask-gc/run` 和 `progress`，只持有六项原生 GC 能力与 cleanup HTTP token。`MASK_GC_POLL_ENABLED=false` 关闭自动轮询，保留手动操作和健康检查。应用的 `LABELING_MASK_GC_URL/TOKEN` 选择该 owner；配置失败或不可用时不回退本地实现。

| run 参数       | 默认值 | 规则                                         |
| -------------- | ------ | -------------------------------------------- |
| `scan_pages`   | 1      | 1–20 页，每页最多 1000 项                    |
| `delete_limit` | 100    | 1–100，与扫描预算独立                        |
| `dry_run`      | false  | true 时仅扫描和只读分类                      |
| `after_key`    | null   | 仅 dry-run 可提供；使用前次 `next_after_key` |

预览只调用 `inspect_mask_gc_page`，不改变 sweep、删除意图、代次、预约、业务根或字节日志。`next_after_key` 和 `scan_complete` 表示已完成的物理检查点；不完成的页不会推进。每次请求首次成功分类时返回全局当前存活引用键的去重数 `referenced`，无法观察时为 null；该计数不是删除授权。

预览的 `eligible + skipped = scanned`，`staged` 保持 0。正式执行的 `eligible = staged`，只计实际建立的意图；`deleted/missing/content_changed` 对应明确终态回执。`pending/indeterminate`、`durable_uncertainties` 和 `finish_errors` 分别保留未完成、持久不确定和确认失败的事实。清单失败不阻止既有意图继续协调。

Celery 消费者保留原任务名与 `cleanup` 路由，先提交领域过期记录并释放指标读取事务，再调用同一协调器；因此其 `dry_run` 仍会提交领域过期清理。独立服务的预览完全只读。现有故障证据覆盖本地数据库、真实对象存储、迟到回执及重启；本地容量验证不代表完整 Labeling 或生产容量资格。
