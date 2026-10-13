"""Prometheus 指标 + GPU / 容器 PerfHud 采样 (yolo-backend).

指标命名与 sam3-backend / grounded-sam2-backend 对齐, 靠 Prometheus `service`
label 区分 backend 实例, 不靠 metric 名前缀. yolo 无 embedding cache, 该组 metric 不暴露;
新增 model_pool 维度 (因为 yolo 池可同时托管 ~7 series × ~5 size × 4 task 任意子集).

暴露的 metric:
    inference_latency_seconds{task,series,size}  Histogram
    model_pool_size                              Gauge (当前驻留 model 数)
    model_pool_loads_total{task,series,size}     Counter (lazy load 触发次数)
    model_pool_evicts_total                      Counter (LRU 淘汰次数)
    model_pool_idle_unloads_total                Counter (空闲卸载次数)
    gpu_utilization_percent / temperature / power / memory_used / memory_total  Gauge
    container_cpu_percent / container_memory_percent  Gauge

`/metrics` 端点在 main.py 注册.
"""

from __future__ import annotations

import logging

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)


INFERENCE_LATENCY = Histogram(
    "inference_latency_seconds",
    "/predict 端到端耗时 (秒) — yolo",
    labelnames=("task", "series", "size"),
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

MODEL_POOL_SIZE = Gauge("model_pool_size", "yolo model_pool 当前驻留 model 数")
MODEL_POOL_LOADS = Counter(
    "model_pool_loads_total",
    "yolo model_pool lazy load 触发次数",
    labelnames=("task", "series", "size"),
)
MODEL_POOL_EVICTS = Counter("model_pool_evicts_total", "yolo model_pool LRU 淘汰次数")
MODEL_POOL_IDLE_UNLOADS = Counter(
    "model_pool_idle_unloads_total", "yolo model_pool 空闲卸载次数"
)


GPU_UTILIZATION = Gauge("gpu_utilization_percent", "GPU SM 利用率 (%) — yolo-backend")
GPU_TEMPERATURE = Gauge("gpu_temperature_celsius", "GPU 温度 (°C) — yolo-backend")
GPU_POWER = Gauge("gpu_power_watts", "GPU 实时功耗 (W) — yolo-backend")
GPU_MEMORY_USED = Gauge("gpu_memory_used_mb", "GPU 已用显存 (MB)")
GPU_MEMORY_TOTAL = Gauge("gpu_memory_total_mb", "GPU 总显存 (MB)")
CONTAINER_CPU = Gauge("container_cpu_percent", "容器 CPU 利用率 (%) — yolo-backend")
CONTAINER_MEM = Gauge("container_memory_percent", "容器内存利用率 (%) — yolo-backend")


def record_inference(
    task: str, series: str, size: str, duration_seconds: float
) -> None:
    INFERENCE_LATENCY.labels(task=task, series=series, size=size).observe(
        duration_seconds
    )


def record_pool_load(task: str, series: str, size: str) -> None:
    MODEL_POOL_LOADS.labels(task=task, series=series, size=size).inc()


def record_pool_evict() -> None:
    MODEL_POOL_EVICTS.inc()


def record_pool_idle_unload() -> None:
    MODEL_POOL_IDLE_UNLOADS.inc()


def update_pool_size(size: int) -> None:
    MODEL_POOL_SIZE.set(size)


_pynvml_initialized = False
_pynvml_handle = None
_psutil = None


def init_perfhud_collectors() -> None:
    """lifespan startup 调用一次. 失败不阻塞 (无 GPU 环境降级)."""
    global _pynvml_initialized, _pynvml_handle, _psutil
    try:
        import pynvml

        pynvml.nvmlInit()
        _pynvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        _pynvml_initialized = True
        logger.info("pynvml initialized for GPU 0")
    except Exception as exc:  # noqa: BLE001
        logger.warning("pynvml init failed (无 GPU 或 driver 不可用): %s", exc)
        _pynvml_initialized = False
    try:
        import psutil

        _psutil = psutil
        psutil.cpu_percent(interval=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("psutil 不可用: %s", exc)
        _psutil = None


def shutdown_perfhud_collectors() -> None:
    global _pynvml_initialized, _pynvml_handle
    if _pynvml_initialized:
        try:
            import pynvml

            pynvml.nvmlShutdown()
        except Exception:  # noqa: BLE001
            pass
        _pynvml_initialized = False
        _pynvml_handle = None


def sample_perfhud() -> dict:
    """同步采样一次 GPU + 容器指标, 写入 Gauge 并返回 dict 供 /health 使用."""
    out: dict = {
        "gpu_device_name": None,
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
            import pynvml

            name = pynvml.nvmlDeviceGetName(_pynvml_handle)
            out["gpu_device_name"] = name.decode() if isinstance(name, bytes) else name
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
