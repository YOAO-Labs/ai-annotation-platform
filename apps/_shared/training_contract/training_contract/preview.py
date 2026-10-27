"""CPU-only native preview policy, shared without importing any ML framework."""

from .catalog import digest
from .validation import native_values as native_training_values, verify_frozen
from .dataset import DETECTION, config_task

PREVIEW_REVISION = 1
PREVIEW_PURPOSE = "augmentation-preview"
LIMITS = {
    "sources": 16,
    "candidate_scan": 512,
    "outputs": 4,
    "source_bytes": 8 * 1024**2,
    "pool_bytes": 64 * 1024**2,
    "source_pixels": 16_000_000,
    "pool_pixels": 32_000_000,
    "source_boxes": 256,
    "pool_boxes": 4096,
    "output_edge": 2560,
    "output_boxes": 4096,
    "output_bytes": 24 * 1024**2,
    "report_bytes": 8 * 1024**2,
    "address_bytes": 16 * 1024**3,
    "cpu_seconds": 30,
    "cpu_hard_seconds": 35,
    "child_seconds": 45,
    "execution_seconds": 60,
    "threads": 1,
    "ttl_seconds": 24 * 60 * 60,
    "put_seconds": 60,
}
POLICY = {
    "revision": PREVIEW_REVISION,
    "purpose": PREVIEW_PURPOSE,
    "frameworks": ["ultralytics"],
    "task": "image-detection",
    "model_family": "yolo11",
    "pool_selection": "sha256-seed-task-v1",
    "buffer_policy": "native-sequential-pool-load-v1",
    "native_detection_defaults": {"copy_paste": 0.0},
    "limits": LIMITS,
}
POLICY_DIGEST = digest(POLICY)
INPUT_KEYS = (
    "config",
    "config_digest",
    "native_values",
    "dataset_snapshot_digest",
    "policy_digest",
    "seed",
    "epoch",
    "count",
    "sources",
    "pool_summary",
)


def input_digest(spec):
    return digest({key: spec[key] for key in INPUT_KEYS})


def validate_request(config, seed, epoch, count):
    frozen = verify_frozen(config)
    if config["framework"] != "ultralytics" or config_task(config) != DETECTION:
        raise ValueError("当前仅 Ultralytics YOLO11 图片检测通过原生增强预览验证")
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("预览随机种子超出范围")
    if type(epoch) is not int or not 1 <= epoch <= config["parameters"]["epochs"]:
        raise ValueError("预览轮次必须位于本次训练轮次内")
    if type(count) is not int or not 1 <= count <= LIMITS["outputs"]:
        raise ValueError("一次最多生成四张预览")
    values = frozen["effective_values"]
    if values["imgsz"] * (1 + values["multi_scale"]) > LIMITS["output_edge"]:
        raise ValueError("当前多尺度配置超出原生预览尺寸上限")
    return frozen


def native_values(config):
    """Freeze the pinned inactive segmentation default and CPU materialization."""
    return {
        **native_training_values(verify_frozen(config)),
        **POLICY["native_detection_defaults"],
        "device": "cpu",
        "workers": 0,
        "task": "detect",
    }


def closed_mosaic(config, epoch):
    values = config["configuration"]["effective_values"]
    tail = values["close_mosaic"]
    return bool(tail and epoch - 1 >= values["epochs"] - tail)
