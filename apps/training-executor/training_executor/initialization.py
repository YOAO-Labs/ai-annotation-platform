"""Native full-weight transfer and fresh-state evidence, separate from resume."""

import hashlib
import json
from pathlib import Path

from training_contract.initialization import validate_pin, verify_evidence
from training_contract.dataset import SEGMENTATION
from training_contract.observability import InputMismatchError

from .files import digest_file
from .native_identity import validate_architecture


def tensor_identity(model):
    identity = hashlib.sha256()
    state = model.state_dict()
    for name, value in sorted(state.items()):
        tensor = value.detach().cpu().contiguous()
        if tensor.is_floating_point():
            tensor = tensor.float()
        identity.update(
            json.dumps(
                [name, list(tensor.shape), str(tensor.dtype)], separators=(",", ":")
            ).encode()
        )
        identity.update(tensor.numpy().tobytes())
    return identity.hexdigest(), len(state)


def validate_input(spec):
    pin = spec["frozen_config"].get("initialization")
    if pin is None:
        return
    try:
        validate_pin(pin)
        if not spec.get("resume_path"):
            source = Path(spec["initialization_path"])
            if (
                not source.is_file()
                or source.stat().st_size != pin["artifact"]["size_bytes"]
                or digest_file(source) != pin["artifact"]["sha256"]
            ):
                raise ValueError(
                    "Native initialization artifact size or digest differs"
                )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InputMismatchError(str(exc)) from exc


def prepare(spec, model):
    pin = spec["frozen_config"].get("initialization")
    if pin is None:
        return None
    try:
        validate_pin(pin)
        common = {
            "policy_digest": pin["policy_digest"],
            "source_digest": pin["source_digest"],
            "source_sha256": pin["artifact"]["sha256"],
        }
        if spec.get("resume_path"):
            return {**common, "mode": "checkpoint_resume"}
        from ultralytics.nn.tasks import DetectionModel, SegmentationModel

        names = getattr(model.model, "names", None)
        ordered = (
            [names.get(index) for index in range(len(names))]
            if isinstance(names, dict)
            else names
        )
        segmentation = pin["task"] == SEGMENTATION
        expected_type = SegmentationModel if segmentation else DetectionModel
        validate_architecture(model.model, pin["base_model"], classes=len(ordered))
        if (
            type(model.model) is not expected_type
            or model.task != ("segment" if segmentation else "detect")
            or ordered != pin["classes"]
            or ordered != spec["frozen_config"]["classes"]
        ):
            raise ValueError(
                "Native initialization artifact, architecture or class identity differs"
            )
        fingerprint, count = tensor_identity(model.model)
        head_identity = tensor_identity(model.model.model[-1]) if segmentation else None
        return {
            **common,
            **(
                {
                    "mask_head_tensor_digest": head_identity[0],
                    "mask_head_tensor_count": head_identity[1],
                }
                if head_identity
                else {}
            ),
            "mode": "weights_only",
            "source_tensor_digest": fingerprint,
            "tensor_count": count,
        }
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InputMismatchError(str(exc)) from exc


def observation(spec, baseline, trainer):
    if baseline is None:
        return None
    evidence = {**baseline, "start_epoch": int(trainer.start_epoch)}
    if baseline["mode"] == "weights_only":
        model = getattr(trainer.model, "module", trainer.model)
        fingerprint, count = tensor_identity(model)
        if count != baseline["tensor_count"]:
            raise InputMismatchError("Native initialization state tensor count changed")
        if "mask_head_tensor_digest" in baseline:
            head_fingerprint, head_count = tensor_identity(model.model[-1])
            if head_count != baseline["mask_head_tensor_count"]:
                raise InputMismatchError(
                    "Native initialization mask head tensor count changed"
                )
            evidence["loaded_mask_head_tensor_digest"] = head_fingerprint
        evidence.update(
            loaded_tensor_digest=fingerprint,
            optimizer_state_count=len(trainer.optimizer.state),
            scheduler_last_epoch=trainer.scheduler.last_epoch,
            scaler_state=trainer.scaler.state_dict(),
            ema_updates=trainer.ema.updates,
            best_fitness=float(trainer.best_fitness)
            if trainer.best_fitness is not None
            else None,
            best_weights_present=Path(trainer.best).exists(),
        )
    try:
        verify_evidence(
            spec["frozen_config"], evidence, resumed=bool(spec.get("resume_path"))
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InputMismatchError(str(exc)) from exc
    return evidence
