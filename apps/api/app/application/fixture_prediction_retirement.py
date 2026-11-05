"""Retire the exact managed Inference scopes owned by a sealed fixture run."""

from __future__ import annotations

from uuid import UUID, uuid5

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from yoao_inference_contract.routing import RoutingUnavailable
from yoao_inference_contract.scope_retirement import (
    ScopeRetirementView,
    make_scope_retirement_request,
)

from app.db.base import async_session
from app.services.ml_routing.authority import RoutingAuthority


class FixturePredictionRetirementBlocked(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def _inventory(run_id, project_ids, task_ids):
    try:
        async with async_session() as db:
            value = await db.scalar(
                text(
                    "SELECT aap_inference_routing_v1.fixture_prediction_scope_inventory("
                    "CAST(:run_id AS uuid), CAST(:project_ids AS uuid[]), CAST(:task_ids AS uuid[]))"
                ),
                {
                    "run_id": run_id,
                    "project_ids": list(project_ids),
                    "task_ids": list(task_ids),
                },
            )
    except SQLAlchemyError as exc:
        raise FixturePredictionRetirementBlocked("scope_inventory_unavailable") from exc
    if (
        type(value) is not dict
        or set(value) != {"schema", "scopes"}
        or value["schema"] != "aap-fixture-prediction-scopes/v1"
        or type(value["scopes"]) is not list
    ):
        raise FixturePredictionRetirementBlocked("scope_inventory_invalid")
    scopes = []
    seen = set()
    for item in value["scopes"]:
        try:
            if type(item) is not dict or set(item) != {
                "scope_id",
                "capability",
                "retirement_id",
            }:
                raise ValueError("invalid scope fields")
            scope_id = UUID(item["scope_id"])
            retirement_id = (
                UUID(item["retirement_id"])
                if item["retirement_id"] is not None
                else uuid5(scope_id, "aap-fixture-prediction-retirement/v1")
            )
            if (
                str(scope_id) != item["scope_id"]
                or scope_id in seen
                or type(item["capability"]) is not str
                or not item["capability"]
            ):
                raise ValueError("invalid scope identity")
            request = make_scope_retirement_request(
                retirement_id, scope_id, item["capability"]
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise FixturePredictionRetirementBlocked("scope_inventory_invalid") from exc
        seen.add(scope_id)
        scopes.append(request)
    return scopes


async def retire_fixture_prediction_work(
    *, run_id: UUID, project_ids: tuple[UUID, ...], task_ids: tuple[UUID, ...]
) -> None:
    requests = await _inventory(run_id, project_ids, task_ids)
    authority = RoutingAuthority()
    for request in requests:
        try:
            result = await authority.retire_scope(request)
        except RoutingUnavailable as exc:
            raise FixturePredictionRetirementBlocked(
                "scope_retirement_unavailable"
            ) from exc
        if (
            result.state != "committed"
            or not isinstance(result.result, ScopeRetirementView)
            or result.result.phase != "completed"
        ):
            raise FixturePredictionRetirementBlocked("scope_retirement_pending")
    try:
        async with async_session() as db:
            retired = await db.scalar(
                text(
                    "SELECT aap_inference_routing_v1.fixture_prediction_tests_retired("
                    "CAST(:run_id AS uuid), CAST(:project_ids AS uuid[]), CAST(:task_ids AS uuid[]))"
                ),
                {
                    "run_id": run_id,
                    "project_ids": list(project_ids),
                    "task_ids": list(task_ids),
                },
            )
    except SQLAlchemyError as exc:
        raise FixturePredictionRetirementBlocked("scope_inventory_unavailable") from exc
    if retired is not True:
        raise FixturePredictionRetirementBlocked("scope_retirement_pending")
