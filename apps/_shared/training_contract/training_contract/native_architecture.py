"""Framework-free topology and tensor-shape identity, including official legacy YAML."""

from copy import deepcopy
from functools import lru_cache
from pathlib import Path


def validate_architecture_yaml(actual, spec, canonical):
    if not isinstance(actual, dict) or any(
        actual.get(key, 3) != 3 for key in ("ch", "channels")
    ):
        raise ValueError("Native input-channel identity differs")
    expected_graph = {key: canonical[key] for key in ("backbone", "head")}
    if actual.get("scale") is not None:
        if (
            actual["scale"] != spec.scale
            or Path(actual.get("yaml_file", "")).name != spec.yaml_filename
            or actual.get("scales") != canonical.get("scales")
        ):
            raise ValueError("Native family or scale metadata differs")
    else:
        if spec.family != "yolov8":
            raise ValueError("Only the explicit legacy YOLOv8 YAML layout is supported")
        depth, width, maximum = canonical["scales"][spec.scale]
        if (
            actual.get("depth_multiple") != depth
            or actual.get("width_multiple") != width
            or actual.get("yaml_file")
            and Path(actual["yaml_file"]).name != spec.yaml_filename
        ):
            raise ValueError("Legacy native scale metadata differs")
        expected_graph = deepcopy(expected_graph)
        for layer in expected_graph["backbone"] + expected_graph["head"]:
            if layer[2] in {"Conv", "C2f", "SPPF"}:
                layer[3][0] = min(layer[3][0], maximum)
    if any(actual.get(key) != value for key, value in expected_graph.items()):
        raise ValueError("Native backbone or head topology differs")


def architecture_signature(model):
    return (
        tuple(
            (name, type(module).__module__, type(module).__qualname__)
            for name, module in model.named_modules(remove_duplicate=False)
        ),
        tuple(
            (name, tuple(tensor.shape)) for name, tensor in model.state_dict().items()
        ),
    )


@lru_cache(maxsize=128)
def reference_signature(base_model, classes, factory, framework_version):
    """Cache only compact identity data; the runtime owns construction and RNG isolation."""
    return architecture_signature(factory(base_model, classes))


def matches_architecture_signature(
    model, base_model, classes, factory, framework_version
):
    actual = architecture_signature(model)
    return actual == reference_signature(
        base_model, classes, factory, framework_version
    ) or actual == reference_signature(
        (base_model, 80), classes, factory, framework_version
    )
