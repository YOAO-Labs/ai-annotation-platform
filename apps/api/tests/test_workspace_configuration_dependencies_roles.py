"""The Workspace code owner alone can call the new cross-owner helpers."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


FUNCTIONS = (
    (
        "aap_identity_v1.workspace_configuration_org_scope(uuid,uuid)",
        "identity_code",
    ),
    (
        "aap_assets_v1.workspace_configuration_datasets(uuid)",
        "assets_code",
    ),
    (
        "aap_labeling_v1.workspace_configuration_video_facts(uuid)",
        "labeling_code",
    ),
    (
        "aap_inference_v1.enable_workspace_project_backend(uuid,uuid)",
        "inference_code",
    ),
)


def _decoded_json(value):
    return json.loads(value) if isinstance(value, str) else value


FORBIDDEN_RAW_TABLES = (
    "public.organization_members",
    "public.datasets",
    "public.project_datasets",
    "public.scenes",
    "public.annotations",
    "public.tasks",
    "public.video_tracker_jobs",
    "public.ml_backend_registry",
    "public.ml_backend_pool_members",
    "public.ml_backend_service_pools",
    "public.project_ml_backend_pool",
)


async def test_workspace_configuration_dependencies_only_expose_typed_functions(
    test_engine,
):
    async with test_engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT namespace.nspname || '.' || function.proname || '(' ||
                           pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature,
                           pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                           pg_catalog.has_function_privilege(
                               'workspace_code', function.oid, 'EXECUTE'
                           ) AS workspace_can_execute,
                           ARRAY(
                               SELECT COALESCE(role.rolname, 'PUBLIC')
                               FROM pg_catalog.aclexplode(
                                   COALESCE(
                                       function.proacl,
                                       pg_catalog.acldefault('f', function.proowner)
                                   )
                               ) AS acl
                               LEFT JOIN pg_catalog.pg_roles AS role
                                 ON role.oid = acl.grantee
                               WHERE acl.privilege_type = 'EXECUTE'
                               ORDER BY COALESCE(role.rolname, 'PUBLIC')
                           ) AS execute_grantees,
                           EXISTS (
                               SELECT 1
                               FROM pg_catalog.aclexplode(
                                   COALESCE(
                                       function.proacl,
                                       pg_catalog.acldefault('f', function.proowner)
                                   )
                               ) AS acl
                               WHERE acl.grantee = 0
                                 AND acl.privilege_type = 'EXECUTE'
                           ) AS public_can_execute
                    FROM pg_catalog.pg_proc AS function
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = function.pronamespace
                    WHERE (namespace.nspname, function.proname,
                           pg_catalog.oidvectortypes(function.proargtypes)) IN (
                        ('aap_identity_v1', 'workspace_configuration_org_scope',
                         'uuid, uuid'),
                        ('aap_assets_v1', 'workspace_configuration_datasets', 'uuid'),
                        ('aap_labeling_v1', 'workspace_configuration_video_facts', 'uuid'),
                        ('aap_inference_v1', 'enable_workspace_project_backend',
                         'uuid, uuid')
                    )
                    ORDER BY namespace.nspname, function.proname
                    """
                    )
                )
            )
            .mappings()
            .all()
        )

        expected = {signature.replace(" ", ""): owner for signature, owner in FUNCTIONS}
        assert len(rows) == len(FUNCTIONS)
        for row in rows:
            compact_signature = row["signature"].replace(" ", "")
            assert compact_signature in expected
            assert row["owner"] == expected[compact_signature]
            assert row["workspace_can_execute"] is True
            assert set(row["execute_grantees"]) == {row["owner"], "workspace_code"}
            assert row["public_can_execute"] is False

        privileges = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT relation_name,
                           pg_catalog.has_table_privilege(
                               'workspace_code', relation_name,
                               'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                           ) AS table_access,
                           pg_catalog.has_any_column_privilege(
                               'workspace_code', relation_name,
                               'SELECT,INSERT,UPDATE,REFERENCES'
                           ) AS column_access
                    FROM unnest(CAST(:tables AS text[])) AS item(relation_name)
                    """
                    ),
                    {"tables": list(FORBIDDEN_RAW_TABLES)},
                )
            )
            .mappings()
            .all()
        )
        assert len(privileges) == len(FORBIDDEN_RAW_TABLES)
        assert all(
            not row["table_access"] and not row["column_access"] for row in privileges
        )

    # Exercise every function under the intended Workspace NOLOGIN code role.
    org_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    registry_id = uuid.uuid4()
    async with test_engine.begin() as connection:
        await connection.execute(text("SET LOCAL ROLE workspace_code"))
        identity = _decoded_json(
            await connection.scalar(
                text(
                    """
                    SELECT aap_identity_v1.workspace_configuration_org_scope(
                        CAST(:actor AS uuid), CAST(:organization AS uuid)
                    )
                    """
                ),
                {"actor": actor_id, "organization": org_id},
            )
        )
        assets = _decoded_json(
            await connection.scalar(
                text(
                    """
                    SELECT aap_assets_v1.workspace_configuration_datasets(
                        CAST(:project AS uuid)
                    )
                    """
                ),
                {"project": project_id},
            )
        )
        labeling = _decoded_json(
            await connection.scalar(
                text(
                    """
                    SELECT aap_labeling_v1.workspace_configuration_video_facts(
                        CAST(:project AS uuid)
                    )
                    """
                ),
                {"project": project_id},
            )
        )
        inference = _decoded_json(
            await connection.scalar(
                text(
                    """
                    SELECT aap_inference_v1.enable_workspace_project_backend(
                        CAST(:project AS uuid), CAST(:registry AS uuid)
                    )
                    """
                ),
                {"project": project_id, "registry": registry_id},
            )
        )

    assert identity["visible"] is False
    assert assets["datasets"] == []
    assert labeling["annotation_count"] == 0
    assert labeling["active_tracker_count"] == 0
    assert labeling["dataset_item_ids"] == []
    assert inference["pool_id"] is None
