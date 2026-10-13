"""Prometheus 指标定义 (含 PerfHud 扩展).

风格对齐 apps/api/app/observability/metrics.py: raw prometheus_client,
集中注册 + 一组 record_* helper.

暴露的 metric:
    embedding_cache_hits_total{prompt_type}      Counter
    embedding_cache_misses_total{prompt_type}    Counter
    embedding_cache_size                         Gauge
    inference_latency_seconds{prompt_type,cache} Histogram
    # PerfHud: NVML / psutil 实时指标
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
    "SAM 2 image embedding 缓存命中次数",
    labelnames=("prompt_type",),
)

EMBEDDING_CACHE_MISSES = Counter(
    "embedding_cache_misses_total",
    "SAM 2 image embedding 缓存未命中次数",
    labelnames=("prompt_type",),
)

EMBEDDING_CACHE_SIZE = Gauge(
    "embedding_cache_size",
    "SAM 2 image embedding 缓存当前条目数",
)

INFERENCE_LATENCY = Histogram(
    "inference_latency_seconds",
    "/predict 端到端耗时 (秒)",
    labelnames=("prompt_type", "cache"),
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


# sam2_video tracker 指标 (与图片池可分辨; sam_variant 维度).
VIDEO_TRACKER_FRAMES = Counter(
    "video_tracker_frames_processed_total",
    "sam2_video tracker 逐帧传播处理的帧数",
    labelnames=("sam_variant",),
)

VIDEO_TRACKER_LATENCY = Histogram(
    "video_tracker_latency_seconds",
    "sam2_video 单次窗口传播 (init_state→propagate→release) 端到端耗时 (秒)",
    labelnames=("sam_variant",),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
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


def record_video_tracker(
    sam_variant: str, frames: int, duration_seconds: float
) -> None:
    VIDEO_TRACKER_FRAMES.labels(sam_variant=sam_variant).inc(frames)
    VIDEO_TRACKER_LATENCY.labels(sam_variant=sam_variant).observe(duration_seconds)


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


# PerfHud · NVML / psutil 实时指标 (lifespan startup 初始化, /health + /metrics 共用)
GPU_UTILIZATION = Gauge("gpu_utilization_percent", "GPU SM 利用率 (%)")
GPU_TEMPERATURE = Gauge("gpu_temperature_celsius", "GPU 温度 (°C)")
GPU_POWER = Gauge("gpu_power_watts", "GPU 实时功耗 (W)")
GPU_MEMORY_USED = Gauge("gpu_memory_used_mb", "GPU 已用显存 (MB)")
GPU_MEMORY_TOTAL = Gauge("gpu_memory_total_mb", "GPU 总显存 (MB)")
CONTAINER_CPU = Gauge("container_cpu_percent", "容器 CPU 利用率 (cgroup 视角, %)")
CONTAINER_MEM = Gauge("container_memory_percent", "容器内存利用率 (%)")


def record_inference(
    prompt_type: str, cache_status: str, duration_seconds: float
) -> None:
    INFERENCE_LATENCY.labels(prompt_type=prompt_type, cache=cache_status).observe(
        duration_seconds
    )


def record_cache(prompt_type: str, hit: bool) -> None:
    if hit:
        EMBEDDING_CACHE_HITS.labels(prompt_type=prompt_type).inc()
    else:
        EMBEDDING_CACHE_MISSES.labels(prompt_type=prompt_type).inc()


def update_cache_size(size: int) -> None:
    EMBEDDING_CACHE_SIZE.set(size)


# ── PerfHud · pynvml + psutil 采样 ──────────────────────────────────────────

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
        # 第一次 cpu_percent 调用仅启采样窗口; 后续才返回真实值.
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
    """同步采样一次 GPU + 容器指标, 写入 Gauge 并返回 dict 供 /health 使用.

    返回字段 (任一不可用走 None):
      gpu_utilization_percent, gpu_temperature_celsius, gpu_power_watts,
      container_cpu_percent, container_memory_percent
    """
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
