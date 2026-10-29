"""Bind historical plugin capabilities to an explicit administrator inventory."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import UUID

from alembic import context, op
import sqlalchemy as sa

revision = "bo0401"
down_revision = "bo0400"
branch_labels = depends_on = None

_TABLE = "aap_inference_topology_v1.plugin_capability_migration_0401"
_TRIGGERS = {
    "aap_route_registry_before_change": (
        "aap_inference_routing_v1",
        "_routing_registry_before_change",
    ),
    "trg_sync_gpu_backend_membership_update": ("public", "sync_gpu_backend_membership"),
}


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate inventory key")
        result[key] = value
    return result


def _load_inventory():
    path = context.get_x_argument(as_dictionary=True).get(
        "plugin_capabilities_inventory"
    )
    if not path:
        return None
    source = Path(path)
    if (
        not source.is_absolute()
        or not source.is_file()
        or source.stat().st_size > 8 * 1024 * 1024
    ):
        raise RuntimeError(
            "Plugin capability inventory must be an absolute, bounded regular file"
        )
    try:
        value = json.loads(
            source.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("Cannot read valid plugin capability inventory") from exc
    return value


def _validated_inventory(value, rows):
    from aap_inference_contract.plugin import PluginInstallation, PluginManifest
    from aap_protocol_v2.vocab import GEOMETRY_VALUES, INPUT_VALUES, PROMPT_VALUES

    def check_known(manifest):
        if any(
            not set(model.supported_inputs).issubset(INPUT_VALUES)
            or not set(model.supported_prompts).issubset(PROMPT_VALUES)
            or not set(model.supported_geometric_outputs).issubset(GEOMETRY_VALUES)
            for model in manifest.models
        ):
            raise RuntimeError("Inventory contains unknown protocol capabilities")

    legacy = {}
    for row in rows:
        extra = row["extra_params"]
        plugin = extra.get("plugin") if isinstance(extra, dict) else None
        manifest = plugin.get("manifest") if isinstance(plugin, dict) else None
        if not isinstance(manifest, dict):
            raise RuntimeError(
                "Malformed historical plugin; repair or reapprove it before upgrading"
            )
        if "models" in manifest or "pools" in manifest:
            if "models" not in manifest or "pools" not in manifest:
                raise RuntimeError(
                    "Incomplete plugin capability intent; reapprove it before upgrading"
                )
            try:
                installation = PluginInstallation.model_validate(plugin)
                if str(installation.installation_id) != str(row["id"]):
                    raise ValueError("installation identity differs from registry")
                check_known(installation.manifest)
            except ValueError as exc:
                raise RuntimeError(
                    "Invalid plugin capability intent; reapprove it before upgrading"
                ) from exc
            continue
        legacy[str(row["id"])] = row

    if not legacy and value is None:
        return []
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "installations"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or not isinstance(value["installations"], list)
    ):
        raise RuntimeError(
            "Historical plugins require an administrator-approved capability inventory; supply -x plugin_capabilities_inventory=/absolute/path"
        )

    entries = {}
    fields = {
        "installation_id",
        "registry_id",
        "artifact",
        "image_id",
        "old_manifest",
        "old_manifest_sha256",
        "asset_manifest_sha256",
        "models",
        "pools",
        "approval_ref",
    }
    for entry in value["installations"]:
        if not isinstance(entry, dict) or set(entry) != fields:
            raise RuntimeError("Invalid plugin capability inventory entry")
        try:
            ident = str(UUID(entry["installation_id"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError("Invalid inventory installation identity") from exc
        if ident != entry["installation_id"] or ident in entries:
            raise RuntimeError(
                "Inventory installation identities must be canonical and unique"
            )
        if (
            not isinstance(entry["image_id"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", entry["image_id"]) is None
            or not isinstance(entry["approval_ref"], str)
            or not entry["approval_ref"]
            or entry["approval_ref"].strip() != entry["approval_ref"]
            or len(entry["approval_ref"]) > 512
        ):
            raise RuntimeError(
                "Inventory requires an inspected image ID and administrator approval reference"
            )
        entries[ident] = entry
    if set(entries) != set(legacy):
        raise RuntimeError(
            "Inventory must cover every historical plugin exactly; inspect and reapprove unknown installations before upgrading"
        )

    updates = []
    for ident, row in legacy.items():
        entry = entries[ident]
        extra = row["extra_params"]
        plugin = extra["plugin"]
        old = plugin["manifest"]
        try:
            matches = _canonical(entry["old_manifest"]) == _canonical(old)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Inventory contains invalid JSON") from exc
        if (
            not matches
            or plugin.get("installation_id") != ident
            or entry["registry_id"] != ident
            or entry["old_manifest_sha256"]
            != hashlib.sha256(_canonical(old).encode("ascii")).hexdigest()
            or entry["asset_manifest_sha256"] != old.get("asset_manifest_sha256")
            or entry["artifact"] != old.get("artifact")
        ):
            raise RuntimeError(
                "Inventory does not match the immutable historical manifest and installation"
            )
        lifecycle = extra.get("plugin_lifecycle")
        if not isinstance(lifecycle, dict):
            raise RuntimeError(
                "Historical plugin lifecycle is unavailable; reapprove it before upgrading"
            )
        launch = lifecycle.get("launch")
        if launch is not None and (
            not isinstance(launch, dict)
            or launch.get("installation_id") != ident
            or launch.get("registry_id") != ident
            or launch.get("artifact") != entry["artifact"]
            or launch.get("image_id") != entry["image_id"]
            or _canonical(launch.get("resource")) != _canonical(plugin.get("resource"))
        ):
            raise RuntimeError(
                "Inventory image or resource differs from the recorded launch"
            )
        new = {**old, "models": entry["models"], "pools": entry["pools"]}
        try:
            manifest = PluginManifest.model_validate(new)
            PluginInstallation.model_validate({**plugin, "manifest": new})
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Inventory contains invalid plugin capabilities or resource binding"
            ) from exc
        check_known(manifest)
        updates.append((ident, old, new, entry["image_id"], entry["approval_ref"]))
    return updates


def _lock_and_rows(bind):
    bind.execute(
        sa.text("LOCK TABLE public.ml_backend_registry IN ACCESS EXCLUSIVE MODE")
    )
    return list(
        bind.execute(
            sa.text(
                "SELECT id, extra_params FROM public.ml_backend_registry "
                "WHERE extra_params ? 'plugin' ORDER BY id FOR UPDATE"
            )
        ).mappings()
    )


def _set_trigger_state(bind, enabled):
    for name, (schema, function) in _TRIGGERS.items():
        found = bind.execute(
            sa.text("""
            SELECT t.tgenabled::text FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE t.tgrelid='public.ml_backend_registry'::regclass
              AND t.tgname=:name AND n.nspname=:schema AND p.proname=:function
              AND NOT t.tgisinternal
        """),
            {"name": name, "schema": schema, "function": function},
        ).scalar_one_or_none()
        expected = "D" if enabled else "O"
        if found != expected:
            raise RuntimeError(
                f"Plugin registry trigger {name} is missing or unexpectedly configured ({found!r})"
            )
        action = "ENABLE" if enabled else "DISABLE"
        bind.execute(
            sa.text(f'ALTER TABLE public.ml_backend_registry {action} TRIGGER "{name}"')
        )


def _replace_manifests(bind, updates):
    if not updates:
        return
    _set_trigger_state(bind, False)
    for ident, manifest in updates:
        result = bind.execute(
            sa.text("""
            UPDATE public.ml_backend_registry
            SET extra_params = jsonb_set(extra_params, '{plugin,manifest}', CAST(:manifest AS jsonb), false)
            WHERE id=CAST(:id AS uuid) AND extra_params ? 'plugin'
        """),
            {"id": ident, "manifest": _canonical(manifest)},
        )
        if result.rowcount != 1:
            raise RuntimeError("Plugin registry changed during capability migration")
    _set_trigger_state(bind, True)


def upgrade():
    bind = op.get_bind()
    rows = _lock_and_rows(bind)
    updates = _validated_inventory(_load_inventory(), rows)
    bind.execute(
        sa.text(f"""
        CREATE TABLE {_TABLE} (
            registry_id uuid PRIMARY KEY,
            old_manifest jsonb NOT NULL,
            new_manifest jsonb NOT NULL,
            image_id text NOT NULL,
            approval_ref text NOT NULL
        )
    """)
    )
    bind.execute(sa.text(f"REVOKE ALL ON {_TABLE} FROM PUBLIC"))
    for ident, old, new, image_id, approval_ref in updates:
        bind.execute(
            sa.text(f"""
            INSERT INTO {_TABLE} (registry_id, old_manifest, new_manifest, image_id, approval_ref)
            VALUES (CAST(:id AS uuid), CAST(:old AS jsonb), CAST(:new AS jsonb), :image_id, :approval_ref)
        """),
            {
                "id": ident,
                "old": _canonical(old),
                "new": _canonical(new),
                "image_id": image_id,
                "approval_ref": approval_ref,
            },
        )
    _replace_manifests(bind, [(ident, new) for ident, _, new, _, _ in updates])


def downgrade():
    bind = op.get_bind()
    rows = _lock_and_rows(bind)
    backups = list(
        bind.execute(
            sa.text(
                f"SELECT registry_id, old_manifest, new_manifest FROM {_TABLE} ORDER BY registry_id FOR UPDATE"
            )
        ).mappings()
    )
    current = {str(row["id"]): row["extra_params"] for row in rows}
    if set(current) != {str(row["registry_id"]) for row in backups}:
        raise RuntimeError(
            "Cannot downgrade with new, deleted or untracked plugin installations; retain the capability migration"
        )
    for row in backups:
        plugin = current[str(row["registry_id"])].get("plugin")
        if not isinstance(plugin, dict) or _canonical(
            plugin.get("manifest")
        ) != _canonical(row["new_manifest"]):
            raise RuntimeError(
                "Cannot downgrade a changed plugin capability intent; retain the capability migration"
            )
    _replace_manifests(
        bind, [(str(row["registry_id"]), row["old_manifest"]) for row in backups]
    )
    bind.execute(sa.text(f"DROP TABLE {_TABLE}"))
