---
title: Runbook：GPU 显存仲裁验收
audience: [ops, developer]
type: how-to
status: active
last_reviewed: 2026-10-20
---

# Runbook：GPU 显存仲裁验收

本流程用于在隔离的非生产 GPU 节点验证跨 Backend 显存仲裁。验收器会真实执行 workload，
可能触发模型加载、drain、恢复和卸载；不要在承载生产流量的节点运行。

## 验收边界

验收分成三组，必须分别保留证据：

1. 单机单卡：同卡多 Backend 共驻、按预算驱逐和拒绝，以及 victim 的完整状态跃迁。
2. 单机多卡：不同物理资源上的真实 HTTP 执行至少重叠 500 ms，且一张卡的队列、transition
   或故障不能污染另一张卡。
3. 跨宿主同号卡：恰好两台主机各提交一份 `cross-host` 报告；两者使用不同 node/resource
   domain、相同物理卡索引，并用 PostgreSQL 时钟证明执行窗口重叠。

`preflight` 是只读操作。`run` 先只读验证除数据库缓存证明外的全部安全门禁，通过后才为范围内
每个 Backend 重新取得 challenge-bound health 并持久化运行时证明，再执行一次完整预检。
任一刷新或二次预检失败都会在业务 HTTP 前阻断。随后它通过验收专用入口直接执行真实 dispatch
authority；这条路径独立于生产 release latch，用于在开启逐资源 rollout 前取得证据，不会把生产请求切到
effective `enforce`。证明刷新不会修改 claim、fence、rollout mode 或 Redis 账本；`run` 也不会迁移
数据库、repair Redis、修改 release latch 或启停服务。

## 前置条件

- 使用独立维护窗口，暂停与验收 Backend、GPU resource 相关的其他任务。
- PostgreSQL schema 与当前代码 head 一致，Redis 账本为 ready，目标 membership 为 active。
- `celery-worker-gpu-control` 独立订阅 `gpu.control` 且健康；普通应用与 collector 使用不同
  PostgreSQL 角色。最近一轮 repair 不得出现 `gpu_collector_isolation_unavailable`，普通角色无
  membership/fence DELETE，collector 也没有约定外的写权限。
- lifecycle signer 已配置；Backend `/setup` 与 challenge-bound `/health` 能证明 exact
  backend/resource/boot/control identity。
- `nvidia-smi --query-gpu=index,uuid,memory.used,memory.total` 可用。
- 每个 Backend 的 `/health.gpu_info` 必须给出物理身份。Compose 从同一
  `*_GPU_DEVICE_ID` 派生的 `AAP_GPU_PHYSICAL_DEVICE_TOKEN` 优先于可被 runtime 重写的
  `NVIDIA_VISIBLE_DEVICES` 及逻辑 `CUDA_VISIBLE_DEVICES`；完整 GPU UUID 对应 `device_uuid`，
  物理索引对应 `physical_device_token=index:N` 与 `device_index=N`。本卡级 runner 只关闭完整
  GPU 的单卡/多卡门禁；共享 helper 可以报告 `mig_uuid`，但 MIG 实例不能用本流程代替专项验收。
- manifest action Backend 与资源域内每个现存 allocation Backend 都已注册到声明的唯一
  `gpu_resource_id`；preflight 会逐一核对 durable fence、challenge health、物理卡身份、预算和并发参数，
  任一存量 Backend 不可信都不会执行 workload。
- 任一 Backend 加入仲裁 manifest 前，先在目标镜像内运行其
  `scripts/validate_managed_lifecycle.py`。验收必须使用即将上线的完整业务权重、实际 pool cap、物理 GPU
  和 runtime；只是结构相似、更小或未经批准的模型不能关闭部署门禁。

先创建一个仓库外证据目录，避免把运行产物混入源码：

```bash
install -d -m 700 /tmp/aap-gpu-acceptance
```

## 先完成 Backend 部署资格

五个验收器使用同一严格 JSON 证据合同，但会调用各 Backend 的真实业务路径与模型池：

| Backend       | 必须验证的驻留面                                                | 模型审批变量                                                  |
| ------------- | --------------------------------------------------------------- | ------------------------------------------------------------- |
| YOLO          | detect / segment / pose / OBB 多权重池、video tracker、整池清理 | `VALIDATION_WEIGHT_SHA256_JSON`                               |
| Grounded-SAM2 | image + video 双池、DINO/SAM、embedding cache                   | `VALIDATION_WEIGHT_SHA256_JSON`                               |
| RapidOCR      | 3 个 composite engine、9 条 det/cls/rec ORT session             | `VALIDATION_WEIGHT_SHA256_JSON`                               |
| SAM3          | image / multiplex / PVS 三池、BF16 CUDA 路径                    | `VALIDATION_SAM3_SHA256`、`VALIDATION_MULTIPLEX_SHA256`       |
| ONNXTools     | pipeline / detector / va 三句柄、4 条 ORT session               | `VALIDATION_DET_SHA256`、`VALIDATION_VA_SHA256`、经批准代表图 |

部署受控 `trained:<版本>` 检测或实例分割模型时，YOLO 还必须提供 `VALIDATION_CONTROLLED_MODEL_WEIGHTS`、`VALIDATION_CONTROLLED_MODEL_FIXTURE`、`VALIDATION_CONTROLLED_MODEL_IMAGE` 三项输入。schema 3 夹具显式固定任务、基础模型和框架版本；分别取得 `controlled_detect` 或 `controlled_segment` 两轮证据及附加检查。原有 `VALIDATION_CONTROLLED_SEGMENT_*` 分割输入组兼容保留，两组不能混用。只有普通四任务报告不能关闭该受控模型路径的资格门禁；每个待部署权重／身份／GPU 分别验收，准备方法见[实例分割在线学习验收](/dev/how-to/validate-segmentation-online-learning#受控分割模型生命周期)。

公共环境变量为：

- `VALIDATION_GIT_COMMIT`：当前镜像对应的完整 commit；
- `VALIDATION_IMAGE_ID`：实际执行验收器的 image ID 或 digest；
- `VALIDATION_GPU_UUID`：该容器唯一可见卡的完整 UUID；
- `VALIDATION_MODEL_APPROVAL_REF` 和 `VALIDATION_FIXTURE_APPROVAL_REF`：可审计的批准记录引用。

每个后端的 README 给出了可直接执行的镜像内命令。必须在命令前启用 `set -o pipefail`，否则
`tee` 可能遮蔽验收器的非零退出码。证据只写入上述仓库外、权限 `0700` 的目录；不得存放私钥、
admission token、原始业务图像、签名 URL 或本地路径。

通过证据必须同时满足：

```bash
jq -e '
  .schema_version == "1" and
  .passed == true and
  .blockers == [] and
  .runtime_ephemera_clean == true and
  (.cycles | length) == 2 and
  .final_residency.state == "unloaded" and
  .final_residency.gpu_loaded == false
' /tmp/aap-gpu-acceptance/<backend>-managed-lifecycle.json
```

两轮都必须有 5 个卸载稳定样本，最大波动不超过 64 MiB，且工作集回收率至少 90%。五个
contract check 组会共同覆盖真实推理、GPU provider/device、partial header、token replay、旧 generation、
取消计费、busy unload 和 full cleanup。任一项失败都使 `passed=false`，且不得将对应
`*_MANAGED_LIFECYCLE_VERIFIED` 改为 `1`。

通过后先复核证据中的 commit、image、权重摘要、GPU UUID、driver/runtime 和 pool 拓扑，再单独把
该 Backend 的验收开关设为字面量 `1` 并重创容器。值只能是 `0` 或 `1`；非法值会拒绝启动。重创后必须读回
`/setup.managed_lifecycle` 严格九字段对象和空池 `/health.residency`，不能因已声明能力就伪造当前驻留。

## 编写 manifest

manifest 使用严格 JSON，拒绝额外字段。下面示例验证同一物理卡上的两个 Backend 共驻：

```json
{
  "schema_version": 1,
  "cohort_id": "gpu-acceptance-20260716",
  "node_id": "gpu-node-a",
  "scenario": "single-card-co-residency",
  "resources": [
    {
      "resource_id": "gpu-node-a/GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "gpu_uuid": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    }
  ],
  "actions": [
    {
      "id": "warmup-a",
      "role": "requester",
      "backend_id": "11111111-1111-4111-8111-111111111111",
      "resource_id": "gpu-node-a/GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "operation": "warmup",
      "body": {},
      "timeout_seconds": 600
    },
    {
      "id": "warmup-b",
      "role": "peer",
      "backend_id": "22222222-2222-4222-8222-222222222222",
      "resource_id": "gpu-node-a/GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "operation": "warmup",
      "body": {},
      "timeout_seconds": 600
    }
  ]
}
```

支持的 `scenario` 为 `single-card-co-residency`、`single-card-eviction`、
`single-card-capacity-rejection`、`dual-card` 和 `cross-host`。操作支持 `predict`、
`predict_interactive`、`warmup`、`reload`；真实验收应选择
会覆盖目标模型池的业务 body。`dual-card` 在一份 manifest 中声明至少两张卡；`cross-host`
则每台主机各用一份只声明本机一张卡的 manifest，并复用相同 `cohort_id`。
`single-card-capacity-rejection` 只允许一个 `requester` action，且必须声明
`"expected_error_code": "gpu_capacity_unavailable"`。只有 authority 在 grant 和 Backend HTTP 前
返回精确 503，且前后 allocation 与 `committed_mb` 完全不变，该 action 才按通过计。
验证 busy victim 时，victim action 必须命中已经 `Resident` 的模型池；冷建期间的
`Loading`/builder 是受保护现场，不能用来证明 active drain。

## 执行与验证

在 API 虚拟环境中运行：

```bash
cd apps/api
export PYTHONPATH=../_shared/protocol_v2/src:.

uv run python scripts/validate_gpu_arbitration.py preflight \
  --manifest /tmp/aap-gpu-acceptance/node-a.json \
  --output /tmp/aap-gpu-acceptance/node-a-preflight.json

uv run python scripts/validate_gpu_arbitration.py run \
  --manifest /tmp/aap-gpu-acceptance/node-a.json \
  --run-id gpu-node-a-01 \
  --confirm-run-id gpu-node-a-01 \
  --output /tmp/aap-gpu-acceptance/node-a-run.json

uv run python scripts/validate_gpu_arbitration.py verify \
  --scenario single-card-co-residency \
  /tmp/aap-gpu-acceptance/node-a-run.json \
  --output /tmp/aap-gpu-acceptance/single-card-verify.json
```

双卡验收只接受一份 `dual-card` 报告：

```bash
uv run python scripts/validate_gpu_arbitration.py verify \
  --scenario dual-card /tmp/aap-gpu-acceptance/dual-card-run.json
```

跨宿主验收只接受恰好两份报告：

```bash
uv run python scripts/validate_gpu_arbitration.py verify \
  --scenario cross-host \
  /tmp/aap-gpu-acceptance/node-a-cross-host.json \
  /tmp/aap-gpu-acceptance/node-b-cross-host.json
```

退出码 `0` 表示通过，`1` 表示证据验证失败，`2` 表示安全或环境前置条件阻断。
单卡 `verify` 必须分别选择 `single-card-co-residency`、`single-card-eviction` 或
`single-card-capacity-rejection`，每次通过只表示该子场景的证据一致；三个子场景的
组合复核仍属于 B，任何单份报告都不能关闭 B 门禁。

## 故障注入

每次故障注入使用新的 `run-id`，且一次只命中一个目标：

```bash
# fault-target 是 action id
uv run python scripts/validate_gpu_arbitration.py run \
  --manifest /tmp/aap-gpu-acceptance/node-a.json \
  --run-id response-lost-01 --confirm-run-id response-lost-01 \
  --fault response-lost-after-http --fault-target warmup-a \
  --output /tmp/aap-gpu-acceptance/response-lost.json

# fault-target 是 action id
uv run python scripts/validate_gpu_arbitration.py run \
  --manifest /tmp/aap-gpu-acceptance/node-a.json \
  --run-id cancel-01 --confirm-run-id cancel-01 \
  --fault cancel-after-grant --fault-target warmup-a \
  --output /tmp/aap-gpu-acceptance/cancel.json

# fault-target 是 backend UUID
uv run python scripts/validate_gpu_arbitration.py run \
  --manifest /tmp/aap-gpu-acceptance/node-a.json \
  --run-id health-timeout-01 --confirm-run-id health-timeout-01 \
  --fault health-timeout \
  --fault-target 11111111-1111-4111-8111-111111111111 \
  --output /tmp/aap-gpu-acceptance/health-timeout.json
```

传输结果不确定时，exact target allocation 必须继续保守计费为 Resident 或 Unknown，且只允许
exact resource/backend/generation 出现 uncertain/stale lease。health timeout 不得改变 victim 的
完整 allocation。非目标卡和 peer Backend 必须保持可执行且最终真值一致。

故障报告的检查由 `run` 在注入现场从内存中的 exact fault target 与原始快照计算。当前外部
`verify` 有意只接受 `faults=[]` 的无故障主报告；故障证据应连同原 manifest、节点日志和原始报告
一起归档，不能拿来替代单卡、双卡或跨宿主的正常通过报告。

## 固定阈值与证据解释

- 稳定显存窗口：连续 5 个样本、间隔 0.5 秒，同卡最大波动不超过 64 MiB。
- 并行窗口：不同资源或不同主机的真实 HTTP 执行重叠至少 500 ms。跨宿主报告会用开始、
  结束两次 PostgreSQL 时钟探针的完整 RTT 向内收缩 HTTP 区间，只接受保守重叠下界。
- 显存回收率下限：90%，只适用于隔离的 full-unload 生命周期证据。跨 Backend 驱逐完成后
  requester 仍驻留，整卡显存不会回到空卡基线，因此常规 co-residency/eviction run 不执行该断言；
  报告的 `threshold_applicability` 会明确标记这一边界。

## 运行时 deadline 与驱逐分支检查

每次成功 admission 都会在同一条 Redis 原子脚本中，把卡级 `reconcile_deadline_ms`
至少续到本次 workload 的 hard deadline 之后。续期使用剩余 health proof 窗口，且从 admission
时刻起不得超过 5 分钟；这保证长于定时 repair 周期的真实模型加载不会在 token 仍有效时因卡级
证明过期被中途拒绝。enforce 部署的 `ml_predict_timeout` 必须不大于 150 秒，从而在 30 秒 hard
deadline 收尾之后仍保留至少 120 秒 reconcile proof 窗口；配置超出该边界会在 HTTP 前以
`gpu_config_invalid` 阻断。验收长加载时应同时确认 workload lease 的 hard deadline、卡级 reconcile
deadline 单调续期，以及操作结束后 lease 正常释放。

空闲驱逐从 `draining` 进入 `unloading` 时必须原子冻结 `eviction_branch=unload`，并移除 transition
的自动过期时间。随后只有同一 owner、generation 的 `unloading → unloaded` 能完成该分支；cancel
分支不得再接管，同一跃迁的响应丢失重试必须返回幂等成功且不增加 ledger revision。若 victim
在冻结前重新变忙，则走新的 generation 和 `eviction_branch=cancel`
恢复 Resident。验收报告必须保留冻结后的 transition 快照和最终 allocation，不能只凭 Backend
返回 200 判断驱逐成功。

报告包含脱敏 manifest、原 manifest 内容摘要、数据库控制窗口、Redis
allocation/lease/queue/transition、challenge health、`nvidia-smi`、HTTP 执行窗口、故障命中与
清理结果。对于无故障主报告，`verify` 会从原始快照重算全部主断言，并拒绝阈值、脱敏 manifest
拓扑/action 元数据或摘要字段漂移。action 原始业务 body 不写入报告，因此脱离原 manifest 时只能
检查其摘要格式，不能独立证明 body 未被替换；内容摘要也不是签名或外部信任锚。需要防止人为
伪造时，应同时保留两台运行主机的原 manifest、原始报告、系统日志和只读对象存储审计记录。

## 收尾

确认报告中的 `runtime_ephemera_clean` 已通过，且不存在遗留 lease、ticket、queue 或 transition。
验收器只清理自己创建的 HTTP client、采样器与子进程，不会删除 Redis namespace 或用户数据。
归档所需证据后删除临时目录：

```bash
rm -rf /tmp/aap-gpu-acceptance
```

任何 `blocked`、Unknown、物理身份不一致或跨卡污染都必须先排障；不得把对应资源提升到 enforce。

## 相关文档

- [生产 Docker Compose 部署](/ops/deploy/docker-compose)
- [ML Backend 不可用](/ops/runbooks/ml-backend-down)
- [ML Backend 协议](/dev/reference/ml-backend-protocol)
- [GPU 显存仲裁 ADR](/dev/adr/archive/0049-cross-backend-gpu-memory-arbitration)
