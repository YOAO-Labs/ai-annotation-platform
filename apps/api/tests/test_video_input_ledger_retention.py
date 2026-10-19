"""Native checks for typed video evidence and partitioned retention cleanup."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql"
pytestmark = pytest.mark.asyncio


async def _run_file(db, name: str) -> None:
    for statement in (SQL / name).read_text().split("-- aap-statement-break"):
        if statement.strip():
            await db.execute(text(statement.strip().replace(":", r"\:")))


async def _installed(db) -> None:
    if not await db.scalar(
        text("SELECT pg_catalog.to_regclass('public.asset_accepted_video_inputs')")
    ):
        await _run_file(db, "0225_video_inputs_state.sql")
        await _run_file(db, "0225_video_inputs_control.sql")
    if not await db.scalar(
        text(
            "SELECT pg_catalog.to_regprocedure('aap_assets_v1.sweep_accepted_video_inputs(integer)')"
        )
    ):
        await _run_file(db, "0225_video_inputs_retention.sql")
    if not await db.scalar(
        text(
            "SELECT pg_catalog.to_regprocedure('aap_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)')"
        )
    ):
        await _run_file(db, "0225_video_inputs_garbage.sql")


def _reply(value):
    return json.loads(value) if isinstance(value, str) else value


async def test_native_evidence_validator_is_typed_strict_and_has_only_annotation_fk(
    db_session,
):
    await _installed(db_session)
    ready = {
        "schema": "aap-assets-video-input-evidence/v1",
        "input_id": "00000000-0000-4000-8000-000000000010",
        "job_id": "00000000-0000-4000-8000-000000000011",
        "task_id": "00000000-0000-4000-8000-000000000012",
        "project_id": "00000000-0000-4000-8000-000000000013",
        "plan_digest": "a" * 64,
        "timebase_digest": "b" * 64,
        "frame_id": "00000000-0000-4000-8000-000000000014",
        "entry": {"ordinal": 1, "frame_index": 0, "pts_ms": -3, "is_keyframe": False},
        "source_sha256": "c" * 64,
        "frame_sha256": "d" * 64,
    }
    failed = {
        **ready,
        "schema": "aap-assets-video-input-failure/v1",
        "frame_id": None,
        "frame_sha256": None,
    }
    values = await db_session.execute(
        text(
            "SELECT aap_assets_v1._video_input_evidence_valid(CAST(:ready AS jsonb),true),"
            "aap_assets_v1._video_input_evidence_valid(CAST(:failed AS jsonb),false),"
            "aap_assets_v1._video_input_evidence_valid(CAST(:failed AS jsonb),true),"
            "aap_labeling_v1._video_input_evidence_valid(CAST(:ready AS jsonb),true),"
            "aap_labeling_v1._video_input_evidence_valid(CAST(:failed AS jsonb),false)"
        ),
        {"ready": json.dumps(ready), "failed": json.dumps(failed)},
    )
    assert tuple(values.one()) == (True, True, False, True, True)

    for path, invalid in (
        ("extra", True),
        ("task_id", None),
        ("frame_sha256", "not-a-digest"),
    ):
        malformed = json.loads(json.dumps(ready))
        if path == "extra":
            malformed["unexpected"] = invalid
        else:
            malformed[path] = invalid
        assert not await db_session.scalar(
            text(
                "SELECT aap_assets_v1._video_input_evidence_valid(CAST(:value AS jsonb),true)"
            ),
            {"value": json.dumps(malformed)},
        )
        assert not await db_session.scalar(
            text(
                "SELECT aap_labeling_v1._video_input_evidence_valid(CAST(:value AS jsonb),true)"
            ),
            {"value": json.dumps(malformed)},
        )

    foreign_keys = await db_session.execute(
        text(
            "SELECT pg_catalog.pg_get_constraintdef(constraint_row.oid) "
            "FROM pg_catalog.pg_constraint AS constraint_row "
            "WHERE constraint_row.conrelid='public.annotation_video_input_sources'::regclass "
            "AND constraint_row.contype='f'"
        )
    )
    definitions = [row[0] for row in foreign_keys]
    assert len(definitions) == 1 and "REFERENCES annotations" in definitions[0]


async def test_image_and_video_garbage_clients_claim_only_their_prefix_partition(
    db_session,
):
    await _installed(db_session)
    image_id, video_id = uuid.uuid4(), uuid.uuid4()
    token_worker = uuid.uuid4()
    image_prefix = f"accepted-inputs/{uuid.uuid4()}/g1/a{uuid.uuid4()}/"
    video_prefix = f"accepted-video-inputs/{uuid.uuid4()}/g1/a{uuid.uuid4()}/"
    await db_session.execute(
        text(
            "INSERT INTO public.asset_accepted_input_garbage(garbage_id,prefix,reason,not_before) "
            "VALUES (CAST(:image_id AS uuid),:image_prefix,'test_partition',"
            "pg_catalog.clock_timestamp()-interval '1 second'),"
            "(CAST(:video_id AS uuid),:video_prefix,'test_partition',"
            "pg_catalog.clock_timestamp()-interval '1 second')"
        ),
        {
            "image_id": image_id,
            "video_id": video_id,
            "image_prefix": image_prefix,
            "video_prefix": video_prefix,
        },
    )
    image_reply = _reply(
        await db_session.scalar(
            text(
                "SELECT aap_assets_v1.claim_accepted_input_garbage(CAST(:worker AS uuid),300,10)"
            ),
            {"worker": token_worker},
        )
    )
    video_reply = _reply(
        await db_session.scalar(
            text(
                "SELECT aap_assets_v1.claim_accepted_video_input_garbage(CAST(:worker AS uuid),300,10)"
            ),
            {"worker": token_worker},
        )
    )
    assert image_reply["schema"] == "aap-assets-inputs/v1" and image_reply["ok"]
    assert (
        video_reply["schema"] == "aap-assets-video-input-execution/v1"
        and video_reply["ok"]
    )
    assert [row["garbage_id"] for row in image_reply["data"]["items"]] == [
        str(image_id)
    ]
    assert [row["prefix"] for row in image_reply["data"]["items"]] == [image_prefix]
    assert [row["garbage_id"] for row in video_reply["data"]["items"]] == [
        str(video_id)
    ]
    assert [row["prefix"] for row in video_reply["data"]["items"]] == [video_prefix]

    image_token = image_reply["data"]["items"][0]["lease_token"]
    video_token = video_reply["data"]["items"][0]["lease_token"]
    wrong_partition = _reply(
        await db_session.scalar(
            text(
                "SELECT aap_assets_v1.complete_accepted_video_input_garbage("
                "CAST(:garbage AS uuid),CAST(:token AS uuid))"
            ),
            {"garbage": image_id, "token": image_token},
        )
    )
    assert wrong_partition["ok"] is False
    assert wrong_partition["error"]["code"] == "input_scope_mismatch"

    wrong_image_ack = _reply(
        await db_session.scalar(
            text(
                "SELECT aap_assets_v1.complete_accepted_input_garbage("
                "CAST(:garbage AS uuid),CAST(:token AS uuid))"
            ),
            {"garbage": video_id, "token": video_token},
        )
    )
    assert wrong_image_ack["ok"] is False
    assert wrong_image_ack["error"]["code"] == "input_scope_mismatch"

    completed = _reply(
        await db_session.scalar(
            text(
                "SELECT aap_assets_v1.complete_accepted_video_input_garbage("
                "CAST(:garbage AS uuid),CAST(:token AS uuid))"
            ),
            {"garbage": video_id, "token": video_token},
        )
    )
    assert completed["ok"] is True and completed["data"]["completed"] is True
    repeated = _reply(
        await db_session.scalar(
            text(
                "SELECT aap_assets_v1.complete_accepted_video_input_garbage("
                "CAST(:garbage AS uuid),CAST(:token AS uuid))"
            ),
            {"garbage": video_id, "token": video_token},
        )
    )
    assert repeated == completed


async def test_video_garbage_queue_will_not_start_cleanup_before_late_put_grace(
    db_session,
):
    await _installed(db_session)
    prefix = f"accepted-video-inputs/{uuid.uuid4()}/g3/a{uuid.uuid4()}/"
    await db_session.execute(
        text(
            "SELECT aap_assets_v1._video_input_queue_prefix(:prefix,'lease_expired',"
            "pg_catalog.clock_timestamp()-interval '1 day')"
        ),
        {"prefix": prefix},
    )
    not_before = await db_session.scalar(
        text(
            "SELECT not_before FROM public.asset_accepted_input_garbage WHERE prefix=:prefix"
        ),
        {"prefix": prefix},
    )
    remaining = await db_session.scalar(
        text(
            "SELECT not_before-pg_catalog.clock_timestamp() FROM public.asset_accepted_input_garbage WHERE prefix=:prefix"
        ),
        {"prefix": prefix},
    )
    assert not_before is not None and remaining.total_seconds() >= 23 * 60 * 60
    assert await db_session.scalar(
        text("SELECT aap_assets_v1._video_input_prefix_valid(:prefix)"),
        {"prefix": prefix},
    )
