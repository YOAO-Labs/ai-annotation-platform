"""Prometheus 指标定义.

镜像 grounded-sam2-backend/observability.py. 指标名与 grounded-sam2 逐字同名 (无
`sam3_` 前缀), 靠 Prometheus `service` label 区分两个 backend 实例, 不靠名字前缀.

暴露的 metric:
    embedding_cache_hits_total{prompt_type}      Counter
    embedding_cache_misses_total{prompt_type}    Counter
    embedding_cache_size                         Gauge
    inference_latency_seconds{prompt_type,cache} Histogram
    gpu_utilization_percent                      Gauge
    gpu_temperature_celsius                      Gauge
    gpu_power_watts                              Gauge
    gpu_memory_used_mb                           Gauge
    gpu_memory_total_mb                          Gauge
    container_cpu_percent                        Gauge
    container_memory_percent                     Gauge

`/metrics` 端点在 main.py 注册, 用 prometheus_client.generate_latest().
"""

from __future__ import annotations

import logging

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)


EMBEDDING_CACHE_HITS = Counter(
    "embedding_cache_hits_total",
    "SAM 3 image embedding 缓存命中次数",
    labelnames=("prompt_type",),
)

EMBEDDING_CACHE_MISSES = Counter(
    "embedding_cache_misses_total",
    "SAM 3 image embedding 缓存未命中次数",
    labelnames=("prompt_type",),
)

EMBEDDING_CACHE_SIZE = Gauge(
    "embedding_cache_size",
    "SAM 3 image embedding 缓存当前条目数",
)

INFERENCE_LATENCY = Histogram(
    "inference_latency_seconds",
    "/predict 端到端耗时 (秒) — SAM 3",
    labelnames=("prompt_type", "cache"),
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

MASK_AI_BACKEND_INFERENCE_TOTAL = Counter(
    "mask_ai_backend_inference_total",
    "Mask-capable video inference operations",
    labelnames=(
        "model_role",
        "operation",
        "fallback_reason",
        "candidate_count",
        "outcome",
    ),
)

MASK_AI_BACKEND_INFERENCE_SECONDS = Histogram(
    "mask_ai_backend_inference_seconds",
    "Mask-capable video inference duration in seconds",
    labelnames=("model_role", "operation", "outcome"),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)


GPU_UTILIZATION = Gauge("gpu_utilization_percent", "GPU SM 利用率 (%) — sam3-backend")
GPU_TEMPERATURE = Gauge("gpu_temperature_celsius", "GPU 温度 (°C) — sam3-backend")
GPU_POWER = Gauge("gpu_power_watts", "GPU 实时功耗 (W) — sam3-backend")
GPU_MEMORY_USED = Gauge("gpu_memory_used_mb", "GPU 已用显存 (MB)")
GPU_MEMORY_TOTAL = Gauge("gpu_memory_total_mb", "GPU 总显存 (MB)")
CONTAINER_CPU = Gauge("container_cpu_percent", "容器 CPU 利用率 (%) — sam3-backend")
CONTAINER_MEM = Gauge("container_memory_percent", "容器内存利用率 (%) — sam3-backend")


def record_inference(
    prompt_type: str, cache_status: str, duration_seconds: float
) -> None:
    INFERENCE_LATENCY.labels(prompt_type=prompt_type, cache=cache_status).observe(
        duration_seconds
    )


def record_mask_ai_backend_inference(
    *,
    model_role: str,
    operation: str,
    fallback_reason: str | None,
    candidate_count: int,
    outcome: str,
    duration_seconds: float,
) -> None:
    count_bucket = (
        "0"
        if candidate_count <= 0
        else "1"
        if candidate_count == 1
        else "2_3"
        if candidate_count <= 3
        else "4_10"
        if candidate_count <= 10
        else "11_plus"
    )
    normalized_fallback = (
        "mask_prompt_unsupported"
        if fallback_reason == "mask_prompt_unsupported"
        else "none"
    )
    normalized_outcome = outcome if outcome in {"success", "error"} else "error"
    normalized_operation = (
        operation if operation in {"tracking", "correction"} else "tracking"
    )
    try:
        MASK_AI_BACKEND_INFERENCE_TOTAL.labels(
            model_role=model_role,
            operation=normalized_operation,
            fallback_reason=normalized_fallback,
            candidate_count=count_bucket,
            outcome=normalized_outcome,
        ).inc()
        MASK_AI_BACKEND_INFERENCE_SECONDS.labels(
            model_role=model_role,
            operation=normalized_operation,
            outcome=normalized_outcome,
        ).observe(max(0.0, duration_seconds))
    except Exception:  # noqa: BLE001 - metrics must never break inference
        return


def record_cache(prompt_type: str, hit: bool) -> None:
    if hit:
        EMBEDDING_CACHE_HITS.labels(prompt_type=prompt_type).inc()
    else:
        EMBEDDING_CACHE_MISSES.labels(prompt_type=prompt_type).inc()


def update_cache_size(size: int) -> None:
    EMBEDDING_CACHE_SIZE.set(size)


_pynvml_initialized = False
_pynvml_handle = None
_psutil = None


def init_perfhud_collectors() -> None:
    """lifespan startup 调用一次. 失败不阻塞 (无 GPU 环境降级)."""
    global _pynvml_initialized, _pynvml_handle, _psutil
    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        _pynvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        _pynvml_initialized = True
        logger.info("pynvml initialized for GPU 0")
    except Exception as exc:  # noqa: BLE001
        logger.warning("pynvml init failed (无 GPU 或 driver 不可用): %s", exc)
        _pynvml_initialized = False
    try:
        import psutil  # type: ignore

        _psutil = psutil
        psutil.cpu_percent(interval=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("psutil 不可用: %s", exc)
        _psutil = None


def shutdown_perfhud_collectors() -> None:
    global _pynvml_initialized, _pynvml_handle
    if _pynvml_initialized:
        try:
            import pynvml  # type: ignore

            pynvml.nvmlShutdown()
        except Exception:  # noqa: BLE001
            pass
        _pynvml_initialized = False
        _pynvml_handle = None


def sample_perfhud() -> dict:
    """同步采样一次 GPU + 容器指标, 写入 Gauge 并返回 dict 供 /health 使用."""
    out: dict = {
        "gpu_utilization_percent": None,
        "gpu_temperature_celsius": None,
        "gpu_power_watts": None,
        "gpu_memory_used_mb": None,
        "gpu_memory_total_mb": None,
        "container_cpu_percent": None,
        "container_memory_percent": None,
    }
    if _pynvml_initialized and _pynvml_handle is not None:
        try:
            import pynvml  # type: ignore

            util = pynvml.nvmlDeviceGetUtilizationRates(_pynvml_handle).gpu
            temp = pynvml.nvmlDeviceGetTemperature(
                _pynvml_handle, pynvml.NVML_TEMPERATURE_GPU
            )
            power = pynvml.nvmlDeviceGetPowerUsage(_pynvml_handle) / 1000.0
            mem = pynvml.nvmlDeviceGetMemoryInfo(_pynvml_handle)
            out["gpu_utilization_percent"] = int(util)
            out["gpu_temperature_celsius"] = int(temp)
            out["gpu_power_watts"] = round(float(power), 1)
            out["gpu_memory_used_mb"] = int(mem.used / 1024**2)
            out["gpu_memory_total_mb"] = int(mem.total / 1024**2)
            GPU_UTILIZATION.set(out["gpu_utilization_percent"])
            GPU_TEMPERATURE.set(out["gpu_temperature_celsius"])
            GPU_POWER.set(out["gpu_power_watts"])
            GPU_MEMORY_USED.set(out["gpu_memory_used_mb"])
            GPU_MEMORY_TOTAL.set(out["gpu_memory_total_mb"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("pynvml sample failed: %s", exc)
    if _psutil is not None:
        try:
            cpu = _psutil.cpu_percent(interval=None)
            mem = _psutil.virtual_memory().percent
            out["container_cpu_percent"] = round(float(cpu), 1)
            out["container_memory_percent"] = round(float(mem), 1)
            CONTAINER_CPU.set(out["container_cpu_percent"])
            CONTAINER_MEM.set(out["container_memory_percent"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("psutil sample failed: %s", exc)
    return out
