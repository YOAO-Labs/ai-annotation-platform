# aap-backend-runtime

ML backend 运行时共享组件 (无状态叶子函数 + 取消安全的 LRU 池内核)。单一来源, 避免跨 backend 复制漂移。

被 `apps/yolo-backend` / `apps/sam3-backend` / `apps/grounded-sam2-backend` /
`apps/onnxtools-backend` / `apps/rapidocr-backend` 编辑安装复用。

## 边界

**包含**（同契约、零 per-backend 状态的叶子函数）:

- `fetch_image(file_path, *, timeout=10.0) -> PIL.Image` — 统一 `data:` base64 /
  `http(s)://` presigned URL / 本地绝对路径三种来源, 解码为 RGB `PIL.Image`。
- `free_gpu_memory() -> None` — `gc.collect` + (若 CUDA 可用) `empty_cache` + `ipc_collect`;
  torch 不可用时仅 `gc.collect`。
- `versions_payload(model_version, backend_version, **extra) -> dict` — 统一 `GET /versions`
  载荷形状 `{"versions": [model_version], "backend_version": ..., **extra}`。
- `gpu_info_snapshot() -> dict` — torch CUDA context 视角显存快照 (used/total/free MB 等);
  无 torch / 无 GPU 返回 `{}`，并叠加宿主可见的物理卡身份。
- `physical_gpu_identity() -> dict` — 从部署时与 device reservation 同源的
  `AAP_GPU_PHYSICAL_DEVICE_TOKEN` 解析宿主物理卡 token；它未设置时才回落
  runtime 可见设备配置，避免把重映射后的逻辑 `cuda:0` 误当宿主卡 0。
- `validate_single_gpu_device_set()` — backend 启动门禁，拒绝逗号多卡列表和已暴露 GPU 的无界 `all` 可见集合。
- `build_managed_lifecycle_evidence()` / `validate_managed_lifecycle_evidence()` — 五个 Backend
  共用的部署级受管生命周期验收证据外壳；严格校验部署与 GPU 身份、制品摘要、两轮显存回落、
  必做故障合同、最终空池状态和运行时中间产物，并从 blockers 自动推导 `passed`。
- `artifact_evidence()` / `memory_cycle_evidence()` — 生成不含本地路径的权重/fixture 摘要，
  以及 64 MiB 稳定窗口与 90% 工作集回收率指标。
- `ManagedLruPool` / `BuildArtifact` / `ManagedBuildTimeout` / `ManagedPoolBusyError` /
  `ResourceLease` — 取消安全的 LRU 所有权内核 (builder 单飞、borrower 串行、租约、
  quarantine 清理与强制回收)。每个消费方通过 `logger_name` 传入自己的 logger 名称,
  保持日志身份与 backend 一致 (如 `grounded-sam2-backend.managed-pool`)。
  之前 `grounded-sam2-backend/managed_pool.py` 与 `sam3-backend/managed_pool.py` 仅差
  docstring 与 logger 名, 现由本包统一提供。
- `gpu_lifecycle.ManagedGpuLifecycle(pool, *, verify_keyring, evictable_verified=False,
  boot_id=None, expected_registry_id=None, expected_gpu_resource_id=None,
  terminal_cpu_enabled=False, allow_legacy_workloads=True)` — 签名控制、generation、JTI、
  取消与池排空状态机；`allow_legacy_workloads=False` 拒绝 LEGACY 状态下所有工作请求
  （即使提供签名头），但不阻止签名启动控制。受管 CUDA 服务须设置为 False，直到
  合格实例完成签名切换至 ENFORCE 才能接收持有效许可的工作；独占本地开发和旧调用方
  默认保留 LEGACY 行为。签名控制始终要求预配置 Registry 和 GPU resource 双重绑定。
  CPU 可只配置 Registry；`terminal_cpu_enabled` 只能由部署配置确认的 CPU 实例开启。
  registry ID 是平台实例 UUID，resource ID 是平台 `<node>/<physical-token>`；
  调用方必须独立校验实际物理 GPU UUID 与部署绑定，不能信任逻辑 CUDA 序号。
  `terminal_drain(registry_id=..., boot_id=..., operation_id=...)` 是 CPU 专用、
  不可逆的安装停止栅栏：调用方必须先验证业务 Bearer。GPU 必须使用
  `terminal_drain_signed(generation, generation_header=..., token=...)` 和已绑定的
  签名 DRAIN 控制令牌，不接受 CPU 业务认证代替签名。两者阻止后续工作与恢复，
  等待所有真实请求、future、builder、borrower、waiter 和清理结束后强制清空
  `models` 池并验证可信空状态；断开的调用者不会取消后台清理。同一身份可重试，
  不同身份不可复用，进程健康信息仍可读取。CPU 返回 registry_id、boot_id、
  operation_id、null control_epoch/generation、pool_ids 和 unloaded；GPU 另返回
  gpu_resource_id 及签名绑定的 control_epoch/generation。该进程内证明必须由平台
  持久停止栅栏和容器身份共同约束，不能独立作为重启后的停止授权。
- `managed_models.ManagedModelStore(root, *, inspect, validate_receipt)` — 复用锁、
  有界下载、摘要、不可变收据和原子安装。调用方负责严格验证请求、下载来源和
  checkpoint/receipt；`inspect(path, receipt)` 与 `validate_receipt(receipt, path)`
  注入框架专属校验。下载不跟随重定向。
- `backend_auth.require_external_bearer(request, expected_token)` — 外部实例独立
  Bearer token 校验，不赋予任何第一方 provider 身份。

**不包含** (见 `docs/plans/archive/2026-06-29-v0.20.3-ml-backend-shared-layer-extraction.md`):

- 各 backend 的池包装 (`model_pool.py` / `video_pool.py` / `pool_domain.py`) / `observability.py` /
  FastAPI app 骨架 —— 各有 per-model / per-vendor 语义, 留作本地实现而非共享 import;
  它们复用上面的 `ManagedLruPool` 内核。
- 各 backend 的 `/health` `gpu_info`: 多叠加 pynvml 整卡视角 + util/温度/功耗 + 协议嵌套形状，
  与 `gpu_info_snapshot` 的通用版差异大，仍由各自 observability 构造；物理卡 token 则统一复用
  `physical_gpu_identity()`。

## 依赖说明

- `httpx` / `pillow` 仅 `fetch_image` 用, 函数内惰性 import (纯 `versions_payload` 消费方无需装)。
- `torch` **不在依赖里**: 各 backend 的 torch / torchvision 由 docker base image 锁定, 在此声明会让
  `pip install -e` 误升级覆盖预装版本。`free_gpu_memory` / `gpu_info_snapshot` 把 `import torch` 包在
  try 里, 不可用时降级。
- lifecycle evidence helper 只使用 Python 标准库，不读取或持久化签名私钥、admission token、
  原始业务图像、文件路径或 signed URL；证据应写入仓库外的受限目录。

## 引用方式 (backend Dockerfile)

```dockerfile
# build context = ./apps; setuptools>=68 已在装 protocol_v2 前 upgrade 过, 复用即可
COPY _shared/backend_runtime/ /app/backend_runtime/
RUN pip install --no-build-isolation -e /app/backend_runtime
```

backend 代码:

```python
from aap_backend_runtime import (
    fetch_image,
    free_gpu_memory,
    gpu_info_snapshot,
    physical_gpu_identity,
    versions_payload,
)
```
