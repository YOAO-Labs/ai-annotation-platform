"""Controlled routing never selects another physical model instance."""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services.ml_routing.client import RoutedMLBackendClient
from app.services.ml_routing.contracts import RejectionReason, RouterMode, RoutingError
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


@pytest.mark.parametrize(
    "mode", [RouterMode.OFF, RouterMode.OBSERVE, RouterMode.ENFORCE]
)
async def test_controlled_route_cannot_enter_the_unpooled_legacy_fallback(
    db_session, super_admin, mode, monkeypatch
):
    user, _ = super_admin
    project = await create_project(db_session, owner_id=user.id)
    client = RoutedMLBackendClient(
        db_session,
        uuid.uuid4(),
        project_id=project.id,
        owner="test",
        operation="predict",
        mode=mode,
    )
    monkeypatch.setattr(client, "_pool", AsyncMock(return_value=None))
    with pytest.raises(RoutingError):
        async with client._routed_transport(
            required_instance_id=client.requested_registry_id
        ):
            pytest.fail("Unpooled controlled request must never receive a transport")


@pytest.mark.parametrize("returned_ids", [["other"], [], ["wanted", "wanted"]])
async def test_controlled_response_must_match_requested_task_identities(
    monkeypatch, returned_ids
):
    from app.services import trained_models

    target = uuid.uuid4()
    ref = {
        "model_version_id": str(uuid.uuid4()),
        "sha256": "a" * 64,
        "classes_digest": "b" * 64,
    }
    client = RoutedMLBackendClient(
        None, target, project_id=uuid.uuid4(), owner="test", operation="predict"
    )
    reference_port = SimpleNamespace(validate=AsyncMock(return_value=target))
    monkeypatch.setattr(
        trained_models,
        "bind_frozen_model_reference",
        lambda _db: reference_port,
    )

    @asynccontextmanager
    async def transport(**kwargs):
        assert kwargs == {"required_instance_id": target}
        yield SimpleNamespace(
            predict=AsyncMock(
                return_value=[SimpleNamespace(task_id=value) for value in returned_ids]
            )
        )

    monkeypatch.setattr(client, "_routed_transport", transport)
    with pytest.raises(ValueError, match="task identities"):
        await client.predict(
            [{"id": "wanted"}],
            context={
                "model_id": "trained:" + ref["model_version_id"],
                "frozen_model_ref": ref,
            },
        )


async def test_frozen_reference_forbidden_error_keeps_http_projection(monkeypatch):
    from app.services import trained_models

    target = uuid.uuid4()
    client = RoutedMLBackendClient(
        None, target, project_id=uuid.uuid4(), owner="test", operation="predict"
    )
    error = FreezeModelReferenceError(
        "project_access_changed",
        "permission changed",
        status_code=403,
        detail={"reason": "permission_changed"},
    )
    reference_port = SimpleNamespace(validate=AsyncMock(side_effect=error))
    monkeypatch.setattr(
        trained_models,
        "bind_frozen_model_reference",
        lambda _db: reference_port,
    )

    with pytest.raises(HTTPException) as failure:
        await client.predict(
            [{"id": "wanted"}],
            context={
                "model_id": "trained:version",
                "frozen_model_ref": {"model_version_id": "version"},
            },
        )

    assert failure.value.status_code == 403
    assert failure.value.detail == {"reason": "permission_changed"}
