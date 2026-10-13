"""yolo-backend FastAPI 入口.

端点 (ml-backend 协议 v2):
    GET  /health          健康检查 + provisioning 元信息 + 池状态
    GET  /setup           协议 v2 多模型目录 (4 task × series × size variants)
    GET  /versions        当前部署的 backend / ultralytics 版本
    POST /predict         批量预测 (单 task / 多 task 共享同 context)
    POST /unload          手动释放显存 (运维侧用)
    GET  /metrics         Prometheus

`/setup.supported_prompts = ["none"]`: yolo 是纯批量, 无交互式 prompt;
平台 ToolDock 据此把 yolo 排除出工作台交互工具栏, 只走「批量预标」入口.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import torch
from aap_backend_runtime import (
    deployment_verified_flag,
    effective_device,
    effective_device_value,
    free_gpu_memory,
    is_device_error,
    latch_cpu,
    physical_gpu_identity,
    versions_payload,
    validate_single_gpu_device_set,
)
from aap_protocol_v2 import (
    COMPAT_PROTOCOL_VERSIONS,
    PROTOCOL_VERSION,
    BatchPredictResponse,
    ModelUnavailableError,
    PlatformRole,
    PredictionResult,
    VariantNotSupportedError,
)
from aap_protocol_v2.errors import LifecycleErrorCode, LifecycleHTTPError
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    GPU_HEALTH_CHALLENGE_HEADER,
    GPU_HEALTH_CHALLENGE_QUERY_PARAM,
    GenerationTransitionRequest,
    LifecycleModeRequest,
    LifecycleResetRequest,
    ManagedLifecycleCapabilities,
    load_verify_keyring,
    match_gpu_health_challenge,
    parse_gpu_admission_header_values,
    parse_gpu_control_token_header_values,
)
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import ValidationError
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import class_names
from gpu_lifecycle import WorkloadOperation, YoloGpuLifecycle
from model_pool import ModelBuildTimeout, ModelPool, PoolBusyError
from model_registry import (
    MODEL_MATRIX,
    OPENVOCAB_DEFAULT_WORLD,
    OPENVOCAB_DEFAULT_YOLOE,
    OPENVOCAB_SERIES_LABEL,
    OPENVOCAB_WORLD_SERIES,
    OPENVOCAB_YOLOE_SERIES,
    POOL_TASK_OPENVOCAB,
    POOL_TASK_OPENVOCAB_VP,
    RECOMMENDED_SERIES,
    RECOMMENDED_SIZE,
    SERIES_LABEL,
    SIZE_META,
    UnsupportedVariantError,
    is_openvocab_series,
    is_openvocab_supported,
    openvocab_family,
    resolve_openvocab_weight_filename,
    resolve_weight_filename,
)
from observability import (
    init_perfhud_collectors,
    sample_perfhud,
    shutdown_perfhud_collectors,
    update_pool_size,
)
from predictor import YoloPredictor
from schemas import (
    BatchPredictRequest,
    Context,
    InteractiveRequest,
    WarmupRequest,
    WarmupResponse,
)

logger = logging.getLogger("yolo-backend")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

BACKEND_VERSION = "0.1.1"  # backend 仓自身版本, 与 ultralytics 版本独立
MODEL_VERSION = "ultralytics-8.4.x"

DEVICE = os.environ.get("YOLO_DEVICE", "cuda:0")
MODEL_POOL_CAP = int(os.environ.get("YOLO_MODEL_POOL_CAP", "2"))
BUILD_TIMEOUT = float(os.environ.get("YOLO_BUILD_TIMEOUT", "30"))
IDLE_UNLOAD_SECONDS = float(os.environ.get("YOLO_IDLE_UNLOAD_SECONDS", "600"))
IDLE_CHECK_INTERVAL = float(os.environ.get("YOLO_IDLE_CHECK_INTERVAL", "60"))
STRICT_OFFLINE = os.environ.get("YOLO_STRICT_OFFLINE", "0") not in (
    "0",
    "",
    "false",
    "False",
)
CHECKPOINTS_DIR = Path(os.environ.get("YOLO_CHECKPOINTS_DIR", "/app/checkpoints"))
# Code support and deployment verification are separate. The backend does not
# advertise or enter enforce mode until real-card load/unload evidence exists.
MANAGED_LIFECYCLE_VERIFIED = deployment_verified_flag("YOLO_MANAGED_LIFECYCLE_VERIFIED")


def _strict_free_gpu_memory() -> None:
    """Release managed CUDA allocator state without hiding an untrusted outcome."""

    if not torch.cuda.is_available() or not torch.cuda.is_initialized():
        return
    torch.cuda.synchronize()
    clear_cublas_workspaces = getattr(
        torch._C,  # noqa: SLF001 - PyTorch exposes no public equivalent
        "_cuda_clearCublasWorkspaces",
        None,
    )
    if callable(clear_cublas_workspaces):
        clear_cublas_workspaces()
    torch.cuda.empty_cache()
    torch.cuda.ipc_collect()


def _build_model(task: str, series: str, size: str):
    """同步构建 ultralytics 模型实例. 走 run_in_executor.

    开集 series (world/yoloe) 用 YOLOWorld/YOLOE 类加载; 闭集用 YOLO. 文件名按各自规则解析.
    """
    from ultralytics import YOLO  # noqa: PLC0415, 延迟到首次 build 避免 import 阻塞启动

    if is_openvocab_series(series):
        filename = resolve_openvocab_weight_filename(series, size)
        if openvocab_family(series) == "world":
            from ultralytics import YOLOWorld  # noqa: PLC0415

            model_cls = YOLOWorld
        else:
            from ultralytics import YOLOE  # noqa: PLC0415

            model_cls = YOLOE
    else:
        filename = resolve_weight_filename(task, series, size)
        model_cls = YOLO
    weight_path = CHECKPOINTS_DIR / filename
    if not weight_path.exists():
        if STRICT_OFFLINE:
            raise FileNotFoundError(
                f"weight {filename} not found in {CHECKPOINTS_DIR} (STRICT_OFFLINE=1)"
            )
        # 让 ultralytics 自己去 GH release 拉; YOLO("yolo11s.pt") 在 cwd 下载.
        # cd 到 checkpoints 目录, 让下载产物落对地方.
        cwd = os.getcwd()
        try:
            os.chdir(CHECKPOINTS_DIR)
            model = model_cls(filename)
        finally:
            os.chdir(cwd)
    else:
        model = model_cls(str(weight_path))

    return _move_model_to_effective_device(model)


def _move_model_to_effective_device(model):
    """移动模型；只在 CPU move 成功后才提交进程级 latch。"""
    dev = effective_device(DEVICE)
    if dev == "cpu":
        model.to("cpu")
        return model
    try:
        model.to(dev)
    except Exception as exc:  # noqa: BLE001
        if not is_device_error(exc):
            raise
        model.to("cpu")
        free_gpu_memory()
        latch_cpu(f"model.to({dev}) 失败，CPU replacement 已提交: {exc}")
    return model


_model_pool: ModelPool | None = None
_predictor: YoloPredictor | None = None
_gpu_lifecycle: YoloGpuLifecycle | None = None
_idle_task: asyncio.Task | None = None


async def _idle_watcher() -> None:
    """周期检查池空闲，并通过池内原子判断安全卸载。"""
    assert _gpu_lifecycle is not None
    while True:
        try:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            if IDLE_UNLOAD_SECONDS <= 0:
                continue
            n = await _gpu_lifecycle.try_idle_unload(
                idle_before=time.monotonic() - IDLE_UNLOAD_SECONDS
            )
            if n:
                logger.info("idle unloaded %d models", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("idle_watcher error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model_pool, _predictor, _gpu_lifecycle, _idle_task
    validate_single_gpu_device_set()
    raw_keyring = os.environ.get("GPU_LIFECYCLE_VERIFY_KEYS_JSON", "").strip()
    verify_keyring = load_verify_keyring(raw_keyring) if raw_keyring else {}
    CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    # 创建文本编码器权重目录 (Dockerfile 软链 /app/weights → 此处持久卷子目录).
    # ultralytics WEIGHTS_DIR 相对 "weights" 落 /app/weights → 经软链入卷; 目录须先存在,
    # 否则首个开集文本 /predict 时 clip.load(download_root="weights/clip") makedirs 失败.
    (CHECKPOINTS_DIR / "weights").mkdir(parents=True, exist_ok=True)
    init_perfhud_collectors()
    _model_pool = ModelPool(
        cap=MODEL_POOL_CAP,
        build_model=_build_model,
        free_gpu_memory=_strict_free_gpu_memory,
        build_timeout=BUILD_TIMEOUT,
    )
    _gpu_lifecycle = YoloGpuLifecycle(
        _model_pool,
        verify_keyring=verify_keyring,
        evictable_verified=MANAGED_LIFECYCLE_VERIFIED,
    )
    update_pool_size(0)
    _predictor = YoloPredictor(_model_pool)
    _idle_task = asyncio.create_task(_idle_watcher())
    logger.info(
        "yolo-backend startup: device=%s pool_cap=%d strict_offline=%s "
        "managed_lifecycle_verified=%s checkpoints=%s",
        DEVICE,
        MODEL_POOL_CAP,
        STRICT_OFFLINE,
        MANAGED_LIFECYCLE_VERIFIED,
        CHECKPOINTS_DIR,
    )
    try:
        yield
    finally:
        if _idle_task is not None:
            _idle_task.cancel()
            try:
                await _idle_task
            except (asyncio.CancelledError, BaseException):
                pass
        if _gpu_lifecycle is not None:
            await _gpu_lifecycle.shutdown()
        shutdown_perfhud_collectors()


app = FastAPI(title="yolo-backend", version=BACKEND_VERSION, lifespan=lifespan)


def _echo_gpu_health_challenge(request: Request, response: Response) -> None:
    challenge = match_gpu_health_challenge(
        request.headers.getlist(GPU_HEALTH_CHALLENGE_HEADER),
        request.query_params.getlist(GPU_HEALTH_CHALLENGE_QUERY_PARAM),
    )
    if challenge is not None:
        response.headers[GPU_HEALTH_CHALLENGE_HEADER] = challenge
        response.headers["Cache-Control"] = "no-store"


@app.get("/health", dependencies=[Depends(_echo_gpu_health_challenge)])
async def health() -> dict[str, Any]:
    perf = sample_perfhud()
    # 平台 PerfHud / 观测面板读顶层 gpu_info + host (见 api/app/workers/ml_health.py
    # _PERFHUD_META_KEYS, gsam2 参考实现). sample_perfhud() 是扁平形, 这里映射成协议标准的
    # 嵌套结构, 否则平台拿不到指标 → 面板四条 bar 全显示 "—".
    used = perf.get("gpu_memory_used_mb")
    total = perf.get("gpu_memory_total_mb")
    # 本容器自身视角: 物理卡号 (多卡部署按容器绑卡, CUDA_VISIBLE_DEVICES 固定单卡时取该号)
    # + 本进程 torch 已保留显存 (不含 ~数百 MB CUDA 上下文). memory_used_mb 仍是整卡全局。
    _vis = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    _cuda = False
    device_index: int | None = None
    process_memory_mb: int | None = None
    try:
        _cuda = bool(torch.cuda.is_available())
        if _cuda:
            device_index = int(_vis) if _vis.isdigit() else torch.cuda.current_device()
            process_memory_mb = int(torch.cuda.memory_reserved() / 1024**2)
    except Exception:  # noqa: BLE001 — CUDA 运行时损坏不应拖垮 /health
        _cuda = False
        device_index = None
        process_memory_mb = None
    gpu_info = {
        "device_name": perf.get("gpu_device_name"),
        "device_index": device_index,
        "memory_used_mb": used,
        "memory_total_mb": total,
        "memory_free_mb": (total - used)
        if (used is not None and total is not None)
        else None,
        "process_memory_mb": process_memory_mb,
        "gpu_utilization_percent": perf.get("gpu_utilization_percent"),
        "gpu_temperature_celsius": perf.get("gpu_temperature_celsius"),
        "gpu_power_watts": perf.get("gpu_power_watts"),
    }
    gpu_info.update(physical_gpu_identity())
    host = {
        "container_cpu_percent": perf.get("container_cpu_percent"),
        "container_memory_percent": perf.get("container_memory_percent"),
    }
    # NOTE: ModelPool 实现了 __len__, 用 `if pool` 会因 __len__()==0 退化为 False;
    # 必须用 `is not None`.
    if _gpu_lifecycle is not None:
        pool_snapshot, residency = await _gpu_lifecycle.snapshot_and_residency()
        residency_payload = residency.model_dump(mode="json")
    else:
        pool_snapshot = {
            "cap": 0,
            "current_size": 0,
            "loaded_keys": [],
            "last_evict": None,
        }
        residency_payload = None
    return {
        "status": "ok",
        "service": "yolo-backend",
        "version": BACKEND_VERSION,
        "model_version": MODEL_VERSION,
        "provisioning": {
            "device": DEVICE,
            "strict_offline": STRICT_OFFLINE,
            "checkpoints_dir": str(CHECKPOINTS_DIR),
        },
        # 五镜像统一有效设备观测 (torch 系 effective_device / ORT 系 effective_provider)。
        # configured_device = 环境配置; effective_device = 真实探测生效设备 (None=尚未加载,
        # "cpu"=GPU 配置但已静默退回, 供观测「GPU 静默退化」根因排查)。
        "compute": {
            "configured_device": DEVICE,
            "effective_device": effective_device_value(),
            "cpu_fallback_supported": True,
        },
        "pool": {
            key: pool_snapshot[key]
            for key in ("cap", "current_size", "loaded_keys", "last_evict")
        },
        "residency": residency_payload,
        "gpu_info": gpu_info,
        "host": host,
    }


def _build_size_variants_for(task: str, series: str) -> list[dict[str, Any]]:
    sizes = MODEL_MATRIX.get(task, {}).get(series, ())
    out: list[dict[str, Any]] = []
    for sz in sizes:
        meta = SIZE_META.get(sz, {})
        item: dict[str, Any] = {"value": sz, "label": meta.get("label", sz)}
        if "vram_gb" in meta:
            item["vram_gb"] = meta["vram_gb"]
        if "tier" in meta:
            item["tier"] = meta["tier"]
        if sz == RECOMMENDED_SIZE and series == RECOMMENDED_SERIES:
            item["recommended"] = True
        out.append(item)
    return out


def _build_series_variants_for(task: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    series_options = list(MODEL_MATRIX.get(task, {}).keys())
    for series in series_options:
        item: dict[str, Any] = {
            "value": series,
            "label": SERIES_LABEL.get(series, series),
        }
        if series == RECOMMENDED_SERIES:
            item["recommended"] = True
        out.append(item)
    return out


def _supported_variants_for(task: str) -> list[dict[str, Any]]:
    """协议 v2 §4.1.6 形态: 两轴 series × size, 每轴 variants 严格按预训练矩阵.

    yolo 这版 size 选项与 series 共生 (v9 在 seg 只有 c/e), 所以 size 轴的 variants
    实际上不是「全 series 共享」而是「按 series 子集合并」. 这里取**该 task 下所有
    series 用过的 size 的并集**, 前端选了 series 后再以 model_pool 的 400 兜底.
    更优做法是 protocol 支持 conditional variants (size depends on series), 留待
    协议 v3 扩展; 这版 yolo 取并集 + 服务端校验.
    """
    series_axis = {
        "key": "series",
        "title": "版本系列",
        "variants": _build_series_variants_for(task),
    }
    # 该 task 下所有 series 出现过的 size 并集 (顺序按 SIZE_META).
    used_sizes: set[str] = set()
    for series, sizes in MODEL_MATRIX.get(task, {}).items():
        used_sizes.update(sizes)
    size_variants: list[dict[str, Any]] = []
    for sz in ("n", "t", "s", "m", "b", "c", "l", "e", "x"):
        if sz not in used_sizes:
            continue
        meta = SIZE_META.get(sz, {})
        item: dict[str, Any] = {"value": sz, "label": meta.get("label", sz)}
        if "vram_gb" in meta:
            item["vram_gb"] = meta["vram_gb"]
        if "tier" in meta:
            item["tier"] = meta["tier"]
        if sz == RECOMMENDED_SIZE:
            item["recommended"] = True
        size_variants.append(item)
    size_axis = {"key": "size", "title": "尺寸 / 精度档", "variants": size_variants}
    return [series_axis, size_axis]


# 闭集四 task (detect/segment/pose/obb) 全继承 DetectionPredictor.postprocess, 走同一条
# NMS, conf/iou/max_det 三参对四 task 都生效 (obb 仅多 rotated=True 走 probiou NMS, 阈值语义
# 不变)。故参数集本就一致, 无需按 task 拆; 仅 description/default 按上下文 (闭集/开集/obb) 派生。
_BASE_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "conf": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 0.25,
            "title": "置信度阈值",
            "x-platform-role": PlatformRole.CONFIDENCE.value,
            # task 中性文案: 被 detect/segment/pose/obb/开集六个 model 共用, 不写死"检测/分割".
            "description": "保留置信度高于此值的结果. 调高=更少更准, 调低=更多但含噪.",
        },
        "iou": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 0.70,
            "title": "NMS IoU 阈值",
            "x-platform-role": PlatformRole.IOU.value,
            "description": "非极大值抑制重叠阈值. 调高保留更多重叠框, 调低更严格去重.",
        },
        "max_det": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "default": 300,
            "title": "单图最大检出数",
            "x-platform-role": PlatformRole.MAX_DET.value,
        },
    },
}


def _build_params_schema(
    *,
    conf_default: float = 0.25,
    conf_desc: str | None = None,
    iou_desc: str | None = None,
) -> dict[str, Any]:
    """从基础参数表派生一份**独立** schema (深拷贝, 避免多处共享同一可变 dict),
    仅按上下文覆盖 conf 默认值/文案与 iou 文案。参数集 (conf/iou/max_det) 保持一致。"""
    schema = copy.deepcopy(_BASE_PARAMS_SCHEMA)
    props = schema["properties"]
    props["conf"]["default"] = conf_default
    if conf_desc is not None:
        props["conf"]["description"] = conf_desc
    if iou_desc is not None:
        props["iou"]["description"] = iou_desc
    return schema


# 闭集四 task + 顶层 hint 共用基础默认 (等价改造前, 零回归)。
_PARAMS_SCHEMA = _build_params_schema()

# obb: iou 仍走 NMS 但是旋转 (probiou) 版本, 阈值语义不变; 文案补一句说明。conf/max_det 同基础。
_OBB_PARAMS_SCHEMA = _build_params_schema(
    iou_desc="非极大值抑制重叠阈值 (朝向框走旋转 NMS / probiou, 阈值含义相同). "
    "调高保留更多重叠框, 调低更严格去重.",
)

# 开集文本 (detect-world / detect-yoloe / segment-yoloe): conf 是文本匹配置信度。默认仍取 0.25
# (与闭集同值)——本版无 GPU 实测证据下调, 不盲改 (校准依据见计划 §3); 仅文案点明开集语义。
_OPENVOCAB_PARAMS_SCHEMA = _build_params_schema(
    conf_desc="保留文本匹配置信度高于此值的结果. YOLOE/World 开集打分偏保守, "
    "调高=更少更准, 调低=更多但含噪.",
)

# exemplar (YOLOE 视觉提示) 专属 params: 用 `score_threshold` 替代 conf 作为置信度
# 旋钮 (与 sam3 字段名对齐, 平台 exemplar 阈值滑块读 model.params.score_threshold.default)。
# 默认 0.25, 与闭集 conf 同值但语义是"相似度"而非检测置信度: 实测 YOLOE VP 相似度分天然打得
# 保守 (相似小目标多框命中也仅 ~0.5), 取更高阈值 (如 0.5) 会把正确候选挡在门外; 0.25 是召回与
# 噪声的平衡点 (大目标仍 >0.9, 不受影响)。
_EXEMPLAR_DEFAULT_SCORE_THRESHOLD = 0.25
_EXEMPLAR_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "score_threshold": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": _EXEMPLAR_DEFAULT_SCORE_THRESHOLD,
            "title": "置信度阈值",
            "x-platform-role": PlatformRole.CONFIDENCE.value,
            "description": "只保留相似度高于此值的候选。YOLOE 视觉提示对相似目标打分偏保守, 阈值不宜过高; 调低=更多候选但可能含误检。",
        },
        "iou": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 0.70,
            "title": "NMS IoU 阈值",
            "x-platform-role": PlatformRole.IOU.value,
            "description": "非极大值抑制重叠阈值. 调高保留更多重叠框, 调低更严格去重.",
        },
        "max_det": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "default": 300,
            "title": "单图最大检出数",
            "x-platform-role": PlatformRole.MAX_DET.value,
        },
    },
}


# 检测式视频追踪 params: 闭集 conf/iou/max_det + tracker 算法选择 (ByteTrack/BoT-SORT)。
# tracker 是 param 不是 variant 轴 (不换权重, 只换关联算法), 平台 apply-time 选, 不进全局池;
# enum 约束到 supported_trackers, 默认 bytetrack (快、通用基线)。
_TRACKER_PARAMS_SCHEMA = _build_params_schema()
_TRACKER_PARAMS_SCHEMA["properties"]["tracker"] = {
    "type": "string",
    "enum": ["bytetrack", "botsort"],
    "default": "bytetrack",
    "title": "追踪算法",
    "description": "多目标关联算法。ByteTrack 快、通用基线; BoT-SORT 带外观 ReID, 遮挡 / 交错更稳但更慢。",
}


def _variant_combinations_for(task: str) -> list[list[str]]:
    """该 task 下所有合法 (series, size) 组合, 与 supported_variants 轴顺序一致.

    协议 v2 字段 `variant_combinations` (可选): 当 backend 的多个 axis 不是真笛卡尔积
    (yolo 的 v9 在 detection 只支持 t/s/m/c/e, 不能与 size=n 组合) 时, 用这个字段显式
    列出合法组合; 前端目录展示时严格按列表展开. 若字段缺省, 前端默认按 axes 笛卡尔积.
    """
    out: list[list[str]] = []
    matrix = MODEL_MATRIX.get(task, {})
    for series, sizes in matrix.items():
        for size in sizes:
            out.append([series, size])
    return out


def _default_variants_for(task: str) -> dict[str, str]:
    """协议 v2.1 字段 `default_variants`: backend 自报该 task 的默认 variant 组合,
    供前端在用户未选择时作为初值 (优先级低于项目级 default_variants, 高于 backend env).

    优先用 RECOMMENDED_SERIES + RECOMMENDED_SIZE (yolo11/s); 若该组合在 task 内无
    预训练权重 (理论不会发生, yolo11/s 4 task 全覆盖), 回退到该 task 第一个合法组合.
    """
    matrix = MODEL_MATRIX.get(task, {})
    if RECOMMENDED_SERIES in matrix and RECOMMENDED_SIZE in matrix[RECOMMENDED_SERIES]:
        return {"series": RECOMMENDED_SERIES, "size": RECOMMENDED_SIZE}
    for series, sizes in matrix.items():
        if sizes:
            return {"series": series, "size": sizes[0]}
    return {}  # 不应发生 (task 至少一个合法权重), 留 {} 让前端走"无默认"逻辑


def _build_model_entry(
    model_id: str,
    display_name: str,
    task: str,
    geometric_outputs: list[str],
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": model_id,
        "display_name": display_name,
        "task": task,
        "model_family": "yolo",
        "infra": "pytorch",
        "is_interactive": False,
        # YOLO 各 task 均单次推理,原子(协议 v2.2)。
        "composition": "atom",
        "supported_prompts": ["none"],
        # 纯批量检测/分割: 整图, 也可在父框 crop 上跑 (crop-detect 下游)。
        "supported_inputs": ["full_image", "crop"],
        "supported_geometric_outputs": geometric_outputs,
        "output_attribute_types": ["class"],
        # YOLO 各 task 均 GPU 批量推理。
        "resource_profile": {"device": "gpu", "batchable": True},
        "supported_variants": _supported_variants_for(task),
        "variant_combinations": _variant_combinations_for(task),
        "default_variants": _default_variants_for(task),
        "params": params if params is not None else _PARAMS_SCHEMA,
    }
    # 模型类别表, 供前端渲染类别白名单. 当前权重矩阵全为官方 COCO/DOTA 预训练, 类别表是已知
    # 真值, 按 task 静态自报 (免预热、切模型稳定). 静态表覆盖 detection/segmentation/obb/keypoint;
    # 开集 task 无固定类别 (走 _build_openvocab_model_entry, 不经此函数). 若某 task 无静态表
    # (理论不会), 回退权重 metadata (仅 warmup/首次 predict 后有值).
    classes = class_names.classes_for_task(task)
    if classes:
        entry["classes"] = classes
    return entry


def _build_openvocab_variants(
    series_matrix: dict[str, tuple[str, ...]],
    default: tuple[str, str],
) -> list[dict[str, Any]]:
    """开集 model 的 series × size 两轴 (series 来自该条目自身的 series 子集)."""
    default_series, default_size = default
    series_variants: list[dict[str, Any]] = []
    for series in series_matrix:
        item: dict[str, Any] = {
            "value": series,
            "label": OPENVOCAB_SERIES_LABEL.get(series, series),
        }
        if series == default_series:
            item["recommended"] = True
        series_variants.append(item)
    used_sizes: set[str] = set()
    for sizes in series_matrix.values():
        used_sizes.update(sizes)
    size_variants: list[dict[str, Any]] = []
    for sz in ("n", "t", "s", "m", "b", "c", "l", "e", "x"):
        if sz not in used_sizes:
            continue
        meta = SIZE_META.get(sz, {})
        item = {"value": sz, "label": meta.get("label", sz)}
        if "tier" in meta:
            item["tier"] = meta["tier"]
        if sz == default_size:
            item["recommended"] = True
        size_variants.append(item)
    return [
        {"key": "series", "title": "版本系列", "variants": series_variants},
        {"key": "size", "title": "尺寸 / 精度档", "variants": size_variants},
    ]


def _openvocab_variant_combinations(
    series_matrix: dict[str, tuple[str, ...]],
) -> list[list[str]]:
    return [[series, size] for series, sizes in series_matrix.items() for size in sizes]


def _build_openvocab_model_entry(
    model_id: str,
    display_name: str,
    series_matrix: dict[str, tuple[str, ...]],
    default: tuple[str, str],
    *,
    task: str = "detection",
    geometric_outputs: list[str] | None = None,
    supported_text_outputs: list[str] | None = None,
) -> dict[str, Any]:
    """开集文本 model 条目 (检测 / 分割). 与闭集结构一致, 但
    supported_prompts=['text']; task/几何输出/文本输出形态按条目区分.

    supported_text_outputs (协议: 文本批量路径的输出形态选项, 与 gsam2 同形) 决定前端文本面板
    是否展示 box/mask/both 三选: 检测条目 ['box'] 锁框, 分割条目 ['mask','both'] 出掩膜。
    """
    default_series, default_size = default
    entry: dict[str, Any] = {
        "id": model_id,
        "display_name": display_name,
        "task": task,
        "model_family": "yolo",
        "infra": "pytorch",
        "is_interactive": False,  # 文本=批量, 不进交互工具栏.
        "composition": "atom",
        "supported_prompts": ["text"],
        "supported_inputs": ["full_image", "crop"],
        "supported_geometric_outputs": geometric_outputs or ["bbox"],
        "output_attribute_types": ["class"],
        "resource_profile": {"device": "gpu", "batchable": True},
        "supported_variants": _build_openvocab_variants(series_matrix, default),
        "variant_combinations": _openvocab_variant_combinations(series_matrix),
        "default_variants": {"series": default_series, "size": default_size},
        "params": _OPENVOCAB_PARAMS_SCHEMA,
    }
    if supported_text_outputs is not None:
        entry["supported_text_outputs"] = supported_text_outputs
    return entry


def _build_exemplar_model_entry() -> dict[str, Any]:
    """YOLOE visual prompt exemplar 交互模型条目.

    is_interactive=True + supported_prompts=["exemplar"] → 平台 useBackendRouting 据此把
    yolo-backend 视为交互 backend, 工作台 ExemplarTool 启用 (与 sam3 exemplar 同列, 路由由
    用户选定的当前交互 backend 决定)。框样例找全图同类, 输出 box/mask/both。仅 yoloe series。
    """
    default_series, default_size = OPENVOCAB_DEFAULT_YOLOE
    return {
        "id": "exemplar-yoloe",
        "display_name": "YOLOE 视觉提示 (框样例找同类)",
        "task": "interactive_seg",
        "model_family": "yolo",
        "infra": "pytorch",
        "is_interactive": True,
        "composition": "atom",
        "supported_prompts": ["exemplar"],
        # exemplar 能力声明 (字段与 sam3 对齐, 供前端按能力渲染控件):
        # YOLOE 支持多正框 + per-request 阈值, 但**无负框** (negative_box=False) /
        # MVP 不叠 text (text_combination=False) → 前端据此隐藏负极性按钮与 text 输入。
        "exemplar_capabilities": {
            "multi_box": True,
            "negative_box": False,
            "text_combination": False,
            "threshold_refilter": True,
        },
        # 交互: 框样例驱动整图相似 (不作批量 crop 下游)。
        "supported_inputs": ["full_image"],
        # VPSeg 同时产出 box + mask → 三档输出皆可。
        "supported_geometric_outputs": ["bbox", "polygon"],
        "output_attribute_types": ["class"],
        # 单次交互推理, 不作批量。
        "resource_profile": {"device": "gpu", "batchable": False},
        "supported_variants": _build_openvocab_variants(
            OPENVOCAB_YOLOE_SERIES, OPENVOCAB_DEFAULT_YOLOE
        ),
        "variant_combinations": _openvocab_variant_combinations(OPENVOCAB_YOLOE_SERIES),
        "default_variants": {"series": default_series, "size": default_size},
        # exemplar 专属 params: score_threshold 默认 0.25 (前端阈值滑块初值由此而来)。
        "params": _EXEMPLAR_PARAMS_SCHEMA,
    }


def _build_tracker_model_entry() -> dict[str, Any]:
    """检测式视频追踪 (detect-then-track) model 条目.

    复用 detection 权重 + ultralytics 原生 ByteTrack/BoT-SORT: task=tracker、仅吃 video、
    输出带 track_id 的逐帧 bbox (result item type=video_track_bbox)。走标准批量 /predict +
    Celery pipeline, 与交互式 SAM2/SAM3 tracker (predict_interactive, 单对象种子传播) 是**两条
    不同的链**。tracker 算法 (bytetrack/botsort) 经 params.tracker apply-time 选定 —— 不是
    variant 轴 (不换权重), 也不进全局编排池 (与 conf/iou 阈值同属 apply-time 参数)。

    supported_variants 复用 detection 的 series×size 轴 (MODEL_MATRIX 已把 tracker 别名到
    detection)。类别表同 COCO detection。
    """
    entry: dict[str, Any] = {
        "id": "track",
        "display_name": "YOLO 检测式视频追踪",
        "task": "tracker",
        "model_family": "yolo",
        "infra": "pytorch",
        "is_interactive": False,
        "composition": "atom",
        "supported_prompts": ["none"],
        # 仅 video: 单帧图像无跨帧状态、产不出有意义的 track_id (ultralytics 硬边界)。
        # 故不含 full_image / crop —— 与检测/分割 model 条目的关键区别。
        "supported_inputs": ["video"],
        # 协议 · backend 自报可用追踪算法; 平台 apply-time 从中选 (默认取首项)。
        "supported_trackers": ["bytetrack", "botsort"],
        "supported_geometric_outputs": ["bbox"],
        "output_attribute_types": ["class"],
        "resource_profile": {"device": "gpu", "batchable": True},
        "supported_variants": _supported_variants_for("tracker"),
        "variant_combinations": _variant_combinations_for("tracker"),
        "default_variants": _default_variants_for("tracker"),
        "params": _TRACKER_PARAMS_SCHEMA,
    }
    # 追踪的是 detection 权重, 类别表同 COCO detection (供前端类别白名单 UI)。
    classes = class_names.classes_for_task("detection")
    if classes:
        entry["classes"] = classes
    return entry


@app.get("/setup")
def setup() -> dict[str, Any]:
    """协议 v2 多模型目录. 详见 docs-site/dev/reference/ml-backend-protocol.md §4.1.6."""
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "compat_protocol_versions": COMPAT_PROTOCOL_VERSIONS,
        "name": "yolo-backend",
        "version": BACKEND_VERSION,
        "model_version": MODEL_VERSION,
        "labels": [],  # 顶层 hint 留空; 类别表逐 model 暴露 (models[].classes) 供前端
        #               类别白名单 UI. 平台仍不做"模型类→项目标签"映射 (NG6 保留, 由 alias 配置 + 采纳时人选承担).
        # 顶层 hint; 平台实际按 models[] 并集派生 (is_interactive=any(model)),
        # exemplar 模型令本 backend 整体成为交互 backend。顶层仍报 false 仅为兼容旧消费方。
        "is_interactive": False,
        # 闭集四 task 纯批量(none) + 开集文本检测/分割(text) + 视觉提示(exemplar).
        # 顶层为各 model 并集 hint; 平台按 models[].supported_prompts 逐 model 路由
        # (text → 批量文本面板; exemplar → 工作台交互工具)。
        "supported_prompts": ["none", "text", "exemplar"],
        "supported_geometric_outputs": ["bbox", "polygon", "keypoint", "rotated_bbox"],
        "supported_variants": [],  # 顶层留空, 由 models[].supported_variants 各自声明.
        "infra": "pytorch",
        "warmup_endpoint": True,  # 声明本 backend 支持 POST /warmup (协议 §4.4)
        "params": _PARAMS_SCHEMA,
        "models": [
            _build_model_entry(
                "detect",
                "YOLO 目标检测",
                "detection",
                ["bbox"],
            ),
            _build_model_entry(
                "segment",
                "YOLO 实例分割",
                "segmentation",
                ["polygon"],
            ),
            _build_model_entry(
                "pose",
                "YOLO 人体关键点",
                "keypoint",
                ["keypoint"],
            ),
            _build_model_entry(
                "obb",
                "YOLO 朝向框",
                "obb",
                ["rotated_bbox"],
                params=_OBB_PARAMS_SCHEMA,
            ),
            # 开集文本检测 (批量文本面板, 与 gsam2 text 同列).
            _build_openvocab_model_entry(
                "detect-world",
                "YOLO-World 开集文本检测",
                OPENVOCAB_WORLD_SERIES,
                OPENVOCAB_DEFAULT_WORLD,
                task="detection",
                geometric_outputs=["bbox"],
                supported_text_outputs=["box"],
            ),
            _build_openvocab_model_entry(
                "detect-yoloe",
                "YOLOE 开集文本检测",
                OPENVOCAB_YOLOE_SERIES,
                OPENVOCAB_DEFAULT_YOLOE,
                task="detection",
                geometric_outputs=["bbox"],
                supported_text_outputs=["box"],
            ),
            # YOLOE 开集文本分割 (同 -seg 权重出 mask, 与 detect-yoloe 共用句柄).
            _build_openvocab_model_entry(
                "segment-yoloe",
                "YOLOE 开集文本分割",
                OPENVOCAB_YOLOE_SERIES,
                OPENVOCAB_DEFAULT_YOLOE,
                task="segmentation",
                geometric_outputs=["polygon"],
                supported_text_outputs=["mask", "both"],
            ),
            # YOLOE visual prompt exemplar (交互工具, is_interactive=true).
            _build_exemplar_model_entry(),
            # 检测式视频追踪 (video 源, 复用 detection 权重 + ultralytics tracker).
            _build_tracker_model_entry(),
        ],
    }
    if MANAGED_LIFECYCLE_VERIFIED:
        payload["managed_lifecycle"] = ManagedLifecycleCapabilities().model_dump(
            mode="json"
        )
    return payload


@app.get("/versions")
def versions() -> dict[str, Any]:
    try:
        from ultralytics import __version__ as ul_ver
    except Exception:  # noqa: BLE001
        ul_ver = "unknown"
    return versions_payload(MODEL_VERSION, BACKEND_VERSION, ultralytics=ul_ver)


async def _run_predict(
    req: BatchPredictRequest,
    *,
    operation: WorkloadOperation | None = None,
) -> list[PredictionResult]:
    """核心: 校验组合 + 逐 task 推理, 产出 PredictionResult 列表。响应形态由调用方按 wire 决定。"""
    if _predictor is None or _model_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    ctx: Context = req.context
    if not _is_supported_combo(ctx):
        raise _unsupported_combo_error(ctx.type, ctx.variants.series, ctx.variants.size)
    results: list[PredictionResult] = []
    for t in req.tasks:
        try:
            result_items, cache_hit, load_ms, infer_ms = await _predictor.predict_one(
                t.file_path, ctx
            )
            score = max((r.get("score", 0.0) for r in result_items), default=0.0)
            results.append(
                PredictionResult(
                    task=t.id,
                    result=result_items,
                    score=score,
                    model_version=f"{ctx.variants.series}{ctx.variants.size}",
                    inference_time_ms=infer_ms,
                    cache_hit=cache_hit,
                    model_load_ms=load_ms,
                )
            )
        except UnsupportedVariantError as exc:
            raise VariantNotSupportedError("model_variants", str(exc), []) from exc
        except FileNotFoundError as exc:
            raise ModelUnavailableError(
                _pool_key(ctx.type, ctx.variants.series, ctx.variants.size),
                str(exc),
            ) from exc
        except ModelBuildTimeout as exc:
            if operation is not None:
                operation.track_future(exc.builder)
            raise ModelUnavailableError(
                _pool_key(ctx.type, ctx.variants.series, ctx.variants.size),
                str(exc),
            ) from exc
        except PoolBusyError as exc:
            raise ModelUnavailableError(
                _pool_key(ctx.type, ctx.variants.series, ctx.variants.size),
                str(exc),
            ) from exc
        except asyncio.CancelledError:
            if operation is not None:
                pool_task = _pool_task_for_context(ctx)
                operation.track_future(
                    _model_pool.builder_for_now(
                        pool_task,
                        ctx.variants.series,
                        ctx.variants.size,
                    )
                )
            raise
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("predict failed for task %s", t.id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return results


@app.post("/predict")
async def predict(request: Request) -> dict[str, Any]:
    """批量 (复数 tasks[]) 与交互 (单数 task) 共用端点, 按 wire 形态回不同响应:

    - 单数 ``{task, context}`` (平台 predict_interactive 的 exemplar / 交互调用) → **单数**
      ``PredictionResult`` (顶层 ``result``); 平台交互客户端读 ``data["result"]``, 必须单数。
    - 复数 ``{tasks, context}`` (批量预标) → ``BatchPredictResponse`` (``results[]``)。

    与 gsam2/sam3 的 /predict 双形态契约一致。
    """
    operation = await _begin_workload(request, AdmissionScope.PREDICT)
    try:
        body = await _request_json(request)
        is_single = isinstance(body, dict) and "task" in body and "tasks" not in body
        req = _validate_body(BatchPredictRequest, body)
        results = await _run_predict(req, operation=operation)
        if is_single:
            return results[0].model_dump(exclude_none=True)
        return BatchPredictResponse(results=results).model_dump(exclude_none=True)
    finally:
        await operation.close()


@app.post("/predict/interactive")
async def predict_interactive(request: Request) -> dict[str, Any]:
    """交互式单 task 路由 (兼容旧路径): 返回单数 PredictionResult, 与 /predict 单数 wire 一致。"""
    operation = await _begin_workload(request, AdmissionScope.PREDICT)
    try:
        req = _validate_body(InteractiveRequest, await _request_json(request))
        results = await _run_predict(
            BatchPredictRequest(tasks=[req.task], context=req.context),
            operation=operation,
        )
        return results[0].model_dump(exclude_none=True)
    finally:
        await operation.close()


def _is_supported_combo(ctx: Context) -> bool:
    series = ctx.variants.series
    size = ctx.variants.size
    if ctx.type == "text" or is_openvocab_series(series):
        return is_openvocab_supported(series, size)
    return size in MODEL_MATRIX.get(ctx.type, {}).get(series, ())


def _unsupported_combo_error(
    task: str, series: str, size: str
) -> VariantNotSupportedError:
    if is_openvocab_series(series) or task == "text":
        from model_registry import openvocab_sizes  # noqa: PLC0415

        return VariantNotSupportedError("size", size, openvocab_sizes(series))
    task_matrix = MODEL_MATRIX.get(task, {})
    if series not in task_matrix:
        return VariantNotSupportedError("series", series, tuple(task_matrix))
    return VariantNotSupportedError("size", size, tuple(task_matrix.get(series, ())))


def _pool_key(task: str, series: str, size: str) -> str:
    return f"{series}/{size}/{task}"


def _pool_task_for_context(ctx: Context) -> str:
    if ctx.type == "text":
        return POOL_TASK_OPENVOCAB
    if ctx.type == "exemplar":
        return POOL_TASK_OPENVOCAB_VP
    return ctx.type


def _validate_body(model_type, body):
    try:
        return model_type.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False)
        ) from exc


async def _request_json(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="invalid JSON request body"
        ) from exc


def _managed_lifecycle_headers(request: Request) -> tuple[str | None, str | None]:
    try:
        headers = parse_gpu_admission_header_values(
            request.headers.getlist(GPU_GENERATION_HEADER),
            request.headers.getlist(GPU_ADMISSION_TOKEN_HEADER),
        )
    except ValueError as exc:
        raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED) from exc
    return headers if headers is not None else (None, None)


def _managed_control_token(request: Request) -> str:
    try:
        return parse_gpu_control_token_header_values(
            request.headers.getlist(GPU_GENERATION_HEADER),
            request.headers.getlist(GPU_ADMISSION_TOKEN_HEADER),
        )
    except ValueError as exc:
        raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED) from exc


async def _begin_workload(
    request: Request,
    scope: AdmissionScope,
) -> WorkloadOperation:
    generation_header, token = _managed_lifecycle_headers(request)
    if _gpu_lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    return await _gpu_lifecycle.begin_workload(
        scope,
        generation_header=generation_header,
        token=token,
    )


@app.post("/unload")
async def unload(request: Request) -> dict[str, Any]:
    generation_header, token = _managed_lifecycle_headers(request)
    if _gpu_lifecycle is None:
        if generation_header is not None:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        return {"ok": True, "unloaded": 0}
    raw_body = await request.body()
    if not raw_body.strip():
        if generation_header is not None:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        return await _gpu_lifecycle.legacy_unload()
    body = _validate_body(GenerationTransitionRequest, await _request_json(request))
    response = await _gpu_lifecycle.managed_unload(
        body.generation,
        generation_header=generation_header,
        token=token,
    )
    return response.model_dump(mode="json")


@app.post("/drain")
async def drain(request: Request) -> dict[str, Any]:
    generation_header, token = _managed_lifecycle_headers(request)
    if _gpu_lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    body = _validate_body(GenerationTransitionRequest, await _request_json(request))
    response = await _gpu_lifecycle.drain(
        body.generation,
        generation_header=generation_header,
        token=token,
    )
    return response.model_dump(mode="json")


@app.post("/drain/cancel")
async def cancel_drain(request: Request) -> dict[str, Any]:
    generation_header, token = _managed_lifecycle_headers(request)
    if _gpu_lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    body = _validate_body(GenerationTransitionRequest, await _request_json(request))
    response = await _gpu_lifecycle.cancel_drain(
        body.generation,
        generation_header=generation_header,
        token=token,
    )
    return response.model_dump(mode="json")


@app.post("/lifecycle/mode")
async def lifecycle_mode(request: Request) -> dict[str, Any]:
    token = _managed_control_token(request)
    if _gpu_lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    body = _validate_body(LifecycleModeRequest, await _request_json(request))
    response = await _gpu_lifecycle.set_mode(
        body,
        token=token,
    )
    return response.model_dump(mode="json")


@app.post("/lifecycle/reset")
async def lifecycle_reset(request: Request) -> dict[str, Any]:
    token = _managed_control_token(request)
    if _gpu_lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    body = _validate_body(LifecycleResetRequest, await _request_json(request))
    response = await _gpu_lifecycle.reset(
        body,
        token=token,
    )
    return response.model_dump(mode="json")


@app.post("/warmup", response_model=WarmupResponse)
async def warmup(request: Request) -> WarmupResponse:
    """协议 §4.4: 加载指定 (task, series, size) 权重到 pool, 不跑 forward.

    重复预热同 variant 返回 cache_hit=true. pool 满时按 LRU 淘汰最旧的 key, evicted 字段
    回填被淘汰的 key 名供前端 toast 提示.
    """
    if _model_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    operation = await _begin_workload(request, AdmissionScope.WARMUP)
    try:
        req = _validate_body(WarmupRequest, await _request_json(request))
        return await _run_warmup(req, operation=operation)
    finally:
        await operation.close()


async def _run_warmup(
    req: WarmupRequest,
    *,
    operation: WorkloadOperation,
) -> WarmupResponse:
    assert _model_pool is not None
    series, size = req.variants.series, req.variants.size
    # 开集 series: 校验 + 预热. exemplar (task=interactive_seg, = /setup exemplar 模型条目的
    # task) 走独立 VP pool (与 /predict 视觉提示路径 _predict_visual_prompt 同 key), 否则首次
    # 拖框仍冷启 (见 issue 0003); 文本检测/分割走 openvocab pool (与文本路径同 key).
    if is_openvocab_series(series):
        if not is_openvocab_supported(series, size):
            raise _unsupported_combo_error(req.task, series, size)
        pool_task = (
            POOL_TASK_OPENVOCAB_VP
            if req.task == "interactive_seg"
            else POOL_TASK_OPENVOCAB
        )
    else:
        if size not in MODEL_MATRIX.get(req.task, {}).get(series, ()):
            raise _unsupported_combo_error(req.task, series, size)
        pool_task = req.task
    try:
        cache_hit, load_ms, evicted = await _model_pool.warmup(pool_task, series, size)
    except FileNotFoundError as exc:
        raise ModelUnavailableError(
            _pool_key(pool_task, series, size), str(exc)
        ) from exc
    except ModelBuildTimeout as exc:
        operation.track_future(exc.builder)
        raise ModelUnavailableError(
            _pool_key(pool_task, series, size), str(exc)
        ) from exc
    except PoolBusyError as exc:
        raise ModelUnavailableError(
            _pool_key(pool_task, series, size), str(exc)
        ) from exc
    except asyncio.CancelledError:
        operation.track_future(_model_pool.builder_for_now(pool_task, series, size))
        raise
    return WarmupResponse(
        ok=True,
        model_load_ms=load_ms,
        cache_hit=cache_hit,
        evicted=evicted,
    )


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
