"""Controlled routing never selects another physical model instance."""

import uuid

import pytest
from fastapi import HTTPException

from aap_inference_contract.execution import ModelTaskResult
from app.services import trained_models
from app.services.ml_routing.contracts import RejectionReason, RouterMode
from app.services.ml_routing.router import MLBackendRouter
from app.services.trained_model_reference_contract import FreezeModelReferenceError
from tests.factory import create_project
from tests.test_ml_routing_router import _seed_pool_with_two_members


@pytest.mark.parametrize(
    "mode", [RouterMode.OFF, RouterMode.OBSERVE, RouterMode.ENFORCE]
)
async def test_exact_target_is_never_replaced_by_another_pool_member(
    db_session, super_admin, mode
):
    user, _ = super_admin
    project = await create_project(db_session, owner_id=user.id)
    pool, legacy, target = await _seed_pool_with_two_members(db_session, project.id)

    class SaturatedLedger:
        seen = []

        async def circuit_open_instances(self, _pool):
            return set()

        async def acquire(self, _pool, _generation, candidates, **_kwargs):
            self.seen = [candidate.instance_id for candidate in candidates]
            return None, RejectionReason.POOL_SATURATED

    ledger = SaturatedLedger()
    router = MLBackendRouter(db_session, ledger=ledger, mode=mode)
    selection = await router.acquire(
        pool.id,
        owner="test",
        operation="predict",
        project_id=project.id,
        required_instance_id=target.id,
    )
    assert selection.instance_id is None
    if mode == RouterMode.ENFORCE:
        assert ledger.seen == [target.id]
        assert selection.rejection == RejectionReason.POOL_SATURATED
    else:
        assert selection.rejection == RejectionReason.POOL_UNAVAILABLE
    assert legacy.id != target.id


@pytest.mark.parametrize("mode", [RouterMode.OFF, RouterMode.OBSERVE])
async def test_target_filter_does_not_create_a_fake_singleton_fingerprint_exemption(
    db_session, super_admin, mode
):
    user, _ = super_admin
    project = await create_project(db_session, owner_id=user.id)
    pool, legacy, _ = await _seed_pool_with_two_members(db_session, project.id)
    pool.capability_fingerprint = None
    await db_session.flush()
    router = MLBackendRouter(db_session, ledger=None, mode=mode)
    candidates = await router._build_candidates(pool, required_instance_id=legacy.id)
    assert len(candidates) == 1 and candidates[0].fingerprint_ok is False
    selection = await router.acquire(
        pool.id,
        owner="test",
        operation="predict",
        project_id=project.id,
        required_instance_id=legacy.id,
    )
    assert (
        selection.instance_id is None
        and selection.rejection == RejectionReason.POOL_UNAVAILABLE
    )


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
