"""Exercise video owner functions under the provisioned restricted roles."""

import os
import uuid

import pytest
from sqlalchemy import text

from aap_assets_video_inputs import execution as c
from tests.test_video_input_ledger_control import _call, _captured, _observation, _page

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data LOGIN role qualification",
    ),
]


async def test_video_role_matrix_exposes_exact_caller_capabilities_without_table_access(
    db_session,
):
    roles = {
        "append_client": {"workflow"},
        "asset_media_control_client": {"control"},
        "asset_media_worker_client": {"worker", "system"},
        "asset_cleanup_client": {"cleanup"},
        "labeling_code": {"labeling"},
        "identity_code": set(),
        "workspace_code": set(),
        "jobs_code": set(),
        "learning_code": set(),
        "asset_image_worker_client": set(),
    }
    for role, permitted in roles.items():
        for name, definition in c.OPERATIONS.items():
            signature = f"aap_assets_v1.{name}({','.join(definition.sql_types)})"
            granted = await db_session.scalar(
                text(
                    "SELECT pg_catalog.has_function_privilege(:role,:signature,'EXECUTE')"
                ),
                {"role": role, "signature": signature},
            )
            assert granted == (definition.role in permitted), (role, name)
        for table in (
            "asset_accepted_video_manifests",
            "asset_accepted_video_members",
            "asset_accepted_video_inputs",
            "asset_accepted_video_input_attempts",
            "asset_accepted_video_times",
            "asset_accepted_video_frames",
            "asset_accepted_video_frame_attempts",
        ):
            assert not await db_session.scalar(
                text(
                    "SELECT pg_catalog.has_any_column_privilege(:role,:table,'SELECT,INSERT,UPDATE,REFERENCES')"
                ),
                {"role": role, "table": "public." + table},
            ), (role, table)
    for table in (
        "tasks",
        "task_batches",
        "task_locks",
        "async_jobs",
        "annotations",
        "annotation_video_input_sources",
        "users",
        "projects",
    ):
        assert not await db_session.scalar(
            text(
                "SELECT pg_catalog.has_any_column_privilege('assets_code',:table,'SELECT,INSERT,UPDATE,REFERENCES')"
            ),
            {"table": "public." + table},
        ), table
    assert (
        await db_session.scalar(
            text(
                "SELECT pg_catalog.pg_get_userbyid(proowner) FROM pg_catalog.pg_proc WHERE oid="
                "'aap_assets_v1.capture_accepted_video_manifest(uuid,uuid,uuid,jsonb)'::regprocedure"
            )
        )
        == "assets_code"
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT pg_catalog.pg_get_userbyid(proowner) FROM pg_catalog.pg_proc WHERE oid="
                "'aap_labeling_v1.accepted_video_input_referenced(uuid)'::regprocedure"
            )
        )
        == "labeling_code"
    )


async def test_video_control_and_worker_execute_through_real_owner_boundaries(
    db_session,
):
    actor, _, _, _, header, _ = await _captured(db_session)
    await db_session.execute(text("SET LOCAL ROLE asset_media_control_client"))
    try:
        member = (await _page(db_session, header)).members[0]
        receipt = await _call(
            db_session,
            "prepare_accepted_video_member",
            _observation(
                header,
                member,
                identity={"etag": "role-proof", "byte_size": 128, "version_id": None},
            ),
        )
    finally:
        await db_session.execute(text("RESET ROLE"))
    assert receipt.member.state == "preparing"
    await db_session.execute(text("SET LOCAL ROLE asset_media_worker_client"))
    try:
        claimed = await _call(
            db_session,
            "claim_accepted_video_input",
            c.ClaimVideoInputRequest(
                worker_id=uuid.uuid4(), input_id=receipt.member.input.input_id
            ),
        )
        assert claimed.claim is not None and claimed.claim.input.actor_id == actor
        failed = await _call(
            db_session,
            "fail_accepted_video_input",
            c.FailVideoInputRequest(
                input_id=claimed.claim.input.input_id,
                generation=claimed.claim.generation,
                lease_token=claimed.claim.lease_token,
                error_code="processing_failed",
            ),
        )
        assert failed.status == "failed"
    finally:
        await db_session.execute(text("RESET ROLE"))
    await db_session.execute(text("SET LOCAL ROLE asset_media_control_client"))
    try:
        sealed = await _call(
            db_session,
            "seal_accepted_video_manifest",
            c.SealVideoManifestRequest(
                job_id=header.job_id,
                actor_id=actor,
                project_id=header.project_id,
                selection_digest=header.selection_digest,
            ),
        )
        assert sealed.failed_count == 1
    finally:
        await db_session.execute(text("RESET ROLE"))
