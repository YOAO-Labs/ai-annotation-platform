"""Native retained-video admission, replay and preparation barriers."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs.digests import (
    member_digest,
    outcome_digest,
    sealed_seed,
    selection_seed,
)
from tests.test_video_input_authorities_native import _seed_video_job, _seed_video_task
from tests.test_video_input_ledger_state import _installed
from tests.test_video_input_selection_native import _add_task, _selection

pytestmark = pytest.mark.asyncio


async def _call(db, operation, request):
    definition = c.OPERATIONS[operation]
    arguments = ",".join(
        f"CAST(:{name} AS {kind})"
        for name, kind in zip(definition.arguments, definition.sql_types, strict=True)
    )
    params = request.model_dump(mode="json")
    for name, kind in zip(definition.arguments, definition.sql_types, strict=True):
        if kind == "jsonb" and params[name] is not None:
            params[name] = json.dumps(params[name], ensure_ascii=False)
    raw = await db.scalar(
        text(f"SELECT aap_assets_v1.{operation}({arguments})"), params
    )
    return c.parse_native(operation, request, raw)


async def _captured(db, *, linked=True, size=1):
    await _installed(db)
    actor, project, dataset, item, task = await _seed_video_task(db, linked=linked)
    tasks = [task]
    for _ in range(size - 1):
        added, _ = await _add_task(db, project_id=project.id, dataset_id=dataset)
        tasks.append(added)
    selection = _selection()
    job = await _seed_video_job(
        db, actor_id=actor, project_id=project.id, selection=selection
    )
    request = c.CaptureVideoManifestRequest(
        job_id=job, actor_id=actor, project_id=project.id, selection=selection
    )
    header = await _call(db, "capture_accepted_video_manifest", request)
    await db.execute(
        text(
            "UPDATE public.async_jobs SET payload=payload || "
            "pg_catalog.jsonb_build_object('accepted_video_selection_digest',CAST(:digest AS text)) WHERE id=:job"
        ),
        {"digest": header.selection_digest, "job": job},
    )
    return actor, project.id, item, task, header, request


async def _page(db, header, *, cursor=0, size=100):
    return await _call(
        db,
        "read_accepted_video_manifest_page",
        c.VideoManifestPageRequest(
            **{
                key: getattr(header, key)
                for key in ("job_id", "actor_id", "project_id")
            },
            selection_digest=header.selection_digest,
            cursor=cursor,
            page_size=size,
        ),
    )


def _observation(header, member, *, identity=None, error=None):
    return c.ObserveVideoMemberRequest(
        job_id=header.job_id,
        actor_id=header.actor_id,
        project_id=header.project_id,
        selection_digest=header.selection_digest,
        ordinal=member.ordinal,
        task_id=member.task.task_id,
        source_snapshot=member.source.source_snapshot if member.source else None,
        timetable_revision=member.source.timetable_revision if member.source else None,
        object_identity=identity,
        terminal_error=error,
    )


async def test_native_capture_pages_complete_membership_and_hashes_original_facts(
    db_session,
):
    _, _, _, _, header, request = await _captured(db_session, size=205)
    assert header.candidate_count == header.selected_count == 205
    assert header.prepared_count == 0
    digest = selection_seed(header, header.selection, 205, 205)
    seen = []
    for cursor, expected in ((0, 100), (100, 100), (200, 5)):
        page = await _page(db_session, header, cursor=cursor)
        assert len(page.members) == expected
        for member in page.members:
            seen.append(member.task.task_id)
            digest = member_digest(
                digest,
                c.CapturedVideoMember(
                    ordinal=member.ordinal,
                    task=member.task,
                    source=member.source,
                    error=None,
                ),
            )
    assert seen == sorted(seen)
    assert len(set(seen)) == 205
    assert digest == header.selection_digest
    replay = await _call(db_session, "capture_accepted_video_manifest", request)
    assert replay == header
    with pytest.raises(c.VideoInputFailure, match="manifest_cursor_invalid"):
        await _page(db_session, header, cursor=206)


async def test_first_identity_is_retained_and_prepare_replay_never_retargets_source(
    db_session,
):
    actor, _, item, task, header, _ = await _captured(db_session)
    member = (await _page(db_session, header)).members[0]
    assert member.task.file_path != member.source.file_path
    assert member.source.object_identity is None
    request = _observation(
        header,
        member,
        identity={"etag": "original", "byte_size": 128, "version_id": None},
    )
    prepared = await _call(db_session, "prepare_accepted_video_member", request)
    assert prepared.member.state == "preparing"
    assert prepared.manifest.prepared_count == 0
    record = prepared.member.input
    assert record.task == member.task.binding()
    assert record.source.object_identity == request.object_identity
    observed = await db_session.scalar(
        text("SELECT media_object_identity FROM public.dataset_items WHERE id=:item"),
        {"item": item},
    )
    assert observed == request.object_identity.model_dump(mode="json")
    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET file_path='source/replaced.mp4' WHERE id=:item"
        ),
        {"item": item},
    )
    replay = await _call(db_session, "prepare_accepted_video_member", request)
    assert replay == prepared
    work = await _call(
        db_session,
        "read_accepted_video_input_work",
        c.ReadVideoInputRequest(
            input_id=record.input_id, job_id=header.job_id, task_id=task, actor_id=actor
        ),
    )
    assert work.source.file_path == member.source.file_path
    assert work.input == record
    with pytest.raises(c.VideoInputFailure, match="manifest_not_ready"):
        await _call(
            db_session,
            "seal_accepted_video_manifest",
            c.SealVideoManifestRequest(
                job_id=header.job_id,
                actor_id=actor,
                project_id=header.project_id,
                selection_digest=header.selection_digest,
            ),
        )


async def test_drift_is_committed_with_terminal_member_and_sealed_digest(db_session):
    _, _, item, _, header, _ = await _captured(db_session)
    member = (await _page(db_session, header)).members[0]
    result = await _call(
        db_session,
        "prepare_accepted_video_member",
        _observation(
            header,
            member,
            identity={"etag": "changed", "byte_size": 256, "version_id": "source-v2"},
        ),
    )
    assert result.member.state == "failed"
    assert result.member.error == "source_changed"
    assert result.member.input is None
    assert result.manifest.prepared_count == 1
    assert await db_session.scalar(
        text("SELECT media_source_drifted FROM public.dataset_items WHERE id=:item"),
        {"item": item},
    )
    seal_request = c.SealVideoManifestRequest(
        job_id=header.job_id,
        actor_id=header.actor_id,
        project_id=header.project_id,
        selection_digest=header.selection_digest,
    )
    sealed = await _call(db_session, "seal_accepted_video_manifest", seal_request)
    assert sealed.ready_count == 0 and sealed.failed_count == 1
    assert sealed.sealed_digest == outcome_digest(
        sealed_seed(result.manifest, 0, 1), result.member
    )
    assert (
        await _call(db_session, "seal_accepted_video_manifest", seal_request) == sealed
    )
    assert (await _page(db_session, sealed)).members[0] == result.member


async def test_missing_task_link_is_a_complete_fixed_member_without_source_io(
    db_session,
):
    _, _, _, _, header, _ = await _captured(db_session, linked=False)
    member = (await _page(db_session, header)).members[0]
    assert header.prepared_count == 1
    assert member.source is None and member.input is None
    assert member.state == "failed" and member.error == "source_missing"
    replay = await _call(
        db_session,
        "prepare_accepted_video_member",
        _observation(header, member, error="source_missing"),
    )
    assert replay.member == member and replay.manifest == header


async def test_capture_replay_recovers_before_job_digest_but_execution_rejects_mismatch(
    db_session,
):
    _, _, _, _, header, capture = await _captured(db_session)
    member = (await _page(db_session, header)).members[0]
    await db_session.execute(
        text(
            "UPDATE public.async_jobs SET payload=payload-'accepted_video_selection_digest' WHERE id=:job"
        ),
        {"job": header.job_id},
    )
    assert await _call(db_session, "capture_accepted_video_manifest", capture) == header
    request = _observation(header, member, error="source_missing")
    with pytest.raises(c.VideoInputFailure, match="manifest_scope_mismatch"):
        await _call(db_session, "prepare_accepted_video_member", request)
    assert (
        await db_session.scalar(
            text(
                "SELECT state FROM public.asset_accepted_video_members WHERE job_id=:job"
            ),
            {"job": header.job_id},
        )
        == "unprepared"
    )
    assert not await db_session.scalar(
        text(
            "SELECT pg_catalog.count(*) FROM public.asset_accepted_video_inputs WHERE job_id=:job"
        ),
        {"job": header.job_id},
    )
    wrong = capture.model_copy(update={"job_id": uuid4()})
    with pytest.raises(c.VideoInputFailure, match="job_not_active"):
        await _call(db_session, "capture_accepted_video_manifest", wrong)
    assert (
        await db_session.scalar(
            text(
                "SELECT pg_catalog.count(*) FROM public.asset_accepted_video_members WHERE job_id=:job"
            ),
            {"job": wrong.job_id},
        )
        == 0
    )
