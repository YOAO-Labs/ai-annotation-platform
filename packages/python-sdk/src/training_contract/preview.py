"""CPU-only trainer preview bounds and digest contract without ML framework imports."""

from .dataset import DETECTION, config_task
from .trainer_plugin import canonical_json_digest as digest

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
    "pool_selection": "sha256-seed-task-v1",
    "buffer_policy": "native-sequential-pool-load-v1",
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
    if not isinstance(config, dict):
        raise ValueError("预览训练配置无效")
    try:
        resolved_task = config_task(config)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("原生增强预览当前仅支持图片检测") from None
    if resolved_task != DETECTION:
        raise ValueError("原生增强预览当前仅支持图片检测")
    plugin = config.get("trainer_plugin")
    if isinstance(plugin, dict) and "preview" not in (plugin.get("capabilities") or []):
        raise ValueError("当前训练器插件未声明增强预览能力")
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("预览随机种子超出范围")
    epochs = (config.get("parameters") or {}).get("epochs")
    if type(epochs) is not int or epochs < 1:
        raise ValueError("预览训练配置缺少有效训练轮次")
    if type(epoch) is not int or not 1 <= epoch <= epochs:
        raise ValueError("预览轮次必须位于本次训练轮次内")
    if type(count) is not int or not 1 <= count <= LIMITS["outputs"]:
        raise ValueError("一次最多生成四张预览")
    values = (config.get("configuration") or {}).get("effective_values") or {}
    imgsz = values.get("imgsz")
    multi_scale = values.get("multi_scale", 0.0)
    if (
        isinstance(imgsz, (int, float))
        and not isinstance(imgsz, bool)
        and isinstance(multi_scale, (int, float))
        and not isinstance(multi_scale, bool)
        and imgsz * (1 + multi_scale) > LIMITS["output_edge"]
    ):
        raise ValueError("当前多尺度配置超出原生预览尺寸上限")
    return config.get("configuration")
