# aap-protocol-v2

ML backend 协议 v2 的共享 Pydantic schema 与受控词表常量。

被 `apps/sam3-backend` / `apps/grounded-sam2-backend` / `apps/yolo-backend` 等 backend 编辑安装复用，单一来源避免协议字段在各 backend 之间漂移。

## 边界

**包含**：

- `aap_protocol_v2.schemas` — 跨 backend 通用的请求 / 响应 Pydantic 模型
  - `TaskItem` (id + file_path)
  - `PredictionResult` (单 task 的预测结果壳)
  - `BatchPredictResponse` (`/predict` 顶层响应)
  - `NativeMaskCandidate` / `MaskPromptPayload` / `ScribblePrompt` /
    `CorrectionFramePrompt`（原生 Mask 交互与视频纠错的加法 wire contract）
  - `MaskInteractionDiagnostic` / `MaskInteractionFallback`（空结果、稳定错误原因与显式降级 lineage）
- `aap_protocol_v2.vocab` — 协议 v2 受控词表常量（任务词表来自 `aap-task-contract`，其余元数据与 API registry 同源镜像）
  - `TASK_VALUES` / `INFRA_VALUES` / `GEOMETRY_VALUES` / `PROMPT_VALUES` /
    `INPUT_VALUES`
- `aap_protocol_v2.lifecycle` — 受管 GPU 生命周期 wire 模型、header 常量与 EdDSA admission token codec
  - generation / control epoch 使用 canonical positive int64 字符串
  - token 固定 `aud=aap-gpu-lifecycle`，通过 Ed25519 公钥 keyring 按 `kid` 验签
- `aap_protocol_v2.errors` — 生命周期结构化错误词表与 FastAPI `detail.error_code` helper

**不包含**：

- 每个 backend 的 `Context` —— prompt 字段集差异大（sam3 有 `exemplar`、yolo 走 `variants`），各 backend 自己定义。
- `/setup` 字典字面量 —— 各 backend 自己构造。
- lifecycle lock、active / borrower / builder、token replay tombstone、模型池与全池释放实现。它们必须留在各 backend 的本地并发模型中。详见 [ADR-0038](../../../docs/adr/archive/0038-defer-ml-backend-base-class.md)与 [ADR-0049](../../../docs/adr/archive/0049-cross-backend-gpu-memory-arbitration.md)。

## 协议版本同步

`vocab.py` 的常量值与 `apps/api/app/services/capability_registry.py` 的 `TASK_VALUES` / `INFRA_VALUES` / `GEOMETRY_VALUES` / `PROMPT_VALUES` / `INPUT_VALUES` 手工同源。协议 v2 词表稳定后改动概率低；若 capability_registry 扩展，需要镜像更新此处并 bump 本包 minor 版本。

## 引用方式（backend Dockerfile）

```dockerfile
# build context = ./apps
COPY _shared/task_contract/ /app/task_contract/
COPY _shared/protocol_v2/ /app/protocol_v2/
RUN pip install --no-build-isolation -e /app/task_contract -e /app/protocol_v2
```

backend 代码：

```python
from aap_protocol_v2.schemas import TaskItem, PredictionResult, BatchPredictResponse
from aap_protocol_v2.vocab import TASK_VALUES, INFRA_VALUES
from aap_protocol_v2.lifecycle import GPU_GENERATION_HEADER, BackendResidency
```

生命周期符号必须从 `aap_protocol_v2.lifecycle` 显式导入；包根不 eager re-export，避免尚未接入受管生命周期的
backend 仅导入旧 schema 时也被迫加载验签依赖。

只有完整实现 `/drain`、`/drain/cancel`、managed `/unload`、`/lifecycle/mode`、
`/lifecycle/reset`、active / borrower / builder fencing 与 token replay 防护的 backend，才可在
`/setup` 返回 `ManagedLifecycleCapabilities`。只导入 schema 不代表 backend 可驱逐。
