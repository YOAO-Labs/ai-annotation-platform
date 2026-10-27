"""Restricted PostgreSQL LOGIN interoperability for operator registration."""

from __future__ import annotations

import json
import os
import subprocess
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_source.operator_contract import OPERATOR_NATIVE_ARGUMENTS
from tests.test_asset_storage_cleanup_runtime import _required_env, _verify_postgres
from tests.test_assets_catalog_native import _seed_actor

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable module-data verifier",
    ),
]

_OPERATOR_URL = "AAP_MODULE_DATA_ASSET_OPERATOR_URL"
_CONTAINER_ID = "AAP_MODULE_DATA_PG_CONTAINER_ID"


def _native_psql(
    operator_url,
    *,
    actor_id: UUID,
    image_operation_id: UUID,
    ensure_operation_id: UUID,
    append_operation_id: UUID,
    patch_operation_id: UUID,
    recount_operation_id: UUID,
    image_name: str,
    nuscenes_spec: dict,
    entries: list[dict],
):
    container_id = _required_env(_CONTAINER_ID)
    sql = """\
BEGIN;
SELECT current_user || '|' || session_user || '|' || current_database();
SELECT
    pg_catalog.has_schema_privilege(session_user, 'aap_assets_v1', 'USAGE')
    AND NOT pg_catalog.has_schema_privilege(session_user, 'aap_assets_v1', 'CREATE')
    AND NOT pg_catalog.has_database_privilege(session_user, current_database(), 'CREATE')
    AND NOT pg_catalog.has_database_privilege(session_user, current_database(), 'TEMP')
    AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND namespace.nspname <> 'aap_assets_v1'
          AND pg_catalog.has_schema_privilege(session_user, namespace.oid, 'USAGE')
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace=namespace.oid
        CROSS JOIN (VALUES
            ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'),
            ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
        ) AS privilege(name)
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_table_privilege(session_user, relation.oid, privilege.name)
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace=namespace.oid
        CROSS JOIN (VALUES
            ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')
        ) AS privilege(name)
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_any_column_privilege(
              session_user, relation.oid, privilege.name
          )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace=namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind = 'S'
          AND (
              pg_catalog.has_sequence_privilege(session_user, relation.oid, 'USAGE')
              OR pg_catalog.has_sequence_privilege(session_user, relation.oid, 'SELECT')
              OR pg_catalog.has_sequence_privilege(session_user, relation.oid, 'UPDATE')
          )
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_proc AS proc ON proc.pronamespace=namespace.oid
        WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND proc.prokind IN ('f', 'p')
          AND namespace.nspname <> 'aap_assets_v1'
          AND pg_catalog.has_function_privilege(session_user, proc.oid, 'EXECUTE')
    );
SELECT COALESCE(
    pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object('name', proc.proname, 'args', proc.proargnames)
        ORDER BY proc.proname
    ), '[]'::jsonb
)
FROM pg_catalog.pg_proc AS proc
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=proc.pronamespace
WHERE namespace.nspname='aap_assets_v1' AND proc.prokind='f'
  AND pg_catalog.has_function_privilege(session_user, proc.oid, 'EXECUTE');
SELECT aap_assets_v1.create_operator_image_dataset(
    operation_id => :'image_operation_id'::uuid,
    created_by => :'actor_id'::uuid,
    name => :'image_name'::text,
    description => 'restricted psql operator image import'
);
SELECT aap_assets_v1.ensure_operator_nuscenes_dataset(
    operation_id => :'ensure_operation_id'::uuid,
    owner_id => :'actor_id'::uuid,
    spec => :'nuscenes_spec'::jsonb
);
SELECT aap_assets_v1.append_operator_items(
    operation_id => :'append_operation_id'::uuid,
    dataset_id => :'image_operation_id'::uuid,
    entries => :'entries'::jsonb
);
WITH page AS (
    SELECT aap_assets_v1.read_operator_items(
        dataset_id => :'image_operation_id'::uuid,
        after_item_id => NULL::uuid,
        page_size => 5
    ) -> 'data' AS data
)
SELECT aap_assets_v1.patch_operator_items(
    operation_id => :'patch_operation_id'::uuid,
    dataset_id => :'image_operation_id'::uuid,
    entries => pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
        'id', page.data #> '{items,0,id}',
        'source_snapshot', page.data #> '{items,0,source_snapshot}',
        'nuscenes_export', pg_catalog.jsonb_build_object('writer', 'psql')
    ))
)
FROM page;
SELECT aap_assets_v1.recount_operator_items(
    operation_id => :'recount_operation_id'::uuid,
    dataset_id => :'image_operation_id'::uuid
);
SELECT aap_assets_v1.read_operator_items(
    dataset_id => :'image_operation_id'::uuid,
    after_item_id => NULL::uuid,
    page_size => 5
);
ROLLBACK;
"""
    environment = os.environ.copy()
    environment["PGPASSWORD"] = operator_url.password or ""
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
        "asset_operator_client",
        "-d",
        operator_url.database,
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"actor_id={actor_id}",
        "-v",
        f"image_operation_id={image_operation_id}",
        "-v",
        f"ensure_operation_id={ensure_operation_id}",
        "-v",
        f"append_operation_id={append_operation_id}",
        "-v",
        f"patch_operation_id={patch_operation_id}",
        "-v",
        f"recount_operation_id={recount_operation_id}",
        "-v",
        f"image_name={image_name}",
        "-v",
        f"nuscenes_spec={json.dumps(nuscenes_spec, ensure_ascii=False)}",
        "-v",
        f"entries={json.dumps(entries, ensure_ascii=False)}",
        "-f",
        "-",
    ]
    completed = subprocess.run(
        command,
        input=sql,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        pytest.fail("restricted operator psql transaction failed", pytrace=False)
    return completed.stdout.splitlines()


def _json_object(line: str) -> dict:
    value = json.loads(line)
    assert isinstance(value, dict)
    return value


def _success(value: dict) -> dict:
    assert set(value) == {"schema", "ok", "data"}
    assert value["schema"] == "aap-assets-operator/v1"
    assert value["ok"] is True
    assert isinstance(value["data"], dict)
    return value["data"]


async def test_operator_psql_login_executes_operator_capabilities_and_rolls_back(
    test_engine,
):
    raw_url = os.environ.get(_OPERATOR_URL)
    if not raw_url:
        pytest.fail(f"the module-data verifier did not provide {_OPERATOR_URL}")
    try:
        operator_url = make_url(raw_url)
    except Exception:
        pytest.fail("the restricted operator connection URL is invalid", pytrace=False)
    if not (
        operator_url.username == "asset_operator_client"
        and operator_url.password
        and operator_url.host == "127.0.0.1"
        and operator_url.port is not None
        and operator_url.database
        and operator_url.database.endswith("_test")
        and test_engine.url.database == operator_url.database
    ):
        pytest.fail("Operator LOGIN URL did not target the owned test database")
    async with test_engine.connect() as owner:
        owner_database = await owner.scalar(text("SELECT current_database()"))
        non_operator_has_operator_execute = await owner.scalar(
            text(
                "SELECT pg_catalog.has_function_privilege('asset_source_client', "
                "'aap_assets_v1.create_operator_image_dataset(uuid,uuid,text,text)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_source_client', "
                "'aap_assets_v1.ensure_operator_nuscenes_dataset(uuid,uuid,jsonb)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_source_client', "
                "'aap_assets_v1.append_operator_items(uuid,uuid,jsonb)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_source_client', "
                "'aap_assets_v1.patch_operator_items(uuid,uuid,jsonb)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_source_client', "
                "'aap_assets_v1.recount_operator_items(uuid,uuid)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_source_client', "
                "'aap_assets_v1.read_operator_items(uuid,uuid,integer)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_catalog_client', "
                "'aap_assets_v1.create_operator_image_dataset(uuid,uuid,text,text)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_catalog_client', "
                "'aap_assets_v1.ensure_operator_nuscenes_dataset(uuid,uuid,jsonb)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_catalog_client', "
                "'aap_assets_v1.append_operator_items(uuid,uuid,jsonb)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_catalog_client', "
                "'aap_assets_v1.patch_operator_items(uuid,uuid,jsonb)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_catalog_client', "
                "'aap_assets_v1.recount_operator_items(uuid,uuid)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_catalog_client', "
                "'aap_assets_v1.read_operator_items(uuid,uuid,integer)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_import_client', "
                "'aap_assets_v1.create_operator_image_dataset(uuid,uuid,text,text)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_import_client', "
                "'aap_assets_v1.ensure_operator_nuscenes_dataset(uuid,uuid,jsonb)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_import_client', "
                "'aap_assets_v1.append_operator_items(uuid,uuid,jsonb)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_import_client', "
                "'aap_assets_v1.patch_operator_items(uuid,uuid,jsonb)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_import_client', "
                "'aap_assets_v1.recount_operator_items(uuid,uuid)', 'EXECUTE') "
                "OR pg_catalog.has_function_privilege('asset_import_client', "
                "'aap_assets_v1.read_operator_items(uuid,uuid,integer)', 'EXECUTE')"
            )
        )
    if owner_database != operator_url.database or not owner_database.endswith("_test"):
        pytest.fail("owner connection did not target the operator test database")
    assert non_operator_has_operator_execute is False
    _verify_postgres(operator_url)

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as owner:
        actor_id = await _seed_actor(owner, role="viewer", active=True)
        await owner.commit()

    image_operation_id = uuid4()
    ensure_operation_id = uuid4()
    append_operation_id = uuid4()
    patch_operation_id = uuid4()
    recount_operation_id = uuid4()
    item_id = uuid4()
    display_id = f"DS-OP-{ensure_operation_id.hex[:8]}"
    image_name = f"psql-operator-{image_operation_id.hex[:8]}"
    nuscenes_name = f"psql-nuscenes-{ensure_operation_id.hex[:8]}"
    spec = {
        "display_id": display_id,
        "name": nuscenes_name,
        "version": "v1.0-mini",
        "scene_names": ["scene-0002", "scene-0001"],
        "axis_convention": "apollo",
        "frame": "sensor",
        "pcd_encoding": "float32",
    }
    entries = [
        {
            "id": str(item_id),
            "file_name": "psql-frame.pcd",
            "file_path": "psql-source/scene/psql-frame.pcd",
            "file_type": "point_cloud",
            "file_size": 123,
            "content_hash": "psql-hash",
            "width": None,
            "height": None,
            "metadata": {"writer": "psql"},
        }
    ]
    lines = _native_psql(
        operator_url,
        actor_id=actor_id,
        image_operation_id=image_operation_id,
        ensure_operation_id=ensure_operation_id,
        append_operation_id=append_operation_id,
        patch_operation_id=patch_operation_id,
        recount_operation_id=recount_operation_id,
        image_name=image_name,
        nuscenes_spec=spec,
        entries=entries,
    )
    assert len(lines) == 9
    assert (
        lines[0]
        == f"asset_operator_client|asset_operator_client|{operator_url.database}"
    )
    assert lines[1] == "t"
    capabilities = json.loads(lines[2])
    by_name = {entry["name"]: entry["args"] for entry in capabilities}
    # The operator LOGIN legitimately also holds the auxiliary-original and
    # storage-operation capabilities that create_item_original calls; the
    # registration surface below is the exact contract this LOGIN must keep.
    assert set(OPERATOR_NATIVE_ARGUMENTS) <= set(by_name)
    for name, arguments in OPERATOR_NATIVE_ARGUMENTS.items():
        assert by_name[name] == list(arguments)
    created = _success(_json_object(lines[3]))
    assert created["operation_id"] == str(image_operation_id)
    assert created["dataset_id"] == str(image_operation_id)
    assert created["dataset"]["created_by"] == str(actor_id)
    ensured = _success(_json_object(lines[4]))
    assert ensured["action"] == "ensure_nuscenes_dataset"
    assert ensured["dataset"]["created_by"] == str(actor_id)
    appended = _success(_json_object(lines[5]))
    assert [item["id"] for item in appended["items"]] == [str(item_id)]
    patched = _success(_json_object(lines[6]))
    assert patched["items"][0]["metadata"]["nuscenes_export"] == {"writer": "psql"}
    recounted = _success(_json_object(lines[7]))
    assert recounted["file_count"] == 1
    page = _success(_json_object(lines[8]))
    assert page["total"] == 1 and page["complete"] is True
    assert page["items"][0]["source_snapshot"] == patched["items"][0]["source_snapshot"]

    operation_ids = [
        image_operation_id,
        ensure_operation_id,
        append_operation_id,
        patch_operation_id,
        recount_operation_id,
    ]
    ensure_dataset_id = UUID(ensured["dataset_id"])
    async with maker() as owner:
        assert (
            await owner.scalar(
                text("SELECT count(*) FROM public.datasets WHERE id = ANY(:ids)"),
                {"ids": [image_operation_id, ensure_dataset_id]},
            )
            == 0
        )
        assert (
            await owner.scalar(
                text("SELECT count(*) FROM public.dataset_items WHERE id=:id"),
                {"id": item_id},
            )
            == 0
        )
        assert (
            await owner.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_operator_operations "
                    "WHERE operation_id = ANY(:ids)"
                ),
                {"ids": operation_ids},
            )
            == 0
        )
        assert (
            await owner.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_spatial_operations "
                    "WHERE scope->>'dataset_id'=:dataset_id"
                ),
                {"dataset_id": str(ensure_dataset_id)},
            )
            == 0
        )
