"""Portable policy for reading and patching user preference JSON.

This module owns normalization, validation, merge semantics and named-preset
compare-and-swap policy. It deliberately has no API, database or ORM imports;
callers pass the latest stored snapshot and persist the returned value in their
own transaction.
"""

from __future__ import annotations

from math import isfinite
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from .preference_models import (
    NamedPresetsRevision,
    ShortcutBinding,
    UserPreferences,
    WorkbenchShortcutPreferences,
    shortcut_command_domain,
)
from .workbench_workspace import (
    MAX_NAMED_PRESETS,
    NamedWorkspacePreset,
    PresetId,
)


class PreferencePolicyError(Exception):
    """A stable public policy rejection without an HTTP framework dependency."""

    def __init__(self, status_code: int, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__("Preference policy rejected the request")


class PreferenceValidationError(Exception):
    """Validation diagnostics safe to adapt to an HTTP 422 response."""

    def __init__(self, errors: list[dict]):
        self.errors = tuple(
            {
                "type": error["type"],
                "loc": tuple(error["loc"]),
                "msg": error["msg"],
            }
            for error in errors
        )
        super().__init__("User preferences are invalid")


_LEGACY_WORKBENCH_KEYS: dict[str, str] = {
    "smoothImage": "image",
    "cssImageFilter": "image",
    "controlPointsSize": "image",
    "snapToGrid": "image",
    "longTaskSampleRate": "common",
}


def _promote_legacy_workbench_keys(payload: dict) -> dict:
    """Promote flattened keys; a value in the current subtree takes precedence."""
    workbench = payload.get("workbench")
    if not isinstance(workbench, dict) or not any(
        key in workbench for key in _LEGACY_WORKBENCH_KEYS
    ):
        return payload
    workbench = dict(workbench)
    for key, subtree_key in _LEGACY_WORKBENCH_KEYS.items():
        if key not in workbench:
            continue
        subtree = workbench.get(subtree_key)
        if subtree is not None and not isinstance(subtree, dict):
            continue
        value = workbench.pop(key)
        merged_subtree = dict(subtree or {})
        merged_subtree.setdefault(key, value)
        workbench[subtree_key] = merged_subtree
    return {**payload, "workbench": workbench}


_REMOVED_WORKBENCH_LAYOUT_KEYS = ("leftWidth", "rightWidth")


_ATOMIC_PREFERENCE_MAP_PATHS = {
    ("workbench", "layout", "cameraPanels"),
    ("workbench", "layout", "workspace", "namedPresets"),
}
_WORKSPACE_CONTEXTS_PATH = ("workbench", "layout", "workspace", "contexts")
_WORKSPACE_PATH = ("workbench", "layout", "workspace")
_NAMED_PRESETS_REVISION_KEY = "namedPresetsRevision"
_INITIAL_NAMED_PRESETS_REVISION = "0"
_NAMED_PRESET_ADAPTER = TypeAdapter(dict[PresetId, NamedWorkspacePreset])
_NAMED_PRESETS_REVISION_ADAPTER = TypeAdapter(NamedPresetsRevision)

_SHORTCUTS_PATH = ("workbench", "shortcuts")
_SHORTCUTS_DOMAINS = ("common", "image", "video")
_SHORTCUT_ENTRY_ADAPTER = TypeAdapter(list[ShortcutBinding] | None)


def _shortcuts_value(prefs: dict) -> dict | None:
    value = prefs
    for key in _SHORTCUTS_PATH:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value if isinstance(value, dict) else None


def _with_shortcuts(prefs: dict, shortcuts: dict) -> dict:
    workbench = dict(prefs.get("workbench") or {})
    workbench["shortcuts"] = shortcuts
    return {**prefs, "workbench": workbench}


def _prepare_shortcuts_validation(payload: dict, existing: dict) -> tuple[dict, dict]:
    """Strictly validate changed shortcut entries; untouched stored entries stay opaque.

    Mirrors the workspace-envelope contract: an unrelated preference PATCH never
    revalidates the stored subtree as a new shortcut write, an unchanged entry
    that no longer validates keeps its stored value verbatim, and explicitly
    resetting an affected entry to null still passes strict validation.
    """
    incoming = _shortcuts_value(payload)
    if incoming is None:
        return payload, {}
    stored = _shortcuts_value(existing) or {}
    opaque: dict[str, dict[str, object]] = {}
    validation = dict(incoming)
    for domain in _SHORTCUTS_DOMAINS:
        if domain not in incoming:
            continue
        incoming_bucket = incoming[domain]
        if not isinstance(incoming_bucket, dict):
            continue
        stored_bucket = (
            stored.get(domain) if isinstance(stored.get(domain), dict) else {}
        )
        strict_bucket: dict[str, object] = {}
        for command_id, value in incoming_bucket.items():
            stored_value = stored_bucket.get(command_id)
            if (
                value is not None
                and command_id in stored_bucket
                and _same_json_value(value, stored_value)
            ):
                try:
                    WorkbenchShortcutPreferences.model_validate(
                        {domain: {command_id: value}}
                    )
                except ValidationError:
                    opaque.setdefault(domain, {})[command_id] = stored_value
                    continue
            try:
                normalized = _SHORTCUT_ENTRY_ADAPTER.validate_python(value)
            except ValidationError:
                strict_bucket[command_id] = value
            else:
                strict_bucket[command_id] = (
                    [b.model_dump(mode="json", by_alias=True) for b in normalized]
                    if normalized is not None
                    else None
                )
        validation[domain] = strict_bucket
    return _with_shortcuts(payload, validation), opaque


def _becomes_null_in_javascript(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return not isfinite(float(value))
    except OverflowError:
        return True


def _same_json_value(left, right) -> bool:
    """Compare JSON recursively without Python's bool/int equality coercion."""
    if left is None and _becomes_null_in_javascript(right):
        return True
    if type(left) in (int, float) and type(right) in (int, float):
        try:
            return float(left) == float(right)
        except OverflowError:
            return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json_value(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def _duplicate_preset_name_groups(presets: dict) -> dict[str, frozenset[str]]:
    """Return trimmed names shared by multiple raw preset entries."""
    ids_by_name: dict[str, set[str]] = {}
    for preset_id, preset in presets.items():
        if not isinstance(preset_id, str) or not isinstance(preset, dict):
            continue
        name = preset.get("name")
        if isinstance(name, str):
            ids_by_name.setdefault(name.strip(), set()).add(preset_id)
    return {
        name: frozenset(preset_ids)
        for name, preset_ids in ids_by_name.items()
        if len(preset_ids) > 1
    }


def _workspace_value(prefs: dict) -> dict | None:
    value = prefs
    for key in _WORKSPACE_PATH:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value if isinstance(value, dict) else None


def _with_workspace(prefs: dict, workspace: dict) -> dict:
    workbench = dict(prefs["workbench"])
    layout = dict(workbench["layout"])
    layout["workspace"] = workspace
    workbench["layout"] = layout
    return {**prefs, "workbench": workbench}


def _named_presets_conflict(current_revision) -> PreferencePolicyError:
    detail = {
        "code": "named_presets_conflict",
        "message": "命名布局预设已在其他会话更新，请刷新后重试",
    }
    if isinstance(current_revision, str):
        detail["currentRevision"] = current_revision
    return PreferencePolicyError(status_code=409, detail=detail)


def _prepare_workspace_validation(
    payload: dict, existing: dict
) -> tuple[dict, dict[str, object], bool, bool]:
    """Validate current entries strictly while carrying stored opaque entries verbatim."""
    workspace = _workspace_value(payload)
    if workspace is None:
        return payload, {}, False, False
    stored_workspace = _workspace_value(existing)
    stored_engine = stored_workspace.get("engine") if stored_workspace else None
    engine_supplied = "engine" in workspace
    if (
        engine_supplied
        and stored_engine is not None
        and stored_engine != "dockview@8"
        and workspace["engine"] != stored_engine
    ):
        raise PreferencePolicyError(status_code=409, detail="layout_engine_downgrade")

    validation_workspace = dict(workspace)
    preset_only = "namedPresets" in workspace and set(workspace) == {"namedPresets"}
    if not engine_supplied and preset_only:
        validation_workspace["engine"] = "dockview@8"

    opaque: dict[str, object] = {}
    incoming_presets = workspace.get("namedPresets")
    stored_presets = (
        stored_workspace.get("namedPresets", {}) if stored_workspace else {}
    )
    if isinstance(incoming_presets, dict) and isinstance(stored_presets, dict):
        incoming_keys = set(incoming_presets)
        stored_keys = set(stored_presets)
        over_limit_compat = (
            len(incoming_presets) > MAX_NAMED_PRESETS and incoming_keys <= stored_keys
        )
        if len(incoming_presets) > MAX_NAMED_PRESETS and not over_limit_compat:
            return (
                _with_workspace(payload, validation_workspace),
                {},
                engine_supplied,
                stored_workspace is not None,
            )

        incoming_duplicates = _duplicate_preset_name_groups(incoming_presets)
        stored_duplicates = _duplicate_preset_name_groups(stored_presets)
        if any(
            stored_duplicates.get(name) is None
            or not preset_ids <= stored_duplicates[name]
            for name, preset_ids in incoming_duplicates.items()
        ):
            return (
                _with_workspace(payload, validation_workspace),
                {},
                engine_supplied,
                stored_workspace is not None,
            )
        preserved_duplicate_ids = (
            set().union(*incoming_duplicates.values()) if incoming_duplicates else set()
        )

        strict: dict[str, object] = {}
        for preset_id, preset in incoming_presets.items():
            unchanged = preset_id in stored_presets and _same_json_value(
                preset, stored_presets[preset_id]
            )
            if unchanged and (
                over_limit_compat or preset_id in preserved_duplicate_ids
            ):
                opaque[preset_id] = stored_presets[preset_id]
                continue
            try:
                _NAMED_PRESET_ADAPTER.validate_python({preset_id: preset})
            except ValidationError:
                if unchanged:
                    opaque[preset_id] = stored_presets[preset_id]
                else:
                    strict[preset_id] = preset
            else:
                strict[preset_id] = preset
        validation_workspace["namedPresets"] = strict

    return (
        _with_workspace(payload, validation_workspace),
        opaque,
        engine_supplied,
        stored_workspace is not None,
    )


def _deep_merge_preferences(
    existing: dict, incoming: dict, *, _path: tuple[str, ...] = ()
) -> dict:
    """把 incoming 深合并到 existing 的副本: dict 递归、其它类型 (list / scalar) 直接覆盖。

    动机: pydantic exclude_unset PATCH 只带用户本次改的键, 顶层浅合并会让"改一个字段=
    整棵子树被 incoming 替换"→ 相邻字段被吹没。深合并让 workbench.layout.attrPanelCollapsed 单键 PATCH 不冲掉 layout 其他
    字段, ai.secondary_by_model 里单 backend 桶 PATCH 也不冲掉其它 backend 的偏好。

    注: list 直接覆盖 (合并语义不确定), 前端如需增删列表元素应提交完整列表。
    workbench.layout.cameraPanels 是按 role 提交的完整 map；缺失 role 表示删除
    其自定义状态，故该路径不能递归合并。
    """
    out: dict = dict(existing)
    for k, v in incoming.items():
        cur = out.get(k)
        path = (*_path, k)
        if path in _ATOMIC_PREFERENCE_MAP_PATHS or _path == _WORKSPACE_CONTEXTS_PATH:
            out[k] = v
        elif isinstance(v, dict) and isinstance(cur, dict):
            out[k] = _deep_merge_preferences(cur, v, _path=path)
        else:
            out[k] = v
    return out


def _strip_removed_workbench_keys(prefs: dict) -> dict:
    """Remove retired pixel-width keys from stored workbench layouts."""
    workbench = prefs.get("workbench")
    if not isinstance(workbench, dict):
        return prefs
    layout = workbench.get("layout")
    if not isinstance(layout, dict) or not any(
        key in layout for key in _REMOVED_WORKBENCH_LAYOUT_KEYS
    ):
        return prefs
    clean_layout = {
        key: value
        for key, value in layout.items()
        if key not in _REMOVED_WORKBENCH_LAYOUT_KEYS
    }
    return {**prefs, "workbench": {**workbench, "layout": clean_layout}}


def _strip_unknown_shortcut_resets(prefs: dict) -> dict:
    """删除指向已注销命令的 null 重置条目。

    客户端用 ``null`` 清除损坏 / 历史遗留的快捷键覆盖；深合并只会把该键的值写成
    ``null``，命令 ID 仍在，读路径会继续报 unknown-command。这里直接移除这些键，
    让「重置」真正生效。带绑定值的未知条目仍然保留，供客户端识别与修正。
    """
    workbench = prefs.get("workbench")
    if not isinstance(workbench, dict):
        return prefs
    shortcuts = workbench.get("shortcuts")
    if not isinstance(shortcuts, dict):
        return prefs
    changed = False
    cleaned: dict[str, object] = {}
    for domain, bucket in shortcuts.items():
        if not isinstance(bucket, dict):
            cleaned[domain] = bucket
            continue
        kept = {
            command_id: value
            for command_id, value in bucket.items()
            if not (value is None and shortcut_command_domain(command_id) is None)
        }
        changed = changed or len(kept) != len(bucket)
        cleaned[domain] = kept
    if not changed:
        return prefs
    return {**prefs, "workbench": {**workbench, "shortcuts": cleaned}}


def read_preferences(prefs: dict) -> dict:
    """Keep stored workspace envelopes and shortcut subtrees intact for client recovery.

    Incoming workspace / shortcut writes are strict. Reading must also tolerate
    corrupt or future subtrees without failing the whole workbench or silently
    overwriting them with today's defaults. Other preferences keep their
    existing validation.
    """
    prefs = _strip_removed_workbench_keys(prefs)
    prefs = _strip_unknown_shortcut_resets(prefs)
    workbench = prefs.get("workbench", {})
    layout = workbench.get("layout", {}) if isinstance(workbench, dict) else {}
    has_workspace = isinstance(layout, dict) and "workspace" in layout
    shortcuts_raw = workbench.get("shortcuts") if isinstance(workbench, dict) else None
    has_shortcuts = shortcuts_raw is not None
    if has_workspace:
        prefs = {
            **prefs,
            "workbench": {
                **workbench,
                "layout": {
                    key: value for key, value in layout.items() if key != "workspace"
                },
            },
        }
    if has_shortcuts:
        prefs = _with_shortcuts(prefs, {})
    content = UserPreferences.model_validate(prefs).model_dump(
        mode="json", by_alias=True
    )
    if has_workspace:
        content["workbench"]["layout"]["workspace"] = layout["workspace"]
    else:
        content["workbench"]["layout"].pop("workspace", None)
    if has_shortcuts:
        try:
            content["workbench"]["shortcuts"] = (
                WorkbenchShortcutPreferences.model_validate(shortcuts_raw).model_dump(
                    mode="json", by_alias=True
                )
            )
        except ValidationError:
            content["workbench"]["shortcuts"] = shortcuts_raw
    return content


def _workspace_contexts(prefs: dict) -> dict:
    value = prefs
    for key in _WORKSPACE_CONTEXTS_PATH:
        if not isinstance(value, dict):
            return {}
        value = value.get(key, {})
    return value if isinstance(value, dict) else {}


def patch_preferences(existing: dict, payload: dict) -> tuple[dict, dict]:
    """Validate and apply one sparse preferences PATCH to the latest snapshot.

    The caller must serialize this operation with writes to the same user row.
    The existing value is the snapshot read under that lock. The returned tuple
    is the complete stored value and its public read representation.
    """
    promoted = _strip_removed_workbench_keys(_promote_legacy_workbench_keys(payload))
    existing = existing or {}
    raw_workspace = _workspace_value(promoted)
    named_presets_supplied = (
        raw_workspace is not None and "namedPresets" in raw_workspace
    )
    revision_supplied = _NAMED_PRESETS_REVISION_KEY in promoted
    if named_presets_supplied:
        stored_revision_value = existing.get(
            _NAMED_PRESETS_REVISION_KEY, _INITIAL_NAMED_PRESETS_REVISION
        )
        try:
            stored_revision = _NAMED_PRESETS_REVISION_ADAPTER.validate_python(
                stored_revision_value, strict=True
            )
        except ValidationError:
            raise _named_presets_conflict(stored_revision_value) from None
        if revision_supplied:
            try:
                expected_revision = _NAMED_PRESETS_REVISION_ADAPTER.validate_python(
                    promoted[_NAMED_PRESETS_REVISION_KEY], strict=True
                )
            except ValidationError:
                pass
            else:
                if expected_revision != stored_revision:
                    raise _named_presets_conflict(stored_revision)
        elif _NAMED_PRESETS_REVISION_KEY in existing:
            raise _named_presets_conflict(stored_revision)

    validation_payload, opaque_presets, engine_supplied, had_stored_workspace = (
        _prepare_workspace_validation(promoted, existing)
    )
    validation_payload, opaque_shortcuts = _prepare_shortcuts_validation(
        validation_payload, existing
    )
    try:
        validated = UserPreferences.model_validate(validation_payload)
    except ValidationError as error:
        raise PreferenceValidationError(error.errors()) from None

    incoming = validated.model_dump(mode="json", exclude_unset=True, by_alias=True)
    if revision_supplied and not named_presets_supplied:
        raise PreferencePolicyError(
            status_code=422, detail="named_presets_revision_read_only"
        )
    if named_presets_supplied:
        incoming.pop(_NAMED_PRESETS_REVISION_KEY, None)
    incoming_workspace = _workspace_value(incoming)
    if incoming_workspace is not None:
        incoming_workspace = dict(incoming_workspace)
        if not engine_supplied and had_stored_workspace:
            incoming_workspace.pop("engine", None)
        if opaque_presets:
            incoming_workspace["namedPresets"] = {
                **incoming_workspace.get("namedPresets", {}),
                **opaque_presets,
            }
        incoming = _with_workspace(incoming, incoming_workspace)

    if opaque_shortcuts:
        incoming_shortcuts = dict(_shortcuts_value(incoming) or {})
        for domain, entries in opaque_shortcuts.items():
            bucket = dict(incoming_shortcuts.get(domain) or {})
            bucket.update(entries)
            incoming_shortcuts[domain] = bucket
        incoming = _with_shortcuts(incoming, incoming_shortcuts)

    stored_contexts = _workspace_contexts(existing)
    for context, envelope in _workspace_contexts(incoming).items():
        stored = stored_contexts.get(context)
        stored_version = (
            stored.get("schemaVersion") if isinstance(stored, dict) else None
        )
        if type(stored_version) is int and stored_version > envelope["schemaVersion"]:
            raise PreferencePolicyError(
                status_code=409, detail="layout_schema_downgrade"
            )

    merged = _deep_merge_preferences(existing, incoming)
    merged = _strip_removed_workbench_keys(merged)
    merged = _strip_unknown_shortcut_resets(merged)
    if named_presets_supplied:
        merged[_NAMED_PRESETS_REVISION_KEY] = uuid4().hex
    return merged, read_preferences(merged)
