"""Verify exact native role boundaries for retained full-video tracker input."""

import os

import pytest
from sqlalchemy import text

from aap_assets_video_inputs import tracker as c

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data role verifier",
    ),
]


async def _function_allowed(db, role: str, signature: str) -> bool:
    return bool(
        await db.scalar(
            text(
                "SELECT pg_catalog.has_function_privilege(:role,:signature,'EXECUTE')"
            ),
            {"role": role, "signature": signature},
        )
    )


async def test_tracker_clients_have_only_their_control_and_worker_operations(
    db_session,
):
    for name, operation in c.TRACKER_OPERATIONS.items():
        signature = (
            operation.namespace + "." + name + "(" + ",".join(operation.sql_types) + ")"
        )
        for role, expected in (
            ("asset_media_control_client", operation.role == "control"),
            ("asset_media_worker_client", operation.role == "worker"),
        ):
            assert await _function_allowed(db_session, role, signature) is expected, (
                role,
                signature,
            )

    # Retention is an internal Labeling projection called by the Assets-owned
    # sweeper; media logins cannot call Labeling directly.
    referenced = "aap_labeling_v1.video_tracker_input_referenced(uuid)"
    assert await _function_allowed(db_session, "assets_code", referenced)
    assert not await _function_allowed(
        db_session, "asset_media_worker_client", referenced
    )
    assert not await _function_allowed(
        db_session, "asset_media_control_client", referenced
    )


async def test_tracker_owners_keep_private_rows_and_cross_owner_reads_narrow(
    db_session,
):
    for table in (
        "video_tracker_jobs",
        "video_tracker_input_scopes",
        "annotation_video_tracker_input_sources",
        "annotations",
        "tasks",
    ):
        assert not await db_session.scalar(
            text(
                "SELECT pg_catalog.has_any_column_privilege("
                ":role,:table,'SELECT,INSERT,UPDATE,REFERENCES')"
            ),
            {"role": "assets_code", "table": "public." + table},
        ), table
        assert not await db_session.scalar(
            text(
                "SELECT pg_catalog.has_any_column_privilege("
                ":role,:table,'SELECT,INSERT,UPDATE,REFERENCES')"
            ),
            {"role": "asset_media_control_client", "table": "public." + table},
        ), table
    for table in (
        "asset_accepted_video_inputs",
        "asset_accepted_video_input_attempts",
        "asset_accepted_video_times",
        "asset_accepted_video_frames",
    ):
        assert not await db_session.scalar(
            text(
                "SELECT pg_catalog.has_any_column_privilege("
                ":role,:table,'SELECT,INSERT,UPDATE,REFERENCES')"
            ),
            {"role": "labeling_code", "table": "public." + table},
        ), table

    for operation, definition in c.ASSETS_OPERATIONS.items():
        signature = (
            "aap_assets_v1." + operation + "(" + ",".join(definition.sql_types) + ")"
        )
        owner = await db_session.scalar(
            text(
                "SELECT pg_catalog.pg_get_userbyid(proc.proowner) "
                "FROM pg_catalog.pg_proc AS proc "
                "WHERE proc.oid=CAST(:signature AS regprocedure)"
            ),
            {"signature": signature},
        )
        assert owner == "assets_code", signature

    for operation in c.LABELING_OPERATIONS:
        signature = (
            "aap_labeling_v1."
            + operation
            + "("
            + ",".join(c.LABELING_OPERATIONS[operation].sql_types)
            + ")"
        )
        owner = await db_session.scalar(
            text(
                "SELECT pg_catalog.pg_get_userbyid(proc.proowner) "
                "FROM pg_catalog.pg_proc AS proc "
                "WHERE proc.oid=CAST(:signature AS regprocedure)"
            ),
            {"signature": signature},
        )
        assert owner == "labeling_code", signature

    # Assets receives exactly three Labeling projections: current canonical
    # scope, an admission failure update, and owner-local retention lookup.
    for name, operation in c.LABELING_OPERATIONS.items():
        signature = (
            "aap_labeling_v1." + name + "(" + ",".join(operation.sql_types) + ")"
        )
        assert await _function_allowed(db_session, "assets_code", signature) is (
            name
            in {
                "project_video_tracker_input",
                "fail_video_tracker_input_preparation",
                "video_tracker_input_referenced",
            }
        )


async def test_labeling_table_grants_are_scoped_to_its_native_receipts(db_session):
    # Native video publication owns status/channel facts; Celery identifiers stay private.
    for column, privilege, expected in (
        ("id", "UPDATE", True),
        ("status", "SELECT", True),
        ("status", "UPDATE", True),
        ("event_channel", "SELECT", True),
        ("celery_task_id", "SELECT", False),
        ("celery_task_id", "UPDATE", False),
        ("created_by", "UPDATE", False),
    ):
        assert (
            await db_session.scalar(
                text(
                    "SELECT pg_catalog.has_column_privilege(:role,:table,:column,:privilege)"
                ),
                {
                    "role": "labeling_code",
                    "table": "public.video_tracker_jobs",
                    "column": column,
                    "privilege": privilege,
                },
            )
            is expected
        ), (column, privilege)
    grants = (
        ("video_tracker_input_scopes", "SELECT,INSERT,UPDATE"),
        ("annotation_video_tracker_input_sources", "SELECT,INSERT"),
    )
    for table, privileges in grants:
        assert await db_session.scalar(
            text("SELECT pg_catalog.has_table_privilege(:role,:table,:privileges)"),
            {
                "role": "labeling_code",
                "table": "public." + table,
                "privileges": privileges,
            },
        ), (table, privileges)
        assert not await db_session.scalar(
            text(
                "SELECT pg_catalog.has_any_column_privilege("
                ":role,:table,'SELECT,INSERT,UPDATE,REFERENCES')"
            ),
            {"role": "assets_code", "table": "public." + table},
        ), table


async def test_collaboration_policy_is_a_workspace_owned_labeling_projection(
    db_session,
):
    signature = "aap_workspace_v1.read_media_video_collaboration_policy(uuid)"
    assert (
        await db_session.scalar(
            text(
                "SELECT pg_catalog.pg_get_userbyid(proc.proowner) FROM pg_catalog.pg_proc AS proc "
                "WHERE proc.oid=CAST(:signature AS regprocedure)"
            ),
            {"signature": signature},
        )
        == "workspace_code"
    )
    for role, expected in (
        ("labeling_code", True),
        ("assets_code", False),
        ("asset_media_control_client", False),
        ("asset_media_worker_client", False),
    ):
        assert await _function_allowed(db_session, role, signature) is expected
    assert not await db_session.scalar(
        text(
            "SELECT pg_catalog.has_column_privilege("
            "'labeling_code','public.projects','video_collaboration','SELECT')"
        )
    )
