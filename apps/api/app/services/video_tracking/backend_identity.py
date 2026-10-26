"""Build portable video tracker identities from the selected backend facts."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from aap_preannotation_results.video_execution_plan import BackendCompatibilityIdentity

from aap_inference.tracker import (
    TrackerFrameResult,
    bbox_from_geometry as _bbox_from_geometry,
    bbox_from_points as _bbox_from_points,
)
from aap_inference_contract.topology import to_record
from app.services.video_tracking.adapters import MockBboxTrackerAdapter, TrackerContext

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_BUILTIN_MODEL_KEY = "mock_bbox"
_BUILTIN_IMPLEMENTATION_ID = "aap-mock-bbox/v1"
_BUILTIN_CAPABILITY_SCHEMA = "aap-video-builtin-capability/v1"
_REGISTRY_CAPABILITY_SCHEMA = "aap-video-registry-capability/v1"


class VideoBackendIdentityError(ValueError):
    """The selected tracker backend cannot be represented without guessing."""


def _value(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _uuid_text(value: object) -> str | None:
    if not isinstance(value, uuid.UUID) and type(value) is not str:
        return None
    try:
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    text = str(parsed)
    if type(value) is str and value != text:
        return None
    return text


def _exact_text(value: object) -> str | None:
    if type(value) is not str or not value or value.strip() != value:
        return None
    return value


def _text_set(value: object) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    if any(type(item) is not str or not item or item.strip() != item for item in value):
        return None
    return sorted(set(value))


def _positive_limit(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _model_entries(backend: object) -> Sequence[Mapping[str, Any]]:
    health_meta = _value(backend, "health_meta")
    capabilities = _value(health_meta, "capabilities")
    models = _value(capabilities, "models")
    if models is None and isinstance(capabilities, Mapping):
        supported = capabilities.get("supported_trackers")
        if _text_set(supported):
            # The legacy aggregate has no per-model artifact identity.
            return [
                {
                    **capabilities,
                    "id": None,
                    "weights_release_id": None,
                    "weights_sha256": None,
                    "tracker_context_mode": "none",
                    "max_context_frames": 0,
                }
            ]
    if not isinstance(models, (list, tuple)) or any(
        not isinstance(item, Mapping) for item in models
    ):
        raise VideoBackendIdentityError("tracker_backend_capabilities_missing")
    return models


def _selected_model(
    backend: object,
    *,
    model_key: str,
    model_id: str | None,
) -> Mapping[str, Any]:
    matches: list[Mapping[str, Any]] = []
    for entry in _model_entries(backend):
        supported = entry.get("supported_trackers")
        if not isinstance(supported, (list, tuple)) or model_key not in supported:
            continue
        if model_id is not None and str(entry.get("id") or "") != model_id:
            continue
        matches.append(entry)
    if len(matches) != 1:
        raise VideoBackendIdentityError(
            "tracker_backend_model_missing"
            if not matches
            else "tracker_backend_model_ambiguous"
        )
    return matches[0]


def _weights(model: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Read only the explicit release and digest fields of this exact model."""
    release = _exact_text(model.get("weights_release_id"))
    digest = model.get("weights_sha256")
    if type(digest) is not str or _DIGEST_RE.fullmatch(digest) is None:
        return None, None
    if release is None or len(release) > 255:
        return None, None
    return release, digest


def _tracker_limits(model: Mapping[str, Any]) -> tuple[str, int, int | None]:
    max_window_frames = _positive_limit(model.get("max_window_frames"))
    context_mode = model.get("tracker_context_mode")
    max_context_frames = _positive_limit(model.get("max_context_frames"))
    if context_mode == "session" and max_context_frames is not None:
        return "session", max_context_frames, max_window_frames
    return "none", 0, max_window_frames


def _capability_digest(
    *,
    schema: str,
    role: Literal["track", "discovery"],
    model_key: str,
    model_id: str | None,
    model: Mapping[str, Any] | None,
    tracker_context_mode: Literal["none", "session"],
    max_context_frames: int,
    max_window_frames: int | None,
    builtin_implementation_id: str | None = None,
    dispatch_parameters: object = None,
) -> str | None:
    try:
        payload: dict[str, Any] = {
            "schema": schema,
            "role": role,
            "model_key": model_key,
            "model_id": model_id,
            "tracker_context_mode": tracker_context_mode,
            "max_context_frames": max_context_frames,
            "max_window_frames": max_window_frames,
            "builtin_implementation_id": builtin_implementation_id,
            "dispatch_parameters": dispatch_parameters,
        }
        if model is not None:
            for field in (
                "supported_trackers",
                "supported_prompts",
                "supported_inputs",
                "supported_geometric_outputs",
                "text_driven_trackers",
            ):
                normalized = _text_set(model.get(field, []))
                if normalized is None:
                    return None
                payload[field] = normalized
            payload["defaults"] = {
                key: model.get(key)
                for key in (
                    "default_thresholds",
                    "default_variants",
                    "supported_variants",
                    "variant_combinations",
                    "params",
                )
            }
        encoded = json.dumps(
            to_record(payload),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def bind_video_normalization_policy(identities, *, outside_threshold):
    """Refuse cross-worker reuse under a different result-processing policy."""
    if type(outside_threshold) not in (float, int) or not math.isfinite(
        outside_threshold
    ):
        raise VideoBackendIdentityError("tracker_normalization_policy_invalid")
    policy = {
        "schema": "aap-video-frame-normalization/v1",
        "outside_threshold": repr(float(outside_threshold)),
        "multiplex_association": "aap-video-multiplex-association/v1",
    }
    return tuple(
        identity.model_copy(
            update={
                "capability_digest": hashlib.sha256(
                    json.dumps(
                        {
                            "backend_capability_digest": identity.capability_digest,
                            "normalization": policy,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
                if identity.capability_digest is not None
                else None
            }
        )
        for identity in identities
    )


def builtin_mock_implementation_digest() -> str | None:
    """Hash source for the loaded built-in adapter and all geometry helpers it uses.

    Source inspection is deliberately fail-closed. A worker without inspectable source
    can still run a first mock prediction, but its plan cannot claim reusable identity.
    """
    try:
        implementation = (
            ("MockBboxTrackerAdapter", MockBboxTrackerAdapter),
            ("TrackerContext", TrackerContext),
            ("TrackerFrameResult", TrackerFrameResult),
            ("_bbox_from_geometry", _bbox_from_geometry),
            ("_bbox_from_points", _bbox_from_points),
            ("MockBboxTrackerAdapter.propagate", MockBboxTrackerAdapter.propagate),
        )
        digest = hashlib.sha256()
        for name, artifact in implementation:
            source = inspect.getsource(artifact).encode("utf-8", errors="strict")
            name_bytes = name.encode("utf-8", errors="strict")
            digest.update(len(name_bytes).to_bytes(4, "big"))
            digest.update(name_bytes)
            digest.update(len(source).to_bytes(8, "big"))
            digest.update(source)
        model_key = MockBboxTrackerAdapter.model_key.encode("utf-8", errors="strict")
        digest.update(len(model_key).to_bytes(4, "big"))
        digest.update(model_key)
        return digest.hexdigest()
    except Exception:
        return None


def _runtime_incarnation(
    *,
    runtime_snapshot: object | None,
    bound_scope: object | None,
    registry_id: str,
) -> str | None:
    if runtime_snapshot is None or bound_scope is None:
        return None
    scope_registry = _uuid_text(_value(bound_scope, "registry_id"))
    scope_epoch = _uuid_text(_value(bound_scope, "authority_epoch"))
    registries = _value(runtime_snapshot, "registries")
    if (
        scope_registry != registry_id
        or scope_epoch is None
        or not isinstance(registries, (list, tuple))
    ):
        return None
    matches = [
        row
        for row in registries
        if _uuid_text(_value(row, "registry_id")) == registry_id
    ]
    if len(matches) != 1:
        return None
    row = matches[0]
    if (
        _value(row, "continuity") != "ready"
        or _uuid_text(_value(row, "authority_epoch")) != scope_epoch
    ):
        return None
    return _uuid_text(_value(row, "backend_incarnation"))


def _registry_identity(
    *,
    role: Literal["track", "discovery"],
    model_key: str,
    backend: object,
    model_id: str | None,
    runtime_snapshot: object | None,
    bound_scope: object | None,
) -> BackendCompatibilityIdentity:
    registry_id = _uuid_text(_value(backend, "id"))
    if registry_id is None:
        raise VideoBackendIdentityError("tracker_backend_registry_identity_missing")
    model = _selected_model(backend, model_key=model_key, model_id=model_id)
    selected_model_id = _exact_text(model.get("id"))
    if selected_model_id is not None and len(selected_model_id) > 255:
        selected_model_id = None
    context_mode, max_context_frames, max_window_frames = _tracker_limits(model)
    weights_release_id, weights_sha256 = _weights(model)
    capability_digest = _capability_digest(
        schema=_REGISTRY_CAPABILITY_SCHEMA,
        role=role,
        model_key=model_key,
        model_id=selected_model_id,
        model=model,
        tracker_context_mode=context_mode,
        max_context_frames=max_context_frames,
        max_window_frames=max_window_frames,
        dispatch_parameters=_value(backend, "extra_params") or {},
    )
    return BackendCompatibilityIdentity(
        role=role,
        provider_kind="registry",
        registry_id=registry_id,
        backend_incarnation=_runtime_incarnation(
            runtime_snapshot=runtime_snapshot,
            bound_scope=bound_scope,
            registry_id=registry_id,
        ),
        builtin_implementation_id=None,
        builtin_implementation_digest=None,
        model_key=model_key,
        model_id=selected_model_id,
        weights_release_id=weights_release_id,
        weights_sha256=weights_sha256,
        tracker_context_mode=context_mode,
        max_context_frames=max_context_frames,
        max_window_frames=max_window_frames,
        capability_digest=capability_digest,
    )


def _builtin_identity(
    *,
    role: Literal["track", "discovery"],
    model_key: str,
) -> BackendCompatibilityIdentity:
    if role != "track" or model_key != _BUILTIN_MODEL_KEY:
        raise VideoBackendIdentityError("tracker_backend_registry_identity_missing")
    implementation_digest = builtin_mock_implementation_digest()
    capability_digest = _capability_digest(
        schema=_BUILTIN_CAPABILITY_SCHEMA,
        role="track",
        model_key=_BUILTIN_MODEL_KEY,
        model_id=_BUILTIN_MODEL_KEY,
        model=None,
        tracker_context_mode="none",
        max_context_frames=0,
        max_window_frames=None,
        builtin_implementation_id=_BUILTIN_IMPLEMENTATION_ID,
    )
    return BackendCompatibilityIdentity(
        role="track",
        provider_kind="builtin",
        registry_id=None,
        backend_incarnation=None,
        builtin_implementation_id=_BUILTIN_IMPLEMENTATION_ID,
        builtin_implementation_digest=implementation_digest,
        model_key=_BUILTIN_MODEL_KEY,
        model_id=_BUILTIN_MODEL_KEY,
        weights_release_id=None,
        weights_sha256=None,
        tracker_context_mode="none",
        max_context_frames=0,
        max_window_frames=None,
        capability_digest=capability_digest,
    )


def build_video_backend_compatibility(
    *,
    model_key: str,
    selected_backend: object | None,
    model_id: str | None = None,
    runtime_snapshot: object | None = None,
    bound_scope: object | None = None,
    role: Literal["track", "discovery"] = "track",
    discovery_backend: object | None = None,
    discovery_model_key: str = "sam3_video",
    discovery_model_id: str | None = None,
) -> tuple[BackendCompatibilityIdentity, ...]:
    """Build one identity per selected video model role without fabricating facts.

    The optional discovery role is used by the combo tracker. It normally shares the
    physical registry and route scope with the interactive tracking role, while retaining
    its own exact model capability entry and digest.
    """
    if role not in {"track", "discovery"}:
        raise VideoBackendIdentityError("tracker_backend_role_invalid")
    if selected_backend is None:
        primary = _builtin_identity(role=role, model_key=model_key)
    else:
        primary = _registry_identity(
            role=role,
            model_key=model_key,
            backend=selected_backend,
            model_id=model_id,
            runtime_snapshot=runtime_snapshot,
            bound_scope=bound_scope,
        )
    if discovery_backend is None:
        return (primary,)
    if role != "track":
        raise VideoBackendIdentityError("tracker_backend_role_invalid")
    discovery = _registry_identity(
        role="discovery",
        model_key=discovery_model_key,
        backend=discovery_backend,
        model_id=discovery_model_id,
        runtime_snapshot=runtime_snapshot,
        bound_scope=bound_scope,
    )
    return primary, discovery
