"""v0.8.7 F2 · Prometheus 指标集中注册。

- `ml_backend_request_duration_seconds` Histogram(backend_id, outcome)：
  ML backend predict / interactive 调用耗时；outcome ∈ {"success","error"}。
- `celery_queue_length` Gauge(queue)：active + reserved 数量之和。
- `celery_worker_heartbeat_seconds` Gauge(worker)：上次心跳距今秒数。

后两者按需在 /health/celery 端点采样填充（懒采样，避免 beat 高频抓取 broker）。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram


HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests",
    labelnames=("method", "path", "status_code"),
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=("method", "path"),
)


ML_BACKEND_REQUEST_DURATION = Histogram(
    "ml_backend_request_duration_seconds",
    "ML backend predict/interactive 单次调用耗时（秒）",
    labelnames=("backend_id", "outcome"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

CELERY_QUEUE_LENGTH = Gauge(
    "celery_queue_length",
    "Celery 队列待处理 + 在执行任务数",
    labelnames=("queue",),
)

CELERY_WORKER_HEARTBEAT_SECONDS = Gauge(
    "celery_worker_heartbeat_seconds",
    "Celery worker 上次心跳距今秒数（越小越新鲜）",
    labelnames=("worker",),
)

VIDEO_CHUNK_REQUESTS_TOTAL = Counter(
    "video_chunk_requests_total",
    "视频 chunk 请求次数",
    labelnames=("status",),
)

VIDEO_CHUNK_GENERATION_SECONDS = Histogram(
    "video_chunk_generation_seconds",
    "视频 chunk 生成耗时（秒）",
    labelnames=("outcome",),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

VIDEO_FRAME_CACHE_TOTAL = Counter(
    "video_frame_cache_total",
    "视频单帧缓存命中情况",
    labelnames=("result", "format"),
)

VIDEO_FRAME_EXTRACTION_SECONDS = Histogram(
    "video_frame_extraction_seconds",
    "视频单帧抽取耗时（秒）",
    labelnames=("outcome", "format"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

VIDEO_FRAME_ASSET_BYTES = Gauge(
    "video_frame_asset_bytes",
    "视频帧服务已缓存对象字节数",
    labelnames=("asset_type",),
)

IMAGE_PYRAMID_API_REQUESTS_TOTAL = Counter(
    "image_pyramid_api_requests_total",
    "Image-pyramid manifest and asset URL API outcomes",
    labelnames=("operation", "outcome"),
)

IMAGE_PYRAMID_ASSET_URL_ITEMS_TOTAL = Counter(
    "image_pyramid_asset_url_items_total",
    "Signed image-pyramid asset URL items",
    labelnames=("kind",),
)

IMAGE_PYRAMID_GENERATIONS_TOTAL = Counter(
    "image_pyramid_generations_total",
    "Immutable image-pyramid generation outcomes",
    labelnames=("outcome", "error_code"),
)

IMAGE_PYRAMID_PHASE_DURATION_SECONDS = Histogram(
    "image_pyramid_phase_duration_seconds",
    "Image-pyramid worker phase duration in seconds",
    labelnames=("phase", "outcome"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900, 1800),
)

IMAGE_PYRAMID_SOURCE_BYTES = Histogram(
    "image_pyramid_source_bytes",
    "Compressed source bytes admitted by image-pyramid jobs",
    buckets=(
        1_048_576,
        8_388_608,
        33_554_432,
        134_217_728,
        536_870_912,
        2_147_483_648,
        8_589_934_592,
    ),
)

IMAGE_PYRAMID_DECODED_PIXELS = Histogram(
    "image_pyramid_decoded_pixels",
    "Logical decoded pixels in completed image-pyramid jobs",
    buckets=(16_777_216, 50_000_000, 100_000_000, 200_000_000, 300_000_000),
)

IMAGE_PYRAMID_TILE_COUNT = Histogram(
    "image_pyramid_tile_count",
    "Verified tiles in completed image-pyramid jobs",
    buckets=(64, 128, 256, 512, 1024, 2048, 4096, 8192, 20_000),
)

IMAGE_PYRAMID_DERIVED_BYTES = Histogram(
    "image_pyramid_derived_bytes",
    "Retained derived bytes in completed image-pyramid jobs",
    buckets=(
        1_048_576,
        8_388_608,
        33_554_432,
        134_217_728,
        536_870_912,
        2_147_483_648,
        8_589_934_592,
    ),
)

IMAGE_PYRAMID_TEMP_BYTES = Histogram(
    "image_pyramid_temp_bytes",
    "Peak accounted source plus generated temporary bytes",
    buckets=(
        8_388_608,
        33_554_432,
        134_217_728,
        536_870_912,
        2_147_483_648,
        8_589_934_592,
        12_884_901_888,
    ),
)

IMAGE_PYRAMID_ASSET_BYTES = Gauge(
    "image_pyramid_asset_bytes",
    "Bytes retained by ready image-pyramid generations",
)

IMAGE_PYRAMID_GC_TOTAL = Counter(
    "image_pyramid_gc_total",
    "Image-pyramid reconciliation outcomes",
    labelnames=("outcome",),
)

RASTER_MASK_CONTENT_OPERATIONS_TOTAL = Counter(
    "raster_mask_content_operations_total",
    "Raster mask 内容存储操作次数",
    labelnames=("operation", "outcome"),
)

RASTER_MASK_CONTENT_ERRORS_TOTAL = Counter(
    "raster_mask_content_errors_total",
    "Raster mask 内容存储错误次数",
    labelnames=("operation", "reason"),
)

RASTER_MASK_ACTIVE_GEOMETRIES = Gauge(
    "raster_mask_active_geometries",
    "当前持久化的活跃图片 raster mask 几何数量",
    labelnames=("kind",),
)

MASK_AI_OPERATIONS_TOTAL = Counter(
    "mask_ai_operations_total",
    "Native Mask AI operations by controlled interaction outcome",
    labelnames=(
        "operation",
        "prompt_family",
        "output_geometry",
        "candidate_count",
        "decision",
        "fallback_reason",
        "outcome",
    ),
)

MASK_AI_PHASE_DURATION_SECONDS = Histogram(
    "mask_ai_phase_duration_seconds",
    "Native Mask AI phase duration in seconds",
    labelnames=("operation", "phase", "outcome"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)

MASK_AI_CORRECTION_JOBS = Gauge(
    "mask_ai_correction_jobs",
    "Video Mask correction jobs by persisted status",
    labelnames=("status",),
)

MASK_AI_CORRECTION_OLDEST_AGE_SECONDS = Gauge(
    "mask_ai_correction_oldest_age_seconds",
    "Age of the oldest active Video Mask correction job",
    labelnames=("status",),
)

MASK_AI_STAGED_MASK_REFERENCES = Gauge(
    "mask_ai_staged_mask_references",
    "Distinct staged raster Mask object references by tracker job kind",
    labelnames=("job_kind",),
)

MASK_AI_ACCEPT_DECISIONS = Gauge(
    "mask_ai_accept_decisions",
    "Native Mask accept idempotency decisions by expiry state",
    labelnames=("state",),
)

MASK_AI_OLDEST_EXPIRED_DECISION_AGE_SECONDS = Gauge(
    "mask_ai_oldest_expired_decision_age_seconds",
    "Age of the oldest expired native Mask accept decision",
)

RASTER_MASK_CONTENT_OPERATIONS = frozenset({"load", "store", "verify"})
RASTER_MASK_CONTENT_OUTCOMES = frozenset({"success", "error"})
RASTER_MASK_CONTENT_ERROR_REASONS = frozenset(
    {
        "missing_object",
        "digest_mismatch",
        "size_mismatch",
        "run_mismatch",
        "byte_mismatch",
        "invalid_encoding",
        "invalid_payload",
        "storage_unavailable",
        "unknown",
    }
)

MASK_AI_OPERATIONS = frozenset({"single_frame", "refine", "correction"})
MASK_AI_PROMPT_FAMILIES = frozenset(
    {
        "point",
        "bbox",
        "mask",
        "scribble",
        "text",
        "exemplar",
        "correction_frame",
        "unknown",
    }
)
MASK_AI_OUTPUT_GEOMETRIES = frozenset({"mask", "polygon", "bbox", "unknown"})
MASK_AI_CANDIDATE_COUNT_BUCKETS = frozenset({"0", "1", "2_3", "4_10", "11_plus"})
MASK_AI_DECISIONS = frozenset({"none", "accept", "reject", "cancel"})
MASK_AI_FALLBACK_REASONS = frozenset({"none", "mask_prompt_unsupported", "unknown"})
MASK_AI_OUTCOMES = frozenset({"success", "error", "conflict"})
MASK_AI_PHASES = frozenset({"decode", "inference", "encode", "upload", "commit"})


def record_raster_mask_content_operation(
    operation: str,
    outcome: str,
    *,
    error_reason: str | None = None,
) -> None:
    """Best-effort metric update; instrumentation never breaks content I/O."""
    if (
        operation not in RASTER_MASK_CONTENT_OPERATIONS
        or outcome not in RASTER_MASK_CONTENT_OUTCOMES
    ):
        return
    reason = (
        error_reason if error_reason in RASTER_MASK_CONTENT_ERROR_REASONS else "unknown"
    )
    try:
        RASTER_MASK_CONTENT_OPERATIONS_TOTAL.labels(
            operation=operation,
            outcome=outcome,
        ).inc()
        if outcome == "error":
            RASTER_MASK_CONTENT_ERRORS_TOTAL.labels(
                operation=operation,
                reason=reason,
            ).inc()
    except Exception:  # noqa: BLE001 - metrics must not break content I/O
        return


def mask_ai_candidate_count_bucket(count: int) -> str:
    """Collapse candidate counts into a fixed label vocabulary."""
    if count <= 0:
        return "0"
    if count == 1:
        return "1"
    if count <= 3:
        return "2_3"
    if count <= 10:
        return "4_10"
    return "11_plus"


def mask_ai_operation(context: dict | None) -> str:
    """Classify a single-frame request without retaining prompt contents."""
    payload = context or {}
    return (
        "refine"
        if payload.get("mask_prompt") or payload.get("mask_input")
        else "single_frame"
    )


def mask_ai_prompt_family(context: dict | None) -> str:
    prompt = str((context or {}).get("type") or "unknown")
    prompt = "bbox" if prompt == "interactive_box" else prompt
    return prompt if prompt in MASK_AI_PROMPT_FAMILIES else "unknown"


def record_mask_ai_operation(
    *,
    operation: str,
    prompt_family: str,
    output_geometry: str,
    candidate_count: int = 0,
    decision: str = "none",
    fallback_reason: str | None = None,
    outcome: str,
) -> None:
    """Record only controlled, low-cardinality labels; never propagate caller IDs."""
    normalized = {
        "operation": operation if operation in MASK_AI_OPERATIONS else "single_frame",
        "prompt_family": (
            prompt_family if prompt_family in MASK_AI_PROMPT_FAMILIES else "unknown"
        ),
        "output_geometry": (
            output_geometry
            if output_geometry in MASK_AI_OUTPUT_GEOMETRIES
            else "unknown"
        ),
        "candidate_count": mask_ai_candidate_count_bucket(candidate_count),
        "decision": decision if decision in MASK_AI_DECISIONS else "none",
        "fallback_reason": (
            fallback_reason
            if fallback_reason in MASK_AI_FALLBACK_REASONS
            else "none"
            if fallback_reason is None
            else "unknown"
        ),
        "outcome": outcome if outcome in MASK_AI_OUTCOMES else "error",
    }
    try:
        MASK_AI_OPERATIONS_TOTAL.labels(**normalized).inc()
    except Exception:  # noqa: BLE001 - metrics must not break user operations
        return


def observe_mask_ai_phase(
    *,
    operation: str,
    phase: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    """Observe one controlled phase without high-cardinality request context."""
    normalized_operation = (
        operation if operation in MASK_AI_OPERATIONS else "single_frame"
    )
    normalized_phase = phase if phase in MASK_AI_PHASES else "inference"
    normalized_outcome = outcome if outcome in MASK_AI_OUTCOMES else "error"
    try:
        MASK_AI_PHASE_DURATION_SECONDS.labels(
            operation=normalized_operation,
            phase=normalized_phase,
            outcome=normalized_outcome,
        ).observe(max(0.0, duration_seconds))
    except Exception:  # noqa: BLE001 - metrics must not break user operations
        return


def observe_ml_backend(
    backend_id: str | None, outcome: str, duration_seconds: float
) -> None:
    ML_BACKEND_REQUEST_DURATION.labels(
        backend_id=backend_id or "unknown",
        outcome=outcome,
    ).observe(duration_seconds)
