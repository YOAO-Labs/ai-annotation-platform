"""Retire the actual prior scope before making replacement work admissible."""

from __future__ import annotations

from uuid import uuid5

from aap_inference_contract.routing import RoutingUnavailable
from aap_inference_contract.scope_retirement import (
    ScopeRetirementView,
    make_scope_retirement_request,
)
from aap_preannotation_results.video_execution_journal import ReadRequest

from app.services.ml_routing.authority import RoutingAuthority
from .execution import TrackerExecutionDeferred, TrackerExecutionLost, execution_proof
from .journal import TrackerWindowJournal


async def retire_tracker_scope(db, job_id, *, authority=None):
    authority = authority or RoutingAuthority(db.bind)
    try:
        status = await authority.video_execution_status(
            execution_proof(job_id).model_dump(mode="json")
        )
        scope = status["scope"]
        if scope is None:
            # Native recovery_ready/retire still requires an exact no-scope
            # rejection if an acquire intent was prepared.
            return
        request = make_scope_retirement_request(
            uuid5(scope.id, "aap-video-execution-retirement/v1"),
            scope.id,
            scope.capability,
        )
        result = await authority.retire_scope(request)
        if (
            result.state != "committed"
            or not isinstance(result.result, ScopeRetirementView)
            or result.result.phase != "completed"
        ):
            raise TrackerExecutionDeferred("scope_retirement_pending")
    except RoutingUnavailable:
        raise TrackerExecutionDeferred("scope_retirement_unavailable") from None


async def recover_tracker_execution(db, job_id):
    await retire_tracker_scope(db, job_id)
    journal = TrackerWindowJournal(db, job_id)
    try:
        await journal.committed("recovery_ready", journal.request(ReadRequest))
    except TrackerExecutionLost:
        # A terminal/deleted Job can win while physical retirement is in flight.
        # The retire RPC still fences this exact proof before releasing any pin.
        await journal.retire()
        return False
    return True


async def retire_terminal_tracker_execution(db, job_id):
    """Keep terminal cleanup redeliverable even after this proof is lost."""
    try:
        await retire_tracker_scope(db, job_id)
        await TrackerWindowJournal(db, job_id).retire()
    except TrackerExecutionLost:
        raise TrackerExecutionDeferred("terminal_retirement_pending") from None
