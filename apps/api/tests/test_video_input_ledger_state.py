"""Owned-database state/hash checks for the retained video ledger."""

import json
from pathlib import Path

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql"
VECTOR = ROOT / "apps/assets-media/tests/fixtures/video-input-manifest-vector.json"
pytestmark = pytest.mark.asyncio


async def _installed(db):
    if await db.scalar(
        text("SELECT pg_catalog.to_regclass('public.asset_accepted_video_inputs')")
    ):
        return
    for name in ("0225_video_inputs_state.sql", "0225_video_inputs_control.sql"):
        for statement in (SQL / name).read_text().split("-- aap-statement-break"):
            if statement.strip():
                await db.execute(text(statement.strip().replace(":", r"\:")))


async def test_video_state_compiles_and_native_capture_hash_matches_python_and_node(
    db_session,
):
    await _installed(db_session)
    vector = json.loads(VECTOR.read_text())
    scope = vector["scope"]
    digest = await db_session.scalar(
        text(
            "SELECT aap_assets_v1._video_input_selection_seed(CAST(:job AS uuid),CAST(:actor AS uuid),"
            "CAST(:project AS uuid),CAST(:selection AS jsonb),2,2)"
        ),
        {
            "job": scope["job_id"],
            "actor": scope["actor_id"],
            "project": scope["project_id"],
            "selection": json.dumps(vector["selection"]),
        },
    )
    for member in vector["captures"]:
        row = {
            "ordinal": member["ordinal"],
            "task": member["task"],
            "source_facts": member["source"],
            "capture_error": member["error"],
        }
        digest = await db_session.scalar(
            text(
                "SELECT aap_assets_v1._video_input_capture_digest(:previous,pg_catalog.jsonb_populate_record("
                "NULL::public.asset_accepted_video_members,CAST(:row AS jsonb)))"
            ),
            {"previous": digest, "row": json.dumps(row, ensure_ascii=False)},
        )
    assert digest == vector["selection_digest"]


async def test_native_video_shapes_reject_malformed_source_sampling_and_blob(
    db_session,
):
    await _installed(db_session)
    plan = json.loads(VECTOR.read_text())["members"][0]["input"]["plan"]
    assert await db_session.scalar(
        text(
            "SELECT aap_assets_v1._video_input_plan_shape_valid(CAST(:plan AS jsonb))"
        ),
        {"plan": json.dumps(plan)},
    )
    for path, bad in (
        ("video", None),
        ("actor_id", None),
        ("created_at", "2026-10-19"),
    ):
        changed = json.loads(json.dumps(plan))
        changed["input"][path] = bad
        assert not await db_session.scalar(
            text(
                "SELECT aap_assets_v1._video_input_plan_shape_valid(CAST(:plan AS jsonb))"
            ),
            {"plan": json.dumps(changed)},
        )
    for key, bad in (
        ("frame_count", 0),
        ("width", True),
        ("height", 9007199254740992),
        ("duration_ms", -1),
        ("video_stream", 1),
    ):
        changed = json.loads(json.dumps(plan))
        changed["input"]["video"][key] = bad
        assert not await db_session.scalar(
            text(
                "SELECT aap_assets_v1._video_input_plan_shape_valid(CAST(:plan AS jsonb))"
            ),
            {"plan": json.dumps(changed)},
        )
    for bad in (None, True, "overwrite"):
        selection = json.loads(VECTOR.read_text())["selection"]
        selection["execution_scope"] = bad
        assert not await db_session.scalar(
            text(
                "SELECT aap_assets_v1._video_input_selection_valid(CAST(:selection AS jsonb))"
            ),
            {"selection": json.dumps(selection)},
        )
