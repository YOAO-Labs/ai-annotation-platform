"""Exact native architecture checks shared by initialization and checkpoints."""

from training_contract.models import model_spec
from training_contract.native_architecture import (
    validate_architecture_yaml,
    matches_architecture_signature,
)


def _reference_model(base_model, classes):
    import torch
    from ultralytics.nn.tasks import DetectionModel, SegmentationModel

    source_classes = None
    if isinstance(base_model, tuple):
        base_model, source_classes = base_model
    native_type = SegmentationModel if base_model.endswith("-seg") else DetectionModel
    with torch.random.fork_rng(devices=[]), torch.device("cpu"), torch.no_grad():
        model = native_type(base_model + ".yaml", nc=classes, verbose=False)
        if source_classes is not None:
            source = native_type(base_model + ".yaml", nc=source_classes, verbose=False)
            model.load(source, verbose=False)
        return model


def validate_architecture(model, base_model, *, classes=None):
    from ultralytics import __version__
    from ultralytics.nn.modules import head as native_heads
    from ultralytics.nn.tasks import yaml_model_load

    spec = model_spec("ultralytics", base_model)
    architecture = model.yaml
    head = model.model[-1]
    validate_architecture_yaml(architecture, spec, yaml_model_load(spec.yaml_filename))
    if (
        type(head) is not getattr(native_heads, spec.head)
        or classes is not None
        and head.nc != classes
        or (getattr(head, "one2one_cv2", None) is not None) != spec.end2end
        or spec.end2end
        and head.reg_max != 1
        or not matches_architecture_signature(
            model,
            spec.id,
            head.nc,
            _reference_model,
            __version__,
        )
    ):
        raise ValueError(
            "Unqualified native model family, scale, head or inference branch"
        )
    return spec
