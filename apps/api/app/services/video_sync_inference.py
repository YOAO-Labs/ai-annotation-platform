"""Request-owned, versioned whole-video delivery for synchronous predict-test.

The original FD and its admission remain owned until inference has returned.
Final caller locks protect metadata only, after actual source settlement.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.services import prediction_inputs, prediction_originals as common
from app.services import video_prediction_originals as originals
from app.services.content_snapshot import ContentError
from app.services.dataset_content import content_http_error
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.project_access import (
    get_workspace_project,
    resolve_project_access,
)
from app.services.project_write_guard import (
    _busy_lock_error,
    lock_actor_project_authority,
    lock_actor_scope,
)
from app.services.reference_bindings import task_binding
from app.services.reference_keys import is_reference
from app.services.storage_access import mark_unsettled
from app.services.storage_location import api_node_id


def _changed():
    raise ContentError("prediction_video_backend_changed")


def _dispatch_fact(dispatch):
    """Opaque, credential-free facts for the actual selected attempt."""
    from aap_inference_contract.execution import ModelExecutionDispatch

    if (
        not isinstance(dispatch, ModelExecutionDispatch)
        or type(dispatch.endpoint_digest) is not str
        or len(dispatch.endpoint_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in dispatch.endpoint_digest
        )
    ):
        _changed()
    return {
        "backend_id": str(dispatch.instance_id),
        "pool_id": str(dispatch.pool_id) if dispatch.pool_id else None,
        "route_scope_id": str(dispatch.route_scope_id),
        "attempt_id": str(dispatch.attempt_id),
        "endpoint_digest": dispatch.endpoint_digest,
    }


async def _backend(db, identifier, *, active=True):
    """Selected member facts only; credentials stay in the Inference owner."""
    row = (
        await db.execute(
            select(
                MLBackendRegistry.id,
                MLBackendRegistry.auth_revision,
                MLBackendPoolMember.pool_id,
                MLBackendPoolMember.traffic_state,
            )
            .join(
                MLBackendPoolMember,
                MLBackendPoolMember.registry_id == MLBackendRegistry.id,
            )
            .where(MLBackendRegistry.id == identifier)
            .with_for_update(read=True, nowait=True)
        )
    ).one_or_none()
    if row is None or (active and row.traffic_state != "active"):
        _changed()
    return {
        "backend_id": str(row.id),
        "pool_id": str(row.pool_id),
        "auth_revision": row.auth_revision,
    }


async def _route(db, project_id, backend_id):
    # The requested registry names its logical pool; it need not be the instance
    # selected for traffic. Only the actual dispatch must be an active member.
    backend = await _backend(db, backend_id, active=False)
    row = (
        await db.execute(
            select(
                MLBackendServicePool.id,
                MLBackendServicePool.enabled,
                MLBackendServicePool.routing_generation,
                ProjectMLBackendPool.enabled.label("project_enabled"),
            )
            .join(
                ProjectMLBackendPool,
                ProjectMLBackendPool.pool_id == MLBackendServicePool.id,
            )
            .where(
                MLBackendServicePool.id == uuid.UUID(backend["pool_id"]),
                ProjectMLBackendPool.project_id == project_id,
            )
            .with_for_update(read=True, nowait=True)
        )
    ).one_or_none()
    if row is None or not row.enabled or not row.project_enabled:
        _changed()
    return {**backend, "routing_generation": row.routing_generation}


class VideoPredictTestOperation:
    """One original request, one actual routed dispatch, no job authorization."""

    def __init__(self, db, task, actor, request, *, backend_id):
        self.db, self.task_id, self.project_id = db, task.id, task.project_id
        self.actor_id, self.request = actor.id, request
        self.backend_id = uuid.UUID(str(backend_id))
        raw_key = getattr(request.state, "api_key_id", None)
        self.key_id = uuid.UUID(str(raw_key)) if raw_key is not None else None
        self.request_id = uuid.uuid4()
        self.credentials = self.binding = self.route = None
        self.inputs = self.source = self.url = None
        self._probe = self._transport = self._selected = None
        self._ready = self._closed = self._unsettled = False
        self._source_finished = self._dispatched = self._finalized = False
        self._failure = None
        self._close_lock = asyncio.Lock()

    @property
    def probe(self):
        return deepcopy(self._probe)

    def _remember(self, failure):
        if failure is None:
            return
        if self._failure is None:
            self._failure = failure
        else:
            common._uncertain(self._failure, failure)
            common._uncertain(failure, self._failure)
        self._unsettled |= bool(getattr(failure, "storage_operation_unsettled", False))

    def _check_open(self):
        if (
            not self._ready
            or self._closed
            or self._unsettled
            or self.inputs is None
            or self.inputs._closed
            or self.inputs._unsettled
            or self.source is None
            or self.source.unsettled
            or (self.source.closed and not self._source_finished)
            or self.inputs.record.expires_at <= datetime.now(timezone.utc)
        ):
            raise ContentError("prediction_input_expired")
        common.check_clock(self.credentials)

    async def _parents(self, db):
        await lock_actor_project_authority(db, self.actor_id, self.project_id)
        await lock_actor_scope(db, self.actor_id, self.project_id)

    async def _business(self, db, task, actor):
        from app.services import task_access

        project = await get_workspace_project(
            db,
            actor_id=self.actor_id,
            project_id=self.project_id,
            populate_existing=True,
        )
        if (
            actor is None
            or not actor.is_active
            or project is None
            or project.data_type != "video"
            or task.project_id != self.project_id
            or task.file_type != "video"
            or is_reference(task.file_path)
        ):
            raise ContentError("prediction_authority_revoked", 403)
        access = await resolve_project_access(db, actor_id=actor.id, project=project)
        if not access.is_manager:
            raise ContentError("prediction_authority_revoked", 403)
        await task_access.assert_task_visible(db, task, actor.id, access=access)

    async def _guard(self, db):
        await self._parents(db)
        actor = await common.reauthorize(
            db, self.credentials, self.actor_id, self.key_id
        )
        task = await common.locked_task(db, self.task_id)
        await self._business(db, task, actor)
        binding = {
            **task_binding(task),
            "original": await originals.managed_binding(db, task, lock=True),
        }
        if binding != self.binding:
            raise ContentError("content_binding_changed")
        if await _route(db, self.project_id, self.backend_id) != self.route:
            _changed()
        if self._selected is not None:
            selected = await _backend(db, uuid.UUID(self._selected["backend_id"]))
            if selected != self._selected:
                _changed()
        return task

    async def _fresh(self):
        async with originals._session(self.db) as db:
            return await self._guard(db)

    async def _freeze(self):
        if self.binding is not None or self._closed:
            raise ContentError("prediction_input_already_open")
        if self.db.sync_session.in_nested_transaction():
            raise ContentError("prediction_scope_changed")
        await self._parents(self.db)
        actor = await IdentityAccountLockData(self.db).read_actor_status(self.actor_id)
        if actor is None or not actor.is_active:
            raise ContentError("prediction_authority_revoked", 403)
        self.credentials = await common.capture_credentials(
            self.db, actor, self.request
        )
        task = await common.locked_task(self.db, self.task_id)
        await self._business(self.db, task, actor)
        self.binding = common.bounded_proof(
            {
                **task_binding(task),
                "original": await originals.managed_binding(self.db, task, lock=True),
            }
        )
        self.route = await _route(self.db, self.project_id, self.backend_id)
        await self.db.commit()

    async def open(self, factory):
        try:
            await self._freeze()
            self.inputs = await common.settled(
                prediction_inputs.PredictionInputs.create(
                    factory,
                    owner_id=self.request_id,
                    owner_kind="video_predict_test",
                    task_id=self.task_id,
                    guard=self._guard,
                    source_ref=self.binding,
                ),
                late=lambda value: value.close(),
            )
            self.source = await common.settled(
                originals.open_snapshot(
                    self.db,
                    self.task_id,
                    self.binding["original"],
                    guard=self._guard,
                    purpose=prediction_inputs.purpose(self.inputs.record),
                    node_id=api_node_id(),
                ),
                late=lambda value: value.aclose(),
            )
            self._probe = self.source.probe
            self.url = await self.inputs.upload_video(
                self.source.stage, probe=self._probe
            )
            self._ready = True
            await self.validate()
            return self
        except BaseException as failure:
            self._remember(failure)
            raise

    async def validate(self):
        self._check_open()
        await self.inputs.check_live()
        return await common.settled(self._fresh())

    async def before_dispatch(self, dispatch):
        """Exact owner boundary: routing admitted, endpoint captured, not sent."""
        self._check_open()
        if self._dispatched:
            _changed()
        fact = _dispatch_fact(dispatch)
        async with originals._session(self.db) as db:
            await self._guard(db)
            selected = await _backend(db, uuid.UUID(fact["backend_id"]))
            if (
                selected["pool_id"] != self.route["pool_id"]
                or selected["pool_id"] != fact["pool_id"]
                or selected["backend_id"] != fact["backend_id"]
            ):
                _changed()
            self._selected, self._transport = selected, None
        await self.validate()
        # Routing used the caller session. End that transaction before /predict.
        await self.db.commit()
        self._check_open()
        self._dispatched = True

    async def final(self):
        if not self._dispatched or self._finalized:
            raise ContentError("prediction_video_dispatch_required")
        await self.validate()
        await common.settled(self.db.rollback())
        try:
            await self.source.aclose()
        except BaseException as failure:
            self._remember(failure)
            raise
        if not self.source.settled or self.source.unsettled:
            failure = mark_unsettled(
                ContentError("prediction_original_close_unknown", 503)
            )
            self._remember(failure)
            raise failure
        self._source_finished = True
        self._check_open()
        if self.db.sync_session.in_nested_transaction():
            raise ContentError("prediction_scope_changed")
        task = await self._guard(self.db)
        await prediction_inputs.check_metadata(self.db, self.inputs)
        self._check_open()
        self._finalized = True
        return task

    async def close(self, failure=None):
        self._remember(failure)
        await common.settled(self._close())

    async def _close(self):
        async with self._close_lock:
            if self._closed:
                if self._unsettled:
                    raise mark_unsettled(
                        ContentError("prediction_original_close_unknown", 503)
                    )
                return
            self._closed = True
            closing = None
            try:
                if self.inputs is not None:
                    await common.settled(self.inputs.close())
            except BaseException as failure:
                closing = failure
                self._remember(failure)
            finally:
                if self.source is not None:
                    try:
                        await common.settled(
                            self.source.aclose(
                                None if self.source.settled else self._failure
                            )
                        )
                    except BaseException as failure:
                        if closing is None:
                            closing = failure
                        self._remember(failure)
            if self._unsettled:
                raise mark_unsettled(
                    closing or ContentError("prediction_original_close_unknown", 503)
                )
            if closing is not None:
                raise closing


@asynccontextmanager
async def video_predict_test_operation(
    db, task, actor, request, factory, *, backend_id
):
    """Deliver a canonical video without changing predict-test model/protocol rules."""
    owner = VideoPredictTestOperation(db, task, actor, request, backend_id=backend_id)
    failure = None
    try:
        await owner.open(factory)
        yield owner
        if not owner._finalized:
            raise ContentError("prediction_video_dispatch_required")
    except BaseException as exc:
        failure = exc
        if isinstance(exc, DBAPIError) and _busy_lock_error(exc):
            raise HTTPException(409, detail={"code": "video_inference_busy"}) from None
        if isinstance(exc, Exception):
            raise content_http_error(exc) from None
        raise
    finally:
        try:
            await common.settled(db.rollback())
        finally:
            await owner.close(failure)
