---
title: Mask 字节接口
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-11-23
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

## API 侧持久 RLE 的归属选择

`ASSETS_MASK_CONTENT_URL` 显式选择独立 owner 时，API 的 Mask 内容入口、导入、AI 采纳、修复、QC、追踪、导出、训练／评估读取继续使用既有包装函数，但持久 RLE 只经该 owner 在可移植逻辑键 `raster-masks/sha256/...json[.gz]` 上存取；失败保持失败，不回退 API 自身对象存储。未选择 owner 时，API 沿用自身保存归属：物理对象为 `raster-masks/objects/{uuid}.json[.gz]`，由保存记录、发布保护与受保护清理管理。

两种模式的引用、业务校验、上传预约和每键代次相同。切换模式不迁移已有 Mask，也不双写；切换后已有内容仍留在原归属，新写入按当前选择落位。选择 owner 时的回收由 Labeling 的 Mask 回收集合负责；API 保存归属清理只处理自身登记记录。部署边界见[独立部署台账](/dev/concepts/module-deployment#mask-内容字节服务)，保存归属详见[存储位置、连接器与用途绑定](/dev/concepts/storage-connections#持久-rle-的独立归属与发布保护)。

跟踪 worker 直接调用同一字节核验：最终事务锁定待审候选的 Mask 引用，成功后原子交接
保留根。保留所需的历史对象位置和原所有者；核验不可用时不按当前默认路径猜测归属，
也不会发布未核验的候选。内容 bearer 不能用于删除。

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

默认 Celery Beat 的 `purge_unreferenced_raster_masks` 维护已登记的保存目标对象，
不扫描历史 SHA 对象，也不替代下面的独立预览与恢复。

## 手动回收运行入口 {#mask-gc-operator}

先迁移至当前 head，再应用 `module-data-grants.sql` 和
`labeling-mask-gc-data-grants.sql`。独立 `yoao_labeling_mask_gc_client` 仅取六项
回收函数；清理字节进程只持有独立的 S3 cleanup 身份。准备私有环境文件和只读
secret 文件，由实际容器 UID 可读，宿主父目录权限设为 `0700`：

- `LABELING_RUNTIME_MASK_GC_DATABASE_URL_FILE` 和 `LABELING_RUNTIME_MASK_GC_CONTROL_TOKEN_FILE`；
- `MASK_CLEANUP_TOKEN_FILE`（Labeling GC 与 Assets cleanup 共享的私有 bearer）；
- `MASK_CLEANUP_S3_ACCESS_KEY_FILE`、`MASK_CLEANUP_S3_SECRET_KEY_FILE`、
  `MASK_CONTENT_S3_ENDPOINT_URL` 与 `MASK_CONTENT_BUCKET`。

```bash
docker compose --env-file "$HOME/.config/yoao/mask-gc.env" -p yoao-mask-gc \
  -f docker-compose.assets-mask-content.yml -f docker-compose.labeling-mask-gc.yml \
  --profile mask-gc up -d --build --wait labeling-mask-gc
```

该组合随 `labeling-mask-gc` 启动 `assets-mask-cleanup`，只在回环地址发布 GC 的 8771
端口。镜像入口为 `python -m yoao_labeling_mask_gc_runtime`；该 profile 关闭自动轮询，仅凭
GC 专用 bearer 调用 run／progress 路由，预览先用 `dry_run=true`。显式运行同一镜像入口的
`--once` 会执行有界**写入**回收，而不是预览。任何未知的删除确认都继续走持久意图
与清理回执恢复，不能撤掉围栏或直接删除对象。独立测试环境可验证入口与凭据，
不会为日常环境启用自动回收。
