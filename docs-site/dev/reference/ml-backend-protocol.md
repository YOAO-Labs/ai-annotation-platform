---
audience: [dev, ops]
type: reference
since: v0.9.0
status: stable
last_reviewed: 2026-07-22
---

# ML Backend 协议契约

本页供推理服务接入者查阅健康检查、预测、能力声明和错误响应合同。项目如何启用服务见[全局注册表与项目启用](#全局注册表与项目启用)。

平台不内置任何具体模型。它把可挂接的「推理服务」抽象成 `MLBackend` 行——一个 URL + 鉴权信息 + 几个布尔位（`is_interactive` / `state`）。本文规定接入方需要实现的 HTTP 端点与请求/响应 schema。只要遵循，就能在「模型市场 → 注册管理」里注册、在「项目设置 → ML 模型」里启用。

---

## 端点总览

| 端点        | 方法 | 用途                         | 必需 | 平台调用点                                                                                |
| ----------- | ---- | ---------------------------- | ---- | ----------------------------------------------------------------------------------------- |
| `/health`   | GET  | 健康检查                     | ✅   | `MLBackendClient.health` (`ml_client.py:31`)                                              |
| `/predict`  | POST | 批量 / 交互式预测            | ✅   | `MLBackendClient.predict` (`ml_client.py:41`) / `predict_interactive` (`ml_client.py:64`) |
| `/setup`    | GET  | 返回模型配置（schema、超参） | ⚪   | `MLBackendClient.setup` (`ml_client.py:84`)                                               |
| `/warmup`   | POST | 显式预热模型权重到 pool      | ⚪   | `MLBackendClient.warmup`                                                                  |
| `/versions` | GET  | 列出可用模型版本             | ⚪   | `MLBackendClient.get_versions` (`ml_client.py:90`)                                        |

base URL 由超级管理员在「模型市场 → 注册管理」录入；项目管理员只在
项目设置中启用已注册 backend。末尾 `/` 会被平台自动 `rstrip`
(`ml_client.py:21`)。

---

## 鉴权

`MLBackend.auth_method` 二选一（`ml_backend.py:22`）：

- `none`（默认）— 平台不发送任何认证头。
- `token` — 平台在所有请求加 `Authorization: Bearer <auth_token>`（`ml_client.py:25-29`）。`auth_token` 在全局注册表单录入，存入 PG 且不在 API 响应中回传。

未来扩展（如 mTLS、HMAC 签名）走新 `auth_method` 值，不破坏现有 backend。

---

## 1. `GET /health`

**用途**：握手 / 周期探活。返回 200 表示在线。

**请求**：无 body。可能携带 `Authorization: Bearer ...`。

**响应**：HTTP 状态码即结论。`MLBackendClient.health` 不解析 body，只看 `status_code == 200`（`ml_client.py:33-39`）。

**超时**：服务端配置 `ml_health_timeout`（默认 10s，`config.py:55`）。超时或任何 `httpx.RequestError` 视为不健康，平台将 `ml_backends.state` 改写为 `"error"`（`ml_backend.py:63`）。

平台侧调用时机：

- 项目管理员在前端点「测试连接」（`POST /api/v1/projects/{pid}/ml-backends/{bid}/health`）。
- 周期健康检查可按 ROADMAP 的 ML Backend 健康检查方案扩展。

> **`pool` 子对象**（backend 统一为 `PoolStatus` 结构，详见 §4.3）：`{ cap, current_size, loaded_keys: [{key, loaded_at, last_used_at, hit_count}], last_evict: {key, at, reason} | null }`。`key` 是 backend-defined 的 opaque 字符串（yolo `{series}/{size}/{task}`、gsam2 `sam=X/dino=Y`、sam3 `sam3`），前端只做相等比较。`last_evict.reason` 受控为 `lru | manual | idle_timeout`。平台把健康快照缓存到 `ml_backend_registry.health_meta.pool`，模型市场列表用 `loaded_keys[]` 反查每行 variant 的运行时态。**未统一的老 backend**回的 `pool` 字段结构各家各异，平台层向后兼容；新接入的 backend 必须按 §4.3 落地。<!-- since v0.14.14 (PoolStatus 统一) -->

### 1.1 GPU 仲裁实时健康挑战

绑定了 `gpu_resource_id` 的 backend 在平台周期探活、以及 cold 派发收到完整 HTTP 响应后的立即探活中，会收到同一个随机 challenge：

- Header：`X-AAP-GPU-Health-Challenge`
- Query：`aap_gpu_health_challenge`
- 值：固定 64 位小写十六进制字符串

平台同时发送 header、query 和 `Cache-Control: no-cache`。backend 只有在 header 与 query 各出现一次、值完全
一致且格式合法时，才在响应 header 中精确回显 `X-AAP-GPU-Health-Challenge`，并发送
`Cache-Control: no-store`。challenge 缺失、重复、非法或不一致时，`/health` 仍保持原有响应语义，但不得
回显。旧 backend 或中间代理不支持该契约时，HTTP 200 仍表示已连接，只是不能形成 GPU 仲裁证明。若严格的
旧 backend 因未知 query 返回 400/422，平台只重试一次不带 challenge 与缓存指令的普通 `/health`；该兼容
响应即使携带同名 header 也永远不能升级为证明，原有 Authorization 仍会保留。

平台只接受唯一且精确的响应 header 回显；backend JSON body 不能自行声明回显成功。命中后，健康服务使用
PostgreSQL `clock_timestamp()` 分别记录网络请求开始前的 `probe_started_at` 和响应完整返回后的
`observed_at`，并把 challenge、backend/resource 身份及 membership epoch/state 一并写入
`health_meta.gpu_arbiter_probe`。写回前会依次锁定注册表行和 exact membership 行并重新核对 epoch/state，
使成员状态不能插入最终复核与写回之间；端点或成员配置在探测期间发生变化时丢弃响应。数据库返回无时区
时间或 `observed_at < probe_started_at` 时同样 fail-closed。

`gpu_arbiter_probe` 只是实时证据候选，不单独授权减记显存或重置 Redis 账本。消费方还必须重新读取当前
membership/fence，校验证据时效与完整 residency，并把最终核验和账本提交放在同一个受保护的流程中。周期 proof reset 额外要求 `probe_started_at > token_expiry_high_water`；cold 响应后终态对账则以「challenge 在完整响应后才生成」绑定该次调用，并要求 durable generation 仍与 prepared generation 精确相等。退役记录中的历史 health 只能用于诊断。

### 1.2 `compute` 计算设备观测

GPU backend 应在 `/health` 顶层返回 `compute`：

```json
{
  "compute": {
    "configured_device": "cuda",
    "effective_device": "cpu",
    "effective_provider": null,
    "cpu_fallback_supported": true
  }
}
```

- `configured_device` 表示部署意图或模型/session 构造偏好，常见值为 `cpu`、`cuda`、`cuda:0` 或 `gpu`。
- torch backend 使用 `effective_device` 表示进程已确认的构造/回退路径；ORT backend 使用 `effective_provider`。另一字段应省略或为 `null`。
- `null` 表示尚未加载业务模型、无法完整检查，或多个已加载 session / pool 的生效设备不一致；不得用配置偏好补成 CUDA。
- ORT 的 `effective_provider` 是已加载业务 session 的一致 primary provider（`session.get_providers()[0]`）。启动探测、`use_cuda` 或构造时 provider 列表只是偏好，不是该字段的真值。
- `cpu_fallback_supported=false` 表示 GPU-only；模型加载检测到 GPU 不可用时 backend 应返回结构化 503，不得伪造 CPU 重试。字段缺失或为 `null` 表示旧 backend 的能力未知；平台只对显式 `false` 抑制 fallback 告警。未加载时 `effective_device` 仍为 `null`。

平台仅在“已知配置 GPU + 实际为 CPU + 未显式声明不支持 fallback”时显示 CPU 回退警示；显式 CPU、unknown 和 `null` 均不告警。实时 `/health` 返回可解析的 HTTP 200 时以实时 `compute` 为准，即使 backend 自报 degraded 或值为 `null`；只有实时 HTTP 探测不可达/失败时才使用注册表缓存。torch 的进程级 latch 不能枚举旧 pool，因此 `effective_device=cpu` 与仍有 GPU pool 驻留可以同时成立。`compute` 是诊断信号，不证明 GPU 权重、tensor 或 cache 已释放，不能单独作为显存驻留或账本减账依据。

> **可选模型管理端点 `POST /reload` / `POST /unload`**（非协议必需，部分 backend 实现）：无 lifecycle body 的 `/unload` 是 legacy best-effort 行为，不能统一证明全部 image/video/variant/session pool 已清空。例如 Grounded-SAM2 仍只清 image pool，不能作为显存仲裁减账凭据。YOLO、ONNXTools 与 RapidOCR 的 bodyless legacy 路径会走各自全池清理，但仍只用于向后兼容；没有 generation、fencing 与受管响应，不能作为减账证据。Grounded-SAM2 的 `/reload` 接受可选 body `{ "sam_variant": "small", "dino_variant": "B" }` 预热**指定变体**（缺省回退 backend 启动默认变体；非法变体值 422，校验同 `/predict` 的 `context.model_variants`）；也接受可选 `"task_type": "image" | "video"`（默认 `image`，向后兼容）：`task_type="video"` 时**只认 `sam_variant`**（video tracker 不用 DINO），预热**独立 video 池** `VideoPool`，返回 `{ ok, loaded, reloaded, sam_variant, task_type: "video" }`。平台经 `POST /api/v1/projects/{pid}/ml-backends/{bid}/reload`（同 body）代理，模型市场「变体」面板按图像 / 视频两组分别走此链路。新 backend 应优先实现 §4.4 `/warmup`；接入方不得把未完成受管契约的 legacy unload 声明为可驱逐能力。

### 1.3 受管 GPU 生命周期（能力协商）

共享协议包已经定义受管生命周期的 wire schema、header、错误词表与 admission token 验签 codec。backend
只有在完整实现 active / builder / borrower 保护、全池释放、generation fencing、token replay 防护和全部控制端点后，
才可在 `/setup` 增加：

```json
{
  "managed_lifecycle": {
    "protocol_version": "1",
    "generation_fencing": true,
    "drain_endpoint": "/drain",
    "drain_cancel_endpoint": "/drain/cancel",
    "unload_endpoint": "/unload",
    "mode_endpoint": "/lifecycle/mode",
    "reset_endpoint": "/lifecycle/reset",
    "generation_header": "X-AAP-GPU-Generation",
    "token_header": "X-AAP-GPU-Admission-Token"
  }
}
```

平台消费远端声明时要求该对象是精确的九字段 JSON object：字段不得缺失或额外增加，
`generation_fencing` 必须是 JSON boolean，其余字段必须是 JSON string，并继续通过冻结常量校验。
平台不会用 schema 默认值补齐部分声明，也不会做字符串、数字与布尔值之间的类型转换；缺失或非法声明会落为
`managed_lifecycle=null` 并产生能力诊断，因此不能参与 enforce promotion。

GPU 健康证明把 `/health` 的随机 challenge 回显与同轮 `/setup` 能力探测放在同一观测窗口内；数据库
`observed_at` 只在两次远端调用都结束后取得。平台将规范化声明的 SHA-256 写入 challenge probe，并在证明消费时
重新计算注册表快照中的哈希。缺失或远端非法声明会规范化为 `null/null`，仍可保持 connected，但不能激活
membership 或形成 Redis ready。只有快照与 probe 的非空哈希 exact-match，且后续证明已绑定 active identity、
当前 control high-water 并越过 token horizon，才能恢复保守账本；快照非法、哈希不匹配或 membership
epoch/state 漂移均保持 not-ready。
challenge `/health` 与 `/setup` 请求都携带 `Cache-Control: no-cache`，共享代理必须向 origin 重新验证，不能把
陈旧缓存当成本轮能力证据。
并发健康扫描采用保守的 observation-window fence：写回锁内若发现另一轮扫描已在本轮 `probe_started_at` 之后
提交，就丢弃本轮迟到结果。慢 `/setup` 因而不能借更晚的结束时间给旧 `/health` 续鲜并覆盖更新快照。

只导入 schema、保留 legacy `/unload` 或返回部分 residency 字段都不构成该能力。五个第一方
GPU Backend 的源码都已实现完整 wire 和各自的全池所有权清理：YOLO 覆盖多权重池与 tracker，
Grounded-SAM2 覆盖 image/video 双池和 embedding cache，SAM3 覆盖 image/multiplex/PVS 三池，
ONNXTools 覆盖三句柄四业务 session，RapidOCR 覆盖三引擎九业务 session。源码能力不等于
当前部署已取得资格，仓库文档也不替任何实例作资格背书。

每个 Backend 都必须在目标镜像、完整业务权重和唯一可见物理 GPU 上运行镜像内
`scripts/validate_managed_lifecycle.py`。五个验收器共用严格证据外壳，同时执行真实业务推理、
两轮 full-pool drain/unload、90% 工作集回收门、provider/device 判定，以及 partial header、token replay、
旧 generation、取消计费和 busy unload 故障矩阵。证据必须绑定 git commit、image ID、权重/经批准
fixture 的路径无关 SHA-256、GPU UUID、driver/runtime 和 pool 拓扑，且最终
`passed=true` 与 `runtime_ephemera_clean=true`。

对应的 `*_MANAGED_LIFECYCLE_VERIFIED` 只接受字面量 `0` 或 `1`，非法值使 Backend 拒绝启动。
默认值 `0` 会隐藏能力、拒绝 enforce 并使 `evictable=false`；只有运维已复核当前部署的通过证据时
才设为 `1`。镜像、权重、pool cap、GPU UUID、driver 或 runtime 任一变化都使旧证据失效，必须恢复 `0`
并重新验收。

`/health` 在实现后新增顶层 `residency`，并保留原有 `compute`、`loaded`、`pool` 等兼容字段：

```json
{
  "residency": {
    "state": "resident",
    "gpu_loaded": true,
    "active_requests": 0,
    "builders": 0,
    "borrowers": 0,
    "draining": false,
    "evictable": true,
    "generation": "42",
    "pools": {
      "models": { "resident": true, "device": "cuda:0", "provider": null }
    },
    "boot_id": "<random-per-process-boot-id>",
    "lifecycle_gate": "enforce",
    "control_epoch": "7",
    "identity": {
      "audience": "aap-gpu-lifecycle",
      "backend_registry_id": "<registry-id>",
      "gpu_resource_id": "<resource-id>"
    }
  }
}
```

`state` 只允许 `unloaded|loading|resident|draining|unloading|unknown`。`generation` 与 `control_epoch`
必须是 `1..9223372036854775807` 的无前导零十进制字符串，JSON number、零、符号、空白与越界值均拒绝。
`gpu_loaded` 和逐 pool `resident` 可为 `null`，表示无法可信判断。逐 pool `resident` 专指 GPU residency；
纯 CPU handle 报告 `resident=false`，并通过 `device=cpu` 或 CPU provider 表达。只有所有 pool 的 GPU
residency 都显式为 `false`，且 GPU session/cache、builder 与 borrower 均已安全收敛时，backend 才能报告
`gpu_loaded=false`。

受管 workload / transition 使用独立 header，不复用 `Authorization`：

```text
X-AAP-GPU-Generation: 42
X-AAP-GPU-Admission-Token: <compact EdDSA JWS>
```

token 固定为 Ed25519 / EdDSA compact JWS，protected header 为 `alg=EdDSA`、`typ=aap-gpu+jwt` 并携带
`kid`；`aud` 固定为 `aap-gpu-lifecycle`。平台 signer 独占私钥，backend 只持可重叠轮换的 Ed25519
公钥 keyring。未知 `kid`、其他算法/type、过期、错误 audience/scope/boot/identity 或重放统一拒绝；用户登录
JWT key 不得复用。轮换必须先把新旧公钥共同部署到 backend，再切 signer 的 active `kid`，最后等旧 token、
lease 与 replay tombstone 全部安全过期后移除旧 key。

平台签发进程通过 `GPU_LIFECYCLE_SIGNING_KEYS_FILE` 延迟读取私钥文件，并用
`GPU_LIFECYCLE_ACTIVE_SIGNING_KID` 选择当前签名 key。文件是严格 JSON
`kid -> unpadded-base64url(raw 32-byte Ed25519 private seed)`；只应以只读 secret 挂载给 API、通用 worker
和 GPU worker，不得进入 CPU/export/beat、Web 或任何 ML backend。`off/observe` 派发不会读取私钥文件；
缺失、不可读、重复 key、非法 key 或 active kid 不存在都会让 promotion/enforce 准入保持 fail-closed。

平台调用 `/lifecycle/mode` 使用独立控制 wire：body 为精确的 `gate + control_epoch`，header 只携带
`X-AAP-GPU-Admission-Token`，不携带 generation，也不进入 workload dispatch、shadow decision 或本地
semaphore。远端 ACK 从原始 JSON 严格解析，重复 key、缺失/额外字段、字符串布尔值/计数等类型转换及不一致的
response/residency 一律拒绝；平台不会用共享模型的本地构造默认值补齐部分 ACK。

desired mode 为 enforce 的周期修复会在健康扫描之后处理 membership。平台先非阻塞尝试目标物理资源锁；卡锁忙时立即
fail-closed，不会先占有全局 barrier 让其他卡跟随等待。取得逐卡锁后再非阻塞尝试全局短 promotion barrier；
正常短竞争会先释放事务并按 deadline + jitter 有界重试，耗尽后才阻断，避免并行多卡互相误降级或饥饿。claim、
membership insert 与 health proof 写入按逐卡锁→全局 barrier 排序；数据库 trigger 对两级锁均 fail-fast，忙时以
`40001` 要求整事务重试，避免多资源写事务持有全局 barrier 再等待下一张卡。平台在
完整物理资源锁域内重验 connected registry claim、exact membership epoch/state、非空受管能力哈希、新鲜 challenge proof
与稳定空闲的 legacy residency，并在全局 barrier 内扫描所有 pending/active membership：任意两个成员的
canonical endpoint 相同，或新鲜 challenge-bound proof 回报同一 `boot_id`，都保持阻断；后者不因对端
capability 失效或当时忙碌而放行。只有 signer 已成功加载，平台才在同一短事务把
pending membership 推进到 active runtime epoch，并同时推进 control epoch 与 admission token 的 expiry
high-water。任一 epoch/horizon 推进前，平台必须在仍持有逐卡锁时先将 Redis 卡级 ready 降为 not-ready；只有 Redis 明确返回
`not_ready` 才视为撤销成功，账本损坏等其他状态一律使数据库事务回滚。事务提交后才构造 `scope=mode`、无 generation、绑定 exact boot/backend/resource 的 30 秒 token，
再请求 `gate=legacy`。ACK 除严格 wire 外还必须匹配本次 boot、identity、control epoch，并继续报告稳定空闲、
非 evictable 的 legacy residency。

事务提交后的签名、timeout、拒绝、响应丢失或 ACK 不匹配都不会把 active membership 回滚为 pending；后续周期
必须先取得新的 exact active proof，并在旧 token horizon 之后用更大 control epoch 恢复。任一成员推进 epoch、ACK 未确认、signer/证明/alias 阻断或
证明尚不满足 readiness 时，已有卡级 ready 必须在返回结果前降为 not-ready。成功 ACK 不直接授予
Redis ready 或驱逐权；周期 repair 只在后续 proof 同时具有非空 exact capability、当前 active identity、等于
durable high-water 的 control epoch，且 `probe_started_at` 严格晚于 horizon 时才可恢复 ready。release latch
关闭或逐资源尚未收敛为 `enforcing` 时，实际派发不签业务 token、不创建 admission lease；只有 effective
`enforce` 才进入 Redis authority。rollout 控制操作会把
`reset|mode_enforce|mode_legacy`、exact transition UUID、membership epoch、boot id、control epoch
与 token expiry 持久在 Backend fence；进程重启或 HTTP 响应丢失后，只重签这个意图。
promotion 必须先在 legacy gate 下完成 signed full-reset 并由 post-horizon health 证明空池，
然后才能以更大 epoch 进入 enforce；demotion 则允许保留已驻留 pool，但必须等 active/builder/
borrower 全部归零且 fresh health 确认 legacy gate。只有已稳定收敛为 `off/observe`
且持久 rollout 为 off 的资源才不读 signer、不执行 membership promotion、不调用
`/lifecycle/mode`、也不访问 GPU 仲裁账本；未收敛的 demotion 仍保留账本并使用 signed control wire。
逐卡修复的总时间片会预留独立 fail-closed 收尾预算；即使墓碑收尾、promotion 或 proof reset 耗尽主时间片、
出现普通异常或任务被批次总时限取消，worker 也会在返回失败结果前有界尝试把该卡锁存为 not-ready，避免多卡
批次中的慢卡沿用旧 ready。

五个 GPU backend 从 `GPU_LIFECYCLE_VERIFY_KEYS_JSON` 读取 `kid -> unpadded-base64url-public-key` JSON。空值允许 backend
以 legacy gate 启动；非空但无法解析的配置会阻止启动。`/health` 与 `/setup` 始终免 token；legacy gate 下
无 header 的 `/predict`、`/predict/interactive`、`/warmup` 和 bodyless `/unload` 保持兼容，但会把驻留标记为
unmanaged。workload 与 transition 端点只有在 generation、admission token 两个 header 都完全缺失时才能进入该
legacy 路径；任一 header 出现后，两者必须各出现且只出现一次，并完成全部受管校验。部分 header、重复 header
或非法值都会 fail-closed，不能降级为 unmanaged 请求；bodyless `/unload` 携带任一受管 header 时同样拒绝。
`/lifecycle/mode` 与 `/lifecycle/reset` 只接受唯一 admission token，并拒绝 generation header。enforce gate 下
加载入口必须携带匹配当前 boot、identity、control epoch 与 generation 的 token。

ONNXTools 的 `residency.pools` 固定包含 `pipeline`、`detector`、`va`。其中 composite pipeline 持两个 ORT
session，因此三句柄全驻留时共有四个业务 session。逐池 residency 检查完整 `get_providers()` chain；任一
CUDA/TensorRT provider 即为 GPU 驻留，全部已知且仅 CPU 才为 false，私有 session 路径缺失、builder 或清理失败
保持 unknown。诊断字段 `compute.effective_provider` 仍只在所有已加载 session primary provider 一致时有值，不能
代替 residency。

RapidOCR 的 `residency.pools` 只暴露稳定聚合 ID `engines`，六种动态权重三件套 key
保留在兼容字段 `pool.loaded_keys`。每个 composite engine 无条件持有 det/cls/rec 三个 ORT
session，默认容量三个 engine，因此满池最多九个业务 session。池在构造前预留 slot，
同 key single-flight，只淘汰无 borrower/waiter 的最旧 entry，并在 replacement build 前先释放受害者。
启动软检查不构造临时 session，真实 provider 只从受 admission 保护的业务引擎读取。
仅确认为设备错误时才尝试 CPU replacement；CUDA composite 部分构造失败后即使 CPU 替代成功，
residency 也保持 unknown，直到一次成功的全池清理。

控制端点按以下顺序使用：平台先以 `/lifecycle/mode` 建立 gate；驱逐时用更大 generation 调 `/drain`，待
active、builder、borrower 全部归零后用同 generation、owner、operation 调带 body 的 `/unload`；放弃驱逐则
以更新 generation 调 `/drain/cancel`，其 token 的 owner 与 operation 必须同时精确匹配原 drain；仅 owner
相同不能授权恢复。legacy unmanaged 驻留进入 enforce 前，必须先用更大 control epoch 调
`/lifecycle/reset` 完成可信全池清理，再执行 promotion。managed unload 成功后保留 generation tombstone，
同 generation 不得重新加载。

backend lifecycle 错误保持 FastAPI envelope `{"detail":{"error_code":"..."}}`：

| 场景                                | HTTP / `error_code`                             |
| ----------------------------------- | ----------------------------------------------- |
| draining 拒绝新 workload            | `503 gpu_backend_draining`，可带 `Retry-After`  |
| active 时请求 unload/reset          | `409 gpu_backend_active`                        |
| 旧 generation                       | `409 gpu_generation_conflict`                   |
| 非法或冲突 transition               | `409 gpu_transition_conflict`                   |
| generation 格式错误                 | `422 gpu_generation_invalid`                    |
| header/body/token generation 不一致 | `422 gpu_generation_mismatch`                   |
| token 缺失、验签失败、过期或重放    | `403 gpu_admission_denied`                      |
| 全池清理失败                        | `500 gpu_unload_failed`，residency 保持 unknown |

---

## 2. `POST /predict`

平台用同一个端点跑两种工作流。请求体 schema 由 backend 类型决定。

### 2.1 批量预测（同步）

适用：项目级「自动预标注」。Celery worker 把 task 切片成 batch，逐 batch 调一次 `/predict`。

**请求**：

```json
{
  "tasks": [
    { "id": "<task_uuid>", "file_path": "<presigned_url_or_relative_path>" },
    ...
  ]
}
```

`tasks` 是一个数组；具体每项的字段由平台与 backend 协商，但平台调用方至少传 `id` + 可访问的 `file_path`。详见 `app/workers/tasks.py:batch_predict` 任务（自动预标注的实际生产者）。

**响应**：

```json
{
  "results": [
    {
      "task": "<task_uuid>",                 // 必填；与请求 tasks[i].id 对应
      "result": [<annotation>, ...],         // 必填；标注 schema 见下文 §3
      "score": 0.92,                         // 可选；整体置信度，写入 predictions.score
      "model_version": "v1.2.3",             // 可选；写入 predictions.model_version
      "inference_time_ms": 245,              // 可选；写入 prediction_metas.inference_time_ms
      "cache_hit": true,                     // v0.14.14 可选；true=权重在 pool 内复用,false=本次触发加载
      "model_load_ms": 0,                    // v0.14.14 可选；本次加载耗时(ms)，cache_hit=true 时通常为 0
      "pool_state": { "current_size": 2, "cap": 4 }  // v0.14.14 可选；轻量 pool 快照
    },
    ...
  ]
}
```

> **v0.14.14 运行时观测三件套**（`cache_hit` / `model_load_ms` / `pool_state`）：详见 §4.2。所有 backend 应在 `/predict` 与 `/warmup`（§4.4）响应里至少填 `cache_hit` 与 `model_load_ms`，前端据此切换"加载中…"和"推理中…"按钮文案。三个字段都是可选的，缺省时前端走"未知"路径（按热路径渲染，第一次响应回来后修正）。

平台侧解析：`MLBackendClient.predict` (`ml_client.py:41-62`) 把每项映射到 `PredictionResult` dataclass，再由调用方落到 `predictions` / `prediction_metas` 表。

**超时**：服务端配置 `ml_predict_timeout`（默认 100s，`config.py:54`）。超时由 worker 捕获，写一行 `failed_predictions` 并继续下一 batch（不阻断）。

<!-- since 协议 v2.2 -->

### 2.1.1 几何 prompt 批量（下游编排 stage）

适用：多阶段预标注的**下游 stage** —— 上游检测器（YOLO / onnxtools-detect / grounded-sam2-detection）已产出 bbox，下游一个**非交互、批量**的 model（如 grounded-sam2 `box-seg`）消费这些框、对每框出 mask/polygon。

**载荷形态（form 1a：全图 + 原图坐标框列表）**：下游 stage 收**全图 presigned URL**（复用上游 stage 的同一 URL）+ 该图的 **N 个父框（原图归一化坐标）列表**，而非逐 crop 裁图。这样 backend 对一张图只 `set_image` 一次（SAM2 encoder 成本由 `set_image` 次数决定，与裁多小无关），N 个框共享同一份 image embedding，跑轻量 decoder：

```json
{
  "tasks": [
    {
      "id": "<task_uuid>",
      "file_path": "<presigned_url>",          // 全图，与上游 stage 同一 URL
      "prompts": [                              // 单图多框：上游框列表
        { "box": [x1, y1, x2, y2], "parent_box_idx": 0 },   // 原图归一化坐标 [0,1]
        { "box": [x1, y1, x2, y2], "parent_box_idx": 1 }
      ]
    }
  ]
}
```

**响应**：与 §2.1 同构，但下游每框输出按 `parent_box_idx` 标回，平台据此 merge 回对应父框（polygon 已是原图坐标，**无需坐标回映**）：

```json
{
  "results": [
    {
      "task": "<task_uuid>",
      "result": [
        { "type": "polygonlabels", "value": { "points": [...], "polygonlabels": ["object"] },
          "parent_box_idx": 0 },
        { "type": "polygonlabels", "value": { "points": [...], "polygonlabels": ["object"] },
          "parent_box_idx": 1 }
      ]
    }
  ]
}
```

**判别器**：投递方式是「产物形态」（下游阶段 `write.target`）与「投递方式」（下游 model 的 `supported_inputs`，见 §3.1）二维决定的：

- `write.target=attributes` → **crop 模式**：平台逐父框裁 crop 图上传，下游收单张 ROI 回属性（如 onnxtools 纯分类）。
- `write.target ∈ {geometry, intermediate}` 且下游 `supported_inputs` 含 `bbox_prompt` → **geometry 模式**：全图 + 父框归一化列表喂 box-seg 原子，回原图坐标 polygon。
- `write.target ∈ {geometry, intermediate}` 且下游 `supported_inputs` 含 `crop`（普通检测器） → **crop-detect 模式**：平台裁父框 ROI 喂检测器，检出几何按 crop 仿射变换**回映回原图坐标**，作为新框追加 / 供下游消费（支持几何 depth-3，如 `person → 在 person crop 上检测 hat → 给 hat 分类 color`）。

平台在 `POST /preannotate` 端点按子模型 `supported_inputs` 解析投递方式并烘焙进阶段 `input.mode`，worker 直接消费；产几何的子若 `supported_inputs` 既不含 `bbox_prompt` 也不含 `crop`，端点 422 拒绝（不可达）。老 backend 缺 `supported_inputs` 时平台按 `supported_prompts` 合成兼容默认（见 §3.1），零退化。本约定纯加法。

派发期还会就「跑完必然空结果」的结构性误配再叠加两条 422（同样只在模型**显式**自报了对应字段时触发，缺省跳过，零退化）：

- 阶段所选模型 `resource_profile.batchable=false`（交互 / 有状态视频追踪）→ 拒绝进批量预标流水线（源阶段与下游阶段同判据；比 `is_interactive` 更诚实的单一批量判据）。
- 阶段 `write.target=attributes` 但模型 `output_attribute_types` 不含 `class` → 拒绝（作分类下游只会产出空属性）。

这两条判据是**两层对称**的：派发期 422 是最终拦截，配置期则前移为**非阻断预警**——前端编排面板按同判据对源 / 下游阶段标红（源模型选择器 + 阶段卡），`PATCH /projects/{id}` 保存编排时响应体回带 `capability_warnings[]`（保存是配置中途态，能力快照可能滞后、亦允许「先存草稿、之后换 backend」，故只软提示不硬挡）。判据本体抽在 `app/services/pipeline_validation.py` 的纯函数里，由保存路径、派发路径与前端 `stageWarning` 共用一份，并以跨端 fixture（`apps/web/src/__fixtures__/capability-validation-cases.json`）双端断言防漂移。

### 2.2 交互式预测

适用：标注员在工作台内点「AI 助手」工具发起的单次推理。

只有 `is_interactive=True` 且 `state="connected"` 的 backend 才会被路由到这条路径（`ml_backend.py:67-75`）。

**请求**：

```json
{
  "task": { "id": "<task_uuid>", "file_path": "..." },
  "context": {
    "type": "point" | "interactive_box" | "polygon" | "text" | "exemplar",
    "points": [[x, y], ...],                // type=point 时 (正/负点累加, 前端重发全量点)
    "bbox": [x1, y1, x2, y2],               // type=interactive_box 时 (单框 prompt) 或 type=exemplar 时 (单视觉示例框, 兼容旧路径)
    "exemplars": [                          // type=exemplar 多正负框累加 (优先于单 bbox); 见下
      { "bbox": [x1, y1, x2, y2], "label": true }
    ],
    "labels": [1, 0, ...],                  // 可选；point 类型，1=positive 0=negative
    "multimask_output": false,              // 可选; point/interactive_box 单点歧义出 3 候选 (按 iou 降序), 默认单 mask
    "model_id": "grounded-sam2-interactive-seg", // 可选；指定 /setup.models[] 的目标交互 model
    "output_geometry": "polygon" | "mask", // 可选；缺省继续返回 polygon 兼容字面
    "prompt_revision": "<server-generated>", // Mask 请求必填；经平台代理时由服务端重建
    "mask_input": "<base64>",               // 可选; 上一轮 256×256 low-res logits 回灌 (多点精修, 见下), 不透明字符串
    "text": "ripe apples",                  // type=text 时（Grounded-SAM-2 / SAM 3 PCS 文本入口）; type=exemplar 时可叠加为概念组合
    "output": "box" | "mask" | "both",      // type=text / type=exemplar 生效, 默认 "mask" 老前端兼容
    "box_threshold": 0.35,                  // 可选; type=text 时 backend 的 DINO 阈值 override (grounded-sam2 专属)
    "text_threshold": 0.25,                 // 可选; 同上
    "score_threshold": 0.5,                 // SAM 3 PCS text/exemplar 路径 score 过滤阈值
    "simplify_tolerance": 1.0,              // shapely.simplify 像素级覆盖, 仅 mask/both 路径生效
    "model_variants": {                     // v2.1 通用模型变体轴, key 来自 /setup.supported_variants[].key
      "sam_variant": "large",
      "dino_variant": "B"
    }
  }
}
```

`context` 是开放 dict，旧请求仍按原样透传。显式携带 `output_geometry` 或 `model_id`
时，平台会在调用 backend 前从同一个 `models[]` 条目同时校验 prompt 与输出；
`output_geometry="mask"` 还会由平台覆盖客户端传入的 `prompt_revision`，并在有界读取后
校验每个 RLE、候选 ID、媒体尺寸和空结果诊断。

> **`type=point` / `type=interactive_box`**：SAM-style 单实例交互分割，两个 backend 同名同义（grounded-sam2 走 SAM 2.1 image predictor，SAM 3 走 inst predictor `model.predict_inst`）。`point` 支持正/负点累加——前端把同一对象的全部点每次重发（无状态后端），1=positive 0=negative；`interactive_box` 是单框单 mask。`multimask_output=true` 时单点歧义返回多个候选（`result[]` 按 iou 稳定降序）。缺少 `output_geometry` 或显式传 `polygon` 时保持 `polygonlabels: ["object"]`；显式传 `mask` 时，Grounded-SAM2 和 SAM3 直接编码模型的原分辨率像素作为权威 RLE，并额外生成只用于候选显示的简化轮廓。`bbox` 作为交互 prompt 已退役（仅保留为几何形状）；旧 `type=bbox` 请求返回 422。

Mask 候选的响应字面为：

```json
{
  "result": [
    {
      "type": "mask",
      "value": {
        "rle": { "encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1] },
        "masklabels": ["object"],
        "preview": {
          "points": [
            [0.0, 0.0],
            [0.67, 0.0],
            [0.67, 1.0],
            [0.0, 1.0]
          ]
        }
      },
      "score": 0.91,
      "candidate_id": "sha256:<64 hex>"
    }
  ],
  "diagnostic": null,
  "prompt_revision": "sha256:<64 hex>",
  "output_geometry": "mask",
  "routing": {
    "requested_backend_id": "<uuid>",
    "backend_pool_id": null,
    "backend_instance_id": "<uuid>",
    "model_id": "grounded-sam2-interactive-seg"
  },
  "prompt_summary": {
    "family": "point",
    "positive_points": 1,
    "negative_points": 0,
    "boxes": 0,
    "positive_scribbles": 0,
    "negative_scribbles": 0,
    "multimask": true,
    "parameters_digest": null
  },
  "accept_receipts": {
    "sha256:<candidate digest>": "<signed receipt>"
  }
}
```

`value.preview` 是可选、归一化且有界的显示轮廓，最多 8,192 个点。工作台用它复用
polygon 候选层完成全量动画、缩放和 `Tab` 高亮，只为当前候选懒加载像素预览；
接受与签名始终以 `value.rle` 为准，preview 不参与 `candidate_id`、回执或落库。
旧 backend 不返回 preview 时协议仍兼容，但只能在当前候选的 RLE 分析完成后显示像素预览。

SAM3 示例的 `type=exemplar, output=both, output_geometry=mask` 返回按实例交错的
`[rectanglelabels, mask, rectanglelabels, mask, ...]`。每对使用同一标签和分数；矩形先裁剪至
图像边界，宽高必须为正，空 Mask 连同其配对矩形一起跳过。平台只在这一请求组合放行配对矩形，
Mask 的 RLE、媒体尺寸和 ID 校验保持不变。`candidate_id` 和采纳回执绑定完整数组下标
（Mask 为 `1, 3, ...`），矩形不签发 Mask 回执；客户端消费候选时不得改变剩余 Mask 的原下标。

平台代理转发此组合时设置 `context.native_mask_companion_boxes=true`，表示可以校验混合候选；
该内部标志由平台覆盖，工作台无需传入。SAM3 仅在收到此标志时返回配对矩形，未传入时保留
旧版全 Mask 数组，以兼容仍与同一模型服务连接的旧 API。平台与后端应配套更新以启用完整召回。

全部候选为空时返回 `result=[]` 与
`diagnostic={"reason":"empty_mask","retryable":false}`。单个 RLE 与整个交互响应的上限
分别是 4 MiB 和 16 MiB；整体限额在读取 backend 响应流时执行，无 `Content-Length`
的 chunked 响应也不能绕过。返回给工作台的代理响应同时包含实际 backend
instance / pool、目标 model 与 `model_version`，供后续接受时写入 lineage。

`accept_receipts` 按 `candidate_id` 返回短生命周期签名回执。回执绑定 task、候选像素、候选序号、
prompt revision、服务端生成的 prompt 计数摘要、实际路由、模型、模型变体、推理摘要、源 Mask
digest 与精确接受目标；客户端不能自己构造、改成新建目标或跨任务 / 源版本复用。
接受原生候选使用 `POST /api/v1/tasks/{task_id}/ai-mask-candidates/accept`，提交候选、对应回执、
类别、目标（新建或带源版本精修）、prompt 摘要、路由与推理元数据。服务端重新校验任务与写闸，
并在一次数据库提交中写 Prediction、PredictionMeta、接受 decision、Annotation 与审计。图片写成
`raster_mask`，视频写成仅含当前帧关键帧的 `video_track_mask`。同一 task 与 idempotency key 的相同
请求返回首次完整响应；同 key 不同请求或过期 decision 返回 409。失败事务最多留下受宽限期 GC
管理的未引用内容对象，不会留下半个预测或标注。

> **`mask_input` 回灌（多轮精修增量）**：SAM2/SAM3 的 `predict()` 接收上一轮 256×256 low-res logits 回灌，多次追加点、框或笔迹时提升 Mask 稳定性与边界质量。backend 把本轮 `low_res_masks` 编码为内部 raw logits；平台代理将其封装成五分钟 Fernet 加密鉴权 token 后才作为 `mask_input_next` 返回。浏览器只保存并原样回传 token，不能读取或自行构造 raw logits；平台复核 task、frame、backend、model、模型变体、源 Mask、origin revision 与候选后再解封给 backend。token 不写数据库、对象存储、审计详情或普通日志，部署 secret 轮换会立即使旧会话失效。
>
> - **内部编码**：`float16(256×256)` → `tobytes` → `zlib(level=6)` → 前缀 magic `m1` → `base64(ascii)`；解压后必须精确为 256×256、有限并位于 `[-32,32]`。该 raw 串只存在于平台代理与 backend 之间。
> - **候选边界**：`multimask_output=true` 的多候选存在 index 歧义，不返回下一轮 token；已存 Mask seed 或单候选 point / box / scribble 精修可以返回 token，并跨这三种工具继续同一会话。
> - **失效与恢复**：接受、取消、切题、切帧、切 backend / model / 变体、五分钟 TTL 到期都会释放 token。篡改、过期或绑定不一致的 token 由平台稳定拒绝；工作台清除旧 token，并在仍有已授权源 Mask 时保留笔迹、禁用旧候选接受并允许重试。平台不维护服务端会话缓存，浏览器丢失 token 后重新推理。

> **`type=mask` / `type=scribble`**：浏览器只提交源 annotation ID 与版本，平台完成权限、任务 / 帧、锁、版本和内容摘要校验后，才把有界 inline RLE 交给 backend。`scribble` 使用归一化折线；`width` 是相对图片短边的完整笔宽，后画笔迹覆盖先画笔迹。每请求最多 64 条、8,192 个 wire 点和 2,000,000 个累计栅格工作像素；adapter 最多确定性采样 512 个正负点交给底层 SAM。只有负笔迹时必须同时有已授权 Mask seed 或有效 session token。

> **`type=text`**：Grounded-SAM-2 走 GroundingDINO 文本 → boxes → SAM mask 复合链路；SAM 3 走 PCS 单模型一步出 mask。两者返回 `result[]` 字面一致（多 polygon / 多 rect / 配对）。`box_threshold` / `text_threshold` 仅 grounded-sam2 消费；`score_threshold` 仅 SAM 3 消费。

> **`context.model_variants`**：请求级模型变体热切换。结构是扁平 `dict[axis_key, axis_value]`，`axis_key` 必须来自当前 model 的 `/setup.supported_variants[].key`。yolo 示例：`{"series":"yolov11","size":"s"}`；grounded-sam2 示例：`{"sam_variant":"large","dino_variant":"B"}`；sam3 示例：`{"model_variant":"sam3"}`。backend 内 ModelPool 按这些轴组成 cache key：命中复用、miss 冷启；非法值或非法组合返回 422；变体合法但权重缺失 / 显存不可服务返回 503 + `Retry-After`。返回 `model_version` 可按本次请求变体拼（如 `grounded-sam2-dinoB-sam2.1large`）。embedding cache 按变体分桶（不同变体张量不可跨用），命中只在同变体同图。<!-- since protocol v2.1 -->
>
> **兼容期旧字段**：backend 必须继续接受一版旧写法并 normalize 到 `context.model_variants`：yolo 的 `context.variants`、grounded-sam2 的 `context.sam_variant` / `context.dino_variant`、sam3 的 `context.model_variant`。收到旧字段时应记录 deprecation warning；若新旧字段同时存在，新字段优先。

> **`type=exemplar`**：取图中已有的 bbox 作为视觉示例，返回全图相似实例。SAM 3 PCS 支持正/负框集 + 叠加 text + 阈值 refine；YOLOE visual prompt 支持多正框 + 阈值 refine，但不支持负框或文本叠加。这是一个**无状态迭代 refine 会话**：前端维护「进行中的框集 + 可选 text + 阈值」，每次操作（加框 / 拖阈值 / 改 text）重发全量，backend 按自身能力消费。
>
> - `exemplars[]`（优先）：`[{bbox:[x1,y1,x2,y2], label:bool}]` 多正负框累加。`label=true`=正框（扩召回）/ `label=false`=负框（排误检）。缺省 `exemplars` 时退化为单 `bbox` 正框（旧路径兼容）。
> - `text`：可与 `exemplars` 同时传，组合为「text 概念 + 视觉示例」；非空时返回的 `polygonlabels` 用该短语，否则用 `["object"]`（前端按当前 active label 批量改写）。
> - `score_threshold` / `output`（box/mask/both）复用通用字段。
>
> `bbox`/`exemplars[].bbox` 与 `type=interactive_box` 的 `bbox` 语义靠 `type` 区分：exemplar=全图相似，interactive_box=单框单 mask。`/setup` 在 `exemplar_capabilities`（`multi_box` / `negative_box` / `text_combination` / `threshold_refilter`）声明该 refine 能力，前端据此启用会话控件：如 `negative_box=false` 时隐藏负极性并强制正框，`text_combination=false` 时隐藏叠加文本。缺字段按全支持处理，兼容旧 SAM 3 backend。apps/api 仅在项目挂了支持 exemplar 的 backend（`/setup.supported_prompts` 含 `exemplar`）时才放行；未挂返回 400。前端 UI 入口在工作台 exemplar 工具拖框（Alt 拖框或负极性 = 负框）。

> **`type=video_tracker`**：由 `VideoTrackerJob` worker 使用。平台按模型窗口配置拆分长区间，并从项目已启用 backend 中按 `/setup.supported_trackers` 选择能力匹配项；项目主后端支持该 tracker 时优先，否则选择其它 connected 匹配 backend。请求 `task.file_path` 是视频 signed URL；`context` 包含 `model_key`（`sam2_video` / `sam3_video` / `sam3_video_interactive`）、`job_id`、`dataset_item_id`、`annotation_id`、`from_frame`、`to_frame`、`direction`、`prompt`、`source_geometry` 和种子驱动模型使用的 `seeds`。其中 `sam3_video_interactive` 是 SAM3 的 **PVS 交互追踪**（点/框 seed + 跨帧 memory），与 `sam2_video` 同为 caller 指定 obj_id 的种子驱动多目标；`sam3_video` 则是 multiplex 文本开集检测。响应 `result[]` 每项为 `{ frame_index, geometry, confidence?, outside?, instance_id?, primary? }`；低于平台阈值的 `confidence` 标为 outside。`instance_id` / `primary` 用于 job 内多目标身份，单目标 backend 可整体省略。
>
> 落地细节：
>
> - **输出几何**：`context.output_geometry` 受控取 `bbox / polygon / mask`。`mask` 返回 `{type:"mask", rle:{encoding:"coco_rle", size:[h,w], counts:[...]}}`；counts 使用 COCO column-major runs，平台会校验并转换为内容寻址引用。空 mask 用 `outside=true`，不能返回全零 bbox 冒充对象。
> - **真实推理（gsam2）**：backend 用 `build_sam2_video_predictor` + `SAM2VideoPredictor`（带跨帧 memory bank 的有状态预测，非循环调图片接口），按 `output_geometry` 直接返回 mask、polygon 或外接 bbox。视频解码用容器内 opencv 抽窗内帧到临时 JPEG 目录喂 `init_state`。`confidence` 非空 mask 记 1.0、空 mask（outside）记 0.0。
> - **独立显存池**：video predictor 用独立的 `VideoPool`（按 `sam_variant` 分桶），与图片 `ModelPool` 显存预算分离、互不驱逐，按 job 结束释放会话状态。遵循 [ADR-0012](../adr/archive/0012-sam-backend-as-independent-gpu-service)，predictor 不入 `apps/api`。
> - **`sam_variant`**：请求链路可传——AI 追踪面板为 SAM2 选尺寸 → `VideoTrackerPropagateRequest.sam_variant` → 存入 `job.prompt` → `TrackerContext` → adapter 在 `context.model_variants.sam_variant` 透传；缺省（未选）时 backend 回退默认 tiny。backend `/predict` video_tracker 分支按 `context.model_variants.sam_variant` 从 `VideoPool` 取对应尺寸 tracker。
> - **种子输入 `context.seeds[]`（点 / 框 / 多目标）**：`sam2_video` 与 `sam3_video_interactive` 接受完整点 / 框种子——每条可用 `{obj_id?, bbox?, points?}` 单帧简写或 `{obj_id, prompts:[{frame_index, points?, bbox?}, ...]}` 多帧写法。`sam3_video` multiplex 消费每条 seed 的 `bbox` 或可转成外接框的 `geometry`，在新分窗的种子帧把这些框作为正提示与 `text` 组合；不消费点或多帧纠偏。坐标归一化到 [0,1]，点 `label` 1=正点 / 0=负点，缺 `obj_id` 按序补 1..N。backend 应优先显式 seeds，缺省时才用 `source_geometry` 单框兜底。平台从 `job.prompt.seeds` 读取并经 `TrackerContext.seeds` 透传；首窗下发原始提示，后续窗按各实例续种。
>   - **多帧 prompt（中途纠偏）**：seed 还可写成 `{obj_id, prompts:[{frame_index, points?/bbox?/mask_prompt?}, ...]}`——`frame_index` 是**绝对源帧**，各 prompt 在其（局部）帧对该 obj 播种，PVS memory 逐帧累积。`mask_prompt` 是经共享有界 schema 验证的内联 COCO RLE，尺寸必须与视频帧一致，并直接调用 PVS `add_new_mask`；Multiplex 尚不声明 Mask seed。`prompts` 优先于单帧 `bbox`/`points`/`mask_prompt`，窗外绝对帧会稳定拒绝，不再静默 clamp 到边界帧。
>   - **Mask 纠错帧**：平台先用 `If-Match` 把当前帧单独保存为 `source=manual` 的 RLE 关键帧，再创建纠错作业。创建请求显式绑定 backend id、model id / tracker key、源 annotation version、RLE digest、segment lease、窗口和方向；同一轨迹同时只能有一个活跃纠错作业。原生路径使用共享 `CorrectionFramePrompt` 并且每个方向必须放入一个单窗；只有目标 model 同时声明 `video + bbox_prompt + mask output` 时才可由用户显式确认 bbox fallback。纠错帧从结果流中排除，其余结果只暂存待审。
> - **跨窗续追（平台侧）**：首窗用原始 keyframe 或 `seeds[]`；后续窗为**每个实例**取上一窗最后一个非 outside geometry，并用相同 obj id 重新播种，避免非主实例过窗丢失。只有单实例时继续用 `source_geometry` 兜底。
> - **多目标身份与落库（平台侧）**：每条结果可带 job 作用域的 `instance_id` 与 `primary`。种子驱动模型直接使用 caller 指定 obj id；文本 multiplex 每窗独立检测，平台在窗口边界帧按 IoU 把局部 id 映射为全局稳定 id。`instance_id` 不是数据库 `group_id`，也不等于最终 `track_id`。用户接受 job 后，主实例回填源 annotation（保留其 `track_id`），其余 `instance_id` 各创建一条继承源类别的新 annotation 与新 `track_id`；无 instance id 的老 backend 继续走单实例路径。
> - **局部审核与并发（平台侧）**：preview 为每条 staged result 派生稳定 `candidate_key` 和 geometry digest，并返回 job revision、源 annotation versions、人工帧保护标记与待决计数。客户端通过 decision API 显式提交 `instance_ids + from_frame + to_frame + accept|reject`；第一次局部决定后 job 进入 `partially_reviewed`，只移除已决集合。服务端在事务内重新锁定 job、task、segment 与源 annotation，复核 revision、assignment、segment lease、annotation lock 和源版本；同一候选的相同决定幂等回放，相反决定或旧 revision 返回 409。人工关键帧默认返回 `manual_keyframe_protected`，只有显式 `override_manual=true` 才覆盖并记录审计。未决 Mask 引用继续参与引用扫描与 GC。
> - **只回填网格帧（平台侧）**：采样开启时 `apply_tracker_results` 按项目 `derive_step` 只持久化 `frame_index % step == 0` 的预测帧（tracker 仍逐源帧跑，off-grid 帧丢弃），与导航 / 导出网格一致。
>
> **能力声明（`/setup`）**：支持 video tracker 的 backend 在 `/setup` 返回 `supported_trackers: ["sam2_video", ...]`，平台动态消费并用于模态校验。每个 tracker model 还要按真实 consumer 声明 `supported_prompts` / `supported_inputs` / `supported_geometric_outputs` 和正数 `max_window_frames`；平台对外暴露 backend 上限与平台配置的较小值。**文本驱动的 tracker**（如 `sam3_video`，其 propagate 需 `text` / `exemplars` 而非仅 seed bbox）在此之外再声明 `text_driven_trackers`（`supported_trackers` 的子集）：前端仅在选中此类 tracker 时才显示「文本描述」输入框并强制填写；模型选择器只列出项目已启用、已连接且能力可达的 backend 所声明的 tracker。组合模型的全部原子能力必须由同一个 backend 提供。
>
> **视频单帧交互式（平台中继）**：视频工作台在**单帧**上用智能点 / 框时，走平台端点 `POST /projects/{pid}/ml-backends/{bid}/interactive-annotating-frame`（multipart：`frame` JPEG + `task_id` + `frame_index` + `context` JSON 串）——它先把当前帧上传对象存储，再以该帧图片 URL 调 backend 的 `predict_interactive`。**backend 无需为视频做任何特殊处理**，看到的就是一次普通的图片交互式预测；`frame_index` 仅用于存储 key 命名、不参与坐标变换，候选瞬态返回、不落库（区别于走批量 `/predict` 会落 Prediction 的 `predict-frame`）。
>
> **观测（`/health` + `/metrics`）**：开了 video 独立池的 backend，`/health` 返回 `video_pool` 区块（PoolStatus：`cap/current_size/loaded_keys/last_evict`，外加 `active_sessions/idle_seconds`）；`/metrics` 增 `video_tracker_frames_processed_total{sam_variant}` / `video_tracker_latency_seconds{sam_variant}`，并对推理指标加 `task_type="image|video"` 维度。模型市场观测页据此按图像 / 视频分类。

> **`output: "box" | "mask" | "both"`**（仅 `type=text` 生效）：
>
> - `box`：仅 GroundingDINO 出框，跳过 SAM image embedding + mask 推理 + cv2/shapely 简化。返回 `result[]` 全为 `rectanglelabels`，单图 ~50-100ms（4060 / tiny），相比 mask 全链路 200-500ms 快 50-80%。**适用 image-det 项目**：标注员要的就是 bbox annotation。
> - `mask`（**默认**）：DINO + SAM mask → polygon，返回 `polygonlabels`。
> - `both`：同 instance 配对返回 `[rectanglelabels, polygonlabels, ...]` 严格交错（box 优先，对应 polygon 在后）。前端 `Tab` 切活跃几何，`Enter` 接受当前形态。
> - **老 backend 兼容**：缺 `output` 字段时按 `"mask"` 路径返回，零回归。
> - **老前端兼容**：不识别 `rectanglelabels` 候选时只显示 `polygonlabels`。
> - **point/interactive_box/polygon 类型**：`output` 字段无意义，始终走 SAM mask → polygon。

> **`simplify_tolerance: number`**（可选；缺省走 backend 默认 1.0）：
>
> - 像素级 shapely.simplify 容差。**大物体 / 大致形状** 调高（2-3）减顶点、提速；**精细物体** 调低（0.3-0.5）保细节。
> - 仅 `output ∈ {"mask", "both"}` 路径生效；`output="box"` 不简化。
> - 单次请求级覆盖；项目级常量化未实现（运维 / dev 通过 `Context.simplify_tolerance` 注入足够，未来可加 ProjectSettings 字段，触发条件：客户提需求）。
> - 后端在返回 polygon 顶点 > 200 时 `logger.warning`（非阻塞，仅运维信号）。

**响应**：单条 `PredictionResult`，**没有外层 `results` 数组**：

```json
{
  "result": [<annotation>, ...],
  "score": 0.85,
  "model_version": "sam-vit-h",
  "inference_time_ms": 180,
  "mask_input_next": "<base64>"            // 可选; 仅 point 单 mask 精修阶段非空, 前端原样回带 (见上 mask_input)
}
```

### 2.2.1 交互式 Tracker Context Session

交互式视频 model 可在 `/setup.models[]` 声明：

```json
{
  "supported_trackers": ["sam2_video"],
  "max_window_frames": 300,
  "tracker_context_mode": "session",
  "max_context_frames": 1200
}
```

`tracker_context_mode` 只允许 `none` 或 `session`。只有 `max_context_frames` 大于单窗上限、backend 能在
session 生命周期内保留同一 predictor state 时才声明 `session`；否则声明 `none` 或省略。平台会把长 job 切成
不超过 context 上限的 span，同一 span 的请求依次携带：

```jsonc
{
  "context": {
    "type": "video_tracker",
    "tracker_context": {
      "action": "start", // 后续为 continue，finally 为 close
      "job_id": "<tracker-job-uuid>",
      "direction": "forward",
      "span_start_frame": 0, // 仅 start 必填
      "span_end_frame": 1199, // 仅 start 必填
    },
  },
}
```

`start` 不得携带 token；backend 在 `PredictionResult.meta.tracker_context.token` 返回不可解析的不透明 token。
`continue` 与 `close` 必须原样回传 token，且 token 要绑定 job、方向、模型变体和当前 backend 进程。正常结束、取消、
失败、空闲 TTL 或进程 shutdown 都必须释放 predictor reservation 与临时帧状态。token 不得写日志或持久化。

token 不存在、绑定不匹配或 state 已释放时返回 `409` 和 `{ "reason": "tracker_context_lost" }`。平台必须使整个
tracker job 失败并从 job 起点重试，不能在同一个结果中静默回退到逐窗 seed continuation。`none` backend 和 context
span 边界继续使用上一窗末帧 geometry 续种。

### 2.3 检测式视频追踪（批量 `/predict`）

**与 §2.2 的 `type=video_tracker` 是两条不同的链**。§2.2 是「人在环、单对象、种子传播」的交互式追踪（SAM2/SAM3，`predict_interactive`，平台分窗续追）；这里的**检测式追踪**（detect-then-track）是「无种子、多对象、全自动、离线批量」：检测器逐帧出框 + 内建关联算法（ByteTrack / BoT-SORT），**时间关联全在 backend 内**，平台只投整段视频、收已聚合的轨迹，自己不做任何时间编排。它走**标准批量 `/predict`**（复数 `tasks` wire），不进交互式那条链。

**请求**：`context.type="tracker"`，`task.file_path` 是整段视频（presigned URL 或本地路径，backend 内部解帧）：

```json
{
  "tasks": [{ "id": "v1", "file_path": "https://.../clip.mp4" }],
  "context": {
    "type": "tracker",
    "model_variants": { "series": "yolo11", "size": "s" },
    "params": { "conf": 0.35, "iou": 0.7, "tracker": "bytetrack" },
    "classes": [2] // 可选类别白名单 (模型原生 index)
  }
}
```

- `params.tracker` 从 `/setup.models[].supported_trackers` 选定（缺省取首项）；enum 约束到 backend 内建的 tracker 配置（yolo：`bytetrack` / `botsort`）。追踪算法是 **param**（apply-time 选、不换权重），不是 variant 轴。
- `supported_inputs=["video"]`：检测式追踪**只接受视频**——单帧图像无跨帧状态、产不出有意义的 `track_id`。

**响应**：`result[]` 每项是一条**已聚合好的轨迹**（backend 已 stream 整段视频、按原生 track id 聚合，平台不再聚合），`type="video_track_bbox"`：

```jsonc
{
  "type": "video_track_bbox",
  "track_id": 3, // backend 原生 int; 平台 ingestion 映射成 trk_<uuid>
  "class_name": "car",
  "score": 0.87, // 轨迹级 (帧置信度均值)
  "keyframes": [
    // bbox 用 {x,y,w,h}、直接 0-1 归一化 (不发百分比、平台不做百分比自动探测)
    { "frame_index": 0, "bbox": { "x": 0.1, "y": 0.2, "w": 0.08, "h": 0.06 }, "score": 0.9 },
    { "frame_index": 1, "bbox": { "x": 0.11, "y": 0.21, "w": 0.08, "h": 0.06 }, "score": 0.88 },
    // 某帧无 track id (低置信) 直接不出该帧关键帧
  ],
}
```

平台把它落成 `VideoTrackGeometry` 预标注（每帧 `source="prediction"`），视频工作台按轨迹渲染、人工审核接受。**首版限制**：单次整段追踪（不分窗），帧数超上限（yolo `YOLO_TRACKER_MAX_FRAMES`，默认 900）截断并 `log`（不静默丢）；长超时须配 tracker 专属超时 + 独立 queue / 限并发（长视频整段追踪独占 worker slot）。

---

## 3. `result` 字段 — 标注 schema

`result` 是一个 annotation 对象数组，与 Label Studio 风格兼容。每项至少包含：

```json
{
  "type": "rectanglelabels" | "polygonlabels" | "polylinelabels" | "keypointlabels",
  "value": {
    // type=rectanglelabels：Label Studio 百分比 [0,100]，平台也兼容 [0,1]
    "x": 0.12, "y": 0.34, "width": 0.45, "height": 0.20,
    "rotation": 30,                         // 可选; 存在时读为 rotated_bbox
    "rectanglelabels": ["car"],

    // type=polygonlabels
    "points": [[x, y], ...],   // 同样兼容 [0,100] 与 [0,1]
    "polygonlabels": ["road"],

    // type=polylinelabels
    "points": [[x, y], ...],
    "polylinelabels": ["lane"],

    // type=keypointlabels
    "points": [{"x": 10, "y": 20, "v": 2}, {"x": 30, "y": 40, "v": 1}],
    "keypointlabels": ["person"]
  },
  "score": 0.91                // 单框置信度，可与外层 score 并存
}
```

平台不强校验 schema。图片工作台当前渲染 `rectanglelabels` / `polygonlabels` / `polylinelabels` / `keypointlabels`；`rectanglelabels.value.rotation` 存在时会按旋转框读取，缺失时仍按普通 bbox 读取。`keypointlabels.value.points[]` 使用 `{x,y,v}`，`v` 保留 COCO 可见性 0/1/2。

---

## 4. `GET /setup`

**用途**：自描述 backend 能力，前端 `useMLCapabilities` hook 据此决定哪些 AI 工具可用、参数面板渲染哪些字段。

> 协议背后的架构决策：[ADR-0020 — ML Backend Capability 协商协议](../adr/archive/0020-ml-backend-capability-negotiation.md)。该 ADR 解释了为什么 `params` 限制为 Draft-07 子集、为什么走 apps/api 代理而非前端直连。

**响应**：JSON Schema 自描述协议。**必填**三元组：`name` / `version` / `model_version`；`supported_prompts` 决定 ToolDock 工具置灰；`params` 是 JSON Schema (Draft-07 子集)，前端 schema-form 自动渲染；`supported_variants` 可选，用于给变体选择器补充显存 / 档位 / 推荐等富元数据。

```jsonc
{
  "name": "sam3-backend", // 必填. backend 标识
  "version": "0.10.1", // 必填. backend 镜像/代码版本
  "protocol_version": "2.1", // v0.14.15 起推荐；缺省按 2.0/legacy 兼容
  "compat_protocol_versions": ["2.0"], // 本 backend 仍接受的旧 minor 版本
  "model_version": "sam3", // 必填. 实际加载的模型 ckpt 版本
  "is_interactive": true,
  "labels": [], // 可选. backend 已知类别 hint
  "supported_prompts": ["point", "interactive_box", "text", "exemplar"], // sam3 开 inst_interactivity 后: point/interactive_box (SAM-style 单实例) + exemplar (PCS 找相似) + text
  "supported_text_outputs": ["box", "mask", "both"],
  "supported_variants": [
    {
      "key": "sam_variant",
      "title": "SAM 2 变体",
      "variants": [
        { "value": "tiny", "label": "SAM 2.1 Tiny", "vram_gb": 1.5, "tier": "fast" },
        {
          "value": "small",
          "label": "SAM 2.1 Small",
          "vram_gb": 2.5,
          "tier": "balanced",
          "recommended": true,
        },
      ],
    },
  ],
  "params": {
    "type": "object",
    "properties": {
      "box_threshold": {
        "type": "number",
        "minimum": 0,
        "maximum": 1,
        "default": 0.35,
        "title": "Box 置信度阈值",
        "x-platform-role": "confidence",
      },
      "sam_variant": {
        "type": "string",
        "enum": ["tiny", "small", "base_plus", "large"],
        "default": "tiny",
        "title": "SAM 2 变体",
        "x-platform-role": "modelVariant",
      },
    },
  },
}
```

> **变体 `modelVariant` 语义**：v2.1 起变体选择由 `/setup.supported_variants[].key` 声明，前端发送 `/predict` 时统一放进 `context.model_variants`（详见 §2.2）。如果老 backend 只在 `params.properties.*` 里暴露 enum，则应给该字段标 `x-platform-role: "modelVariant"`；前端会把它移出普通参数表单，渲染到模型变体选择器。
>
> **`supported_variants`（可选）**：用于声明 model variant 轴并补富元数据。结构为数组，每项代表一个轴：`{ key, title?, description?, variants: [{ value, label?, vram_gb?, tier?, recommended?, note? }] }`。`key` 是 `context.model_variants` 的 axis key；`value` 必须与 backend runtime 校验来源一致。前端优先读 `supported_variants` 渲染富选择器，缺失或为空时回落 `params` 中 `x-platform-role=modelVariant` 或 legacy `*_variant.enum` 字段，因此老 backend 不需要立即升级。`tier` 建议使用 `fast | balanced | accurate`，但前端会容忍未知字符串。
>
> 超管运行时观测端点 `GET /admin/ml-integrations/observe` 会把 `/setup.supported_variants` 透传到每个 `ObserveTarget.supported_variants`，用于未注册观测容器的只读多轴变体展示。顶层未声明时，平台合并 `models[].supported_variants` 并按 axis key 去重；旧 grounded-sam2/sam3 的 `sam_variant` / `dino_variant` enum 仍通过 `variant_catalog` 双发。仅声明通用变体目录的容器暂不启用「试启动」，直到 backend 实现通用 warm 接口。

> **`supported_prompts`**：枚举 `point | interactive_box | text | exemplar | sketch | scribble | mask | correction_frame | …`。**交互式用户 prompt**，前端 ToolDock 据此置灰不支持的工具。`correction_frame` 是视频局部纠错种子，不进入单帧图片工具路由。（`bbox` 已退出交互 prompt 命名空间，仅保留为几何形状名。）
>
> **`supported_inputs`**（一等输入契约）：枚举 `full_image | crop | bbox_prompt | point_prompt | mask_prompt | scribble_prompt | video`，声明本 model 能吃哪些**投递形态**，与 `supported_prompts`（交互 prompt）**解耦**——纯分类器 `supported_prompts=[]` 但 `supported_inputs=["full_image","crop"]`；box-seg `supported_inputs=["bbox_prompt","full_image"]`；视频 tracker 必须显式含 `video`。多阶段编排据此判定父子可达性（产几何的子须含 `bbox_prompt` 或 `crop`）并选择投递方式（见 §2.1.1 判别器）；模型市场「可接受输入」行也由它驱动。**老 backend 缺字段时平台合成兼容默认**：含 `interactive_box`（或历史 `bbox`）prompt → 加 `bbox_prompt`；含 `point` → 加 `point_prompt`；含 `mask` / `correction_frame` → 加 `mask_prompt`；含 `scribble` → 加 `scribble_prompt`；任何模型都含 `full_image`，非交互模型额外含 `crop`。平台不会替老 backend 猜测 `video`（见 services/ml_capabilities.py `_synthesize_supported_inputs`）。
>
> **`supported_text_outputs`**：text 路径支持的 `Context.output` 取值。
>
> **`params` JSON Schema**：当前前端消费的最小类型集 `number | integer | string (含 enum) | boolean`；`readOnly: true` 字段在 UI 上展示但不可改。
>
> **`x-platform-role`（v2.1）**：`params.properties.*` 可用该扩展字段声明平台语义，前端用统一标签渲染但仍按原字段名发送。当前受控值：`confidence` / `iou` / `maxDet` / `textThreshold` / `simplifyTolerance` / `modelVariant`。本版**不统一物理参数名**：yolo 仍用 `conf/iou/max_det`，grounded-sam2 仍用 `box_threshold/text_threshold/simplify_tolerance`，sam3 仍用 `score_threshold/simplify_tolerance`。

**平台代理端点**：前端通过 `GET /api/v1/projects/{id}/ml-backends/{bid}/setup` 拉取；apps/api 30s TTL 进程内缓存，update/delete backend 时自动 invalidate。

**前端兜底**：返回体缺 `supported_prompts` 时前端回落 `["point","interactive_box","text"]` 并 `console.warn` 提示升级 backend。`/setup` 502 时整套 AI 工具置灰。

> **能力快照持久化**：除上述代理端点的实时拉取外，平台在 `check_health`（services/ml_backend.py）拉完 `/health` 后会 best-effort 再探一次 `/setup`，把能力快照（`models[]`、`supported_prompts`、`supported_inputs`、`supported_trackers`、`supported_text_outputs`、`supported_geometric_outputs`、`warnings` + 平台派生的 `modalities`）落进 `ml_backend_registry.health_meta["capabilities"]`，供「按模态分流 / 绑定校验 / 列表只读展示」消费（无需每处实时拉 `/setup`）。模态派生规则：`supported_prompts` 非空 ⇒ image、`supported_trackers` 非空 ⇒ video（见 services/ml_capabilities.py）。
>
> **`is_interactive` 改派生**：`is_interactive` 不再由注册表单手填，而是以 backend `/setup.is_interactive` 自报为真值，在 `check_health` 时回写 `MLBackend.is_interactive`。backend 必须在 `/setup` 如实声明该位。
>
> **绑定按 data_type 校验**：`PATCH /projects/{id}` 绑定 backend 时实时探 `/setup` 派生模态，与项目 `data_type` 不兼容 → 422；探测失败则 fail-open 放行（mismatch 留到 `/predict` 暴露）。

---

### 4.1 能力声明协议 v2（多模型目录 + infra）

> 协议背后的架构决策：[ADR-0036 — ML Backend 能力声明协议 v2（多模型目录 + infra）](../adr/archive/0036-ml-backend-capability-protocol-v2-multi-model.md)。

§4 描述的 `/setup` 是**单模型快照**形态——隐含「1 个 backend ≈ 1 个模型族」。协议 v2 在**完全向后兼容**前提下，把能力声明下沉到 **model 粒度**，让一个 backend 暴露一份 model list（一个 backend = N 个 model），每个 model 自带能力 + infra + variants。典型消费者是未来的 **YOLO 官仓 backend**（一个仓覆盖 det/seg/pose/obb/cls × series × size）与 **ONNX 聚合 backend**（一个进程聚合检测 / 关键点 / OCR / 抠图等异构模型）。

`/setup` 仍是能力的唯一真相源（SoT），目录是其派生缓存 + UI 视图。`infra` / `models[]` 只影响能力声明与目录展示，**不改 `/predict` 请求/响应 schema**。

#### 4.1.1 `/setup` 顶层结构（v2）

```jsonc
{
  // ── 必填三元组（协议 v1 已有，不变）──
  "name": "yolo-ultralytics-backend",
  "version": "0.1.0", // backend 镜像/代码版本
  "protocol_version": "2.1", // v0.14.15: model_variants + x-platform-role + 422/503 错误模型
  "compat_protocol_versions": ["2.0"],
  "model_version": "ultralytics-8.3.x",

  // ── v2 新增 ──
  "infra": "pytorch", // backend 默认基础设施；可被 model.infra 覆盖
  "warmup_endpoint": true, // v0.14.14: 声明本 backend 支持 POST /warmup（详见 §4.4）
  "models": [
    /* §4.1.2 model 条目数组 */
  ],

  // ── v1 顶层字段：仍可用，作为「隐式单 model」的兜底（§4.1.5）──
  "is_interactive": false,
  "supported_prompts": [],
  "supported_geometric_outputs": [],
  "supported_variants": [],
  "params": {},
}
```

- `protocol_version="2.1"` ⇒ backend 支持 `context.model_variants`、`x-platform-role` 与标准 422/503 错误模型。`compat_protocol_versions` 用于声明仍接受的旧 minor 版本；v0.14.15 backend 填 `["2.0"]`。
- `protocol_version="2.2"` ⇒ 在 2.1 基础上额外支持 model 条目 `composition`（§4.1.3）与几何 prompt 批量入参 `tasks[].prompts[]`（§2.1.1）。两者均纯加法，2.2 backend 应填 `compat_protocol_versions: ["2.1", "2.0"]`；平台对缺这两项的 2.1/2.0 backend 完全兼容（composition 回落 atom、下游走 crop 模式）。
- `models[]` 存在 ⇒ 平台按多模型目录解析，**忽略顶层能力字段**（顶层仅留 name/version/protocol_version/infra/warmup_endpoint 等 backend 级元数据）。
- `models[]` 缺省 ⇒ 平台用顶层字段合成一个隐式 model（老 backend 路径，§4.1.5）。

#### 4.1.2 model 条目结构

```jsonc
{
  "id": "detect", // 必填. backend 内唯一,(backend_id,id) 构成目录主键
  "display_name": "YOLO 目标检测", // UI 展示名
  "task": "detection", // 必填. 受控词表,条目边界,决定输出几何与项目兼容性
  "model_family": "yolo", // 可选. 家族标签(yolo/sam/paddleocr…),UI 二级分组用
  "infra": "pytorch", // 可选. 缺省继承 backend.infra
  "is_interactive": false, // 该 model 是否支持交互式 /predict
  "composition": "atom", // 可选. atom=单次推理原子; composite=内部编排多原子. 缺省 atom

  "supported_prompts": ["none"], // 受控. none = 纯批量,无交互 prompt
  "supported_inputs": ["full_image", "crop"], // 可选. full_image/crop/bbox_prompt/point_prompt; 缺省由平台合成
  "supported_geometric_outputs": ["bbox"], // 受控,复用现有字段名
  "output_attribute_types": [], // 受控. OCR: ["text","language"]; cls: ["class"]
  "output_attribute_schema": [], // 可选. 属性 key/label/type/options 的结构化声明
  "supported_text_outputs": [], // v1 已有,text 路径专用(box/mask/both)
  "supported_trackers": [], // v1 已有,video tracker 专用

  "supported_variants": [
    /* series/size 多轴,§4.1.6 */
  ],
  "variant_combinations": [
    /* 可选,§4.1.6: 多轴非真笛卡尔积时显式列举合法组合 */
  ],
  "variants_shared_across_tasks": false /* 可选,§4.1.6: True 表同 backend 内多 task 共享同一份物理权重 */,
  "default_variants": {
    "series": "yolo11",
    "size": "s",
  } /* 可选,§4.1.6: backend 自报该 model 默认 variant 组合 */,
  "default_thresholds": { "conf": 0.25, "iou": 0.7 },
  "resource_profile": { "device": "gpu", "batchable": true },
  "params": {
    /* 该 model 专属 JSON Schema(Draft-07 子集),前端 schema-form 渲染 */
  },
}
```

#### 4.1.3 受控词表（capability vocabulary）

能力声明的关键是**一套受控枚举**，且与平台内部类型锚点对齐（`TOOL_UNIT_IDS` / LabelStudio result type / `data_type`）。

**`task`（任务能力，条目边界，必填）** —— 项目兼容性校验的主轴；`model_family` 仅作展示分组，不参与校验：

| `task`            | 输出几何                        | 对应 result type                   | 备注                            |
| ----------------- | ------------------------------- | ---------------------------------- | ------------------------------- |
| `detection`       | `bbox`                          | `rectanglelabels`                  |                                 |
| `obb`             | `rotated_bbox`                  | `rectanglelabels`（带 `rotation`） | 与 detection 输出几何不同，单列 |
| `segmentation`    | `polygon`                       | `polygonlabels`                    | 实例/语义分割统一 polygon 落地  |
| `keypoint`        | `keypoint`                      | `keypointlabels`                   | pose / 关键点                   |
| `classification`  | `none`                          | 无几何，写 attribute               | 整图/区域分类                   |
| `ocr`             | `bbox`/`rotated_bbox`/`polygon` | 对应几何 + `attributes.text`       | §4.1.8                          |
| `doc_layout`      | `bbox`/`polygon`                | 对应几何，class=版面类别           | §4.1.8                          |
| `tracker`         | per-frame geometry              | （video tracker 协议，§2.2）       | 模态=video                      |
| `interactive_seg` | `polygon`/`mask`                | `polygonlabels`                    | SAM 类，prompt 驱动             |

**`supported_geometric_outputs`（几何输出，复用现有字段）** —— 枚举与 `TOOL_UNIT_IDS` 对齐：

`bbox` / `rotated_bbox` / `polygon` / `polyline` / `keypoint` / `mask` / `none`。
（3D 的 `lidar_box_3d` / `point_mask_3d` 暂不在本版 backend 范围，留位。）

**`output_attribute_types`（属性输出，半开放）** —— `text`（OCR 文本） / `language` / `orientation` / `class`（分类标签）。其余按需扩展；layout 版面类别走 `class_name`（而非 attribute）。平台消费：画布对 `text` / `language` / `orientation` 校验项目是否有承接位（缺则非阻断警告「采纳后该属性丢失」，`class` 因 taxonomy 几乎恒在而跳过）；编排分类下游阶段若模型自报此字段却不含 `class`，派发期 422（见 §2.1.1）。

**`output_attribute_schema`（结构化属性输出）** —— 可选数组，每项至少含 `key / label / type`，`type` 取 `text / number / boolean / select / multiselect / range`。`select` / `multiselect` 的 `options` 必须是 `[{"value":"car","label":"小车"}]` 对象数组，不能写成字符串数组；`value` 必须与 `/predict` 实际写入 `attributes[key]` 的值一致。平台用该 schema 给项目设置“从 ML Backend 预填属性”、编排写回键选择器和工作台属性兼容性检查提供真值；缺失时回退 `output_attribute_types` 的扁平提示。

**`infra`（基础设施，受控，v2 新增）** —— `pytorch` / `onnx` / `paddle` / `tensorrt` / `openvino` / `other`（兜底）：

- **层级**：backend 顶层声明默认值；model 条目可覆盖（如 YOLO 仓里部分条目导出 onnx）。
- **缺省**：老 backend 不报 `infra` ⇒ 缓存标 `unknown`，UI 不渲染 badge 或显示「未声明」。
- **边界**：`infra` 是纯元数据 —— 不改 `/predict` 协议、不影响 result schema、不参与项目兼容性的硬校验（仅展示 badge + 排障溯源）。

**`supported_prompts`（prompt 受控词表）** —— `none`（纯批量，无交互） / `point` / `interactive_box` / `text` / `exemplar` / `sketch` / `scribble` / `mask` / `correction_frame` / `bbox`（退役兼容）。YOLO / OCR / layout 闭集模型通常是 `["none"]`（批量自动）；SAM 类按真实实现声明 point/interactive_box/text/exemplar/mask/scribble；视频局部纠错使用 `correction_frame`。`text` 需要用户输入，但不进入画布交互工具线；`bbox` 仅兼容历史快照，新增 backend 应使用 `interactive_box`。

**`supported_inputs`（投递形态受控词表）** —— `full_image` / `crop` / `bbox_prompt` / `point_prompt` / `mask_prompt` / `scribble_prompt` / `video`。它描述平台如何把上游产物交给这个 model：整图、裁剪 ROI、框提示、点提示、受控内联 Mask、正负笔迹或视频。多阶段编排只看这个字段决定父子可达性，不再从 `supported_prompts` 猜测；老 backend 缺字段时由平台按 prompt 合成兼容默认，但 `video` 必须由 backend 显式声明。

原生 Mask 交互使用 `type="mask"` 候选，`value.rle` 携带受限的非压缩 COCO RLE，`candidate_id` 绑定 canonical RLE、prompt revision 和候选序号；可选的 `value.preview.points` 只提供轻量画布轮廓，不改变像素身份。空前景返回空 `result` 与 `reason="empty_mask"`，不得用零框或空 polygon 占位。单个 RLE 限制为 4096×4096、最多 1,000,000 runs 和 4 MiB canonical JSON，整个单帧交互响应最多 16 MiB；请求 Mask 输出但目标 model 未声明 `mask` 时，平台返回 `unsupported_output_geometry`，不会静默转 polygon。Grounded-SAM2 与 SAM3 image 的 point / interactive-box 已声明 Mask 输出；SAM3 PVS 还声明 `correction_frame + mask_prompt`，Multiplex 仅声明已验证的 Mask 输出。`mask`、`scribble`、`correction_frame` 及对应输入只有在实际 consumer 路径通过契约测试后才能写入 `/setup.models[]`。

**`composition`（原子 vs 内部编排，可选）** <!-- since 协议 v2.2 --> —— `atom`（单次推理 / 单原子） / `composite`（一个 model 内部编排多个原子、一次 `/predict` 一气呵成）：

- **动机**：model 目录把「原子」与「内部编排」平铺为平级条目，`task` 只描述输出形态、与「原子/复合」正交。`composition` 把「是原子还是内部编排」做成机器可读字段，取代早期靠读 `display_name` + 经验判断的做法。
- **唯一的可见性/过滤轴**：`composition` 是平台过滤选用入口的唯一依据。**编排下游 stage 选择器只收 `atom`**（编排只组合原子，不把一锅端复合体当 stage）；单阶段 / 工作台多模型选择器不过滤，`composite` 可直接选用（开箱即用）。例：grounded-sam2 `segmentation` = `composite`（单步可选、但不作编排下游）；onnxtools 一锅端 `vehicle-attr` = `composite`（单阶段默认、不作编排下游）；`vehicle-detect` / `vehicle-attr-classify` = `atom`（可作编排上/下游）。
- **缺省**：`atom`（绝大多数 model 是单次推理；老 backend 不报字段即按原子）。平台 `extract_capabilities` 透传，缺省回落 `atom`。
- **边界**：不改 `/predict` 协议、不参与兼容性校验。消费方：模型市场据此给卡片打「原子 / 内置流程」徽标；编排下游选择器 + 属性导入源据此过滤（只取 `atom`）。

#### 4.1.4 平台派生形态（health_meta）

`extract_capabilities(setup)`（`services/ml_capabilities.py`）遍历 `models[]` 派生 model 列表，落进 `ml_backend_registry.health_meta["capabilities"]`：

```jsonc
{
  "infra": "pytorch", // backend 默认
  "models": [
    {
      "id": "detect",
      "task": "detection",
      "model_family": "yolo",
      "infra": "pytorch",
      "supported_prompts": ["none"],
      "supported_inputs": ["full_image", "crop"],
      "supported_geometric_outputs": ["bbox"],
      "output_attribute_types": [],
      "supported_variants": [
        /*…*/
      ],
      "default_thresholds": {
        /*…*/
      },
      "resource_profile": {
        /*…*/
      },
      "modality": "image",
    },
    /* … */
  ],
  // 兼容字段:老消费方仍能读到「扁平并集」(所有 model 的 prompts/geometry 去重合并)
  "supported_prompts": ["none"],
  "supported_inputs": ["full_image", "crop"],
  "supported_geometric_outputs": ["bbox", "polygon", "keypoint", "rotated_bbox"],
  "modalities": ["image"],
  "warnings": [],
}
```

保留顶层「扁平并集」字段，让现有 `useMLCapabilities` / 绑定校验在改造完成前零回归。模态派生升级为 **per-model 派生 + backend 汇总**：`task=tracker` 或 `supported_trackers` 非空 ⇒ video；几何含 `lidar_box_3d` / `point_mask_3d` ⇒ lidar（留位）；否则 ⇒ image（含 `supported_prompts=["none"]` 的批量模型）。backend `modalities` = 各 model modality 去重并集。

`warnings` 是非阻断诊断列表，由平台校验规范化后的 `task` / `infra` / `supported_prompts` / `supported_geometric_outputs` 是否落在受控词表内后生成。每条包含 `{level, model_id, field, value, message}`。模型市场用它显示 `⚠ 协议 N`，帮助接入方发现字段拼写或枚举漂移；平台不会因此丢弃该 model。

#### 4.1.5 向后兼容规则

| backend 形态                  | 平台解析                                                                                                                                                                                                                                               |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 无 `models[]`                 | 顶层 `supported_*` / `params` 合成 1 个隐式 model：`id="default"`，`task` 由现有信号推断（`supported_trackers` 非空 → `tracker`、`supported_prompts` 含 point/interactive_box/text/exemplar → `interactive_seg`、否则 `detection`），`infra="unknown"` |
| 无 `infra`                    | model.infra = backend.infra = `"unknown"`                                                                                                                                                                                                              |
| 有 `models[]` 但条目缺 `task` | 该条目按 `unknown` task 入目录，UI 标「能力未声明」，兼容性校验 fail-open（放行，留到 `/predict` 暴露）                                                                                                                                                |

老 backend（grounded-sam2 / sam3 / echo）**不需要任何改动**即可继续工作 —— 它们落到「隐式单 model」路径。

#### 4.1.6 范例：YOLO 官仓 backend（按任务分条目 + series/size 多轴）

> **真实参考实现**：[`apps/yolo-backend/`](https://github.com/yyq19990828/ai-annotation-platform/tree/main/apps/yolo-backend)（v0.14.12 起）。
> 实仓覆盖 4 task × 7 series × 9 size 的 80 个有效组合，与下方 jsonc 示例完全对齐；本节 jsonc 是缩写说明，实仓 `/setup` 输出是 canonical。
> 注意实仓在 `models[].task` 维度去掉了 `classify`（v0.14.12 NG1：ImageNet-1k 与项目 LabelConfig 几乎对不上），下方示例保留 classify 仅作协议形态说明。

```jsonc
{
  "name": "yolo-backend", // 与 apps/yolo-backend 实仓对齐
  "version": "0.1.0",
  "model_version": "ultralytics-8.4.x",
  "infra": "pytorch",
  "is_interactive": false,
  "models": [
    {
      "id": "detect",
      "display_name": "YOLO 目标检测",
      "task": "detection",
      "model_family": "yolo",
      "supported_prompts": ["none"],
      "supported_geometric_outputs": ["bbox"],
      "supported_variants": [
        {
          "key": "series",
          "title": "版本系列",
          "variants": [
            { "value": "yolov8", "label": "YOLOv8" },
            { "value": "yolo11", "label": "YOLO11", "recommended": true },
            { "value": "yolo12", "label": "YOLO12" },
          ],
        },
        {
          "key": "size",
          "title": "尺寸 / 精度档",
          "variants": [
            { "value": "n", "label": "nano", "vram_gb": 1, "tier": "fast" },
            {
              "value": "s",
              "label": "small",
              "vram_gb": 2,
              "tier": "balanced",
              "recommended": true,
            },
            { "value": "m", "label": "medium", "vram_gb": 4 },
            { "value": "l", "label": "large", "vram_gb": 6 },
            { "value": "x", "label": "xlarge", "vram_gb": 8, "tier": "accurate" },
          ],
        },
      ],
      "default_thresholds": { "conf": 0.25, "iou": 0.7 },
      "resource_profile": { "device": "gpu", "batchable": true },
      "params": {
        "type": "object",
        "properties": {
          "conf": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "default": 0.25,
            "title": "置信度阈值",
            "x-platform-role": "confidence",
          },
          "iou": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "default": 0.7,
            "title": "NMS IoU",
            "x-platform-role": "iou",
          },
          "max_det": {
            "type": "integer",
            "minimum": 1,
            "maximum": 300,
            "default": 100,
            "x-platform-role": "maxDet",
          },
          "series": {
            "type": "string",
            "enum": ["yolov8", "yolo11", "yolo12"],
            "default": "yolo11",
          },
          "size": { "type": "string", "enum": ["n", "s", "m", "l", "x"], "default": "s" },
        },
      },
    },
    {
      "id": "segment",
      "task": "segmentation",
      "supported_geometric_outputs": ["polygon"],
      "model_family": "yolo",
      "/* variants 同上 */": null,
    },
    {
      "id": "pose",
      "task": "keypoint",
      "supported_geometric_outputs": ["keypoint"],
      "model_family": "yolo",
      "/* … */": null,
    },
    {
      "id": "obb",
      "task": "obb",
      "supported_geometric_outputs": ["rotated_bbox"],
      "model_family": "yolo",
      "/* … */": null,
    },
    {
      "id": "classify",
      "task": "classification",
      "supported_geometric_outputs": ["none"],
      "output_attribute_types": ["class"],
      "model_family": "yolo",
      "/* … */": null,
    },
  ],
}
```

> **关键红利**：det/seg/pose/obb 的输出几何恰好命中现有 4 种 result type（`rectanglelabels` / `polygonlabels` / `keypointlabels` / `rectanglelabels+rotation`），所以 **YOLO backend 的 `/predict` 输出零 adapter**，直接落现有渲染链路（§3）。只有 `classify` 的 class 需走 `attributes.class`。

##### `variant_combinations`（可选，v0.14.12 起）

`supported_variants` 暴露多轴时，前端默认按 axes 笛卡尔积渲染目录。但**多轴非真笛卡尔积**的 backend（如 yolo 的 `rtdetr` 只有 `l/x`、`yolov9 detect` 只有 `t/s/m/c/e`、`yolov10` 不支持 seg/pose/obb）需要显式列举合法组合，否则会列出虚假权重（如 yolov10-keypoint 这种实际不存在的 .pt）。

```jsonc
{
  "id": "detect",
  "supported_variants": [
    {
      "key": "series",
      "variants": [
        /* 7 个: v8/v9/v10/v11/v12/v26/rtdetr */
      ],
    },
    {
      "key": "size",
      "variants": [
        /* 9 个 union: n/t/s/m/b/c/l/e/x */
      ],
    },
  ],
  "variant_combinations": [
    ["yolov8", "n"],
    ["yolov8", "s"],
    ["yolov8", "m"],
    ["yolov8", "l"],
    ["yolov8", "x"],
    ["yolov9", "t"],
    ["yolov9", "s"],
    ["yolov9", "m"],
    ["yolov9", "c"],
    ["yolov9", "e"],
    // ...
    ["rtdetr", "l"],
    ["rtdetr", "x"], // 注意 rtdetr 只有 l/x
  ],
}
```

- inner array 顺序必须与 `supported_variants` 的 axis 顺序一致，即 `[axis0_value, axis1_value, ...]`。
- 字段缺省 ⇒ 前端按 axes 笛卡尔积处理（适用于 SAM2 × DINO 等真笛卡尔积场景）。
- 前端目录展示时严格按 `variant_combinations` 过滤；`/predict` 服务端仍独立做 `variant_not_supported` 422 兜底。

##### `variants_shared_across_tasks`（可选，v0.14.12 起）

布尔字段，缺省 `false`。决定前端列表视图如何对待"同 variant 跨多 task"的情形：

- **`false`（yolo 风格，默认）**：每 task 独立物理权重（yolov8n-det.pt / yolov8n-seg.pt / yolov8n-pose.pt / yolov8n-obb.pt 都是独立文件）。模型市场列表中每 (task, variant) 一行，行名加任务后缀（`YOLOv8-Det` / `YOLOv8-OBB`）。
- **`true`（gsam2 / sam3 风格）**：同 backend 内多 task 共享同一份权重（SAM 2.1 Tiny 一份 `.pt` 同时服务 segmentation / interactive_seg / tracker；GroundingDINO Swin-T 一份 `.pt` 同时服务 detection / segmentation）。模型市场列表按 `(backend, axis_key, axis_value)` 聚合到一行，`task` 列汇总所有用到此权重的 task。

**结合 `supported_variants` 按 task 暴露**：当 `variants_shared_across_tasks=true` 时，每个 model 只声明该 task **真正用到的 axes**（如 grounded-sam2 的 `detection` 只声明 `dino_variant` 轴而非两轴），让前端目录的 task 列准确反映哪些 task 用 SAM、哪些 task 用 DINO。

##### `default_variants`（可选，v0.14.13 起）

每个 model 自报该 task 的默认 variant 组合，前端 `VariantSelector` 在用户未显式选择时用这组值作初值。**优先级**：项目级 `projects.default_variants[backend_id]`（v0.14.13 新增字段）> backend `default_variants` > backend 启动时 env 默认。

**结构**：扁平 `dict[axis_key, axis_value]`，key 必须严格匹配该 model 的 `supported_variants[].key`，value 必须在对应 axis 的 `variants[].value` 内、且（多轴时）整体落在 `variant_combinations` 内：

```jsonc
// yolo (两轴, 严格按 variant_combinations 取合法组合):
{ "default_variants": { "series": "yolo11", "size": "s" } }

// grounded-sam2 detection (单轴 dino_variant):
{ "default_variants": { "dino_variant": "T" } }
// grounded-sam2 segmentation (两轴, DINO + SAM):
{ "default_variants": { "sam_variant": "tiny", "dino_variant": "T" } }

// sam3 (单档, 仍按对称约定声明):
{ "default_variants": { "model_variant": "sam3" } }
```

- **每 model 一份**：因为同 backend 不同 task 可能选不同档（yolo 通常 4 task 共用 yolo11/s；但 backend 完全有自由按 task 调整）。
- **轴必须完整**：声明了几轴 `supported_variants` 就要给齐几个值；缺轴等同于"没默认"，前端会回落到 backend 启动 env。
- **不与 `params.*_variant.default` 重复**：老协议 v1 时 `params` 里也带过 `default`（如 `params.properties.sam_variant.default`），新前端优先读 `default_variants`，老前端继续读 `params.*.default` 兼容。
- **校验**：backend 不强制运行时校验 default_variants 是否落在 supported_variants/variant_combinations 内（信任 backend 自报），前端 / API 也不校验；非法值只会让前端初值显示成不存在的选项（用户切换后即修复）。

#### 4.1.7 范例：ONNX 聚合 backend（一个 backend，多家族多任务，统一 infra）

```jsonc
{
  "name": "onnx-zoo-backend",
  "version": "0.1.0",
  "model_version": "onnxruntime-1.x",
  "infra": "onnx",
  "is_interactive": false,
  "models": [
    {
      "id": "yolov8n-coco",
      "task": "detection",
      "model_family": "yolo",
      "supported_geometric_outputs": ["bbox"],
    },
    {
      "id": "rtmpose",
      "task": "keypoint",
      "model_family": "rtmpose",
      "supported_geometric_outputs": ["keypoint"],
    },
    {
      "id": "ppocr",
      "task": "ocr",
      "model_family": "paddleocr",
      "supported_geometric_outputs": ["polygon"],
      "output_attribute_types": ["text", "language"],
    },
    {
      "id": "u2net",
      "task": "segmentation",
      "model_family": "u2net",
      "supported_geometric_outputs": ["polygon"],
    },
  ],
}
```

> 这里 model 都继承 `infra="onnx"`，但 `model_family` / `task` 各异 —— 正是「聚合模型带不同能力」。若某 PaddleOCR 条目用 paddle 运行时，在该条目写 `"infra": "paddle"` 覆盖即可。

#### 4.1.8 OCR / Doc Layout 输出约定（v2 首发模型族）

**OCR**（`task: "ocr"`）：

- 检测文本区域：几何 = `bbox` / `rotated_bbox` / `polygon`。
- 识别文本：写入 prediction result 的 `attributes.text`（必要时 `attributes.language` / `attributes.orientation`，对应 `output_attribute_types` 声明）。
- 采纳后生成普通 annotation + text 写入 attributes；**项目未配置 text attribute 时不静默丢文本** —— 前端提示「可采纳几何，文本字段不会入库」或要求先配置。

**真实参考 backend `rapidocr`**（[`apps/rapidocr-backend/`](https://github.com/yyq19990828/ai-annotation-platform/tree/main/apps/rapidocr-backend)）把 RapidOCR 的 `det → cls → rec` 三段拆为**原子能力 + 端到端编排**，自报三个 model：

- `ocr-det`（`task: "detection"`，原子）：full_image → `polygon` 四点文本框，无属性。
- `ocr-rec`（`task: "ocr"`，原子）：`crop` → `attributes.text` + `orientation`，内部跑文本行方向分类（cls）做 180° 校正。
- `ocr-e2e`（`task: "ocr"`，composite）：full_image → `polygon` + text + orientation + language，一次 `/predict` 跑完三段。

文本行方向分类（0/180）语言/版本无关、内化进 rec 与 e2e，不单独暴露为能力。`attributes.language` 按所选识别模型标（`universal` 中英 / `en` 英文），非逐框检测。`supported_variants` 走 version(PP-OCRv5/v6) × size × lang 三轴。平台 pipeline 可把 `ocr-det`（源阶段出框）→ `ocr-rec`（下游吃 crop 出文本）串成编排；`ocr-e2e` 是单 backend 一次跑完的便捷入口。

**Doc Layout**（`task: "doc_layout"`）：

- 输出区域 class：`title` / `paragraph` / `table` / `figure` / `formula` / `list` / `header` / `footer`，落 `class_name`。
- 几何 = `bbox` / `polygon`，可选 OCR text。
- 目标场景：文档图片标注、OCR 校对、表格/版面区域检测。

result 映射（统一 adapter，不新增 prediction 表）：`ocr_text` → `attributes.text`；`layout_type` → `class_name`；`orientation` → `attributes.orientation`。

#### 4.1.9 平台能力目录端点（派生视图）

能力目录是 `health_meta` 的派生视图，复用现有 `POST …/{bid}/setup`（30s TTL 缓存）的探测链路：

```text
GET  /projects/{pid}/ml-backends/{bid}/capabilities          # 返回 models[] 目录(含 infra/task/variants)
POST /projects/{pid}/ml-backends/{bid}/capabilities/refresh  # 强制重探 /setup 并刷新缓存
```

- `capabilities` 返回派生后的 model 目录（含每条 `infra` / `task` / `supported_inputs` / `supported_geometric_outputs` / `output_attribute_types` / `supported_variants` / `last_seen_at`），并在顶层携带受控词表诊断 `warnings`，供模型市场 / 工作台多模型选择器消费。
- `capabilities/refresh` 跳过缓存强制重探，用于 backend 升级 model_version 后立即看到新能力。
- 批量预标入口扩展 `model_id`（指向目录条目；缺省 = backend 隐式单 model，兼容老路径）；variant / threshold 经 `context` / `params` 透传。

> 若后续需要持久化跨 backend 模型检索，再考虑独立表 `ml_model_capabilities` 与全局聚合端点 `GET /ml-backends/capabilities`。

#### 4.1.10 可跑参考实现

**真实推理参考实现（v0.14.12 起）**：[`apps/yolo-backend/`](https://github.com/yyq19990828/ai-annotation-platform/tree/main/apps/yolo-backend) —— ultralytics 多任务多系列 backend，覆盖 detection / segmentation(instance) / keypoint / obb 四 task × v8/v9/v10/v11/v12/v26/rt-detr 七系列，共 80 个有效预训练组合。`/setup.models[]` 按 task 拆 4 条目，`supported_variants` 走 series × size 两轴，按预训练矩阵严格过滤。`/predict` 零 adapter 命中 4 种 result type，结果直落平台 `apps/api/app/services/prediction.py::to_internal_shape` → internal Geometry。可作为新接入 backend 的首选骨架参考。

**真实 OCR 参考实现**：[`apps/rapidocr-backend/`](https://github.com/yyq19990828/ai-annotation-platform/tree/main/apps/rapidocr-backend) —— RapidOCR(ONNX) backend，`ocr` 任务族首个真实推理实现。把一条 OCR 流水线拆为原子能力（`ocr-det` 检测 + `ocr-rec` 识别）+ 端到端 composite（`ocr-e2e`），详见 §4.1.8。`/setup.models[]` 三条目走 version × size × lang 多轴 `variants`，权重 bind-mount 注入（`download_models.py` 拉取）。可作为「单 backend 把流水线拆成原子 + 编排入口」与 OCR 富属性（text/orientation/language）落点的参考。

**协议形态参考实现（无真实推理）**：[`docs-site/dev/examples/mock-v2-backend/`](https://github.com/yyq19990828/ai-annotation-platform/tree/main/docs-site/dev/examples/mock-v2-backend) —— `/setup` 暴露 YOLO 风格多任务 `models[]`、PaddleOCR / DocLayout、点 / 框 / exemplar 交互分割及视频 tracker 条目；`/predict` 按 `context.type` 返回固定几何、OCR 文本属性或逐帧追踪结果。可直接 `uvicorn main:app --port 9100` 启动，也可通过 `docker-compose.ml.yml` 的 `screenshots` profile 作为截图与视觉回归协议 stub；所有能力仍由平台正常探测和绑定。

**最小 v1 参考实现**：见下文 echo-ml-backend。

#### 4.1.11 协议能力目录端点（v0.14.11）

> 决策见 [ADR-0037 — 协议能力目录与 backend 注册解耦](../adr/archive/0037-protocol-capability-catalog-decoupling.md)。

§4.1 描述的 `/projects/{pid}/ml-backends/{bid}/capabilities` 是**实例能力视图**（已注册 backend 探测出的 `models[]` 派生）。但「模型市场 · 能力目录」面板在用户心智里要回答的是「平台支持哪些 AI 标注能力」，与 backend 是否注册无关。v0.14.11 引入**协议级**端点：

```
GET /api/v1/ml-capabilities/protocol
```

- **数据源**：`apps/api/app/services/capability_registry.py`（SSOT），与 `services/ml_capabilities.py` 的受控词表同源（后者改为 re-export）。
- **鉴权**：登录用户即可访问；不暴露任何 backend 实例信息，无需 super_admin。
- **缓存**：`Cache-Control: private, max-age=300` + ETag；二次请求带 `If-None-Match` 返回 304。

**响应结构**：

```jsonc
{
  "version": "v2",        // 与协议 v2 对齐, 受控词表不兼容变更才 bump
  "tasks": [
    {
      "id": "detection",
      "label": "目标检测",
      "summary": "在图像或视频帧中输出 bbox + 类别标签。",
      "default_geometry": ["bbox"],
      "default_modalities": ["image", "video"],
      "typical_models": ["YOLO 系", "DETR 系", "Grounding DINO"],
      "protocol_notes": "/predict 响应 result.type=rectanglelabels, ...",
      "suggested_backends": [
        {
          "name": "Label Studio ML Backend (YOLO)",
          "repo_url": "https://github.com/HumanSignal/label-studio-ml-backend",
          "summary": "官方示例覆盖 YOLOv8 / DETR, 适合上手。",
          "research_link": "docs/research/01-label-studio.md"
        }
      ]
    }
    // 共 9 个 task
  ],
  "infras":     [ { "id": "pytorch", "label": "PyTorch", "summary": "..." }, ... ],   // 6 项
  "modalities": [ { "id": "image",   "label": "图像",   "summary": "..." }, ... ],   // 3 项 (image/video/lidar)
  "geometries": [ { "id": "bbox",    "label": "bbox",   "summary": "..." }, ... ],  // 8 项
  "prompts": [
    {
      "id": "interactive_box",
      "label": "框提示",
      "summary": "SAM-style 单框单 mask 交互分割。",
      "requires_input": true,
      "interactive_route": true
    },
    {
      "id": "text",
      "label": "文本提示",
      "summary": "开放词汇文本驱动检测/分割; 走批量线, 不进画布交互工具。",
      "requires_input": true,
      "interactive_route": false
    }
    // 共 9 个 prompt
  ]
}
```

**消费方**：前端 `CapabilityCatalogPanel` 默认 `groupBy=task`，遍历 `tasks` 渲染 9 张协议卡；已注册 backend 的 model 按 `model.task` 字段挂载到对应卡下。无 backend 注册时协议卡仍可见（带「暂无接入」徽标 + 推荐 backend CTA），不再阻塞用户探索。

**维护流程**：`capability_registry.py` 是 task / infra / modality / geometry / prompt 的 SSOT。修改受控词表、响应 schema 或序列化后，运行：

```bash
cd apps/api && uv run python ../../scripts/export_capability_registry.py
cd ../.. && pnpm codegen
```

第一步刷新 `apps/api/capability-registry.snapshot.json`；第二步让前端根据 snapshot 生成受控词表常量。pre-commit 会对 registry 相关文件自动重导 snapshot，CI 可用 `uv run python ../../scripts/export_capability_registry.py --check` 检测漂移。

**协议与实例的关系**：

| 端点                                                 | 数据源                                                | 何时可用                              | 视角                                      | 鉴权     |
| ---------------------------------------------------- | ----------------------------------------------------- | ------------------------------------- | ----------------------------------------- | -------- |
| `GET /v1/ml-capabilities/protocol`                   | `capability_registry.py` SSOT                         | 启动即可（与注册无关）                | 协议层「平台支持什么」                    | 登录用户 |
| `GET /v1/ml-capabilities/instances`                  | 注册实例探测 + `ml_backend_registry.health_meta` 合并 | docker-compose 启动或手动注册任一即可 | 实例层「现在跑着哪些 model 可用」         | 登录用户 |
| `GET /projects/{pid}/ml-backends/{bid}/capabilities` | `ml_backend_registry.health_meta["capabilities"]`     | backend 注册并 health 探测后          | 实例层「该项目启用的 backend 暴露了什么」 | 项目成员 |

#### 4.1.12 实例能力清单端点（v0.14.11）

`GET /api/v1/ml-capabilities/instances` 从全局 `ml_backend_registry` 返回 connected backend 的 model 清单。env 配置的 backend 在启动 / 初始化时自动 upsert 为 `source="env"` 的一等注册项；超管手动注册的是 `source="manual"`。因此实例能力不再维护“env-only 临时探测 + 项目内重复注册”两套来源。

**数据源合并**：

1. 从 `ml_backend_registry` 读取 `state="connected"` 的注册项（env 自动注册与 manual 手动注册同表）。
2. 直接消费每项 `health_meta["capabilities"]` 的 `models[]` 快照，并带出 `backend_id / state / source / name`。
3. URL 唯一性与 env upsert 在注册表层解决，instances 端点无需再做临时来源去重。

**字段裁剪**（与项目级 `/capabilities` 的差别）：

| 字段                                                                                                                                               | `/projects/.../capabilities` | `/v1/ml-capabilities/instances`                  |
| -------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- | ------------------------------------------------ |
| url                                                                                                                                                | ✓                            | ✗（避免暴露内网拓扑）                            |
| gpu_info / cache / pool / video_pool                                                                                                               | ✓                            | ✗（运维敏感）                                    |
| params                                                                                                                                             | ✓                            | ✗（运维细节给项目级视图）                        |
| `models[]` 的核心字段（id / display_name / task / infra / prompts / geometry / trackers / modality / output_attribute_types / supported_variants） | ✓                            | ✓                                                |
| supported_inputs / resource_profile                                                                                                                | ✓                            | ✓（全局编排选择器需投递契约 + 批量画像，故透传） |

**响应结构**：

```jsonc
{
  "instances": [
    {
      "backend_id": "9f1c…", // ml_backend_registry.id
      "state": "connected", // 注册表状态; disconnected 已在服务层过滤
      "source": "env", // ml_backend_registry.source: "env"(docker-compose/env 自动注册) | "manual"(superadmin 手动注册)
      "name": "grounded-sam2", // ml_backend_registry.name
      "infra": "pytorch",
      "models": [
        {
          "id": "grounded-sam2-detection",
          "display_name": "Grounded-SAM 2 · 文本检测 (DINO)",
          "task": "detection",
          "infra": "pytorch",
          "is_interactive": false,
          "supported_prompts": ["text"],
          "supported_inputs": ["full_image", "crop"],
          "supported_geometric_outputs": ["bbox"],
          "supported_trackers": [],
          "resource_profile": { "device": "gpu", "batchable": true },
          "modality": "image",
        },
        // ...
      ],
    },
  ],
}
```

**前端消费**：`CapabilityCatalogPanel` 协议卡视图（默认 `groupBy=task`）按 `model.task` 把 instance.models 挂到 9 张协议卡上；子卡的「自带 / 已注册」徽标按该 backend 是否已被某个项目启用（即是否出现在 admin overview）判定——已启用记「已注册」并挂项目名，未接入任何已启用项目的平台内置 backend 记「自带」——而非直接取本端点的 `source` 字段（`source` 只区分 backend 是 env 自动注册还是 superadmin 手动注册）。

---

### 4.2 PredictionResult 运行时观测字段（v0.14.14）

为了让前端把"猜测冷启动"换成"真信号"，`PredictionResult` 新增三个**可选**字段。语义只与本次请求挂钩，不影响存储与协议主路径。

| 字段            | 类型                                    | 含义                                                                                                                                                                                 |
| --------------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `cache_hit`     | `bool \| null`                          | 本次推理是否命中 pool 内已加载权重。`true` = 权重已在内存，本次跳过加载；`false` = 触发了加载（冷启动 / pool evict 后 / 首次拉取 ckpt）；`null` = backend 未上报（前端按"未知"处理） |
| `model_load_ms` | `int \| null`                           | 本次加载耗时毫秒。`cache_hit=true` 时通常为 `0`；`cache_hit=false` 时是从 disk → GPU 的真实耗时；`null` 同上                                                                         |
| `pool_state`    | `{current_size: int, cap: int} \| null` | 轻量 pool 快照，仅供 debug。常态下为 `null`，按需开启（避免每次响应都带）                                                                                                            |

**gsam2 组合判断**：SAM + DINO 双池架构，`cache_hit = sam_hit AND dino_hit`；`model_load_ms = max(sam_load_ms, dino_load_ms)`（取最慢的一边）。若只 SAM hit、DINO miss，`cache_hit=false`，前端感知"冷启动"够用；分轴粒度留给未来扩展。

**sam3 单档**：第一次推理触发懒加载时 `cache_hit=false`；已加载则 `cache_hit=true`；`SAM3_IDLE_UNLOAD_SECONDS` 触发的 idle unload 后再 predict 也是 `cache_hit=false`。

**字段顺序**：响应里 `inference_time_ms`（纯 forward）与 `model_load_ms`（权重加载）分离，**互不相加**，前端按需展示。

---

### 4.3 `/health.pool` 统一 PoolStatus（v0.14.14）

v0.14.12 时三家 backend 的 `/health.pool` 字段各不相同（yolo 用 `pool.loaded[]`、gsam2 用 `pool.loaded_variants[]` + `per_variant_lru_ts`、sam3 用 `loaded: bool`）。v0.14.14 起统一为 `PoolStatus` 结构：

```jsonc
{
  "pool": {
    "cap": 4, // 池容量
    "current_size": 2, // 当前已加载条数
    "loaded_keys": [
      {
        "key": "yolov11/s/detection", // backend-defined opaque 字符串，前端只做相等比较
        "loaded_at": "2026-06-08T03:11:22Z",
        "last_used_at": "2026-06-08T03:15:00Z",
        "hit_count": 12, // 命中次数（不含 warmup）
      },
    ],
    "last_evict": {
      // 可为 null
      "key": "yolov8/x/detection",
      "at": "2026-06-08T03:14:00Z",
      "reason": "lru", // 受控：lru | manual | idle_timeout
    },
  },
}
```

**key 命名约定**（backend 自由选择，前端不解析，只做相等匹配）：

| backend               | key 形式                                                                                                                 |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| yolo-backend          | `{series}/{size}/{task}`，如 `yolov11/s/detection`                                                                       |
| grounded-sam2-backend | image pool 用 `sam={sam_variant}/dino={dino_variant}`，如 `sam=tiny/dino=B`；video pool 的 key 就是 `sam_variant` 字符串 |
| sam3-backend          | 模型变体字符串，如 `sam3`；cap 永远 `1`                                                                                  |

**LRU evict 触发**：pool 满 + miss 时按 LRU 头部淘汰，`last_evict.reason="lru"`。`/unload` 手动卸载用 `"manual"`，sam3 的 idle 超时用 `"idle_timeout"`。

**前端消费**：模型市场列表的"运行时态"列直接从 `health_meta.pool.loaded_keys[]` 反查 `key` 即可；卡片视图按分组展示 `current_size/cap` 与 `last_evict` 摘要。

---

### 4.4 `POST /warmup`（可选，v0.14.14）

**用途**：把指定 variant 的权重显式加载到 pool，不消耗推理算力。模型市场"⚡ 预热"按钮、自动化预热脚本、CI 烟测时手动控制 pool warm 状态都走这个端点。

**前置条件**：backend 在 `/setup` 顶层声明 `warmup_endpoint: true`。未声明的 backend 视为不支持，前端 ⚡ 按钮置灰。

**请求**（per-backend 自定义结构；建议与 `/predict` 的 context 部分一致）：

```jsonc
// yolo-backend (闭集 / 开集文本检测分割)
POST /warmup
{
  "task": "detection",
  "variants": { "series": "yolov11", "size": "s" }
}

// yolo-backend (YOLOE 视觉提示 exemplar)
// task 取 /setup 的 exemplar 模型条目 task=interactive_seg, 令 warmup 命中独立的 VP pool
// (与首次拖框交互同句柄); 若误用 detection/segmentation 会预热到文本 pool, 首次交互仍冷启。
POST /warmup
{
  "task": "interactive_seg",
  "variants": { "series": "yoloe-26", "size": "m" }
}

// grounded-sam2-backend (同时预热 SAM + DINO)
POST /warmup
{
  "task": "segmentation",
  "variants": { "sam_variant": "small", "dino_variant": "B" }
}

// sam3-backend
POST /warmup
{
  "variants": { "model_variant": "sam3" }
}
```

**响应**（统一 `WarmupResponse` schema）：

```jsonc
{
  "ok": true,
  "model_load_ms": 4500, // 加载耗时(ms)，cache_hit=true 时 null/0
  "cache_hit": false, // 已经在 pool 内时 true
  "evicted": "yolov8/n/detection", // 可选；本次因 cap 上限淘汰的 key（前端 toast）
}
```

**行为约定**：

- 只加载权重，不跑真实 forward pass。若 backend 实现上需要触发权重初始化（如 lazy 算子），用最小可能 input（1×1 dummy tensor）。
- 重复预热同一 variant 直接返回 `cache_hit=true, model_load_ms=null`。
- pool 满时按 LRU 淘汰最旧的 key，把被淘汰的 key 名回填 `evicted` 字段供前端 toast 提示。
- `hit_count` 不增加（warmup 不算 hit）。
- 错误码：变体缺失或类型错返回 422（Pydantic/schema），变体值或组合不支持返回 422 + `error_code=variant_not_supported`，权重缺失 / 显存暂不可服务返回 503 + `Retry-After`。

**平台代理**：`POST /api/v1/projects/{pid}/ml-backends/{bid}/warmup`，body 原样转发，权限沿用现有 RBAC。

---

## 5. `GET /versions`（可选）

**响应**：

```json
{ "versions": ["v1.0.0", "v1.1.0", "v1.2.3"] }
```

前端会把这个列表填到「模型版本」下拉框；用户选定后写到 `MLBackend.extra_params` 并在后续 `/predict` 请求 header 或 body 携带（具体由 backend 自行约定）。未实现时返回 `{"versions": []}`。

---

## 6. 错误响应约定

v2.1 推荐 backend 使用结构化错误体。平台代理会保留上游 4xx；503 会保留为 503 并透传 `Retry-After`；其它上游 5xx / 连接超时仍映射为 502 Bad Gateway。

| 场景                                                                      | HTTP | 响应体                                                                                                                              |
| ------------------------------------------------------------------------- | ---: | ----------------------------------------------------------------------------------------------------------------------------------- |
| variant 字段缺失 / 类型错                                                 |  422 | FastAPI/Pydantic 默认 validation error                                                                                              |
| variant 值合法字段但不受支持，或组合不在 `variant_combinations` 内        |  422 | `{ "detail": { "error_code": "variant_not_supported", "axis": "size", "value": "x", "allowed": ["n","s"] } }`                       |
| variant 合法但当前 backend 暂不可服务（权重缺失、未预下载、显存池不可用） |  503 | `{ "detail": { "error_code": "model_unavailable", "key": "yolov11/s/detection", "reason": "checkpoint missing" } }` + `Retry-After` |
| 输入图片 / prompt 本身非法                                                |  422 | backend 自定义 detail                                                                                                               |
| backend bug / 未预期异常                                                  |  500 | backend 自定义 detail；平台交互式代理会映射为 502                                                                                   |

共享实现位于 `apps/_shared/protocol_v2/src/aap_protocol_v2/errors.py`：`VariantNotSupportedError` 与 `ModelUnavailableError`。三家内置 backend 已统一使用这两个错误类。

平台调用行为：

- 批量 `/predict` 中的 task 级失败会写一行 `failed_predictions` 并继续下一条。普通异常保留原有异常类型与消息；平台 GPU 仲裁在 backend HTTP 前拒绝时，`error_type` 直接保存稳定 `error_code`，`message` 保存干净人类消息，完整的 `status_code` / `retry_after_s` 记录保存在 `FailedPrediction.extra.gpu_arbiter_error`。
- 跨 Backend 下游阶段仍按 `keep_parent|drop_box` 降级，但仲裁根因会同时写入 `PredictionMeta.extra.pipeline.gpu_arbiter_failures` 和 `AsyncJob.result.gpu_arbiter_failures`。逐帧预标只在作业结果聚合，不为每帧创建无法按原帧上下文重试的 `FailedPrediction`。
- `gpu_arbiter_failures[].count` 统计被拒绝的派发次数，不是受影响的 task、帧或 ROI 数；摘要按稳定错误码合并并取保守的最大 `retry_after_s`。工作端只记录该窗口，不会自动 sleep 或额外重试。
- 交互式（`/predict` 单条）：服务层 `predict_interactive` (`ml_client.py`) 透传上游 4xx 与 503；其它上游 5xx / 连接超时映射为 502。前端按 422 / 503 / 500+ 分流：422 显示“参数错误，请检查输入”，503 显示“模型暂不可用，N 秒后重试”，500+ 显示“服务异常”。

---

## 7. token / cost 透传

如果你的 backend 是 LLM（Anthropic、OpenAI、本地 vLLM），可以在 `inference_time_ms` 之外补这些字段，平台会写到 `prediction_metas` 表（`prediction.py:34-56`）以后做成本卡片：

| 字段                | 类型   | 说明                                     |
| ------------------- | ------ | ---------------------------------------- |
| `prompt_tokens`     | int    | 输入 token 数                            |
| `completion_tokens` | int    | 输出 token 数                            |
| `total_tokens`      | int    | = prompt + completion                    |
| `prompt_cost`       | float  | 美元；按 backend 计价                    |
| `completion_cost`   | float  | 美元                                     |
| `total_cost`        | float  | 美元                                     |
| `extra`             | object | 任意 JSON，写到 `prediction_metas.extra` |

> 当前 ROADMAP §A「预测成本统计」前端可视化未做；后端字段已经在表里。

---

## 8. 最小 echo backend 示例

> 完整可跑样板（含 Dockerfile + curl 测试脚本 + README）见 [`docs-site/dev/examples/echo-ml-backend/`](https://github.com/yyq19990828/ai-annotation-platform/tree/main/docs-site/dev/examples/echo-ml-backend)。下面的代码块由 `check-doc-snippets.mjs` 锁定到样板源文件，源端改一字 `pnpm docs:build` 即报漂移。

<!-- snippet:docs-site/dev/examples/echo-ml-backend/main.py -->

```python
"""Echo ML backend — 协议 v2.1 参考实现（最小可跑版）。

满足 ml-backend-protocol 的 4 个端点：/health、/setup、/versions、/predict。
所有 /predict 输出固定的 demo bbox，让平台端到端链路可以直接走通。
真实 backend 的 inference 替换到 predict() 内部即可。

零外部依赖（仅 fastapi + pydantic），整目录可直接拷走当 starter。
字段语义以 docs-site/dev/reference/ml-backend-protocol.md 为准。
"""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

MODEL_VERSION = "echo-v1"


class TaskItem(BaseModel):
    id: str
    file_path: str


class PredictRequest(BaseModel):
    tasks: list[TaskItem]


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/setup")
async def setup():
    # 协议 v2.1 能力声明（协议文档 §4 / §4.1）。
    # models[] 是多模型目录：echo 只有一条 detection 条目；
    # supported_prompts=["none"] 表示纯批量、无交互式 prompt。
    return {
        "name": "echo-backend",
        "version": "0.1.0",
        "protocol_version": "2.1",
        "compat_protocol_versions": ["2.0"],
        "model_version": MODEL_VERSION,
        "is_interactive": False,
        "labels": ["demo"],
        "models": [
            {
                "id": "echo-detect",
                "display_name": "Echo 固定框 demo",
                "task": "detection",
                "supported_prompts": ["none"],
                "supported_geometric_outputs": ["bbox"],
            }
        ],
    }


@app.get("/versions")
async def versions():
    return {"versions": [MODEL_VERSION]}


@app.post("/predict")
async def predict(req: PredictRequest):
    results = []
    for t in req.tasks:
        results.append(
            {
                "task": t.id,
                "result": [
                    {
                        "type": "rectanglelabels",
                        "value": {
                            "x": 0.1,
                            "y": 0.1,
                            "width": 0.2,
                            "height": 0.2,
                            "rectanglelabels": ["demo"],
                        },
                        "score": 0.5,
                    }
                ],
                "score": 0.5,
                "model_version": MODEL_VERSION,
                "inference_time_ms": 1,
                # 运行时观测字段（可选，协议文档 §4.2）：echo 无真实权重，
                # 固定上报"已命中、零加载耗时"演示字段形态。
                "cache_hit": True,
                "model_load_ms": 0,
            }
        )
    return {"results": results}
```

<!-- /snippet -->

启动（任选其一）：

```bash
# 直接 uvicorn
pip install -r docs-site/dev/examples/echo-ml-backend/requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000

# Docker
cd docs-site/dev/examples/echo-ml-backend && docker build -t echo-ml-backend . && docker run --rm -p 8000:8000 echo-ml-backend
```

然后在前端 ProjectSettings → ML Backends 添加 `http://host.docker.internal:8000`（如果平台跑 Docker）或 `http://localhost:8000`，点「测试连接」应通过。或直接在样板目录跑 `./test.sh` 脚本三连击校验四个端点。

---

## 9. 接入 checklist

- [ ] `/health` 返回 200
- [ ] `/setup` 声明 `protocol_version: "2.1"` 与 `compat_protocol_versions: ["2.0"]`
- [ ] `/predict` 批量 schema 与 §2.1 对齐，至少回填 `task` + `result`
- [ ] 如声明 `is_interactive=True`，`/predict` 也接受 §2.2 单条请求
- [ ] `/predict.context.model_variants` 支持通用 axis dict；旧字段仅作为兼容期 normalize
- [ ] `/setup.params` 给阈值/数量/简化容差等字段标 `x-platform-role`
- [ ] 每条 result 的 `type` 与项目类型匹配（image-det 项目至少要有 `rectanglelabels`）
- [ ] 非法 variant 返回 422；模型暂不可用返回 503 + `Retry-After`（见 §6）
- [ ] 长任务考虑 backend 内部异步 + 在合理时间内（< `ml_predict_timeout`）返回结果，否则平台会判超时并落 `failed_predictions`

---

## 10. 兼容性与迁移

v0.14.15 是 protocol v2.1 minor bump，不是 v3。平台与内置 backend 保留 v2.0 兼容期，避免外部自建 backend 立刻中断。

| 旧字段                                         | 新字段                                                   | 兼容行为                                                                   |
| ---------------------------------------------- | -------------------------------------------------------- | -------------------------------------------------------------------------- |
| `context.variants.{series,size}`               | `context.model_variants.{series,size}`                   | yolo-backend normalize 并记录 deprecation warning                          |
| `context.sam_variant` / `context.dino_variant` | `context.model_variants.{sam_variant,dino_variant}`      | grounded-sam2-backend normalize；新字段优先                                |
| `context.model_variant`                        | `context.model_variants.model_variant`                   | sam3-backend normalize；非 `sam3` 仍返回 422                               |
| `params.*_variant.enum` 无 role                | `supported_variants[]` 或 `x-platform-role=modelVariant` | 前端仍回落渲染 legacy enum；新 backend 应声明 `supported_variants`         |
| `projects.ai_model`                            | `projects.ml_backend_id` + backend name 展示             | v0.14.15 DB 迁移删除列；解绑过的项目若残留旧 `ai_model` 字符串会被直接丢弃 |

迁移建议：

1. 先在 `/setup` 加 `protocol_version: "2.1"` 与 `compat_protocol_versions: ["2.0"]`。
2. `/predict` 接受 `context.model_variants`，保留旧字段 normalize 一版。
3. 给参数 schema 补 `x-platform-role`，不要改物理参数名。
4. 把非法 variant 统一改为 422，把权重缺失 / 显存暂不可服务改为 503 + `Retry-After`。
5. 确认前端或第三方调用方不再读取 `projects.ai_model`；项目是否绑定 backend 只看 `projects.ml_backend_id`。

## 11. 参考实现

> 适用读者：要把自家推理服务接入到本平台的工程师；项目管理员配置 ML Backend 时遇到调试问题。
>
> 平台侧实现：
>
> - 服务: `apps/api/app/services/ml_backend.py` · `ml_client.py`
> - HTTP 接入点: `apps/api/app/api/v1/ml_backends.py`
> - 数据模型: `apps/api/app/db/models/{ml_backend,prediction}.py`

社区已有几种现成接入：

- **Label Studio ML Backends 模板**（兼容平台 schema）：https://github.com/HumanSignal/label-studio-ml-backend
- **GroundingDINO + SAM**：调研报告 [`docs/research/06-ai-patterns.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/06-ai-patterns.md) §模式 B
- **X-AnyLabeling SAM 工厂**：调研报告 [`docs/research/04-x-anylabeling.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/04-x-anylabeling.md)

## 全局注册表与项目启用

ML Backend 走全局注册表模型（ADR-0044）：一个物理 backend = 全局 `ml_backend_registry` 一行 = 一份能力快照和一份 `max_concurrency` 配置；项目侧只做「启用」。本地 semaphore 在所有模式下提供单进程背压；当 `ML_BACKEND_ROUTER_MODE=enforce` 时，路由 ledger 使用同一上限发放 Redis route lease，把 API 与多个 Celery worker 收口到跨进程实例上限。GPU 仲裁的 effective mode、request lease、release latch 与逐资源 rollout 另行负责显存准入和驱逐（[ADR-0049](/dev/adr/archive/0049-cross-backend-gpu-memory-arbitration)），不能与请求路由模式互相替代。两层职责：

- **全局层（超管）**：`ml_backend_registry`。URL / 鉴权 / `auth_method` / `auth_token` / `extra_params`（含 `max_concurrency`）/ `is_interactive` / `state` 等端点固有属性写在这里，所有启用该 backend 的项目共享。env 配置的 backend 启动时自动 upsert 为 `source=env` 注册项；env 删项时对应行置 `disconnected` 而非删除，保留历史 prediction 溯源。
- **项目层（项目管理员）**：`project_ml_backend_pool` 关联表，仅记「启用 / 停用」+ 项目级变体覆盖（`default_variants`，pool 级）。多阶段编排里选不同 backend 跑不同阶段时，先在「管理 backend」面板里勾选启用，再到编排卡里选用即可。

### 服务池与请求路由（ADR-0050）

在全局注册表之上叠加一层**逻辑服务池**（`ml_backend_service_pools` + `ml_backend_pool_members`），把「项目请求一个逻辑能力」与「平台选择一个物理实例」拆成两个步骤。pool id 是逻辑请求身份，registry id 是物理执行身份，二者永不互换。

- **一个 registry 实例同时最多属于一个服务池**。每个现有 registry 经迁移自动得到一个 singleton 服务池（`legacy_instance_id` 指向该 registry，off mode 下解析回原实例，行为与之前完全一致）。
- **身份边界**：项目启用、项目主绑定和请求 lineage 使用 pool id；`preannotate_pipeline` 、`projects.default_variants` 以及 `users.preferences.ai` 中按 backend 分桶的公共配置仍使用 registry id，与当前 API schema 和前端注册表索引一致。派发时由 registry 的唯一成员关系解析所属 pool，不会在这两种 UUID 之间猜测或混用。
- **能力等价合同**：指纹以 `/setup` 派生的真实 `models[]` 目录为主，包含协议/模型/权重版本、task、modality、请求合法性参数、variant 轴与组合、tracker 及 batchable 等稳定合同字段，列表按确定规则排序后计算 SHA-256。URL、展示名、GPU / VRAM / residency、动态类别等实例态字段不参与。singleton 在首次有效探活时建立指纹；后续漂移的成员自动 disabled。
- **跨进程原子路由 ledger**（Redis namespace `ml-router:v1`，独立于 GPU 仲裁 `gpu-arbiter:v1`）：平滑加权轮询（SWRR）+ per-instance 并发上限 + 被动熔断（仅 transport failure 触发）+ route lease acquire/heartbeat/finish/cancel。
- **双 ID 溯源**：`Prediction` / `FailedPrediction` 同时记录 `ml_backend_id`（实际执行的 selected instance）和 `ml_backend_pool_id`（requested pool）。多阶段聚合的 stage-level lineage 存 `PredictionMeta.extra.pipeline`。
- **灰度**：`ML_BACKEND_ROUTER_MODE=off|observe|enforce`。off/observe 保持 legacy 实例派发（observe 额外记录 would-select 诊断，不门控）；enforce 用 router 选中实例并在 Redis / topology 不确定时 fail-closed。
- **管理 API**：项目池绑定 `GET /projects/:id/ml-backends/pools/available` + `PUT /pools/:pool_id/enablement`；超管 pool/member CRUD + drain/resume `/admin/ml-integrations/service-pools/*`；读模型 `GET /admin/ml-integrations/{topology,runtime-snapshot}`。详见 [ADR-0050](/dev/adr/archive/0050-ml-backend-service-pools-and-request-routing)。
- **破坏性操作门禁**：纳管实例只有在 `router_mode=enforce`、成员精确为 `draining`、Redis 路由账本可用且清理过期 lease 后的 exact `route_inflight=0` 时，才允许卸载、移除成员或物理删除 registry。缺失、过期或不可读值都是未知，不能当作零。

**没有项目级数量上限**。旧的 `max_ml_backends_per_project` 与多阶段 DAG 需 ≥2 backend 直接冲突，已退役；全局行的 `max_concurrency` 同时作为本地 semaphore 配置与 `ML_BACKEND_ROUTER_MODE=enforce` 的 Redis route lease 上限。路由模式为 off/observe 时 API 与多个 Celery worker 的并发仍会叠加；路由 enforce 时才由 route ledger 收口为真正的跨进程实例上限。新建项目不再有「复用 backend = 克隆一行」语义，统一走「在新项目里勾选启用某个已注册 backend」。

平台 API 与 worker 直接消费共享 `aap-protocol-v2` lifecycle wire。`MLBackendClient` 已把 predict、交互预测、warmup、reload 与 unload 收口到同一个派发 context：预测先取得当前 event loop 的本地 semaphore，再进入 context，context 退出后才释放本地许可；health/setup 保持只读，不进入该边界。

GPU `observe` 模式仍只在真实 HTTP 派发前计算非权威 `would-*` 快照。legacy unload
另记只读事件，不能作为显存释放或预算减账证据。effective `off/observe` 不进入权威
authority，也不添加 generation / admission token，因此可能加载的请求在 backend 侧仍按 legacy
路径处理；非空 residency 保持 `generation=null, evictable=false`，不会被影子策略当成可驱逐真值。
当前 context 已建立受管 header/scope 与结构化拒绝接缝：非法 grant 在 HTTP 前 fail-closed，
authority context 也不能抑制 backend 错误或调用取消。五个受管业务入口会在完整 HTTP 响应
返回后、状态或 JSON 解析前同步回报 `response_received`；未收到完整响应的传输超时、断连、
取消或缺失回报则收敛为 `uncertain`。该 outcome 只表达传输边界，不声明 GPU residency，
不能单独提交 allocation 终态。

平台 signer、membership promotion、Redis admission lease、业务 token、Resident 快路、cold admission 与空闲
victim 驱逐均已接入惰性 authority。cold 请求收到完整响应后会用新 challenge 探测，并在
逐资源持久锁内把 Loading 分类为 Resident、CPU fallback、Unloaded 或保守 Unknown；无完整
响应时不发探测，只收口 Unknown。cold 快照容量不足时，authority 只从同一完整
`gpu_resource_id` 选择 exact Resident、可驱逐且零 lease 的空闲 victim，保护严格更高优先级，
并按优先级、LRU 与 backend id 稳定排序。一个或多个 victim 依次完成受签 `/drain`、
严格 ACK + 新鲜空闲证明、受签 `/unload` 与全空驻留证明后，才释放预算并以新
challenge 重读 target cold subject。任一 phase 不确定即保守收口 Unknown 并停止继续驱逐；
同路由响应丢失只做 exact owner/generation/phase/token 重放。FIFO 已接入；新 residency 只在首次可信
Loading→Resident CAS 中按 Redis `TIME` 写入 `not_evict_before_ms`；proof reset 重建 Resident 时以固定
`prepared_at_ms + cooldown` 保守恢复窗口。Python 快照选择与 Lua 原子选择都会排除未到期 victim，
以上精确重放和 Resident 快路均不续期。cold authority 会继续持有 exact card ticket，
按 Redis 快照给出的累计最早时刻在 admission deadline 与固定 ticket TTL 内有界等待；等待不续期，
超时或取消会精确清票，只有 victim 已可驱逐时才为终态清理预留独立窗口。忙碌 victim 的
Redis 原子地基已能在保留旧 lease 时关闭新 admission、进入 Draining，并支持零 lease 后 Unloading、
更新 generation 的 Resident cancel CAS 与携带 lease 的保守 Unknown。authority 仅在空闲候选累计预算不足时
选择 busy victim；严格 drain ACK 后，每轮分别读取新鲜 Redis lease 快照与新 challenge backend health，
只有两域同时归零才进入 Unloading。busy-capable victim proof 严格绑定 fresh challenge、capability、membership、boot、
generation、control/runtime epoch、resource identity 与稳定 pool-id 集合，同时允许旧
workload 仍在 active/borrow。drain 后的 fresh health 仅使用单次 MVCC 快照只读区分
`draining_busy`、`ready_to_unload` 与 `uncertain`；它不写 PostgreSQL/Redis，也不代替
Redis lease-zero 门禁。

Draining→Unloading 与 cancel 分别在同一逐资源 Redis transition owner 上原子冻结持久 `unload` / `cancel`
分支；只有一个分支能成功，unload 已获胜时平台绝不发送 RESUME。工作超时、异常或调用方取消时，平台先以
完全相同参数持久写入或重放 cancel intent，稳定签名后 arm cancel，再调用真实 `/drain/cancel`，最后用 strict
ACK + fresh health 提交 Resident 或保守 Unknown。冻结 marker 不随 owner TTL 消失，只能由 exact 分支终态
释放或 challenge-bound proof reset 清理；缺 key、mirror/branch/deadline 损坏均 fail-closed。DRAIN、双域等待
与 UNLOAD 受工作 deadline 限制，owner 另保留 30 秒取消收尾窗口。以上状态与等待按完整
`gpu_resource_id` 分片，单卡、多卡共用同一路径。参考环境的实物单卡、多卡与跨宿主门禁均已通过；
各部署仍须用自身 Backend、模型制品与 GPU 拓扑完成验收，并由运维逐资源启用默认关闭的 release latch。
五个受管 backend 在启动时同时检查 NVIDIA/CUDA/ROCm 可见设备配置；单索引、单 GPU/MIG UUID
或显式无设备值可用，逗号多值与已暴露 GPU runtime 的无界 `all` 集合会直接使服务启动失败。

派发只认逐卡 effective mode，不能被全局 desired mode 提前短路：demotion 握手完成前，即使 desired 已回到 off/observe，旧 effective=enforce 的卡仍发送受管请求。多卡部分灰度时，已知卡 B 的 off/observe 不受卡 A enforce 影响；但缺失或未知 resource 的注册项无法安全归属，只要任一卡真正 effective enforce 就在 backend HTTP 前返回 `gpu_config_invalid`。仅带新鲜 connected health 且明确 `configured_device=cpu`、没有任何 GPU 正证据的 null-claim backend 可豁免。未注册 URL 的 smoke-test 始终可做只读 health；任一卡进入 effective enforce 后，raw reload 同样在 backend HTTP 前拒绝。

---
