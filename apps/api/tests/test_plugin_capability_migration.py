from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.migration_helpers import load


REVISION = "0401_plugin_capability_inventory.py"


def historical_installation(device="cpu"):
    ident = str(uuid4())
    manifest = {
        "plugin_id": "libreyolo",
        "version": "1.0",
        "publisher": "example",
        "license": "MIT",
        "artifact": "registry.example/plugin@sha256:" + "a" * 64,
        "sdk_compatibility": "1",
        "entrypoint": ["plugin"],
        "capabilities": ["inference", "detection"],
        "resource": {"device": "cpu", "memory_mb": 1024, "gpu_memory_mb": 0},
        "asset_manifest_sha256": "b" * 64,
        "allowed_asset_sources": [],
    }
    resource = {
        "gpu_resource_id": None,
        "physical_gpu_uuid": None,
        "logical_device": "cpu",
    }
    if device == "cuda":
        manifest["resource"] = {
            "device": "cuda",
            "memory_mb": 1024,
            "gpu_memory_mb": 512,
        }
        resource = {
            "gpu_resource_id": "node/GPU-test",
            "physical_gpu_uuid": "GPU-test",
            "logical_device": "cuda:0",
        }
    launch = {
        "installation_id": ident,
        "registry_id": ident,
        "artifact": manifest["artifact"],
        "image_id": "sha256:" + "c" * 64,
        "container_id": "d" * 64,
        "resource": resource,
    }
    plugin = {
        "installation_id": ident,
        "manifest": manifest,
        "status": "active",
        "revision": 3,
        "resource": resource,
    }
    lifecycle = {
        "launch": launch,
        "stopped": [],
        "qualification": {"report": {"old": True}, "sha256": "e" * 64},
    }
    return {
        "id": ident,
        "extra_params": {"plugin": plugin, "plugin_lifecycle": lifecycle},
    }


def inventory(row):
    old = row["extra_params"]["plugin"]["manifest"]
    payload = json.dumps(
        old, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    return {
        "schema": 1,
        "installations": [
            {
                "installation_id": row["id"],
                "registry_id": row["id"],
                "artifact": old["artifact"],
                "image_id": row["extra_params"]["plugin_lifecycle"]["launch"][
                    "image_id"
                ],
                "old_manifest": deepcopy(old),
                "old_manifest_sha256": hashlib.sha256(payload).hexdigest(),
                "asset_manifest_sha256": old["asset_manifest_sha256"],
                "approval_ref": "admin:inspection",
                "models": [
                    {
                        "id": "detect",
                        "task": "detect",
                        "is_interactive": False,
                        "supported_prompts": ["none"],
                        "supported_inputs": ["full_image", "crop"],
                        "supported_geometric_outputs": ["bbox"],
                    }
                ],
                "pools": {"models": {"cap": 1}},
            }
        ],
    }


def test_operator_inventory_file_rejects_duplicate_keys(tmp_path, monkeypatch):
    migration = load(REVISION)
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(inventory(historical_installation())), encoding="utf-8")
    monkeypatch.setattr(
        migration.context,
        "get_x_argument",
        lambda **_: {"plugin_capabilities_inventory": str(path)},
    )
    assert migration._load_inventory()["schema"] == 1
    path.write_text('{"schema":1,"schema":1,"installations":[]}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="valid plugin capability inventory"):
        migration._load_inventory()


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value["installations"].clear(),
        lambda value: value["installations"][0].update(registry_id=str(uuid4())),
        lambda value: value["installations"][0].update(old_manifest_sha256="0" * 64),
        lambda value: value["installations"][0].update(image_id="sha256:" + "f" * 64),
        lambda value: value["installations"][0].update(asset_manifest_sha256="0" * 64),
        lambda value: value["installations"][0]["models"][0].update(
            supported_inputs=["unknown"]
        ),
        lambda value: value["installations"][0]["pools"]["models"].update(cap=True),
    ],
)
def test_inventory_rejects_unproved_or_mismatched_historical_intent(change):
    migration = load(REVISION)
    row = historical_installation()
    value = inventory(row)
    change(value)
    with pytest.raises(RuntimeError):
        migration._validated_inventory(value, [row])


@pytest.mark.asyncio
@pytest.mark.parametrize("device", ["cpu", "cuda"])
async def test_historical_capabilities_migrate_atomically_and_restore(
    db_session, monkeypatch, device
):
    migration = load(REVISION)
    row = historical_installation(device)
    approved = inventory(row)

    def exercise(sync):
        connection = sync.connection()
        migration.op = Operations(MigrationContext.configure(connection))
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.ml_backend_registry WHERE extra_params ? 'plugin'"
                )
            )
            == 0
        )
        connection.execute(text(f"DROP TABLE {migration._TABLE}"))
        connection.execute(
            text("""
            INSERT INTO public.ml_backend_registry
                (id, name, url, state, is_interactive, auth_method, auth_token, extra_params, source, gpu_resource_id, vram_budget_mb)
            VALUES (CAST(:id AS uuid), 'historical', :url, 'disconnected', false,
                    'token', :token, CAST(:extra AS jsonb), 'manual', :gpu, :budget)
        """),
            {
                "id": row["id"],
                "url": "http://historical-" + row["id"] + ".test",
                "token": "x" * 40,
                "extra": json.dumps(row["extra_params"]),
                "gpu": "node/GPU-test" if device == "cuda" else None,
                "budget": 512 if device == "cuda" else None,
            },
        )
        before_membership = (
            connection.execute(
                text(
                    "SELECT to_jsonb(m) FROM public.gpu_backend_memberships m WHERE backend_registry_id=CAST(:id AS uuid)"
                ),
                {"id": row["id"]},
            )
            .scalars()
            .all()
        )
        before_fence = (
            connection.execute(
                text(
                    "SELECT to_jsonb(f) FROM public.gpu_backend_fences f WHERE backend_registry_id=CAST(:id AS uuid)"
                ),
                {"id": row["id"]},
            )
            .scalars()
            .all()
        )
        monkeypatch.setattr(migration, "_load_inventory", lambda: None)
        with pytest.raises(RuntimeError, match="administrator-approved"):
            migration.upgrade()
        assert (
            connection.scalar(
                text("SELECT to_regclass(:name)"), {"name": migration._TABLE}
            )
            is None
        )
        monkeypatch.setattr(migration, "_load_inventory", lambda: approved)
        migration.upgrade()
        assert (
            connection.execute(
                text(
                    "SELECT to_jsonb(m) FROM public.gpu_backend_memberships m WHERE backend_registry_id=CAST(:id AS uuid)"
                ),
                {"id": row["id"]},
            )
            .scalars()
            .all()
            == before_membership
        )
        assert (
            connection.execute(
                text(
                    "SELECT to_jsonb(f) FROM public.gpu_backend_fences f WHERE backend_registry_id=CAST(:id AS uuid)"
                ),
                {"id": row["id"]},
            )
            .scalars()
            .all()
            == before_fence
        )
        current = connection.scalar(
            text(
                "SELECT extra_params FROM public.ml_backend_registry WHERE id=CAST(:id AS uuid)"
            ),
            {"id": row["id"]},
        )
        old_extra = row["extra_params"]
        assert current["plugin_lifecycle"] == old_extra["plugin_lifecycle"]
        assert current["plugin"]["revision"] == old_extra["plugin"]["revision"]
        assert current["plugin"]["resource"] == old_extra["plugin"]["resource"]
        assert current["plugin"]["manifest"] == {
            **old_extra["plugin"]["manifest"],
            "models": approved["installations"][0]["models"],
            "pools": approved["installations"][0]["pools"],
        }
        later = deepcopy(current)
        later_id = str(uuid4())
        later["plugin"]["installation_id"] = later_id
        later["plugin_lifecycle"]["launch"]["installation_id"] = later_id
        later["plugin_lifecycle"]["launch"]["registry_id"] = later_id
        savepoint = connection.begin_nested()
        connection.execute(
            text("""
            INSERT INTO public.ml_backend_registry
                (id, name, url, state, is_interactive, auth_method, auth_token, extra_params, source, gpu_resource_id, vram_budget_mb)
            VALUES (CAST(:id AS uuid), 'new plugin', :url, 'disconnected', false,
                    'token', :token, CAST(:extra AS jsonb), 'manual', :gpu, :budget)
        """),
            {
                "id": later_id,
                "url": "http://new-" + later_id + ".test",
                "token": "y" * 40,
                "extra": json.dumps(later),
                "gpu": "node/GPU-test" if device == "cuda" else None,
                "budget": 512 if device == "cuda" else None,
            },
        )
        with pytest.raises(RuntimeError, match="untracked plugin"):
            migration.downgrade()
        savepoint.rollback()
        trigger_states = connection.execute(
            text("""
            SELECT tgname, tgenabled::text FROM pg_catalog.pg_trigger
            WHERE tgrelid='public.ml_backend_registry'::regclass
              AND tgname IN ('aap_route_registry_before_change',
                             'trg_sync_gpu_backend_membership_update')
        """)
        ).all()
        assert {name: state for name, state in trigger_states} == {
            "aap_route_registry_before_change": "O",
            "trg_sync_gpu_backend_membership_update": "O",
        }
        migration.downgrade()
        restored = connection.scalar(
            text(
                "SELECT extra_params FROM public.ml_backend_registry WHERE id=CAST(:id AS uuid)"
            ),
            {"id": row["id"]},
        )
        assert restored == old_extra

    await db_session.run_sync(exercise)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "uncertain"])
async def test_terminal_drain_migration_refuses_unresolved_downgrade(db_session, state):
    migration = load("0402_plugin_terminal_drain_route.py")
    control_id, registry_id, attempt_id = uuid4(), uuid4(), uuid4()

    def exercise(sync):
        connection = sync.connection()
        migration.op = Operations(MigrationContext.configure(connection))
        connection.execute(
            text("""
                INSERT INTO aap_inference_routing_v1.controls
                    (id, registry_id, kind, phase, challenge, config_revision, realm)
                VALUES (:control, :registry, 'unload', 'prepared', repeat('c', 64), 1, 'test')
            """),
            {"control": control_id, "registry": registry_id},
        )
        connection.execute(
            text("""
                INSERT INTO aap_inference_routing_v1.control_attempts
                    (id, control_id, method, path, transport_identity_sha256, state)
                VALUES (:attempt, :control, 'POST', '/lifecycle/terminal-drain',
                        repeat('a', 64), :state)
            """),
            {"attempt": attempt_id, "control": control_id, "state": state},
        )
        with pytest.raises(DBAPIError, match="unresolved terminal drain"):
            with connection.begin_nested():
                migration.downgrade()
        connection.execute(
            text("""
                UPDATE aap_inference_routing_v1.control_attempts
                SET state='response', status_code=200 WHERE id=:attempt
            """),
            {"attempt": attempt_id},
        )
        migration.downgrade()
        migration.upgrade()

    await db_session.run_sync(exercise)
