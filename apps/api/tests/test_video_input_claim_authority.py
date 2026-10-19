"""A revoked requester must not block another video's global claim queue."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from aap_assets_video_inputs import execution as c
from tests.test_video_input_ledger_control import _call
from tests.test_video_input_ledger_execution import _seed_prepared_video_input
from tests.test_video_input_ledger_lifecycle import _frame_request, _ready_plan

pytestmark = pytest.mark.asyncio


async def test_disabled_original_requester_terminalizes_its_expired_lease_and_allows_next_claim(
    db_session,
):
    blocked = await _seed_prepared_video_input(db_session)
    permitted = await _seed_prepared_video_input(db_session)
    actor, _, _, _, _, input_id, _ = blocked
    for case in (blocked, permitted):
        await db_session.execute(
            text(
                "UPDATE public.asset_accepted_video_inputs SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
                "WHERE input_id=:input"
            ),
            {"input": case[5]},
        )
        await db_session.execute(
            text(
                "UPDATE public.asset_accepted_video_input_attempts SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
                "WHERE input_id=:input"
            ),
            {"input": case[5]},
        )
    await db_session.execute(
        text("UPDATE public.users SET is_active=false WHERE id=:actor"),
        {"actor": actor},
    )
    result = await _call(
        db_session,
        "claim_accepted_video_input",
        c.ClaimVideoInputRequest(worker_id=uuid4()),
    )
    assert result.claim is not None and str(result.claim.input.input_id) == permitted[5]
    assert (
        await db_session.execute(
            text(
                "SELECT status,error_code,lease_token FROM public.asset_accepted_video_inputs WHERE input_id=:input"
            ),
            {"input": input_id},
        )
    ).one() == ("failed", "account_not_active", None)
    assert (
        await db_session.execute(
            text(
                "SELECT state,error_code FROM public.asset_accepted_video_members WHERE input_id=:input"
            ),
            {"input": input_id},
        )
    ).one() == ("failed", "account_not_active")
    assert (
        await db_session.scalar(
            text(
                "SELECT prepared_count FROM public.asset_accepted_video_manifests WHERE job_id=:job"
            ),
            {"job": blocked[2]},
        )
        == 1
    )


async def test_disabled_frame_requester_does_not_poison_the_global_frame_queue(
    db_session,
):
    actor_a, _, plan_a = await _ready_plan(db_session)
    frame_a = await _call(
        db_session, "require_accepted_video_frame", _frame_request(actor_a, plan_a, 1)
    )
    actor_b, _, plan_b = await _ready_plan(db_session)
    frame_b = await _call(
        db_session, "require_accepted_video_frame", _frame_request(actor_b, plan_b, 1)
    )
    await db_session.execute(
        text("UPDATE public.users SET is_active=false WHERE id=:actor"),
        {"actor": actor_a},
    )
    result = await _call(
        db_session,
        "claim_accepted_video_frame",
        c.ClaimVideoFrameRequest(worker_id=uuid4()),
    )
    assert result.claim is not None and result.claim.frame.frame_id == frame_b.frame_id
    assert (
        await db_session.execute(
            text(
                "SELECT status,error_code,lease_token FROM public.asset_accepted_video_frames WHERE frame_id=:frame"
            ),
            {"frame": frame_a.frame_id},
        )
    ).one() == ("failed", "account_not_active", None)
