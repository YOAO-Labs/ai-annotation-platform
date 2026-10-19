"""Video membership crosses Labeling through precise owner EXECUTE grants."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aap_assets_video_inputs import selection as c
from tests.test_video_input_authorities_native import _seed_video_task
from tests.test_video_input_selection_native import _add_task

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data role verifier",
    ),
]


async def test_assets_streams_and_locks_video_members_without_private_table_access(
    db_session,
):
    actor, project, _, _, task = await _seed_video_task(db_session)
    selection = c.VideoSelection(
        batch_id=None,
        task_ids=(task,),
        predict_mode="overwrite",
        execution_scope="bulk",
        sampling={"max_frames": 3},
    )
    request = c.OpenVideoInputManifestMembersRequest(
        actor_id=actor, project_id=project.id, selection=selection
    )
    await db_session.execute(text("SET LOCAL ROLE assets_code"))
    try:
        for relation in (
            "tasks",
            "task_batches",
            "task_locks",
            "async_jobs",
            "users",
            "projects",
            "project_members",
        ):
            assert not await db_session.scalar(
                text(
                    "SELECT has_any_column_privilege(current_user,:relation,'SELECT,INSERT,UPDATE,REFERENCES') "
                    "OR has_table_privilege(current_user,:relation,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                ),
                {"relation": "public." + relation},
            ), relation
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                await db_session.execute(
                    text("SELECT id FROM public.task_batches LIMIT 1")
                )
        raw = await db_session.scalar(
            text(
                "SELECT aap_labeling_v1.open_video_input_manifest_members(:actor,:project,CAST(:selection AS jsonb))"
            ),
            {
                "actor": actor,
                "project": project.id,
                "selection": selection.model_dump_json(),
            },
        )
        cursor = c.parse_native(
            "open_video_input_manifest_members", request, raw
        ).cursor
        # The cursor name was parsed against the protocol's fixed identifier pattern.
        header = c.parse_cursor_row(
            (
                await db_session.execute(text(f'FETCH FORWARD 1 FROM "{cursor}"'))
            ).scalar_one()
        )
        member = c.parse_cursor_row(
            (
                await db_session.execute(text(f'FETCH FORWARD 1 FROM "{cursor}"'))
            ).scalar_one()
        )
        assert (header.candidate_count, header.selected_count) == (1, 1)
        assert member.task.task_id == task
        await db_session.execute(text(f'CLOSE "{cursor}"'))
        lock = c.LockVideoInputManifestMemberRequest(
            actor_id=actor,
            project_id=project.id,
            task_id=task,
            selection=selection,
            captured_task=member.task,
        )
        raw = await db_session.scalar(
            text(
                "SELECT aap_labeling_v1.lock_video_input_manifest_member(:actor,:project,:task,CAST(:selection AS jsonb),CAST(:captured AS jsonb))"
            ),
            {
                "actor": actor,
                "project": project.id,
                "task": task,
                "selection": selection.model_dump_json(),
                "captured": member.task.model_dump_json(),
            },
        )
        assert c.parse_native(
            "lock_video_input_manifest_member", lock, raw
        ).upload_ready
    finally:
        await db_session.execute(text("RESET ROLE"))


async def test_video_selection_exposes_only_two_public_capabilities_to_assets(
    db_session,
):
    for name, operation in c.OPERATIONS.items():
        signature = (
            "aap_labeling_v1." + name + "(" + ",".join(operation.sql_types) + ")"
        )
        for role, allowed in (
            ("assets_code", True),
            ("append_client", False),
            ("asset_media_control_client", False),
            ("asset_image_worker_client", False),
        ):
            assert (
                await db_session.scalar(
                    text(
                        "SELECT has_function_privilege(:role,p.oid,'EXECUTE') FROM pg_catalog.pg_proc p "
                        "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname||'.'||p.proname||'('||replace(pg_catalog.oidvectortypes(p.proargtypes),' ','')||')'=:signature"
                    ),
                    {"role": role, "signature": signature},
                )
                is allowed
            ), (role, signature)
    helpers = (
        await db_session.execute(
            text(
                "SELECT p.oid, pg_catalog.pg_get_userbyid(p.proowner) AS owner FROM pg_catalog.pg_proc p "
                "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='aap_labeling_v1' AND p.proname IN ("
                "'_video_input_selection_reply','_video_input_manifest_selection_valid',"
                "'_video_input_captured_task_valid','_video_input_selection_task_access')"
            )
        )
    ).all()
    assert len(helpers) == 4
    for helper in helpers:
        assert helper.owner == "labeling_code"
        assert not await db_session.scalar(
            text(
                "SELECT has_function_privilege('assets_code',CAST(:oid AS oid),'EXECUTE')"
            ),
            {"oid": helper.oid},
        )


async def test_implicit_video_cursor_streams_complete_membership_beyond_explicit_limit(
    db_session,
):
    actor, project, dataset, _, first = await _seed_video_task(db_session)
    expected = {first}
    for _ in range(204):
        task, _ = await _add_task(
            db_session, project_id=project.id, dataset_id=dataset, linked=False
        )
        expected.add(task)
    selection = c.VideoSelection(
        batch_id=None,
        task_ids=None,
        predict_mode="overwrite",
        execution_scope="bulk",
        sampling={"max_frames": 3},
    )
    request = c.OpenVideoInputManifestMembersRequest(
        actor_id=actor, project_id=project.id, selection=selection
    )
    await db_session.execute(text("SET LOCAL ROLE assets_code"))
    try:
        raw = await db_session.scalar(
            text(
                "SELECT aap_labeling_v1.open_video_input_manifest_members(:actor,:project,CAST(:selection AS jsonb))"
            ),
            {
                "actor": actor,
                "project": project.id,
                "selection": selection.model_dump_json(),
            },
        )
        cursor = c.parse_native(
            "open_video_input_manifest_members", request, raw
        ).cursor
        seen, sizes = [], []
        header = None
        while True:
            rows = (
                (await db_session.execute(text(f'FETCH FORWARD 100 FROM "{cursor}"')))
                .scalars()
                .all()
            )
            if not rows:
                break
            sizes.append(len(rows))
            for raw_row in rows:
                row = c.parse_cursor_row(raw_row)
                if isinstance(row, c.SelectionCursorHeader):
                    assert header is None and not seen
                    header = row
                else:
                    assert isinstance(row, c.SelectionCursorMember)
                    assert row.ordinal == len(seen) + 1
                    seen.append(row.task.task_id)
        await db_session.execute(text(f'CLOSE "{cursor}"'))
        assert sizes == [100, 100, 6]
        assert header.candidate_count == header.selected_count == 205
        assert seen == sorted(expected, key=str)
    finally:
        await db_session.execute(text("RESET ROLE"))
