"""Controlled routing never selects another physical model instance."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from aap_inference_contract.execution import ModelTaskResult
from app.db.models.ml_backend_pool import MLBackendPoolMember
from app.services import trained_models
from app.services.ml_routing.contracts import RejectionReason, RouterMode
from app.services.ml_routing.router import MLBackendRouter
from app.services.trained_model_reference_contract import FreezeModelReferenceError
from tests.factory import create_project

# Fixture dependencies resolve per module: routing_admission requires committed_routes.
from tests.test_ml_routing_router import (  # noqa: F401
    committed_routes,
    routing_admission as routing_admission,
)


@pytest.mark.parametrize(
    "mode", [RouterMode.OFF, RouterMode.OBSERVE, RouterMode.ENFORCE]
)
async def test_exact_target_is_never_replaced_by_another_pool_member(
    routing_admission, mode
):
    db_session, project, pool, legacy, target = routing_admission
    assert legacy.id != target.id
    # A real in-flight durable scope on the exact target fills its declared
    # backend concurrency; the sibling member stays entirely unused.
    target.extra_params = {"max_concurrency": 1}
    await db_session.commit()
    # Configuration changes invalidate observed health. Seed a fresh observation
    # only after that configuration transaction has committed.
    await db_session.refresh(target)
    target.state = "connected"
    target.health_meta = legacy.health_meta
    target.last_checked_at = datetime.now(UTC)
    await db_session.commit()

    enforcing = MLBackendRouter(db_session, ledger=None, mode=RouterMode.ENFORCE)
    in_flight = await enforcing.acquire(
        pool.id,
        owner="test:in-flight",
        operation="predict",
        project_id=project.id,
        required_instance_id=target.id,
    )
    assert in_flight.rejection is None
    assert in_flight.instance_id == target.id
    assert in_flight.lease.scope.pool_id == pool.id
    assert in_flight.lease.scope.registry_id == target.id
    assert in_flight.lease.scope.mode == RouterMode.ENFORCE.value
    assert in_flight.lease.scope.state == "reserved"
    try:
        router = MLBackendRouter(db_session, ledger=None, mode=mode)
        selection = await router.acquire(
            pool.id,
            owner="test",
            operation="predict",
            project_id=project.id,
            required_instance_id=target.id,
        )
        assert selection.lease is None
        assert selection.instance_id is None
        if mode == RouterMode.ENFORCE:
            # Saturation is physical: the exact target is rejected, never swapped
            # for the free sibling.
            assert selection.rejection == RejectionReason.POOL_SATURATED
        else:
            # off/observe admission only ever names the legacy member; an exact
            # target elsewhere must reject rather than dispatch to that legacy.
            assert selection.rejection == RejectionReason.POOL_UNAVAILABLE
        # The other physical member is independently free: rejection above is
        # exact-instance admission, not whole-pool exhaustion.
        sibling = await enforcing.acquire(
            pool.id,
            owner="test:sibling",
            operation="predict",
            project_id=project.id,
            required_instance_id=legacy.id,
        )
        assert sibling.rejection is None
        assert sibling.instance_id == legacy.id
        assert await enforcing.cancel(sibling.lease)
    finally:
        assert await enforcing.cancel(in_flight.lease)


@pytest.mark.parametrize("mode", [RouterMode.OFF, RouterMode.OBSERVE])
async def test_target_filter_does_not_create_a_fake_singleton_fingerprint_exemption(
    routing_admission, mode
):
    db_session, project, pool, legacy, sibling = routing_admission
    assert legacy.id != sibling.id
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MLBackendPoolMember)
            .where(
                MLBackendPoolMember.pool_id == pool.id,
                MLBackendPoolMember.traffic_state == "active",
            )
        )
        == 2
    )
    router = MLBackendRouter(db_session, ledger=None, mode=mode)
    matched = await router.acquire(
        pool.id,
        owner="test",
        operation="predict",
        project_id=project.id,
        required_instance_id=legacy.id,
    )
    assert matched.rejection is None
    assert matched.instance_id == legacy.id
    assert matched.lease.scope.registry_id == legacy.id
    assert await router.cancel(matched.lease)

    # Two active members with no pool fingerprint must stay closed even when the
    # target filter narrows the candidate view to one (no fabricated singleton).
    pool.capability_fingerprint = None
    await db_session.commit()
    selection = await router.acquire(
        pool.id,
        owner="test",
        operation="predict",
        project_id=project.id,
        required_instance_id=legacy.id,
    )
    assert selection.lease is None
    assert selection.instance_id is None
    assert selection.rejection == RejectionReason.POOL_UNAVAILABLE


async def test_controlled_route_rejects_unpooled_registry(db_session, super_admin):
    user, _ = super_admin
    project = await create_project(db_session, owner_id=user.id)
    with pytest.raises(HTTPException) as error:
        await trained_models.assert_routable_backend(
            db_session, project.id, uuid.uuid4()
        )
    assert error.value.status_code == 409


def test_controlled_result_mapping_preserves_task_identity_and_immutability():
    ref = {
        "model_version_id": str(uuid.uuid4()),
        "sha256": "a" * 64,
        "classes_digest": "b" * 64,
        "classes": ["car"],
        "bindings": ["vehicle"],
    }
    result = ModelTaskResult(
        task_id="wanted",
        result=(
            {
                "type": "rectanglelabels",
                "native_class_index": 0,
                "score": 0.8,
                "value": {
                    "x": 1,
                    "y": 2,
                    "width": 3,
                    "height": 4,
                    "rectanglelabels": ["car"],
                },
            },
        ),
        meta={
            "trained_model": {
                key: ref[key]
                for key in ("model_version_id", "sha256", "classes_digest")
            }
        },
    )
    mapped = trained_models.validate_and_map_results((result,), ref)[0]

    assert mapped.task_id == "wanted"
    assert result.result[0]["value"]["rectanglelabels"] == ("car",)
    assert mapped.result[0]["value"]["rectanglelabels"] == ("vehicle",)
    assert mapped.model_version == f"trained:{ref['model_version_id']}"


def test_frozen_reference_forbidden_error_keeps_http_projection():
    error = FreezeModelReferenceError(
        "project_access_changed",
        "permission changed",
        status_code=403,
        detail={"reason": "permission_changed"},
    )

    with pytest.raises(HTTPException) as failure:
        trained_models.raise_frozen_model_reference_error(error)

    assert failure.value.status_code == 403
    assert failure.value.detail == {"reason": "permission_changed"}
