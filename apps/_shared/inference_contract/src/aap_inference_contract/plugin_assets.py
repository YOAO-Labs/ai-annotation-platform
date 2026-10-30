"""Installation-scoped pretrained selection descriptors and approval facts."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)


class PluginAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, strict=True)
    path: StrictStr = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def canonical_path(self):
        path = PurePosixPath(self.path)
        if (
            "\\" in self.path
            or "\x00" in self.path
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
            or str(path) != self.path
        ):
            raise ValueError("asset path must be canonical and data-directory relative")
        return self


class PluginAssetSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: StrictStr = Field(min_length=1, max_length=128)
    variants: dict[StrictStr, StrictStr]
    asset_free: StrictBool
    assets: list[PluginAsset]

    @model_validator(mode="after")
    def validate_selection(self):
        strings = [self.model_id, *self.variants.keys(), *self.variants.values()]
        if any(not item or item.strip() != item for item in strings):
            raise ValueError("model and variant strings must be canonical")
        if self.asset_free != (not self.assets):
            raise ValueError(
                "asset-free selection must contain no assets and vice versa"
            )
        if len({item.path for item in self.assets}) != len(self.assets):
            raise ValueError("duplicate asset path")
        return self


class PluginAssetDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    plugin_id: StrictStr = Field(min_length=1)
    plugin_version: StrictStr = Field(min_length=1)
    artifact: StrictStr = Field(min_length=1)
    asset_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selections: list[PluginAssetSelection] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_selections(self):
        keys = [
            (item.model_id, tuple(sorted(item.variants.items())))
            for item in self.selections
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate model selection")
        paths = {}
        for selection in self.selections:
            for asset in selection.assets:
                identity = (asset.sha256, asset.size_bytes)
                if asset.path in paths and paths[asset.path] != identity:
                    raise ValueError("asset path has conflicting identities")
                paths[asset.path] = identity
        return self

    def sha256(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class PluginSelectionKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: StrictStr = Field(min_length=1, max_length=128)
    variants: dict[StrictStr, StrictStr]


class PluginAssetChoices(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: list[PluginSelectionKey]


class PluginAssetApprovalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0, strict=True)
    expected_installation_revision: int = Field(ge=1, strict=True)
    descriptor: PluginAssetDescriptor
    descriptor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: list[PluginSelectionKey]
    inventory: list[PluginAsset]
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_update(self):
        if self.descriptor.sha256() != self.descriptor_sha256:
            raise ValueError("descriptor digest differs")
        selections = {
            (item.model_id, tuple(sorted(item.variants.items())))
            for item in self.descriptor.selections
        }
        approved = [
            (item.model_id, tuple(sorted(item.variants.items())))
            for item in self.approved
        ]
        if len(approved) != len(set(approved)) or not set(approved) <= selections:
            raise ValueError("approved selections must be unique descriptor entries")
        expected = {
            asset.path: asset
            for selection in self.descriptor.selections
            for asset in selection.assets
        }
        paths = [asset.path for asset in self.inventory]
        if len(paths) != len(set(paths)) or any(
            expected.get(asset.path) != asset for asset in self.inventory
        ):
            raise ValueError(
                "inventory must contain unique measured descriptor identities"
            )
        return self


class PluginAssetApprovalState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0, strict=True)
    decision: PluginAssetApprovalUpdate | None


def resolve_plugin_selection(health_meta, context):
    """Resolve advertised defaults without inventing a model or variant."""
    from collections.abc import Mapping
    from .execution import thaw_json

    meta = thaw_json(health_meta or {})
    context = thaw_json(context or {})
    capabilities = meta.get("capabilities") or {}
    models = capabilities.get("models") or []
    if not isinstance(models, list):
        return None, {}
    model_id = context.get("model_id")
    candidates = [
        model
        for model in models
        if isinstance(model, dict)
        and (
            model.get("id") == model_id
            if model_id
            else not context.get("task") or model.get("task") == context.get("task")
        )
    ]
    if len(candidates) != 1 or not isinstance(candidates[0].get("id"), str):
        return None, {}
    model = candidates[0]
    axes = model.get("supported_variants") or []
    supplied = context.get("model_variants") or {}
    if not isinstance(axes, list) or not isinstance(supplied, Mapping):
        return None, {}
    defaults = capabilities.get("default_variants") or {}
    model_defaults = model.get("default_variants") or {}
    if not isinstance(defaults, Mapping) or not isinstance(model_defaults, Mapping):
        return None, {}
    values = {**defaults, **model_defaults, **supplied}
    valid = {}
    for axis in axes:
        if not isinstance(axis, dict) or not isinstance(axis.get("key"), str):
            return None, {}
        key = axis["key"]
        if key in valid or not isinstance(axis.get("variants"), list):
            return None, {}
        valid[key] = [
            item.get("value") for item in axis["variants"] if isinstance(item, dict)
        ]
    if set(supplied) - set(valid) or any(
        values.get(key) not in choices for key, choices in valid.items()
    ):
        return None, {}
    variants = {key: values[key] for key in valid}
    combinations = model.get("variant_combinations") or []
    if combinations and [variants[key] for key in valid] not in combinations:
        return None, {}
    return model["id"], variants


def plugin_asset_reason(extra_params, state, model_id, variants):
    """Evaluate one exact selection against current owner state; legacy backends pass."""
    from collections.abc import Mapping
    from pydantic import ValidationError
    from .execution import thaw_json

    if not isinstance(extra_params, Mapping) or "plugin" not in extra_params:
        return None
    try:
        decision = PluginAssetApprovalState.model_validate(thaw_json(state)).decision
        if decision is None:
            return "plugin_asset_unapproved"
        plugin = extra_params["plugin"]
        manifest = plugin["manifest"]
        launch = extra_params["plugin_lifecycle"]["launch"]
        descriptor = decision.descriptor
        if (
            descriptor.plugin_id != manifest["plugin_id"]
            or descriptor.plugin_version != manifest["version"]
            or descriptor.artifact != manifest["artifact"]
            or descriptor.asset_manifest_sha256 != manifest["asset_manifest_sha256"]
            or decision.container_id != launch["container_id"]
            or decision.image_id != launch["image_id"]
        ):
            return "plugin_asset_unavailable"
        if not isinstance(model_id, str) or not isinstance(variants, Mapping):
            return "plugin_asset_unapproved"
        selection = next(
            (
                item
                for item in descriptor.selections
                if item.model_id == model_id and item.variants == variants
            ),
            None,
        )
        if selection is None or not any(
            item.model_id == model_id and item.variants == variants
            for item in decision.approved
        ):
            return "plugin_asset_unapproved"
        inventory = {item.path: item for item in decision.inventory}
        if any(inventory.get(item.path) != item for item in selection.assets):
            return "plugin_asset_unavailable"
        return None
    except (KeyError, TypeError, ValidationError):
        return "plugin_asset_unapproved"
