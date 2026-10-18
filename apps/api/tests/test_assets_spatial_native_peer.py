"""Native spatial contract interoperability from a restricted psql client."""

from __future__ import annotations

from datetime import datetime
import json
import os
import re
import subprocess
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.test_asset_storage_cleanup_runtime import _required_env, _verify_postgres
from tests.test_assets_catalog_native import (
    _CATALOG_URL,
    _ROLE_ACCEPTANCE,
    _seed_actor,
    _seed_dataset,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _ROLE_ACCEPTANCE,
        reason="requires the disposable module-data boundary verifier",
    ),
]

_SPATIAL_SCHEMA = "aap-assets-spatial/v1"
_CONTAINER_ID = "AAP_MODULE_DATA_PG_CONTAINER_ID"


def _json_object(line: str) -> dict:
    value = json.loads(line)
    assert isinstance(value, dict)
    return value


def _success(reply: dict, data_fields: set[str]) -> dict:
    assert set(reply) == {"schema", "ok", "data"}
    assert reply["schema"] == _SPATIAL_SCHEMA
    assert reply["ok"] is True
    data = reply["data"]
    assert isinstance(data, dict)
    assert set(data) == data_fields
    return data


def _scene(value: object, *, scene_id: UUID, dataset_id: UUID, actor_id: UUID):
    assert isinstance(value, dict)
    assert set(value) == {
        "id",
        "display_id",
        "dataset_id",
        "name",
        "source_format",
        "source_metadata",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert value["id"] == str(scene_id)
    assert value["dataset_id"] == str(dataset_id)
    assert value["name"] == "native-peer-scene"
    assert value["source_format"] == "operator"
    assert value["source_metadata"] == {"client": "psql-peer"}
    assert value["created_by"] == str(actor_id)
    for field in ("created_at", "updated_at"):
        timestamp = value[field]
        assert isinstance(timestamp, str)
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _native_psql(
    catalog_url: URL,
    *,
    scene_id: UUID,
    dataset_id: UUID,
    actor_id: UUID,
    create_operation_id: UUID,
    pose_operation_id: UUID,
) -> list[str]:
    container_id = _required_env(_CONTAINER_ID)
    sql = """\
BEGIN;
SELECT current_user || '|' || session_user || '|' || current_database();
SELECT aap_assets_v1.create_spatial_scene(
    :'create_operation_id'::uuid,
    :'scene_id'::uuid,
    :'dataset_id'::uuid,
    'native-peer-scene'::text,
    'operator'::text,
    '{"client":"psql-peer"}'::jsonb,
    :'actor_id'::uuid
);
SELECT aap_assets_v1.upsert_spatial_poses(
    :'pose_operation_id'::uuid,
    :'scene_id'::uuid,
    '[{"frame_index":0,"timestamp_us":-9876543210,"ego_translation":[1.25,-2.5,3.75],"ego_rotation":[1,0,0,0],"source_metadata":{"client":"psql-peer"}}]'::jsonb
);
SELECT aap_assets_v1.get_spatial_scene(:'actor_id'::uuid, :'scene_id'::uuid);
SELECT aap_assets_v1.read_spatial_poses(
    :'actor_id'::uuid, :'scene_id'::uuid, NULL::integer, 10
);
ROLLBACK;
"""
    environment = os.environ.copy()
    environment["PGPASSWORD"] = catalog_url.password or ""
    command = [
        "docker",
        "exec",
        "-i",
        "-e",
        "PGPASSWORD",
        container_id,
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-h",
        "127.0.0.1",
        "-p",
        "5432",
        "-U",
        "asset_catalog_client",
        "-d",
        catalog_url.database,
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"scene_id={scene_id}",
        "-v",
        f"dataset_id={dataset_id}",
        "-v",
        f"actor_id={actor_id}",
        "-v",
        f"create_operation_id={create_operation_id}",
        "-v",
        f"pose_operation_id={pose_operation_id}",
        "-f",
        "-",
    ]
    result = subprocess.run(
        command,
        input=sql,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        pytest.fail("restricted Catalog psql spatial transaction failed", pytrace=False)
    return result.stdout.splitlines()


async def test_restricted_catalog_psql_spatial_transaction_rolls_back(test_engine):
    raw_url = os.environ.get(_CATALOG_URL)
    if not raw_url:
        pytest.fail(f"the module-data verifier did not provide {_CATALOG_URL}")
    try:
        catalog_url = make_url(raw_url)
    except Exception:
        pytest.fail("the restricted Catalog connection URL is invalid", pytrace=False)
    if not (
        catalog_url.username == "asset_catalog_client"
        and catalog_url.password
        and catalog_url.host == "127.0.0.1"
        and catalog_url.port is not None
        and catalog_url.database
        and catalog_url.database.endswith("_test")
        and test_engine.url.database == catalog_url.database
    ):
        pytest.fail("Catalog LOGIN URL did not target the owned test database")

    async with test_engine.connect() as owner:
        owner_database = await owner.scalar(text("SELECT current_database()"))
    if owner_database != catalog_url.database or not owner_database.endswith("_test"):
        pytest.fail("owner connection did not target the Catalog test database")

    # This verifies the exact verifier-owned PostgreSQL name, image, labels, and
    # loopback port before either seeding rows or invoking Docker exec.
    _verify_postgres(catalog_url)

    actor_id: UUID | None = None
    dataset_id: UUID | None = None
    scene_id, create_operation_id, pose_operation_id = uuid4(), uuid4(), uuid4()
    seeded = False
    try:
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as owner:
            actor_id = await _seed_actor(owner, role="super_admin")
            dataset_id = await _seed_dataset(
                owner,
                actor_id=actor_id,
                name=f"native-peer-{uuid4().hex[:10]}",
            )
            await owner.commit()
        seeded = True

        lines = _native_psql(
            catalog_url,
            scene_id=scene_id,
            dataset_id=dataset_id,
            actor_id=actor_id,
            create_operation_id=create_operation_id,
            pose_operation_id=pose_operation_id,
        )
        assert len(lines) == 5
        assert lines[0] == (
            f"asset_catalog_client|asset_catalog_client|{catalog_url.database}"
        )

        created = _success(
            _json_object(lines[1]),
            {"operation_id", "actor_id", "scene_id", "scene"},
        )
        assert created["operation_id"] == str(create_operation_id)
        assert created["actor_id"] is None
        assert created["scene_id"] == str(scene_id)
        _scene(
            created["scene"],
            scene_id=scene_id,
            dataset_id=dataset_id,
            actor_id=actor_id,
        )

        upserted = _success(
            _json_object(lines[2]), {"operation_id", "scene_id", "upserted"}
        )
        assert upserted == {
            "operation_id": str(pose_operation_id),
            "scene_id": str(scene_id),
            "upserted": 1,
        }

        fetched = _success(_json_object(lines[3]), {"actor_id", "scene_id", "scene"})
        assert fetched["actor_id"] == str(actor_id)
        assert fetched["scene_id"] == str(scene_id)
        assert fetched["scene"] == created["scene"]

        page = _success(
            _json_object(lines[4]),
            {
                "actor_id",
                "scene_id",
                "after_frame_index",
                "next_frame_index",
                "snapshot_digest",
                "total",
                "complete",
                "poses",
            },
        )
        assert page["actor_id"] == str(actor_id)
        assert page["scene_id"] == str(scene_id)
        assert page["after_frame_index"] is None
        assert page["next_frame_index"] is None
        assert re.fullmatch(r"[0-9a-f]{64}", page["snapshot_digest"])
        assert page["total"] == 1
        assert page["complete"] is True
        assert len(page["poses"]) == 1
        pose = page["poses"][0]
        assert set(pose) == {
            "frame_index",
            "timestamp_us",
            "ego_translation",
            "ego_rotation",
            "source_metadata",
        }
        assert pose == {
            "frame_index": 0,
            "timestamp_us": -9876543210,
            "ego_translation": [1.25, -2.5, 3.75],
            "ego_rotation": [1, 0, 0, 0],
            "source_metadata": {"client": "psql-peer"},
        }

        async with test_engine.connect() as owner:
            persisted = await owner.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM public.scenes WHERE id=:scene_id), "
                    "(SELECT count(*) FROM public.scene_frame_poses WHERE scene_id=:scene_id), "
                    "(SELECT count(*) FROM public.dataset_spatial_operations "
                    "WHERE operation_id IN (:create_operation_id, :pose_operation_id))"
                ),
                {
                    "scene_id": scene_id,
                    "create_operation_id": create_operation_id,
                    "pose_operation_id": pose_operation_id,
                },
            )
            assert persisted.one() == (0, 0, 0)
    finally:
        if seeded and actor_id is not None and dataset_id is not None:
            async with test_engine.begin() as owner:
                await owner.execute(
                    text(
                        "DELETE FROM public.dataset_spatial_operations "
                        "WHERE operation_id IN (:create_operation_id, :pose_operation_id)"
                    ),
                    {
                        "create_operation_id": create_operation_id,
                        "pose_operation_id": pose_operation_id,
                    },
                )
                await owner.execute(
                    text(
                        "DELETE FROM public.scene_frame_poses WHERE scene_id=:scene_id"
                    ),
                    {"scene_id": scene_id},
                )
                await owner.execute(
                    text("DELETE FROM public.scenes WHERE id=:scene_id"),
                    {"scene_id": scene_id},
                )
                await owner.execute(
                    text("DELETE FROM public.datasets WHERE id=:dataset_id"),
                    {"dataset_id": dataset_id},
                )
                await owner.execute(
                    text("DELETE FROM public.users WHERE id=:actor_id"),
                    {"actor_id": actor_id},
                )
