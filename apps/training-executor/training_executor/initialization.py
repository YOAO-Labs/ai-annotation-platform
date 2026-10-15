"""Native full-weight transfer and fresh-state evidence, separate from resume."""

import hashlib
import json
from pathlib import Path

from training_contract.initialization import validate_pin, verify_evidence
from training_contract.observability import InputMismatchError

from .files import digest_file


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
        from ultralytics.nn.tasks import DetectionModel

        names = getattr(model.model, "names", None)
        ordered = (
            [names.get(index) for index in range(len(names))]
            if isinstance(names, dict)
            else names
        )
        if (
            type(model.model) is not DetectionModel
            or model.task != "detect"
            or Path(model.model.yaml.get("yaml_file", "")).name
            != pin["base_model"] + ".yaml"
            or model.model.yaml.get("scale") != pin["base_model"][-1]
            or ordered != pin["classes"]
            or ordered != spec["frozen_config"]["classes"]
        ):
            raise ValueError(
                "Native initialization artifact, architecture or class identity differs"
            )
        fingerprint, count = tensor_identity(model.model)
        return {
            **common,
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
