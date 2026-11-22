"""仅供隔离 E2E / 测试数据库使用的 Seed router。

E2E（Playwright）通过 `POST /api/v1/__test/seed/owned` 创建私有命名空间的
工作台 fixture，通过 `POST /api/v1/__test/seed/login` 跳过 UI 登录直接拿 JWT。

安全：
  - 仅在非 production 且显式设置 `E2E_SEED_ENABLED=true` 时挂载
  - router 级依赖会再次核对环境、开关与当前数据库名
  - 数据库名转为小写后必须严格以 `_e2e` 或 `_test` 结尾
  - 不调 AuditService，避免污染审计测试

不暴露给 OpenAPI 公开 schema（include_in_schema=False）。
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import os
import re
import secrets
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from yoao_labeling.e2e_fixture_contract import TaskFixturePatch
from yoao_workspace.e2e_fixture_contract import (
    WorkspaceE2EFixtureUnavailable,
    decode_fixture_count,
)
from yoao_audit.maintenance_postgres import AuditMaintenancePostgresData

from app.config import settings
from app.core.security import create_access_token
from app.deps import get_db
from app.schemas.user import UserOut
from app.api.v1._test_seed_filters import FilteringSeedManifest, build_filtering_seed
from app.services.test_seed_access import assert_isolated_test_seed_database
from app.services.project_access import get_workspace_project
from app.services.seed_membership_data import SeedMembershipWrites
from app.services.labeling_core_read_relations import batch_row, get_core_row, task_row
from app.services.labeling_e2e_fixture_writes import LabelingE2EFixtureWrites
from app.services.workspace_e2e_fixtures import (
    create_fixture_project,
    guard_fixture_project_cleanup,
)
from app.services.workspace_seed_data import register_seed_project_projection


async def _require_e2e_seed_database(db: AsyncSession = Depends(get_db)) -> None:
    """纵深保护所有 Seed 端点，并复用端点将要使用的同一数据库会话。"""
    await assert_isolated_test_seed_database(db)


router = APIRouter(dependencies=[Depends(_require_e2e_seed_database)])
_SUITE_READER_WAIT_SECONDS = 3
_JOBS_FIXTURE_PROJECT_DELETE_SQL = (
    "SELECT yoao_jobs_v1.delete_fixture_project_jobs(CAST(:pids AS uuid[]))"
)
_LABELING_FIXTURE_DELETE_SQL = (
    "SELECT yoao_labeling_v1.delete_fixture_project_rows(CAST(:pids AS uuid[]))"
)
_AUDIT_FIXTURE_CLEANUP = "audit_fixture_cleanup"


_OWNED_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]{4,12}$")


@dataclass(frozen=True)
class _OwnedFixtureScope:
    """Exact identifiers of one owned E2E fixture namespace."""

    namespace: str
    run_id: UUID | None = None

    @property
    def admin_email(self) -> str:
        return f"admin-{self.namespace}@e2e.test"

    @property
    def annotator_email(self) -> str:
        return f"anno-{self.namespace}@e2e.test"

    @property
    def reviewer_email(self) -> str:
        return f"rev-{self.namespace}@e2e.test"

    @property
    def user_emails(self) -> list[str]:
        return [self.admin_email, self.annotator_email, self.reviewer_email]

    @property
    def project_name(self) -> str:
        return f"E2E Owned {self.namespace}"

    @property
    def batch_display_id(self) -> str:
        return f"B-E2E-{self.namespace}"

    @property
    def dataset_display_id(self) -> str:
        return f"DS-E2E-{self.namespace}"

    @property
    def registry_name(self) -> str:
        return f"E2E Test Provider Owned {self.namespace}"

    @property
    def image_key_prefix(self) -> str:
        return f"e2e/owned/{self.namespace}/image/"

    @property
    def task_display_prefix(self) -> str:
        return f"T-E2E-{self.namespace}"

    @property
    def lidar_project_name(self) -> str:
        return f"E2E Lidar {self.namespace}"

    @property
    def lidar_dataset_display_id(self) -> str:
        return f"DS-LDR-{self.namespace}"

    @property
    def lidar_storage_prefix(self) -> str:
        return f"e2e/lidar/{self.namespace}/"

    @property
    def lidar_admin_email(self) -> str:
        return self.admin_email

    @property
    def lidar_annotator_email(self) -> str:
        return self.annotator_email

    _PR_KEYS = ("a", "b", "c", "d")

    @property
    def pr_user_emails(self) -> list[str]:
        locals_ = (
            "owner-a",
            "owner-b",
            "owner-c",
            "employee",
            "peer",
            "spare",
            "viewer",
            "viewer-unassigned",
            "solo",
        )
        return [f"{local}-{self.namespace}@e2e.test" for local in locals_]

    @property
    def pr_project_names(self) -> list[str]:
        return [
            f"E2E Project Roles {key.upper()} {self.namespace}" for key in self._PR_KEYS
        ]

    @property
    def pr_project_display_ids(self) -> dict[str, str]:
        return {key: f"P-PR-{key.upper()}-{self.namespace}" for key in self._PR_KEYS}

    @property
    def pr_dataset_display_ids(self) -> dict[str, str]:
        return {key: f"DS-PR-{key.upper()}-{self.namespace}" for key in self._PR_KEYS}

    @property
    def pr_task_display_prefix(self) -> dict[str, str]:
        return {key: f"T-PR-{key.upper()}-{self.namespace}-" for key in self._PR_KEYS}

    @property
    def pr_storage_prefix(self) -> str:
        return f"e2e/owned/{self.namespace}/project-roles/"

    @property
    def filter_user_emails(self) -> list[str]:
        return [
            f"filter-active-{self.namespace}@e2e.test",
            f"filter-inactive-{self.namespace}@e2e.test",
        ]

    @property
    def filter_project_display_ids(self) -> list[str]:
        ns = self.namespace
        return [
            f"P-FI-I-{ns}",
            f"P-FI-V-{ns}",
            f"P-FI-P-{ns}",
            f"P-FI-L-{ns}",
            f"P-FI-OA-{ns}",
            f"P-FI-OB-{ns}",
        ]

    @property
    def filter_dataset_display_ids(self) -> list[str]:
        ns = self.namespace
        return [
            f"DS-FI-I-{ns}",
            f"DS-FI-V-{ns}",
            f"DS-FI-P-{ns}",
            f"DS-FI-L-{ns}",
            f"DS-OA-{ns}",
            f"DS-OB-{ns}",
        ]

    @property
    def filter_template_display_ids(self) -> list[str]:
        ns = self.namespace
        return [f"TPLFI-A-{ns}", f"TPLFI-B-{ns}"]

    @property
    def filter_storage_prefix(self) -> str:
        return f"e2e/owned/{self.namespace}/filtering/"

    @property
    def takeover_email(self) -> str:
        """Namespace-scoped actor created through the real invite/register path."""
        return f"takeover-{self.namespace}@e2e.test"

    @property
    def all_user_emails(self) -> list[str]:
        return [
            *self.user_emails,
            self.takeover_email,
            *self.pr_user_emails,
            *self.filter_user_emails,
        ]

    @property
    def all_project_names(self) -> list[str]:
        return [self.project_name, self.lidar_project_name, *self.pr_project_names]

    @property
    def all_project_display_ids(self) -> list[str]:
        return [*self.pr_project_display_ids.values(), *self.filter_project_display_ids]

    @property
    def all_dataset_display_ids(self) -> list[str]:
        return [
            self.dataset_display_id,
            self.lidar_dataset_display_id,
            *self.pr_dataset_display_ids.values(),
            *self.filter_dataset_display_ids,
        ]

    @property
    def all_storage_prefixes(self) -> list[str]:
        return [
            self.image_key_prefix,
            self.lidar_storage_prefix,
            self.pr_storage_prefix,
            self.filter_storage_prefix,
        ]


def _parse_owned_namespace(namespace: str) -> _OwnedFixtureScope:
    if not _OWNED_NAMESPACE_PATTERN.fullmatch(namespace or ""):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "e2e_owned_namespace_invalid",
                "reason": "namespace must match ^[a-z0-9]{4,12}$",
            },
        )
    return _OwnedFixtureScope(namespace=namespace)


def _is_transaction_abort(exc: BaseException) -> bool:
    """Recognise a deadlock/serialization failure for the cleanup fail-fast policy.

    Observed in the P9 owned-cleanup failure: a DELETE that hit a PostgreSQL
    deadlock was rolled back, the following cleanup DELETEs were also skipped, and
    the request ended as a misleading ``e2e_seed_cleanup_incomplete`` residual
    instead of the real cause. The deliberate policy here is therefore: never
    absorb a recognised deadlock/serialization error (``40P01``/``40001``) —
    re-raise it so the original cause stays visible. ``25P02`` (transaction
    already aborted) is treated only as a downstream symptom of another failure,
    not as an independent signal.
    """
    for candidate in (exc, getattr(exc, "orig", None), getattr(exc, "__cause__", None)):
        if candidate is None:
            continue
        state = getattr(candidate, "sqlstate", None) or getattr(
            candidate, "pgcode", None
        )
        if state in {"40P01", "40001"}:
            return True
        if type(candidate).__name__ in {"DeadlockDetectedError", "SerializationError"}:
            return True
    return False


async def _try_fixture_delete(db, operation: str, params: dict | None = None) -> None:
    import logging

    log = logging.getLogger("anno-api.seed_cleanup")
    jobs_fixture_delete = operation in (
        _JOBS_FIXTURE_PROJECT_DELETE_SQL,
        "SELECT yoao_jobs_v1.delete_fixture_media_backfills(CAST(:run AS uuid),CAST(:dataset AS uuid))",
    )
    labeling_fixture_delete = operation == _LABELING_FIXTURE_DELETE_SQL
    audit_fixture_delete = operation == _AUDIT_FIXTURE_CLEANUP
    identity_fixture_delete = operation.startswith("SELECT yoao_identity_fixture_v1.")
    owner_fixture_delete = (
        jobs_fixture_delete or labeling_fixture_delete or audit_fixture_delete
    )
    try:
        async with db.begin_nested():
            if audit_fixture_delete:
                deleted_rows = await AuditMaintenancePostgresData(
                    db
                ).cleanup_fixture_events(params["run_id"])
                decode_fixture_count(deleted_rows)
            else:
                result = await db.execute(text(operation), params or {})
            if not audit_fixture_delete and (
                operation.startswith("SELECT yoao_workspace_fixture_v1.")
                or owner_fixture_delete
            ):
                decode_fixture_count(result.scalar_one())
    except WorkspaceE2EFixtureUnavailable:
        raise HTTPException(
            status_code=503, detail="项目测试清理服务暂不可用"
        ) from None
    except Exception as exc:
        if identity_fixture_delete:
            original = getattr(exc, "orig", exc)
            state = getattr(original, "sqlstate", None) or getattr(
                original, "pgcode", None
            )
            if state == "23503":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "e2e_seed_identity_referenced"},
                ) from exc
            raise
        if (
            operation.startswith("SELECT yoao_workspace_fixture_v1.")
            or owner_fixture_delete
        ):
            original = getattr(exc, "orig", exc)
            state = getattr(original, "sqlstate", None) or getattr(
                original, "pgcode", None
            )
            if state == "23514" and "video_execution_not_retired" in str(original):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "video_execution_not_retired",
                        "message": "存在尚未退役的视频执行，请先取消并完成退役后重试",
                    },
                ) from exc
            if state == "23514" and "fixture_job_execution_unsettled" in str(original):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "fixture_job_execution_unsettled"},
                ) from exc
            if jobs_fixture_delete and state == "23514":
                for code in (
                    "fixture_dataset_scope_changed",
                    "fixture_media_backfill_unsettled",
                ):
                    if code in str(original):
                        raise HTTPException(
                            status_code=409, detail={"code": code}
                        ) from exc
            if state == "55P03":
                raise HTTPException(
                    status_code=409, detail={"reason": "project_busy"}
                ) from exc
        if _is_transaction_abort(exc):
            log.warning("seed_cleanup abort · %s · %s", operation, exc)
            raise
        if owner_fixture_delete:
            raise HTTPException(
                status_code=503, detail="项目测试清理服务暂不可用"
            ) from exc
        raise


async def _resolve_fixture_scope(db, owned):
    from app.api.v1._test_seed_attachments import FixtureScope

    if owned is not None and owned.run_id is not None:

        async def resources(table: str, kind: str) -> tuple:
            return tuple(
                await db.scalars(
                    text(
                        f"SELECT item.id FROM {table} AS item "
                        "JOIN yoao_fixture_v1.fixture_run_resources AS resource "
                        "ON resource.resource_id=item.id "
                        "WHERE resource.run_id=:run AND resource.kind=:kind "
                        "AND resource.state <> 'retired' ORDER BY item.id"
                    ),
                    {"run": owned.run_id, "kind": kind},
                )
            )

        projects = await resources("public.projects", "project")
        users = await resources("public.users", "user")
        datasets = await resources("public.datasets", "dataset")
        tasks = tuple(
            await db.scalars(
                text(
                    "SELECT id FROM public.tasks WHERE project_id=ANY(:ids) ORDER BY id"
                ),
                {"ids": list(projects)},
            )
        )
        annotations = tuple(
            await db.scalars(
                text(
                    "SELECT id FROM public.annotations WHERE project_id=ANY(:ids) ORDER BY id"
                ),
                {"ids": list(projects)},
            )
        )
        return FixtureScope(projects, users, annotations, tasks, datasets)

    if owned is None:
        project_selector = "name = 'E2E Demo Project' OR display_id LIKE 'P-E2E-%'"
        project_params = {}
        user_selector, user_params = "email LIKE '%@e2e.test'", {}
        dataset_selector, dataset_params = "display_id LIKE 'DS-E2E-%'", {}
    else:
        project_selector = "name = ANY(:names) OR display_id = ANY(:display_ids)"
        project_params = {
            "names": owned.all_project_names,
            "display_ids": owned.all_project_display_ids,
        }
        user_selector, user_params = (
            "email = ANY(:emails)",
            {"emails": owned.all_user_emails},
        )
        dataset_selector = "display_id = ANY(:display_ids)"
        dataset_params = {"display_ids": owned.all_dataset_display_ids}

    async def ids(table, selector, parameters):
        return tuple(
            await db.scalars(
                text(f"SELECT id FROM {table} WHERE {selector} ORDER BY id"), parameters
            )
        )

    projects = await ids("projects", project_selector, project_params)
    users = await ids("users", user_selector, user_params)
    datasets = await ids("datasets", dataset_selector, dataset_params)
    tasks = await ids("tasks", "project_id = ANY(:pids)", {"pids": list(projects)})
    annotations = await ids(
        "annotations", "project_id = ANY(:pids)", {"pids": list(projects)}
    )
    return FixtureScope(projects, users, annotations, tasks, datasets)


@dataclass
class _FixtureVideoCleanup:
    scope: Any
    item_ids: tuple
    receipts: tuple
    revisions: dict = field(default_factory=dict)
    deadline: float = 0.0
    exact: bool = False


FIXTURE_VIDEO_DRAIN_SECONDS = 5.0
FIXTURE_VIDEO_DRAIN_INTERVAL = 0.05
FIXTURE_VIDEO_SOURCE_PURPOSE = "video_derivative_original"


def _fixture_video_source_locations(outputs):
    """Locations whose ``video_derivative_original`` read permits gate deletion."""
    return {UUID(output.original_ref["location_id"]) for output in outputs}


def _fixture_video_busy(exc):
    """Map a bounded ownership conflict to the same busy convention.

    A request that is allocating a new owner for the same item holds that row
    with ``FOR UPDATE``. That is known fixture work, so teardown reports a busy
    rejection instead of a driver error; an unrelated database fault still raises.
    """
    if not isinstance(exc, DBAPIError):
        return None
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if state not in {"55P03", "40P01", "40001"}:
        return None
    return HTTPException(409, detail={"code": "e2e_seed_video_cleanup_busy"})


def _fixture_video_purposes(outputs):
    from app.services import video_derivative_objects as objects

    return tuple(objects.purpose(output.id) for output in outputs)


async def _fixture_video_operations(db, *, locations, purposes, excluding=()):
    """Probe the exact permits that can still touch these fixture outputs."""
    from sqlalchemy import and_, or_, select
    from app.db.models.storage_maintenance import StorageOperation

    predicates = []
    if locations:
        predicates.append(
            and_(
                StorageOperation.location_id.in_(tuple(locations)),
                StorageOperation.purpose == FIXTURE_VIDEO_SOURCE_PURPOSE,
            )
        )
    if purposes:
        predicates.append(StorageOperation.purpose.in_(tuple(purposes)))
    if not predicates:
        return ()
    statement = select(StorageOperation.id).where(or_(*predicates))
    if excluding:
        statement = statement.where(StorageOperation.id.not_in(tuple(excluding)))
    with db.no_autoflush:
        return tuple(await db.scalars(statement))


async def _drain_fixture_video_operations(
    db, deadline, *, locations=(), purposes=(), excluding=()
):
    """Wait (bounded) until only this collector's own permits remain.

    Returns ``False`` when the caller's budget expired, so each caller keeps its
    own fail-closed rejection instead of treating an unsettled owner as settled.
    """
    while True:
        live = await _fixture_video_operations(
            db, locations=locations, purposes=purposes, excluding=excluding
        )
        if not live:
            return True
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(FIXTURE_VIDEO_DRAIN_INTERVAL, remaining))


async def _fixture_video_lock_deadline(db, deadline):
    remaining = int((deadline - asyncio.get_running_loop().time()) * 1000)
    if remaining <= 0:
        raise HTTPException(409, detail={"code": "e2e_seed_video_cleanup_busy"})
    await db.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": f"{remaining}ms"},
    )


async def _fixture_video_outputs(db, scope, *, lock=False, deadline=0):
    from sqlalchemy import select
    from app.db.models.dataset import Dataset, DatasetItem
    from app.db.models.video_derivative import VideoDerivativeOutput

    if lock:
        deadline = deadline or (
            asyncio.get_running_loop().time() + FIXTURE_VIDEO_DRAIN_SECONDS
        )
        previous_timeout = await db.scalar(
            text("SELECT current_setting('lock_timeout')")
        )
    if lock and scope.dataset_ids:
        await _fixture_video_lock_deadline(db, deadline)
        await db.execute(
            select(Dataset.id)
            .where(Dataset.id.in_(scope.dataset_ids))
            .order_by(Dataset.id)
            .with_for_update()
        )
    item_ids = tuple(
        await db.scalars(
            select(DatasetItem.id)
            .where(
                DatasetItem.dataset_id.in_(scope.dataset_ids),
                DatasetItem.file_type == "video",
            )
            .order_by(DatasetItem.id)
        )
    )
    if lock and item_ids:
        await _fixture_video_lock_deadline(db, deadline)
        await db.execute(
            select(DatasetItem.id)
            .where(DatasetItem.id.in_(item_ids))
            .order_by(DatasetItem.id)
            .with_for_update()
        )
    statement = (
        select(VideoDerivativeOutput)
        .where(VideoDerivativeOutput.dataset_item_id.in_(item_ids))
        .order_by(VideoDerivativeOutput.id)
    )
    if lock:
        await _fixture_video_lock_deadline(db, deadline)
        statement = statement.with_for_update()
    outputs = list(
        await db.scalars(statement.execution_options(populate_existing=True))
    )
    if lock:
        await db.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": previous_timeout},
        )
    return item_ids, outputs


async def _fixture_video_foreign_refs(db, item_ids, output_ids):
    from sqlalchemy import or_, select
    from app.db.models.dataset import DatasetItem, VideoChunk, VideoFrameCache
    from app.db.models.video_derivative import VideoDerivativeOutput

    if not output_ids:
        return
    tests = (
        select(DatasetItem.id).where(
            DatasetItem.id.not_in(item_ids),
            or_(
                DatasetItem.video_output_id.in_(output_ids),
                DatasetItem.video_building_output_id.in_(output_ids),
            ),
        ),
        select(VideoChunk.id).where(
            VideoChunk.dataset_item_id.not_in(item_ids),
            VideoChunk.output_id.in_(output_ids),
        ),
        select(VideoFrameCache.id).where(
            VideoFrameCache.dataset_item_id.not_in(item_ids),
            VideoFrameCache.output_id.in_(output_ids),
        ),
        select(VideoDerivativeOutput.id).where(
            VideoDerivativeOutput.dataset_item_id.not_in(item_ids),
            VideoDerivativeOutput.metadata_output_id.in_(output_ids),
        ),
    )
    if any([await db.scalar(statement.limit(1)) is not None for statement in tests]):
        raise HTTPException(409, detail={"code": "e2e_seed_video_foreign_reference"})


async def _fixture_video_source_operations(db, receipts):
    from sqlalchemy import select
    from app.db.models.storage_maintenance import StorageOperation

    locations = {UUID(receipt.original_ref["location_id"]) for receipt in receipts}
    if (
        locations
        and await db.scalar(
            select(StorageOperation.id)
            .where(
                StorageOperation.location_id.in_(locations),
                StorageOperation.purpose == "video_derivative_original",
            )
            .limit(1)
        )
        is not None
    ):
        raise HTTPException(409, detail={"code": "e2e_seed_video_operation_unsettled"})


def _fixture_video_seed_claim(output):
    return {
        "item_id": str(output.dataset_item_id),
        "token": str(output.lease_token),
        "original_ref": output.original_ref,
        "storage_ref": output.storage_ref,
    }


async def _prepare_fixture_native_video_cleanup(db, scope, runtime, run_id):
    from app.db.models.dataset import DatasetItem

    deadline = asyncio.get_running_loop().time() + FIXTURE_VIDEO_DRAIN_SECONDS
    _, pending = await _fixture_video_outputs(db, scope)
    if not await _drain_fixture_video_operations(
        db,
        deadline,
        locations=_fixture_video_source_locations(pending),
        purposes=_fixture_video_purposes(pending),
    ):
        raise HTTPException(409, detail={"code": "e2e_seed_video_operation_unsettled"})
    try:
        item_ids, outputs = await _fixture_video_outputs(
            db, scope, lock=True, deadline=deadline
        )
    except DBAPIError as exc:
        busy = _fixture_video_busy(exc)
        if busy is None:
            raise
        raise busy from None
    await _fixture_video_foreign_refs(db, item_ids, [output.id for output in outputs])
    await _fixture_video_source_operations(db, outputs)
    item_datasets = dict(
        (
            await db.execute(
                select(DatasetItem.id, DatasetItem.dataset_id).where(
                    DatasetItem.id.in_(item_ids)
                )
            )
        ).all()
    )
    if set(item_datasets) != set(item_ids):
        raise HTTPException(409, detail={"code": "e2e_seed_video_scope_changed"})
    by_id = {output.id: output for output in outputs}
    incoming = {output.id: 0 for output in outputs}
    for output in outputs:
        if output.metadata_output_id is not None:
            target = by_id.get(output.metadata_output_id)
            if target is None or target.dataset_item_id != output.dataset_item_id:
                raise HTTPException(
                    409, detail={"code": "e2e_seed_video_scope_changed"}
                )
            incoming[target.id] += 1
    ready = sorted(
        (output_id for output_id, count in incoming.items() if not count), key=str
    )
    ordered = []
    while ready:
        output_id = ready.pop(0)
        output = by_id[output_id]
        dataset_id = item_datasets[output.dataset_item_id]
        ordered.append(
            (
                dataset_id,
                output.id,
                {**_fixture_video_seed_claim(output), "dataset_id": str(dataset_id)},
            )
        )
        if output.metadata_output_id is not None:
            incoming[output.metadata_output_id] -= 1
            if incoming[output.metadata_output_id] == 0:
                ready.append(output.metadata_output_id)
                ready.sort(key=str)
    if len(ordered) != len(outputs):
        raise HTTPException(409, detail={"code": "e2e_seed_video_scope_changed"})
    await runtime.classify_video_outputs(run_id, ordered)
    await db.commit()


async def _prepare_fixture_video_cleanup(db, scope, *, seed_claims=None):
    from app.services import video_derivative_objects as objects

    deadline = asyncio.get_running_loop().time() + FIXTURE_VIDEO_DRAIN_SECONDS
    _, pending = await _fixture_video_outputs(db, scope)
    if not await _drain_fixture_video_operations(
        db,
        deadline,
        locations=_fixture_video_source_locations(pending),
        purposes=_fixture_video_purposes(pending),
    ):
        raise HTTPException(409, detail={"code": "e2e_seed_video_operation_unsettled"})
    try:
        item_ids, outputs = await _fixture_video_outputs(
            db, scope, lock=True, deadline=deadline
        )
    except DBAPIError as exc:
        busy = _fixture_video_busy(exc)
        if busy is None:
            raise
        raise busy from None
    if seed_claims is not None and (
        {str(output.id) for output in outputs} != seed_claims.keys()
        or any(
            seed_claims[str(output.id)][key] != value
            for output in outputs
            for key, value in _fixture_video_seed_claim(output).items()
        )
    ):
        raise HTTPException(409, detail={"code": "e2e_seed_video_scope_changed"})
    await _fixture_video_foreign_refs(db, item_ids, [output.id for output in outputs])
    for output in outputs:
        output.state = "retiring"
    await db.flush()
    plan = _FixtureVideoCleanup(
        scope,
        item_ids,
        tuple(objects.snapshot(output) for output in outputs),
        exact=seed_claims is not None,
    )
    await db.commit()
    if not await _drain_fixture_video_operations(
        db,
        deadline,
        locations=_fixture_video_source_locations(plan.receipts),
        purposes=_fixture_video_purposes(plan.receipts),
    ):
        raise HTTPException(409, detail={"code": "e2e_seed_video_operation_unsettled"})
    return plan


async def _delete_fixture_video_objects(db, plan, resolve_scope):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.models.storage_maintenance import StorageOperation
    from app.db.models.video_derivative import VideoDerivativeOutput
    from app.services import storage_access, video_derivative_originals as originals
    from app.services import (
        video_derivative_objects as objects,
        video_derivative_storage as physical,
    )

    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    if plan.receipts and plan.deadline <= 0:
        plan.deadline = asyncio.get_running_loop().time() + FIXTURE_VIDEO_DRAIN_SECONDS
    for receipt in plan.receipts:
        served = []

        async def acquired(permit, served=served):
            served.append(permit.id)

        async def guard(revision, receipt=receipt, served=served):
            async with factory() as fresh:
                if await resolve_scope(fresh) != plan.scope:
                    raise HTTPException(
                        409, detail={"code": "e2e_seed_video_scope_changed"}
                    )
                if not await _drain_fixture_video_operations(
                    fresh,
                    plan.deadline,
                    locations={UUID(receipt.original_ref["location_id"])},
                    purposes=(objects.purpose(receipt.id),),
                    excluding=served,
                ):
                    raise HTTPException(
                        409, detail={"code": "e2e_seed_video_operation_unsettled"}
                    )
                previous_timeout = await fresh.scalar(
                    text("SELECT current_setting('lock_timeout')")
                )
                await _fixture_video_lock_deadline(fresh, plan.deadline)
                current = await fresh.scalar(
                    select(VideoDerivativeOutput)
                    .where(VideoDerivativeOutput.id == receipt.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                await fresh.execute(
                    text("SELECT set_config('lock_timeout', :timeout, true)"),
                    {"timeout": previous_timeout},
                )
                if (
                    current is None
                    or objects.snapshot(current) != receipt
                    or current.state != "retiring"
                ):
                    raise HTTPException(
                        409, detail={"code": "e2e_seed_video_owner_changed"}
                    )
                await _fixture_video_source_operations(fresh, [receipt])
                operations = list(
                    await fresh.scalars(
                        select(StorageOperation).where(
                            StorageOperation.purpose == objects.purpose(receipt.id)
                        )
                    )
                )
                if len(operations) != 1 or operations[0].revision != revision:
                    raise HTTPException(
                        409, detail={"code": "e2e_seed_video_operation_unsettled"}
                    )
                location = receipt.storage_ref["location_id"]
                if location in plan.revisions and plan.revisions[location] != revision:
                    raise HTTPException(
                        409, detail={"code": "storage_maintenance_changed"}
                    )
                plan.revisions[location] = revision
                await fresh.commit()

        try:
            async with objects.access(
                db,
                receipt,
                guard,
                on_acquired=acquired,
                **({"single_attempt": True, "owned": True} if plan.exact else {}),
            ) as storage:
                await originals.physical(
                    physical.delete_output,
                    storage,
                    receipt,
                    timeout=originals.JOB_SECONDS,
                )
        except DBAPIError as exc:
            busy = _fixture_video_busy(exc)
            if busy is None:
                raise
            raise busy from None


async def _finalize_fixture_video_cleanup(db, plan, resolve_scope):
    from sqlalchemy import case, delete, update
    from app.db.models.dataset import DatasetItem, VideoChunk, VideoFrameCache
    from app.db.models.video_derivative import VideoDerivativeOutput
    from app.services import storage_access, video_derivative_objects as objects

    if await resolve_scope(db) != plan.scope:
        raise HTTPException(409, detail={"code": "e2e_seed_video_scope_changed"})
    deadline = asyncio.get_running_loop().time() + FIXTURE_VIDEO_DRAIN_SECONDS
    _, pending = await _fixture_video_outputs(db, plan.scope)
    if not await _drain_fixture_video_operations(
        db,
        deadline,
        locations=_fixture_video_source_locations(pending),
        purposes=_fixture_video_purposes(pending),
    ):
        raise HTTPException(409, detail={"code": "e2e_seed_video_operation_unsettled"})
    item_ids, outputs = await _fixture_video_outputs(
        db, plan.scope, lock=True, deadline=deadline
    )
    expected = {receipt.id: receipt for receipt in plan.receipts}
    current = {output.id for output in outputs}
    if (
        item_ids != plan.item_ids
        or set(expected) - current
        or (plan.exact and current != set(expected))
    ):
        raise HTTPException(409, detail={"code": "e2e_seed_video_owner_changed"})
    stale = []
    for output in outputs:
        if output.id in expected:
            continue
        if (
            output.state != "building"
            or output.lease_token is not None
            or output.inventory is not None
            or await objects._in_use(db, output.id)
        ):
            raise HTTPException(409, detail={"code": "e2e_seed_video_owner_changed"})
        stale.append(output.id)
    identifiers = [*expected, *stale]
    await _fixture_video_foreign_refs(db, item_ids, identifiers)
    await _fixture_video_source_operations(db, plan.receipts)
    for output in outputs:
        if output.id in expected:
            changed = objects.snapshot(output) != expected[output.id]
        else:
            changed = False
            output.state = "retiring"
        if changed or await objects._in_use(db, output.id):
            raise HTTPException(
                409, detail={"code": "e2e_seed_video_operation_unsettled"}
            )
    for location, revision in sorted(plan.revisions.items()):
        await storage_access.check(
            db, UUID(location), expected_revision=revision, lock=True
        )
    if plan.exact and not identifiers:
        return
    for slot in (VideoChunk, VideoFrameCache):
        clear = update(slot).where(slot.dataset_item_id.in_(item_ids))
        if plan.exact:
            clear = clear.where(slot.output_id.in_(identifiers))
        await db.execute(clear.values(output_id=None))
    clear_items = update(DatasetItem).where(DatasetItem.id.in_(item_ids))
    active = DatasetItem.video_output_id
    building = DatasetItem.video_building_output_id
    if plan.exact:
        active_owned = active.in_(identifiers)
        building_owned = building.in_(identifiers)
        clear_items = clear_items.where(or_(active_owned, building_owned))
        active = case((active_owned, None), else_=active)
        building = case((building_owned, None), else_=building)
    else:
        active = building = None
    await db.execute(
        clear_items.values(video_output_id=active, video_building_output_id=building)
    )
    await db.execute(
        update(VideoDerivativeOutput)
        .where(
            VideoDerivativeOutput.id.in_(identifiers),
            VideoDerivativeOutput.state == "retiring",
        )
        .values(state="deleted")
    )
    await db.flush()
    await db.execute(
        delete(VideoDerivativeOutput).where(VideoDerivativeOutput.id.in_(identifiers))
    )
    await db.flush()


async def _owned_resource_ids(db, run_id, kind):
    from app.db.models.fixture_run import FixtureRunResource

    return tuple(
        await db.scalars(
            select(FixtureRunResource.resource_id)
            .where(
                FixtureRunResource.run_id == run_id,
                FixtureRunResource.kind == kind,
            )
            .order_by(FixtureRunResource.resource_id)
        )
    )


async def _guard_fixture_export_anchors(db, run_id, project_ids):
    """Keep independent export evidence until its real owner retires it."""
    from app.db.models.export_artifact import ExportArtifact
    from app.db.models.export_media_bundle import ExportMediaBundle
    from app.db.models.export_output import ExportOutput

    for owner in (ExportArtifact, ExportOutput):
        if (
            await db.scalar(
                select(owner.id).where(owner.project_id.in_(project_ids)).limit(1)
            )
            is not None
        ):
            raise HTTPException(
                409, detail={"code": "fixture_export_retirement_unsettled"}
            )
    bundles = (
        await db.execute(
            select(ExportMediaBundle.project_id, ExportMediaBundle.job_id)
            .where(ExportMediaBundle.project_id.in_(project_ids))
            .distinct()
        )
    ).all()
    for project_id, job_id in bundles:
        retired = await db.scalar(
            text("SELECT public.fixture_export_retired(:run_id, :project_id, :job_id)"),
            {"run_id": run_id, "project_id": project_id, "job_id": job_id},
        )
        if retired is not True:
            raise HTTPException(
                409, detail={"code": "fixture_export_retirement_unsettled"}
            )


async def _delete_scale_project_data(db, scope, run_id):
    """Retire a manifest-verified scale scope through its existing owners."""
    from app.db.models.fixture_run import FixtureRunResource
    from app.services.labeling_scale_seed_operations import LabelingScaleSeedOperations
    from app.services.workspace_scale_seed_data import (
        delete_scale_seed_projects,
        guard_scale_seed_sources,
        prepare_scale_seed_cleanup,
    )

    resources = tuple(
        await db.scalars(
            select(FixtureRunResource)
            .where(FixtureRunResource.run_id == run_id)
            .with_for_update()
        )
    )
    if any(row.kind not in {"project", "dataset"} for row in resources):
        raise HTTPException(409, detail={"code": "fixture_run_scope_changed"})
    projects = {row.resource_id for row in resources if row.kind == "project"}
    datasets = {row.resource_id for row in resources if row.kind == "dataset"}
    marker = f"scale-seed-{run_id.hex}"
    project_ids, dataset_ids = await prepare_scale_seed_cleanup(db, marker)
    if (
        set(project_ids) != projects
        or set(dataset_ids) != datasets
        or set(scope.project_ids) != projects
        or set(scope.dataset_ids) != datasets
    ):
        raise HTTPException(409, detail={"code": "fixture_run_scope_changed"})
    await db.execute(
        text(
            "SELECT yoao_assets_v1.guard_scale_fixture_originals(CAST(:run AS uuid), CAST(:ids AS uuid[]))"
        ),
        {"run": run_id, "ids": dataset_ids},
    )
    await LabelingScaleSeedOperations(db).purge_labeling_rows(marker, project_ids)
    await guard_scale_seed_sources(db, dataset_ids, after_task_cleanup=True)
    await db.execute(
        text("DELETE FROM dataset_items WHERE dataset_id=ANY(CAST(:ids AS uuid[]))"),
        {"ids": dataset_ids},
    )
    deleted = set(
        await db.scalars(
            text(
                "DELETE FROM datasets WHERE id=ANY(CAST(:ids AS uuid[])) AND name=:marker RETURNING id"
            ),
            {"ids": dataset_ids, "marker": marker},
        )
    )
    if deleted != datasets:
        raise HTTPException(409, detail={"code": "fixture_run_scope_changed"})
    await delete_scale_seed_projects(db, marker=marker, project_ids=project_ids)


async def _delete_owned_project_data(db, scope, run_id):
    """Delete run projects and datasets while preserving the reclaim actor."""
    from app.services.dataset import DatasetService
    from app.services.auxiliary_originals import retire_fixture_owners
    from app.services.managed_original_retirement import (
        capture_fixture_auxiliary_retirements,
        capture_managed_original_retirements,
    )

    project_ids, dataset_ids = list(scope.project_ids), list(scope.dataset_ids)
    unexpected_item = await db.scalar(
        text(
            "SELECT item.id FROM public.dataset_items AS item "
            "WHERE item.dataset_id = ANY(CAST(:datasets AS uuid[])) AND ("
            "NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource "
            "WHERE resource.run_id=:run AND resource.kind='item' AND resource.resource_id=item.id) "
            "OR NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource "
            "WHERE resource.run_id=:run AND resource.kind='creation' AND resource.item_id=item.id)) "
            "LIMIT 1"
        ),
        {"datasets": dataset_ids, "run": run_id},
    )
    if unexpected_item is not None:
        raise HTTPException(409, detail={"code": "fixture_dataset_unowned_item"})
    foreign_link = await db.scalar(
        text(
            "SELECT 1 FROM public.project_datasets "
            "WHERE dataset_id=ANY(CAST(:datasets AS uuid[])) "
            "AND NOT (project_id=ANY(CAST(:projects AS uuid[]))) LIMIT 1"
        ),
        {"datasets": dataset_ids, "projects": project_ids},
    )
    if foreign_link is not None:
        raise HTTPException(409, detail={"code": "fixture_dataset_foreign_project"})
    if project_ids:
        params = {"pids": project_ids, "tids": list(scope.task_ids)}
        await _try_fixture_delete(
            db,
            "DELETE FROM bug_comments WHERE bug_report_id IN (SELECT id FROM bug_reports "
            "WHERE project_id=ANY(:pids) OR task_id=ANY(:tids))",
            params,
        )
        await _try_fixture_delete(
            db,
            "DELETE FROM bug_reports WHERE project_id=ANY(:pids) OR task_id=ANY(:tids)",
            params,
        )
        await _try_fixture_delete(
            db, _JOBS_FIXTURE_PROJECT_DELETE_SQL, {"pids": project_ids}
        )
        await _try_fixture_delete(
            db,
            "DELETE FROM project_task_views WHERE project_id=ANY(:pids)",
            {"pids": project_ids},
        )
        await _try_fixture_delete(
            db, _LABELING_FIXTURE_DELETE_SQL, {"pids": project_ids}
        )
        await _try_fixture_delete(
            db,
            "DELETE FROM project_ml_backend_pool WHERE project_id=ANY(:pids)",
            {"pids": project_ids},
        )
        await _try_fixture_delete(
            db,
            "SELECT yoao_workspace_fixture_v1.delete_projects(CAST(:pids AS uuid[]))",
            {"pids": project_ids},
        )
        if (
            await db.scalar(
                text(
                    "SELECT 1 FROM yoao_workspace_v1.project_facts "
                    "WHERE id=ANY(CAST(:ids AS uuid[])) LIMIT 1"
                ),
                {"ids": project_ids},
            )
            is not None
        ):
            raise HTTPException(409, detail={"code": "fixture_projects_not_removed"})
    for dataset_id in dataset_ids:
        await _try_fixture_delete(
            db,
            "SELECT yoao_jobs_v1.delete_fixture_media_backfills(CAST(:run AS uuid),CAST(:dataset AS uuid))",
            {"run": run_id, "dataset": dataset_id},
        )
        await capture_managed_original_retirements(
            db, dataset_id=dataset_id, fixture_run_id=run_id
        )
        auxiliary_ids = await capture_fixture_auxiliary_retirements(
            db, dataset_id=dataset_id, fixture_run_id=run_id
        )
        cleanup = await DatasetService(db).delete(dataset_id)
        if cleanup is None or cleanup.directory_objects:
            raise HTTPException(
                409, detail={"code": "fixture_dataset_cleanup_unqualified"}
            )
        await retire_fixture_owners(
            db, run_id=run_id, dataset_id=dataset_id, owner_ids=auxiliary_ids
        )
    await db.flush()
    if (
        await db.scalar(
            text(
                "SELECT 1 FROM public.datasets WHERE id=ANY(CAST(:ids AS uuid[])) LIMIT 1"
            ),
            {"ids": dataset_ids},
        )
        is not None
    ):
        raise HTTPException(409, detail={"code": "fixture_datasets_not_removed"})


async def _lock_and_check_fixture_backends(
    db, run_id, *, expected_ids=None, owned_project_ids=()
):
    """Lock the exact inventory and reject external references before mutation."""
    from app.db.models.fixture_run import FixtureRunResource

    registry_ids = list(
        await db.scalars(
            select(FixtureRunResource.resource_id)
            .where(
                FixtureRunResource.run_id == run_id,
                FixtureRunResource.kind == "registry",
            )
            .order_by(FixtureRunResource.resource_id)
            .with_for_update()
        )
    )
    if expected_ids is not None and set(registry_ids) != set(expected_ids):
        raise HTTPException(409, detail={"code": "fixture_backend_scope_changed"})
    if not registry_ids:
        return registry_ids
    existing = list(
        await db.scalars(
            text(
                "SELECT id FROM public.ml_backend_registry "
                "WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id FOR UPDATE"
            ),
            {"ids": registry_ids},
        )
    )
    if set(existing) != set(registry_ids):
        raise HTTPException(409, detail={"code": "fixture_backend_scope_changed"})
    pools = list(
        await db.scalars(
            text(
                "SELECT id FROM public.ml_backend_service_pools "
                "WHERE legacy_instance_id=ANY(CAST(:ids AS uuid[])) "
                "OR id IN (SELECT pool_id FROM public.ml_backend_pool_members "
                "WHERE registry_id=ANY(CAST(:ids AS uuid[]))) "
                "ORDER BY id FOR UPDATE"
            ),
            {"ids": registry_ids},
        )
    )
    await db.execute(
        text(
            "SELECT id FROM public.ml_backend_pool_members "
            "WHERE pool_id=ANY(CAST(:pools AS uuid[])) ORDER BY id FOR UPDATE"
        ),
        {"pools": pools},
    )
    foreign_backend = await db.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM public.project_ml_backend_pool "
            "WHERE pool_id=ANY(CAST(:pools AS uuid[])) "
            "AND NOT (project_id=ANY(CAST(:owned_projects AS uuid[])))) "
            "OR EXISTS (SELECT 1 FROM public.projects "
            "WHERE ml_backend_pool_id=ANY(CAST(:pools AS uuid[])) "
            "AND NOT (id=ANY(CAST(:owned_projects AS uuid[])))) "
            "OR EXISTS (SELECT 1 FROM public.predictions "
            "WHERE (ml_backend_id=ANY(CAST(:ids AS uuid[])) "
            "OR ml_backend_pool_id=ANY(CAST(:pools AS uuid[]))) "
            "AND NOT (project_id=ANY(CAST(:owned_projects AS uuid[])))) "
            "OR EXISTS (SELECT 1 FROM public.failed_predictions "
            "WHERE (ml_backend_id=ANY(CAST(:ids AS uuid[])) "
            "OR ml_backend_pool_id=ANY(CAST(:pools AS uuid[]))) "
            "AND NOT (project_id=ANY(CAST(:owned_projects AS uuid[])))) "
            "OR EXISTS (SELECT 1 FROM public.trained_model_deployments "
            "WHERE registry_id=ANY(CAST(:ids AS uuid[])))"
        ),
        {
            "ids": registry_ids,
            "pools": pools,
            "owned_projects": list(owned_project_ids),
        },
    )
    if foreign_backend:
        raise HTTPException(409, detail={"code": "fixture_backend_foreign_reference"})
    from app.services.ml_backend import MLBackendService

    for registry_id in registry_ids:
        if not await MLBackendService(db).validate_delete(registry_id):
            raise HTTPException(409, detail={"code": "fixture_backend_scope_changed"})
    return registry_ids


async def _delete_owned_accounts_and_auxiliary(
    db, owned, scope, *, family="owned", registry_topologies=None
):
    """Remove only inventoried identities and exact owner-managed auxiliaries."""
    from app.api.v1 import _test_seed_attachments as attachments

    run_id = owned.run_id
    users = list(scope.user_ids)
    projects = list(await _owned_resource_ids(db, run_id, "project"))
    tasks = list(scope.task_ids)
    await attachments.fence(db, scope)
    await db.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
    foreign = await db.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM public.bug_reports AS b "
            "WHERE (b.reporter_id=ANY(CAST(:users AS uuid[])) "
            "OR b.assigned_to_id=ANY(CAST(:users AS uuid[]))) "
            "AND NOT (COALESCE(b.project_id=ANY(CAST(:projects AS uuid[])),false) "
            "OR COALESCE(b.task_id=ANY(CAST(:tasks AS uuid[])),false))) "
            "OR EXISTS (SELECT 1 FROM public.bug_comments AS c "
            "JOIN public.bug_reports AS b ON b.id=c.bug_report_id "
            "WHERE c.author_id=ANY(CAST(:users AS uuid[])) "
            "AND NOT (COALESCE(b.project_id=ANY(CAST(:projects AS uuid[])),false) "
            "OR COALESCE(b.task_id=ANY(CAST(:tasks AS uuid[])),false))) "
            "OR EXISTS (SELECT 1 FROM public.task_locks AS l "
            "WHERE l.user_id=ANY(CAST(:users AS uuid[])) AND NOT (l.task_id=ANY(CAST(:tasks AS uuid[])))) "
            "OR EXISTS (SELECT 1 FROM public.annotation_drafts AS d "
            "WHERE d.user_id=ANY(CAST(:users AS uuid[])) AND NOT (d.task_id=ANY(CAST(:tasks AS uuid[]))))"
        ),
        {"users": users, "projects": projects, "tasks": tasks},
    )
    if foreign:
        raise HTTPException(409, detail={"code": "fixture_user_foreign_reference"})
    invitation_ids = set(await _owned_resource_ids(db, run_id, "invitation"))
    invitations = set(
        await db.scalars(
            text(
                "SELECT id FROM public.user_invitations "
                "WHERE invited_by=ANY(CAST(:users AS uuid[])) ORDER BY id FOR UPDATE"
            ),
            {"users": users},
        )
    )
    owned_invitations = set(
        await db.scalars(
            text(
                "SELECT id FROM public.user_invitations "
                "WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id FOR UPDATE"
            ),
            {"ids": list(invitation_ids)},
        )
    )
    if invitations != owned_invitations or not invitations.issubset(invitation_ids):
        raise HTTPException(409, detail={"code": "fixture_invitation_not_owned"})
    template_ids = list(await _owned_resource_ids(db, run_id, "template"))
    template_rows = (
        await db.execute(
            text(
                "SELECT id, display_id FROM public.project_templates "
                "WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id FOR UPDATE"
            ),
            {"ids": template_ids},
        )
    ).all()
    await _try_fixture_delete(
        db,
        "SELECT yoao_workspace_fixture_v1.delete_templates(false, CAST(:ids AS text[]))",
        {"ids": [row.display_id for row in template_rows]},
    )
    if (
        await db.scalar(
            text(
                "SELECT 1 FROM public.project_templates WHERE id=ANY(CAST(:ids AS uuid[])) LIMIT 1"
            ),
            {"ids": template_ids},
        )
        is not None
    ):
        raise HTTPException(409, detail={"code": "fixture_templates_not_removed"})
    if family == "owned":
        await _try_fixture_delete(db, _AUDIT_FIXTURE_CLEANUP, {"run_id": run_id})
    registry_ids = await _lock_and_check_fixture_backends(
        db, run_id, expected_ids=registry_topologies or {}
    )
    if registry_ids:
        from app.application.ml_registry_deletion import _finish_registry_deletion

        for registry_id in registry_ids:
            if not await _finish_registry_deletion(
                db, registry_topologies[registry_id]
            ):
                raise HTTPException(409, detail={"code": "fixture_backend_not_removed"})
    if users:
        await _try_fixture_delete(
            db,
            "SELECT yoao_identity_fixture_v1.delete_reset_tokens(:uids)",
            {"uids": users},
        )
        await _try_fixture_delete(
            db,
            "SELECT yoao_identity_fixture_v1.delete_invitations(:uids)",
            {"uids": users},
        )
        await _try_fixture_delete(
            db,
            "SELECT yoao_identity_fixture_v1.delete_organization_members(:uids)",
            {"uids": users},
        )
        user_rows = (
            await db.execute(
                text(
                    "SELECT id,email FROM public.users WHERE id=ANY(CAST(:ids AS uuid[])) "
                    "ORDER BY id FOR UPDATE"
                ),
                {"ids": users},
            )
        ).all()
        await _try_fixture_delete(
            db,
            "SELECT yoao_identity_fixture_v1.delete_accounts(CAST(:shared AS boolean),CAST(:emails AS text[]))",
            {"shared": False, "emails": [row.email for row in user_rows]},
        )
        remaining = await db.scalar(
            text(
                "SELECT 1 FROM public.users WHERE id=ANY(CAST(:ids AS uuid[])) LIMIT 1"
            ),
            {"ids": users},
        )
        if remaining is not None:
            raise HTTPException(409, detail={"code": "fixture_accounts_not_removed"})


async def _reclaim_owned_originals(db, run_id, actor_id):
    """Use the production retirement, claim and exact-version reclaim owners."""
    from app.db.models.fixture_run import FixtureRunResource
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.managed_original_reclaim import (
        ManagedOriginalReclaimAttempt,
        ManagedOriginalReclaimClaim,
    )
    from app.db.models.managed_original_retirement import ManagedOriginalRetirement
    from app.db.models.user import User
    from app.schemas.managed_original_reclaim import (
        ManagedOriginalReclaimApply,
        ManagedOriginalReclaimReview,
    )
    from app.services import managed_original_reclaim
    from app.workers.managed_original_reclaim import execute

    creations = list(
        await db.scalars(
            select(ManagedOriginalCreation)
            .join(
                FixtureRunResource,
                FixtureRunResource.resource_id == ManagedOriginalCreation.id,
            )
            .where(
                FixtureRunResource.run_id == run_id,
                FixtureRunResource.kind.in_(("creation", "auxiliary")),
            )
            .order_by(ManagedOriginalCreation.id)
        )
    )
    creation_ids = []
    for creation in creations:
        if (
            creation.status in {"failed", "conflict"}
            and creation.write_started_at is None
        ):
            continue
        if creation.status != "verified" or creation.settled_at is None:
            raise HTTPException(409, detail={"code": "fixture_original_unsettled"})
        creation_ids.append(creation.id)
    for creation_id in creation_ids:
        creation = await db.get(
            ManagedOriginalCreation, creation_id, populate_existing=True
        )
        retirement = list(
            await db.scalars(
                select(ManagedOriginalRetirement).where(
                    ManagedOriginalRetirement.creation_id == creation_id
                )
            )
        )
        if len(retirement) != 1:
            raise HTTPException(
                409, detail={"code": "fixture_original_retirement_missing"}
            )
        retirement = retirement[0]
        claim = await db.scalar(
            select(ManagedOriginalReclaimClaim).where(
                ManagedOriginalReclaimClaim.bucket == retirement.bucket,
                ManagedOriginalReclaimClaim.object_key == retirement.object_key,
            )
        )
        attempt = (
            await db.get(ManagedOriginalReclaimAttempt, claim.current_attempt_id)
            if claim
            else None
        )
        if claim is not None and (
            claim.creation_id != creation_id
            or attempt is None
            or claim.version_id != creation.version_id
            or claim.storage_identity != creation.storage_identity
            or attempt.version_id != creation.version_id
            or attempt.requested_by != actor_id
            or attempt.retirement_id != retirement.id
        ):
            raise HTTPException(409, detail={"code": "fixture_original_claim_changed"})
        if (
            claim is not None
            and await db.get(FixtureRunResource, (run_id, "reclaim", attempt.id))
            is None
        ):
            raise HTTPException(
                409, detail={"code": "fixture_original_claim_not_owned"}
            )
        if claim is not None and claim.state == "deleted":
            if (
                attempt.status != "completed"
                or not attempt.physical_settled
                or attempt.absence_verified_at is None
            ):
                raise HTTPException(
                    409, detail={"code": "fixture_original_reclaim_unsettled"}
                )
            continue
        retry_of_id = None
        if (
            claim is not None
            and claim.state == "released"
            and attempt.status == "failed"
            and attempt.physical_settled
        ):
            retry_of_id = attempt.id
        if claim is not None and retry_of_id is None:
            if (
                claim.state != "claimed"
                or attempt.status != "pending"
                or attempt.async_job_id is None
            ):
                raise HTTPException(
                    409, detail={"code": "fixture_original_reclaim_unsettled"}
                )
        else:
            actor = await db.get(User, actor_id, populate_existing=True)
            if actor is None:
                raise HTTPException(
                    409, detail={"code": "fixture_cleanup_actor_missing"}
                )
            review = ManagedOriginalReclaimReview(
                reason="Retire an exact isolated fixture original",
                retry_of_id=retry_of_id,
            )
            preview = await managed_original_reclaim.preview(
                db, retirement.id, review, actor
            )
            if not preview.eligible or preview.impact_token is None:
                raise HTTPException(
                    409,
                    detail={
                        "code": "fixture_original_reclaim_blocked",
                        "blockers": preview.blockers,
                    },
                )
            attempt, job, _ = await managed_original_reclaim.start(
                db,
                retirement.id,
                ManagedOriginalReclaimApply(
                    reason=review.reason,
                    impact_token=preview.impact_token,
                    retry_of_id=review.retry_of_id,
                ),
                actor,
            )
            db.add(
                FixtureRunResource(
                    run_id=run_id,
                    kind="reclaim",
                    resource_id=attempt.id,
                    logical_key=f"reclaim-{attempt.id.hex}",
                    state="published",
                )
            )
            await db.commit()
        attempt_id, job_id, task_id = attempt.id, attempt.async_job_id, attempt.task_id
        await execute(db, job_id, task_id)
        observed = await db.get(
            ManagedOriginalReclaimAttempt, attempt_id, populate_existing=True
        )
        if (
            observed is None
            or observed.status != "completed"
            or not observed.physical_settled
            or observed.absence_verified_at is None
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "fixture_original_reclaim_unsettled",
                    "attempt_id": str(attempt_id),
                    "error_code": observed.error_code if observed is not None else None,
                },
            )


async def _wait_fixture_source_readers(db, run_id, reader_wait_seconds):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.application.fixture_prediction_retirement import (
        FixturePredictionRetirementBlocked,
        wait_for_fixture_original_readers,
    )
    from app.services import storage_access

    deadline = (
        None
        if reader_wait_seconds is None
        else asyncio.get_running_loop().time() + reader_wait_seconds
    )
    try:
        await wait_for_fixture_original_readers(
            async_sessionmaker(storage_access.engine(db), expire_on_commit=False),
            run_id,
            deadline=deadline,
        )
    except FixturePredictionRetirementBlocked as exc:
        raise HTTPException(
            409,
            detail={
                "code": "fixture_prediction_retirement_blocked",
                "reason": exc.reason,
            },
        ) from exc


async def _retire_fixture_execution_work(
    db, owned, *, family="owned", reader_wait_seconds=None, drained_runs=frozenset()
):
    """Retire native work before any sibling Dataset can be reclaimed."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.api.v1 import _test_seed_attachments as attachments
    from app.application.fixture_prediction_retirement import (
        FixturePredictionRetirementBlocked,
        retire_fixture_prediction_work,
    )
    from app.db.models.async_job import AsyncJob
    from app.db.models.fixture_run import FixturePresentationJob
    from app.services import export_media_retirement, storage_access

    run_id = owned.run_id
    scope = await _resolve_fixture_scope(db, owned)
    await db.rollback()
    try:
        session_factory = async_sessionmaker(
            storage_access.engine(db), expire_on_commit=False
        )
        if family in {"owned", "screenshot"} and run_id not in drained_runs:
            await _wait_fixture_source_readers(db, run_id, reader_wait_seconds)
        await retire_fixture_prediction_work(
            session_factory=session_factory,
            run_id=run_id,
            project_ids=scope.project_ids,
            task_ids=scope.task_ids,
        )
    except FixturePredictionRetirementBlocked as exc:
        raise HTTPException(
            409,
            detail={
                "code": "fixture_prediction_retirement_blocked",
                "reason": exc.reason,
            },
        ) from exc
    if await _resolve_fixture_scope(db, owned) != scope:
        attachments.reject()
    await _lock_and_check_fixture_backends(
        db, run_id, owned_project_ids=scope.project_ids
    )
    await db.rollback()
    export_jobs = list(
        (
            await db.execute(
                select(AsyncJob.id, AsyncJob.project_id)
                .where(
                    AsyncJob.project_id.in_(scope.project_ids),
                    AsyncJob.kind == "export",
                    ~select(FixturePresentationJob.job_id)
                    .where(
                        FixturePresentationJob.job_id == AsyncJob.id,
                        FixturePresentationJob.run_id == run_id,
                        FixturePresentationJob.project_id == AsyncJob.project_id,
                        FixturePresentationJob.kind == AsyncJob.kind,
                        FixturePresentationJob.state == "admitted",
                    )
                    .exists(),
                )
                .order_by(AsyncJob.id)
            )
        ).all()
    )
    await db.rollback()
    for job_id, project_id in export_jobs:
        if not await export_media_retirement.retire_fixture_export(
            db, run_id=run_id, project_id=project_id, job_id=job_id
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "fixture_export_retirement_unsettled",
                    "job_id": str(job_id),
                },
            )
    if await _resolve_fixture_scope(db, owned) != scope:
        attachments.reject()
    return scope


def _native_run_owned(native_runtime, run_id: UUID) -> bool:
    """Whether this exact run registered native participants under its suite."""
    return native_runtime is not None and (
        run_id in native_runtime.runs or run_id in native_runtime.retired_runs
    )


async def _drain_native_video_requests(db, native_runtime, run_id):
    if native_runtime is not None and run_id in native_runtime.runs:
        await native_runtime.drain_video_requests_before_seal(
            db, run_id, asyncio.get_running_loop().time() + FIXTURE_VIDEO_DRAIN_SECONDS
        )


async def _cleanup_owned_e2e_fixtures(
    db,
    owned,
    *,
    family="owned",
    registry=None,
    providers=None,
    native_runtime=None,
    drained_runs=frozenset(),
):
    """Seal and reclaim one exact run through the existing fixture cleanup owners."""
    from app.api.v1 import _test_seed_attachments as attachments
    from app.api.v1 import _test_seed_guide_assets as guides
    from app.db.models.fixture_run import (
        FixtureMaintenanceWindow,
        FixtureRun,
        FixtureRunResource,
    )
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.managed_original_reclaim import (
        ManagedOriginalReclaimAttempt,
        ManagedOriginalReclaimClaim,
    )
    from app.db.models.user import User
    from app.services import fixture_run_ownership as ownership, storage_access
    from app.services.original_playback import owned_playback_contexts

    run_id = owned.run_id
    if run_id is None:
        raise HTTPException(422, detail={"code": "fixture_run_required"})

    async def resolve(session):
        return await _resolve_fixture_scope(session, owned)

    async def cleanup():
        await _drain_native_video_requests(db, native_runtime, run_id)
        blockers = await ownership.seal(db, run_id, family=family)
        await db.commit()
        if blockers:
            raise HTTPException(
                409,
                detail={
                    "code": "fixture_run_unsettled",
                    "admissions": [str(value) for value in blockers],
                },
            )
        run = await db.get(FixtureRun, run_id, populate_existing=True)
        completed_drains = drained_runs
        native = native_runtime if _native_run_owned(native_runtime, run_id) else None
        if native is None:
            if (
                run.state != "retired"
                and family in {"owned", "screenshot"}
                and run_id not in drained_runs
            ):
                await db.rollback()
                await _wait_fixture_source_readers(db, run_id, None)
                completed_drains = drained_runs | {run_id}
            proof = await db.scalar(
                text("SELECT yoao_assets_v1.seal_fixture_run_without_media(:run)"),
                {"run": run_id},
            )
            if proof is not True:
                await db.rollback()
                raise HTTPException(
                    409, detail={"code": "fixture_native_no_birth_unproven"}
                )
            await db.commit()
            run = await db.get(FixtureRun, run_id, populate_existing=True)
        active_window = await db.scalar(
            select(FixtureMaintenanceWindow).where(
                FixtureMaintenanceWindow.run_id == run_id,
                FixtureMaintenanceWindow.state != "resumed",
            )
        )
        if run.state == "retired":
            if native is not None and run_id not in native.retired_runs:
                raise HTTPException(
                    409, detail={"code": "fixture_native_run_unsettled"}
                )
            if active_window is not None:
                raise HTTPException(
                    409, detail={"code": "fixture_maintenance_window_changed"}
                )
            if providers is not None:
                await providers.retire(run_id)
            return
        scope = await _retire_fixture_execution_work(
            db, owned, family=family, drained_runs=completed_drains
        )

        fixture_contexts = (
            owned_playback_contexts(scope.task_ids, scope.dataset_ids)
            if registry is not None
            else None
        )

        async def revoke_playback():
            if registry is not None:
                await registry.revoke_contexts(fixture_contexts)
            elif native_runtime is not None:
                await native_runtime.revoke_playback(scope.task_ids, scope.dataset_ids)

        if native is not None and run_id not in native.retired_runs:
            await db.commit()
            await native.stop_producers(run_id)
            await revoke_playback()
            if not native.video_scope_prepared(run_id):
                await _prepare_fixture_native_video_cleanup(db, scope, native, run_id)
            await native.retire_datasets(db, run_id, scope.dataset_ids)

            async def retire_video_seeds(claims):
                plan = await _prepare_fixture_video_cleanup(
                    db, scope, seed_claims=claims
                )
                await _delete_fixture_video_objects(db, plan, resolve)
                await _finalize_fixture_video_cleanup(db, plan, resolve)
                await db.commit()

            await native.retire_roles(run_id, retire_video_seeds)
        active_window = await db.scalar(
            select(FixtureMaintenanceWindow).where(
                FixtureMaintenanceWindow.run_id == run_id,
                FixtureMaintenanceWindow.state != "resumed",
            )
        )
        async with db.begin_nested() as preflight:
            try:
                await attachments.fence(db, scope)
            except DBAPIError as exc:
                attachments.translate(exc)
            if await resolve(db) != scope:
                attachments.reject()
            await guard_fixture_project_cleanup(db, list(scope.project_ids))
            await ownership.guard_project_jobs(db, list(scope.project_ids))
            await ownership.guard_dataset_jobs(db, list(scope.dataset_ids))
            await _guard_fixture_export_anchors(db, run_id, scope.project_ids)
            await preflight.rollback()
        await guides.preflight(db, scope)
        if family != "scale":
            await ownership.recover_verified_originals(db, run_id)
            await ownership.recover_verified_auxiliary_originals(db, run_id)
            await db.commit()

        verified = list(
            await db.scalars(
                select(ManagedOriginalCreation)
                .join(
                    FixtureRunResource,
                    FixtureRunResource.resource_id == ManagedOriginalCreation.id,
                )
                .where(
                    FixtureRunResource.run_id == run_id,
                    FixtureRunResource.kind.in_(("creation", "auxiliary")),
                    ManagedOriginalCreation.status == "verified",
                )
            )
        )
        need_window = active_window is not None
        for creation in verified:
            claim = await db.scalar(
                select(ManagedOriginalReclaimClaim).where(
                    ManagedOriginalReclaimClaim.creation_id == creation.id
                )
            )
            if claim is None or claim.state != "deleted":
                need_window = True
        actor = None
        actor_id = None
        if verified or active_window is not None:
            recorded_actor = await db.scalar(
                select(ManagedOriginalReclaimAttempt.requested_by)
                .join(
                    FixtureRunResource,
                    FixtureRunResource.resource_id == ManagedOriginalReclaimAttempt.id,
                )
                .where(
                    FixtureRunResource.run_id == run_id,
                    FixtureRunResource.kind == "reclaim",
                )
                .limit(1)
            )
            actor_id = (
                active_window.actor_id if active_window is not None else recorded_actor
            )
            if actor_id is None:
                actor_id = await db.scalar(
                    select(User.id)
                    .join(FixtureRunResource, FixtureRunResource.resource_id == User.id)
                    .where(
                        FixtureRunResource.run_id == run_id,
                        FixtureRunResource.kind == "user",
                        User.role == "super_admin",
                        User.is_active.is_(True),
                    )
                    .order_by(User.id)
                    .limit(1)
                )
            actor = await db.get(User, actor_id) if actor_id is not None else None
            if actor is None or not actor.is_active or actor.role != "super_admin":
                raise HTTPException(
                    409, detail={"code": "fixture_cleanup_actor_missing"}
                )
        window = (
            await ownership.begin_reclaim_window(db, run_id, actor)
            if need_window
            else None
        )
        window_id = window.id if window is not None else None
        try:
            plan = await attachments.prepare(db, scope)
            await revoke_playback()
            video_plan = (
                await _prepare_fixture_video_cleanup(db, scope)
                if native is None
                else None
            )
            await revoke_playback()
            await attachments.delete_objects(db, plan, resolve)
            if video_plan is not None:
                await _delete_fixture_video_objects(db, video_plan, resolve)
            await db.commit()

            async def retire_guides():
                await guides.retire_references(db, scope)
                if video_plan is not None:
                    await _finalize_fixture_video_cleanup(db, video_plan, resolve)

            await attachments.finalize(db, plan, resolve, before_gate=retire_guides)
            await _guard_fixture_export_anchors(db, run_id, scope.project_ids)
            await _lock_and_check_fixture_backends(
                db, run_id, owned_project_ids=scope.project_ids
            )
            if family == "scale":
                await _delete_scale_project_data(db, scope, run_id)
            else:
                await ownership.recover_verified_originals(db, run_id)
                await ownership.recover_verified_auxiliary_originals(db, run_id)
                await _delete_owned_project_data(db, scope, run_id)
            for project_id in scope.project_ids:
                resource = await db.get(
                    FixtureRunResource, (run_id, "project", project_id)
                )
                if resource is None or resource.state != "published":
                    raise HTTPException(
                        409, detail={"code": "fixture_run_scope_changed"}
                    )
                resource.state = "retired"
            await attachments.assert_empty(db, plan)
            await db.commit()
            if verified:
                await _reclaim_owned_originals(db, run_id, actor_id)
            if window_id is not None:
                saved_window = await db.get(
                    FixtureMaintenanceWindow, window_id, populate_existing=True
                )
                saved_actor = await db.get(User, actor_id, populate_existing=True)
                await ownership.end_reclaim_window(db, saved_window, saved_actor)
                window_id = None
            registry_ids = await _lock_and_check_fixture_backends(db, run_id)
            from app.application.ml_registry_deletion import _prepare_registry_deletion
            from app.services.inference_topology_data import InferenceTopologyData
            from app.services.ml_backend import MLBackendService

            topology_data = InferenceTopologyData(db)
            backend_service = MLBackendService(db)
            for registry_id in registry_ids:
                pool = await topology_data.pool_for_registry(registry_id)
                if pool is not None:
                    members = await topology_data.pool_members(pool.id)
                    if pool.legacy_instance_id != registry_id or any(
                        member.registry_id != registry_id for member in members
                    ):
                        raise HTTPException(
                            409, detail={"code": "fixture_backend_scope_changed"}
                        )
                    await backend_service.drain_pool_member(pool.id, registry_id)
            await db.commit()
            registry_topologies = {}
            for registry_id in registry_ids:
                await _reconcile_fixture_backend(run_id, registry_id)
                topology = await _prepare_registry_deletion(db, registry_id)
                if topology is None:
                    raise HTTPException(
                        409, detail={"code": "fixture_backend_not_removed"}
                    )
                registry_topologies[registry_id] = topology
            final_scope = await resolve(db)
            await _delete_owned_accounts_and_auxiliary(
                db,
                owned,
                final_scope,
                family=family,
                registry_topologies=registry_topologies,
            )
            remaining = await resolve(db)
            if remaining != attachments.FixtureScope():
                attachments.reject("e2e_seed_cleanup_incomplete", 500)
            run = await ownership._lock_run(db, run_id)
            if run.state != "sealing":
                raise HTTPException(409, detail={"code": "fixture_run_closed"})
            resources = list(
                await db.scalars(
                    select(FixtureRunResource)
                    .where(FixtureRunResource.run_id == run_id)
                    .with_for_update()
                )
            )
            for resource in resources:
                resource.state = "retired"
            run.state = "retired"
            run.retired_at = datetime.now(timezone.utc)
            await db.commit()
            if providers is not None:
                await providers.retire(run_id)
        except BaseException as failure:
            try:
                await storage_access.finish(asyncio.create_task(db.rollback()))
            except BaseException as rollback_failure:
                raise builtins.BaseExceptionGroup(
                    "Fixture cleanup and rollback failed", [failure, rollback_failure]
                ) from None
            raise

    async with storage_access.engine(db).connect() as connection:
        key = f"fixture-cleanup:{run_id}"
        acquired = await connection.scalar(
            text(
                "SELECT pg_catalog.pg_try_advisory_lock(pg_catalog.hashtextextended(:key, 0))"
            ),
            {"key": key},
        )
        await connection.commit()
        if not acquired:
            raise HTTPException(409, detail={"code": "fixture_cleanup_busy"})
        failure = None
        try:
            await cleanup()
        except BaseException as exc:
            failure = exc
            try:
                await storage_access.finish(asyncio.create_task(db.rollback()))
            except BaseException as rollback_failure:
                failure = builtins.BaseExceptionGroup(
                    "Fixture cleanup and rollback failed", [failure, rollback_failure]
                )
        try:
            unlocked = await storage_access.finish(
                asyncio.create_task(
                    connection.execute(
                        text(
                            "SELECT pg_catalog.pg_advisory_unlock(pg_catalog.hashtextextended(:key, 0))"
                        ),
                        {"key": key},
                    )
                )
            )
            if unlocked.scalar_one() is not True:
                raise RuntimeError("Fixture cleanup lock was not owned")
        except BaseException as unlock_failure:
            try:
                await storage_access.finish(
                    asyncio.create_task(connection.invalidate())
                )
            except BaseException as invalidation_failure:
                unlock_failure = builtins.BaseExceptionGroup(
                    "Fixture lock release and connection invalidation failed",
                    [unlock_failure, invalidation_failure],
                )
            if failure is not None:
                raise builtins.BaseExceptionGroup(
                    "Fixture cleanup and lock release failed", [failure, unlock_failure]
                ) from None
            raise unlock_failure from None
        if failure is not None:
            raise failure


class SeedCleanup(BaseModel):
    ok: bool = True


class SeedSuiteCleanupRequest(BaseModel):
    suite_id: UUID


class SeedScreenshotCleanupRequest(BaseModel):
    model_config = {"extra": "forbid"}

    run_id: UUID


@router.post(
    "/seed/cleanup",
    response_model=SeedCleanup,
    status_code=200,
    include_in_schema=False,
)
async def seed_cleanup(
    payload: SeedSuiteCleanupRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SeedCleanup:
    """Seal and clean only the named suite's registered child runs."""
    from app.db.models.fixture_run import FixtureRun
    from app.services import fixture_run_ownership as ownership

    root = (
        await db.execute(
            select(FixtureRun.parent_run_id, FixtureRun.family, FixtureRun.state).where(
                FixtureRun.id == payload.suite_id
            )
        )
    ).one_or_none()
    if root is None or root.parent_run_id is not None or root.family != "owned":
        raise HTTPException(404, detail={"code": "fixture_suite_not_found"})
    native_runtime = getattr(request.app.state, "fixture_native_runtime", None)
    if (
        root.state == "open"
        and native_runtime is not None
        and native_runtime.suite_id == payload.suite_id
    ):
        from app.services.fixture_native_runtime import _pending_video_requests

        child_ids = (
            select(FixtureRun.id)
            .where(FixtureRun.parent_run_id == payload.suite_id)
            .order_by(FixtureRun.id)
        )
        expected = tuple(await db.scalars(child_ids))
        for run_id in expected:
            await _drain_native_video_requests(db, native_runtime, run_id)

        await ownership._lock_run(db, payload.suite_id)
        if tuple(await db.scalars(child_ids)) != expected:
            await db.rollback()
            raise HTTPException(409, detail={"code": "fixture_suite_scope_changed"})
        if expected and await _pending_video_requests(db, expected):
            await db.rollback()
            raise RuntimeError("fixture_video_request_drain_busy")
    blockers = await ownership.seal(db, payload.suite_id, family="owned")
    await db.commit()
    if blockers:
        raise HTTPException(409, detail={"code": "fixture_run_unsettled"})
    providers = getattr(request.app.state, "fixture_backends", None)
    provider_ids = list(providers.runs) if providers is not None else []
    children = (
        await db.execute(
            select(FixtureRun.id, FixtureRun.scope_key)
            .where(
                FixtureRun.parent_run_id == payload.suite_id,
                or_(FixtureRun.state != "retired", FixtureRun.id.in_(provider_ids)),
            )
            .order_by(FixtureRun.id)
        )
    ).all()
    scopes = [
        _request_owned_scope(
            SeedOwnedRequest(
                namespace=namespace, suite_id=payload.suite_id, run_id=run_id
            )
        )
        for run_id, namespace in children
    ]
    drained_runs = set()
    for scope in scopes:
        blockers = await ownership.seal(db, scope.run_id, family="owned")
        await db.commit()
        if blockers:
            raise HTTPException(409, detail={"code": "fixture_run_unsettled"})
        run = await db.get(FixtureRun, scope.run_id, populate_existing=True)
        if run.state != "retired":
            await _retire_fixture_execution_work(
                db, scope, reader_wait_seconds=_SUITE_READER_WAIT_SECONDS
            )
            drained_runs.add(scope.run_id)
    for scope in scopes:
        await _cleanup_owned_e2e_fixtures(
            db,
            owned=scope,
            registry=getattr(request.app.state, "original_playback_registry", None),
            providers=providers,
            native_runtime=native_runtime,
            drained_runs=frozenset(drained_runs),
        )
    root = await ownership._lock_run(db, payload.suite_id)
    if root.state != "retired":
        if (
            root.state != "sealing"
            or await db.scalar(
                select(FixtureRun.id)
                .where(
                    FixtureRun.parent_run_id == root.id, FixtureRun.state != "retired"
                )
                .limit(1)
            )
            is not None
        ):
            raise HTTPException(409, detail={"code": "fixture_suite_unsettled"})
        root.state = "retired"
        root.retired_at = datetime.now(timezone.utc)
    await db.commit()
    return SeedCleanup()


class SeedOwned(BaseModel):
    admin_email: str
    annotator_email: str
    reviewer_email: str
    project_id: str
    task_ids: list[str]
    ml_backend_id: str


async def _register_fixture_original(db, item, payload: bytes):
    """Pin only bytes produced by this isolated fixture, never adopt old paths."""
    from app.services.dataset_assets import register_item_asset

    return await register_item_asset(
        db, item, sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload)
    )


def _raster_image_bytes(index: int, *, width: int = 64, height: int = 48) -> bytes:
    """Supported raster originals with portable, bundled-font fixture pixels."""
    import colorsys
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont

    red, green, blue = colorsys.hls_to_rgb((index * 48 % 360) / 360, 0.88, 0.30)
    fill = (round(red * 255), round(green * 255), round(blue * 255))
    image = Image.new("RGB", (width, height), fill)
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 4, width - 5, height - 5), outline="#64748b", width=1)
    label = f"E2E {index + 1}"
    font = ImageFont.load_default(size=9)
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
    draw.text(
        ((width - (right - left)) / 2 - left, (height - (bottom - top)) / 2 - top),
        label,
        fill="#334155",
        font=font,
    )
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass(frozen=True)
class _ImageWorkbenchFixture:
    """Identifiers used by the standard image-workbench fixture builder."""

    run_id: UUID
    admission_id: UUID
    admin_email: str
    annotator_email: str
    reviewer_email: str
    project_name: str
    batch_display_id: str
    dataset_display_id: str
    dataset_name: str
    task_display_prefix: str
    registry_name: str
    providers: Any
    image_canvas: Literal["default", "720p", "5k", "8k"]


async def _prepare_fixture_image_pyramid(
    db, ids, dataset_id, item_id, actor_id, dimensions
):
    """Keep the fixture admission until the real image owner completes this pyramid."""
    from app.db.models.fixture_run import FixtureNativeMediaWorkIntent
    from app.services.assets_images import image_gateway, image_policy
    from yoao_assets_images.contract import (
        ImageSourceSelector,
        PyramidWorkSpec,
        ReadImageSourceRequest,
        ReadImageSummariesRequest,
        ReadInternalImageWorkRequest,
        ScheduleImageMediaRequest,
    )

    intent = FixtureNativeMediaWorkIntent(
        operation_id=uuid4(),
        family="image",
        run_id=ids.run_id,
        admission_id=ids.admission_id,
        dataset_id=dataset_id,
        state="prepared",
    )
    db.add(intent)
    await db.commit()
    async with image_gateway() as owner:
        source = await owner.call(
            "read_image_source",
            ReadImageSourceRequest(actor_id=actor_id, dataset_item_id=item_id),
        )
        accepted = await owner.call(
            "schedule_image_media",
            ScheduleImageMediaRequest(
                operation_id=intent.operation_id,
                owner_kind="dataset_item",
                owner_id=item_id,
                spec=PyramidWorkSpec(
                    expected_source_snapshot=source.source_snapshot,
                    policy=image_policy(),
                ),
            ),
        )
    intent.job_id = accepted.work.job_id
    intent.generation = accepted.work.generation
    intent.state = "admitted"
    await db.commit()
    deadline = asyncio.get_running_loop().time() + 45
    while True:
        async with image_gateway() as owner:
            status = await owner.call(
                "read_internal_image_work",
                ReadInternalImageWorkRequest(job_id=intent.job_id),
            )
        if status.work.generation != intent.generation:
            raise RuntimeError("Fixture image pyramid generation changed")
        if status.work.status in {"failed", "canceled"}:
            raise RuntimeError(
                f"Fixture image pyramid {status.work.status}: {status.error_code}"
            )
        if status.work.status == "ready":
            async with image_gateway() as owner:
                summaries = await owner.call(
                    "read_image_summaries",
                    ReadImageSummariesRequest(
                        actor_id=actor_id,
                        selectors=[ImageSourceSelector(dataset_item_id=item_id)],
                        profile_version=image_policy().profile_version,
                    ),
                )
            if len(summaries.items) != 1:
                raise RuntimeError(
                    "Fixture image pyramid source is not uniquely resolved"
                )
            summary = summaries.items[0]
            if (
                summary.source.owner.id != item_id
                or summary.source.source_drifted
                or (summary.source.width, summary.source.height) != dimensions
            ):
                raise RuntimeError("Fixture image pyramid source changed")
            if summary.pyramid.status == "ready":
                if (
                    summary.pyramid.width,
                    summary.pyramid.height,
                ) != dimensions or summary.pyramid.generation is None:
                    raise RuntimeError("Fixture image pyramid identity changed")
                break
            if summary.pyramid.status == "failed":
                raise RuntimeError(
                    f"Fixture image pyramid failed: {summary.pyramid.error_code}"
                )
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("Fixture image pyramid owner work is not ready")
        await asyncio.sleep(1)
    intent.state = "settled"
    await db.commit()


async def _create_fixture_backend(db, providers, run_id, *, name, logical_key):
    """Publish and reconcile a provider owned by this fixture authority."""
    from app.services import fixture_run_ownership as ownership
    from app.services.ml_backend import MLBackendService

    backend_service = MLBackendService(db)
    provider = await providers.start(run_id)
    backend = await backend_service.create_registry(
        name=name,
        url=provider.url,
        is_interactive=True,
        auth_method="token",
        auth_token=provider.token,
        extra_params={"e2e_mock": True},
    )
    await provider.control("registry", str(backend.id))
    provider.registry_id = backend.id
    await ownership.reserve(
        db, run_id, "registry", backend.id, logical_key=logical_key, published=True
    )
    await db.commit()
    if not await backend_service.check_health(backend.id):
        raise RuntimeError("Fixture provider health observation failed")
    await db.commit()
    await _reconcile_fixture_backend(run_id, backend.id)
    return backend


async def _build_image_workbench_fixture(
    db: AsyncSession, ids: _ImageWorkbenchFixture
) -> SeedOwned:
    """Build the standard image workbench fixture for one owned namespace."""
    from app.services.identity_e2e_fixtures import IdentityE2EFixtures
    from app.db.models.task import Task
    from app.services import fixture_run_ownership as ownership

    identity = IdentityE2EFixtures(db)
    admin = await identity.create_user("super_admin", ids.admin_email, "E2E Admin")
    annotator = await identity.create_user(
        "employee", ids.annotator_email, "E2E Annotator"
    )
    reviewer = await identity.create_user(
        "employee", ids.reviewer_email, "E2E Reviewer"
    )
    for logical_key, user in (
        ("workbench-admin", admin),
        ("workbench-annotator", annotator),
        ("workbench-reviewer", reviewer),
    ):
        await ownership.reserve(
            db, ids.run_id, "user", user.id, logical_key=logical_key, published=True
        )
    project = await create_fixture_project(db, owner_id=admin.id, name=ids.project_name)
    await ownership.reserve(
        db, ids.run_id, "project", project.id, logical_key="workbench", published=True
    )
    register_seed_project_projection(db, project.id)
    bbox_binding = project.tool_bindings.get("bbox", {})
    project.tool_bindings = {
        **project.tool_bindings,
        "region": {
            "enabled": True,
            "classes": list(bbox_binding.get("classes", [])),
            "attribute_schema": {"fields": []},
        },
    }

    from app.db.models.task_batch import TaskBatch

    membership_writes = SeedMembershipWrites(db)
    fixture_writes = LabelingE2EFixtureWrites(db)
    for user, role in ((annotator, "annotator"), (reviewer, "reviewer")):
        membership_writes.add(
            project_id=project.id, user_id=user.id, role=role, assigned_by=admin.id
        )
    batch = TaskBatch(
        project_id=project.id,
        display_id=ids.batch_display_id,
        name="E2E Default Batch",
        status="annotating",
        annotator_id=annotator.id,
        reviewer_id=reviewer.id,
        assigned_user_ids=[str(annotator.id)],
        created_by=admin.id,
    )
    await membership_writes.flush()
    batch = (await fixture_writes.insert_batches([batch]))[0]

    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset

    image_dataset = Dataset(
        id=uuid4(),
        display_id=ids.dataset_display_id,
        name=ids.dataset_name,
        data_type="image",
        file_count=5,
        created_by=admin.id,
    )
    await ownership.reserve_original_dataset(
        db, ids.run_id, image_dataset.id, logical_key="workbench"
    )
    db.add(image_dataset)
    await db.flush()
    db.add(ProjectDataset(project_id=project.id, dataset_id=image_dataset.id))
    await ownership.publish_resource(db, ids.run_id, "dataset", image_dataset.id)
    await db.commit()

    from datetime import datetime, timedelta, timezone

    tasks = []
    task_created_at = datetime.now(timezone.utc)
    for index in range(5):
        filename = f"task-{index + 1}.png"
        item_id = uuid4()
        await ownership.reserve(
            db, ids.run_id, "item", item_id, logical_key=f"workbench-{index + 1}"
        )
        await db.commit()
        width, height = (
            {
                "default": (64, 48),
                "720p": (1280, 720),
                "5k": (5120, 2880),
                "8k": (8192, 8192),
            }[ids.image_canvas]
            if index == 0
            else (64, 48)
        )
        raster = _raster_image_bytes(index, width=width, height=height)
        item = DatasetItem(
            id=item_id,
            dataset_id=image_dataset.id,
            file_name=filename,
            file_path="",
            file_type="image",
            file_size=len(raster),
            width=width,
            height=height,
        )
        allocation, original = await ownership.create_original(
            db,
            run_id=ids.run_id,
            admission_id=ids.admission_id,
            item=item,
            logical_path=filename,
            payload=raster,
            content_type="image/png",
            requested_by=admin.id,
        )
        await ownership.attach_original(db, item, allocation, original)
        await ownership.publish_resource(db, ids.run_id, "item", item_id)
        t = Task(
            project_id=project.id,
            display_id=f"{ids.task_display_prefix}{index + 1:06d}",
            status="pending",
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="image",
            batch_id=batch.id,
            dataset_item_id=item.id,
            created_at=task_created_at + timedelta(microseconds=index),
            annotation_contributor_ids=[],
        )
        tasks.append(t)
    tasks = await fixture_writes.insert_tasks(tasks)
    for index, task in enumerate(tasks):
        await ownership.reserve(
            db,
            ids.run_id,
            "task",
            task.id,
            logical_key=f"workbench-{index + 1}",
            published=True,
        )
    await fixture_writes.patch_batch(batch, total_tasks=len(tasks))

    from app.services.inference_topology_data import InferenceTopologyData
    from app.services.ml_backend import MLBackendService

    inference_data = InferenceTopologyData(db)
    backend_service = MLBackendService(db)
    mock_backend = await _create_fixture_backend(
        db,
        ids.providers,
        ids.run_id,
        name=ids.registry_name,
        logical_key="workbench",
    )
    await backend_service.set_enabled(project.id, mock_backend.id, True)
    mock_pool = await inference_data.pool_for_registry(mock_backend.id)
    if mock_pool is None:
        raise RuntimeError("Workbench fixture backend has no service pool")
    register_seed_project_projection(db, project.id)
    project.ai_enabled = True
    project.ml_backend_pool_id = mock_pool.id
    await db.commit()
    if ids.image_canvas == "8k":
        await _prepare_fixture_image_pyramid(
            db, ids, image_dataset.id, tasks[0].dataset_item_id, admin.id, (8192, 8192)
        )

    return SeedOwned(
        admin_email=admin.email,
        annotator_email=annotator.email,
        reviewer_email=reviewer.email,
        project_id=str(project.id),
        task_ids=[str(t.id) for t in tasks],
        ml_backend_id=str(mock_backend.id),
    )


class SeedOwnedRequest(BaseModel):
    namespace: str
    suite_id: UUID
    run_id: UUID


class SeedWorkbenchRequest(SeedOwnedRequest):
    image_canvas: Literal["default", "720p", "5k", "8k"] = "default"


def _request_owned_scope(payload: SeedOwnedRequest) -> _OwnedFixtureScope:
    namespace = _parse_owned_namespace(payload.namespace).namespace
    token = hashlib.sha256(payload.run_id.bytes + namespace.encode()).hexdigest()[:12]
    return _OwnedFixtureScope(namespace=token, run_id=payload.run_id)


async def _run_owned_seed(
    db, payload: SeedOwnedRequest, kind: str, build, request: Request
):
    from app.services import fixture_run_ownership as ownership
    from app.services.storage_access import failure_unsettled, finish

    native = getattr(request.app.state, "fixture_native_runtime", None)
    if native is not None and native.suite_id != payload.suite_id:
        raise HTTPException(409, detail={"code": "fixture_native_suite_changed"})

    scope = _request_owned_scope(payload)
    await ownership.start_owned_scope(
        db,
        suite_id=payload.suite_id,
        run_id=payload.run_id,
        namespace=payload.namespace,
    )
    admission_id = await ownership.admit(db, payload.run_id, kind)
    await db.commit()
    try:
        if native is not None:
            await native.prepare(payload.run_id, admission_id)
        result = await build(scope, admission_id)
    except BaseException as failure:
        unsettled = failure_unsettled(failure)

        async def settle_failed() -> None:
            await db.rollback()
            if not unsettled:
                await ownership.settle(db, payload.run_id, admission_id)
                await db.commit()

        try:
            await finish(asyncio.create_task(settle_failed()))
        except BaseException as settlement_failure:
            raise builtins.BaseExceptionGroup(
                "Owned fixture creation and admission settlement failed",
                [failure, settlement_failure],
            ) from None
        raise
    await ownership.settle(db, payload.run_id, admission_id)
    await db.commit()
    return result


async def _run_existing_fixture_seed(db, run_id: UUID, kind: str, build):
    """Admit one mutation of already published run resources before physical I/O."""
    from app.services import fixture_run_ownership as ownership
    from app.services.storage_access import failure_unsettled, finish

    await ownership.lock_original_scope(db, run_id)
    admission_id = await ownership.admit(db, run_id, kind)
    await db.commit()
    scope = _OwnedFixtureScope(namespace="", run_id=run_id)
    try:
        result = await build(scope, admission_id)
    except BaseException as failure:
        unsettled = failure_unsettled(failure)

        async def settle_failed() -> None:
            await db.rollback()
            if not unsettled:
                await ownership.settle(db, run_id, admission_id)
                await db.commit()

        try:
            await finish(asyncio.create_task(settle_failed()))
        except BaseException as settlement_failure:
            raise builtins.BaseExceptionGroup(
                "Fixture mutation and admission settlement failed",
                [failure, settlement_failure],
            ) from None
        raise
    await ownership.settle(db, run_id, admission_id)
    await db.commit()
    return result


async def _reconcile_fixture_backend(run_id: UUID, registry_id: UUID) -> None:
    """Resume only this run's control, rotating after each completed epoch."""
    from app.services.ml_routing.authority import RoutingAuthority

    authority = RoutingAuthority()
    snapshot = await authority.runtime_snapshot([registry_id])
    if len(snapshot.registries) != 1:
        raise HTTPException(409, detail={"code": "fixture_backend_scope_changed"})
    state = snapshot.registries[0]
    control_id = uuid5(run_id, f"fixture-backend:{registry_id}:{state.authority_epoch}")
    if state.control_id is not None and state.control_id != control_id:
        raise HTTPException(409, detail={"code": "fixture_backend_control_busy"})
    result = await authority.execute(
        "reconcile",
        {"registry_id": str(registry_id), "control_id": str(control_id)},
    )
    if result.phase != "completed":
        raise HTTPException(409, detail={"code": "fixture_backend_not_quiescent"})


@router.post("/seed/owned-prepare", include_in_schema=False)
async def prepare_owned_native(
    payload: SeedOwnedRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    """Prepare this exact owned run's native processes before the test body."""
    if getattr(request.app.state, "fixture_native_runtime", None) is None:
        raise HTTPException(409, detail={"code": "fixture_native_suite_required"})

    async def prepared(_scope, _admission_id):
        return {"ok": True}

    return await _run_owned_seed(db, payload, "native_prepare", prepared, request)


@router.post(
    "/seed/owned",
    response_model=SeedOwned,
    status_code=200,
    include_in_schema=False,
)
async def seed_owned(
    payload: SeedWorkbenchRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> SeedOwned:
    """Create one independently identified workbench fixture."""

    async def build(scope, admission_id):
        return await _build_image_workbench_fixture(
            db,
            _ImageWorkbenchFixture(
                run_id=payload.run_id,
                admission_id=admission_id,
                admin_email=scope.admin_email,
                annotator_email=scope.annotator_email,
                reviewer_email=scope.reviewer_email,
                project_name=scope.project_name,
                batch_display_id=scope.batch_display_id,
                dataset_display_id=scope.dataset_display_id,
                dataset_name=f"E2E Owned {scope.namespace} Image Dataset",
                task_display_prefix=scope.task_display_prefix,
                registry_name=scope.registry_name,
                providers=request.app.state.fixture_backends,
                image_canvas=payload.image_canvas,
            ),
        )

    return await _run_owned_seed(db, payload, "owned_seed", build, request)


class FixtureBackendControlRequest(SeedOwnedRequest):
    registry_id: UUID
    action: Literal[
        "attach", "detach", "hold", "release", "fail_all", "fail_next", "requests"
    ]
    value: bool | int | None = None


@router.post("/seed/provider-control", include_in_schema=False)
async def fixture_backend_control(
    payload: FixtureBackendControlRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from app.db.models.fixture_run import FixtureRun

    run = await db.get(FixtureRun, payload.run_id, populate_existing=True)
    if (
        run is None
        or run.family != "owned"
        or run.parent_run_id != payload.suite_id
        or run.scope_key != payload.namespace
        or run.state == "retired"
        or payload.registry_id
        not in await _owned_resource_ids(db, payload.run_id, "registry")
    ):
        raise HTTPException(404, detail={"code": "fixture_backend_not_found"})
    provider = request.app.state.fixture_backends.runs.get(payload.run_id)
    if provider is None or provider.registry_id != payload.registry_id:
        raise HTTPException(409, detail={"code": "fixture_backend_owner_unavailable"})
    return await provider.control(payload.action, payload.value)


@router.post(
    "/seed/owned-cleanup",
    response_model=SeedCleanup,
    status_code=200,
    include_in_schema=False,
)
async def seed_owned_cleanup(
    payload: SeedOwnedRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> SeedCleanup:
    """Delete exactly the named namespace's fixture; idempotent and exact.

    Only the namespace's own users, project, batch, dataset, tasks, media
    objects and mock backend rows are removed. Shared fixtures, neighbouring
    namespaces and ordinary data are never matched, and residuals fail the
    request instead of being swallowed.
    """
    from app.db.models.fixture_run import FixtureRun

    scope = _request_owned_scope(payload)
    run = await db.get(FixtureRun, payload.run_id, populate_existing=True)
    if (
        run is None
        or run.family != "owned"
        or run.parent_run_id != payload.suite_id
        or run.scope_key != payload.namespace
    ):
        raise HTTPException(404, detail={"code": "fixture_run_not_found"})
    await _cleanup_owned_e2e_fixtures(
        db,
        owned=scope,
        registry=getattr(request.app.state, "original_playback_registry", None),
        providers=getattr(request.app.state, "fixture_backends", None),
        native_runtime=getattr(request.app.state, "fixture_native_runtime", None),
    )
    await db.commit()
    return SeedCleanup()


class FixtureInvitationIntentRequest(BaseModel):
    model_config = {"extra": "forbid"}

    run_id: UUID
    intent_id: UUID
    project_id: UUID
    inviter_id: UUID
    email: str


class FixtureInvitationFinishRequest(BaseModel):
    model_config = {"extra": "forbid"}

    run_id: UUID
    intent_id: UUID


@router.post("/seed/invitation-intent", include_in_schema=False)
async def prepare_fixture_invitation(
    payload: FixtureInvitationIntentRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    """Declare one browser invitation before Identity creates either real owner."""
    from app.db.models.fixture_run import FixtureRunResource
    from app.services import fixture_run_ownership as ownership

    run, root = await ownership.lock_original_scope(db, payload.run_id)
    if run.parent_run_id == root.id and root.family == run.family == "owned":
        scope = _request_owned_scope(
            SeedOwnedRequest(namespace=run.scope_key, suite_id=root.id, run_id=run.id)
        )
        valid_email = payload.email == scope.takeover_email
        project_key, inviter_key = "workbench", "workbench-admin"
    else:
        raise HTTPException(404, detail={"code": "fixture_run_not_found"})
    if not valid_email:
        raise HTTPException(422, detail={"code": "fixture_invitation_email_invalid"})
    project = await db.get(FixtureRunResource, (run.id, "project", payload.project_id))
    inviter = await db.get(FixtureRunResource, (run.id, "user", payload.inviter_id))
    if (
        project is None
        or project.state != "published"
        or project.logical_key != project_key
        or inviter is None
        or inviter.state != "published"
        or inviter.logical_key != inviter_key
    ):
        raise HTTPException(409, detail={"code": "fixture_invitation_scope_changed"})
    occupied = await db.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM public.users WHERE email=:email) "
            "OR EXISTS(SELECT 1 FROM public.user_invitations WHERE email=:email)"
        ),
        {"email": payload.email},
    )
    if occupied:
        raise HTTPException(409, detail={"code": "fixture_invitation_email_in_use"})
    admission_id = await ownership.admit(db, run.id, "fixture_invitation")
    await db.execute(
        text(
            "INSERT INTO yoao_fixture_v1.fixture_invitation_intents "
            "(id,run_id,admission_id,project_id,inviter_id,email) "
            "VALUES (:id,:run,:admission,:project,:inviter,:email)"
        ),
        {
            "id": payload.intent_id,
            "run": run.id,
            "admission": admission_id,
            "project": payload.project_id,
            "inviter": payload.inviter_id,
            "email": payload.email,
        },
    )
    await db.commit()
    return {"intent_id": str(payload.intent_id)}


@router.post("/seed/invitation-finish", include_in_schema=False)
async def finish_fixture_invitation(
    payload: FixtureInvitationFinishRequest, db: AsyncSession = Depends(get_db)
) -> SeedCleanup:
    """Fence the actual invitation transaction before settling its admission."""
    from app.services import fixture_run_ownership as ownership

    initial = (
        await db.execute(
            text(
                "SELECT invitation_id FROM yoao_fixture_v1.fixture_invitation_intents "
                "WHERE id=:id AND run_id=:run"
            ),
            {"id": payload.intent_id, "run": payload.run_id},
        )
    ).one_or_none()
    if initial is None:
        raise HTTPException(404, detail={"code": "fixture_invitation_intent_missing"})
    if initial.invitation_id is not None:
        await db.execute(
            text("SELECT id FROM public.user_invitations WHERE id=:id FOR UPDATE"),
            {"id": initial.invitation_id},
        )
    run = await ownership._lock_run(db, payload.run_id)
    if not (
        (run.family == "screenshot" and run.parent_run_id is None)
        or (run.family == "owned" and run.parent_run_id is not None)
    ):
        raise HTTPException(404, detail={"code": "fixture_run_not_found"})
    intent = (
        await db.execute(
            text(
                "SELECT admission_id,invitation_id,state FROM yoao_fixture_v1.fixture_invitation_intents "
                "WHERE id=:id AND run_id=:run FOR UPDATE"
            ),
            {"id": payload.intent_id, "run": payload.run_id},
        )
    ).one()
    if intent.invitation_id != initial.invitation_id:
        raise HTTPException(409, detail={"code": "fixture_invitation_scope_busy"})
    if intent.state == "settled":
        return SeedCleanup()
    await ownership.settle(db, payload.run_id, intent.admission_id)
    await db.execute(
        text(
            "UPDATE yoao_fixture_v1.fixture_invitation_intents SET state='settled' "
            "WHERE id=:id AND run_id=:run"
        ),
        {"id": payload.intent_id, "run": payload.run_id},
    )
    await db.commit()
    return SeedCleanup()


@router.post(
    "/seed/screenshot-cleanup",
    response_model=SeedCleanup,
    status_code=200,
    include_in_schema=False,
)
async def seed_screenshot_cleanup(
    payload: SeedScreenshotCleanupRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SeedCleanup:
    """Reclaim one old screenshot root without opening new native participants."""
    from app.db.models.fixture_run import FixtureRun

    await _require_e2e_seed_database(db)
    run = await db.get(FixtureRun, payload.run_id, populate_existing=True)
    if run is None or run.family != "screenshot" or run.parent_run_id is not None:
        raise HTTPException(404, detail={"code": "fixture_run_not_found"})
    scope = _OwnedFixtureScope(namespace=run.id.hex[:12], run_id=run.id)
    await _cleanup_owned_e2e_fixtures(
        db,
        scope,
        family="screenshot",
        registry=getattr(request.app.state, "original_playback_registry", None),
        providers=getattr(request.app.state, "fixture_backends", None),
    )
    return SeedCleanup()


def _project_role_svg_bytes(index: int) -> bytes:
    """Deterministic tiny image used by the project-roles acceptance fixture."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="48" '
        'viewBox="0 0 64 48">'
        f'<rect width="64" height="48" fill="hsl({index * 37} 32% 88%)"/>'
        '<rect x="4" y="4" width="56" height="40" rx="3" fill="none" '
        'stroke="#64748b" stroke-width="1"/>'
        f'<text x="32" y="27" text-anchor="middle" font-size="9" '
        f'fill="#334155">E2E {index + 1}</text></svg>'
    ).encode("utf-8")


async def _build_project_roles_image_project(
    db: AsyncSession,
    *,
    run_id: UUID,
    admission_id: UUID,
    key: str,
    owner: Any,
    project_display_id: str,
    dataset_display_id: str,
    project_name: str,
    task_display_prefix: str,
    task_count: int,
) -> tuple[Any, list[Any]]:
    """Create one image project with deterministic SVG items and tasks.

    Tasks explicitly carry a *known-empty* ``annotation_contributor_ids`` list:
    the fixture is created by the recording binary, so a later real
    annotation+submit by the assigned employee can freeze complete review
    evidence instead of leaving the round permanently unknown.  The existing
    ``seed.advance_task`` bypass is deliberately not used here.
    """
    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.db.models.task import Task
    from yoao_workspace.configuration_policy import coalesce_legacy_into_tool_bindings
    from app.services import fixture_run_ownership as ownership

    kw: dict = {"classes": ["car", "person"]}
    coalesce_legacy_into_tool_bindings(kw, None, "image-det")
    project = await create_fixture_project(
        db,
        display_id=project_display_id,
        name=project_name,
        type_label="图像目标检测",
        type_key="image-det",
        owner_id=owner.id,
        tool_bindings=kw["tool_bindings"],
    )
    await ownership.reserve(
        db, run_id, "project", project.id, logical_key=f"roles-{key}", published=True
    )

    dataset = Dataset(
        id=uuid4(),
        display_id=dataset_display_id,
        name=f"E2E Roles {key.upper()} Dataset",
        data_type="image",
        file_count=task_count,
        created_by=owner.id,
    )
    await ownership.reserve_original_dataset(
        db, run_id, dataset.id, logical_key=f"roles-{key}"
    )
    db.add(dataset)
    await db.flush()
    db.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
    await ownership.publish_resource(db, run_id, "dataset", dataset.id)
    await db.commit()

    tasks: list[Task] = []
    for index in range(task_count):
        filename = f"{key.lower()}-task-{index + 1}.svg"
        item_id = uuid4()
        await ownership.reserve(
            db, run_id, "item", item_id, logical_key=f"roles-{key}-{index + 1}"
        )
        await db.commit()
        svg = _project_role_svg_bytes(index)
        item = DatasetItem(
            id=item_id,
            dataset_id=dataset.id,
            file_name=filename,
            file_path="",
            file_type="image",
            file_size=len(svg),
            width=64,
            height=48,
        )
        allocation, original = await ownership.create_original(
            db,
            run_id=run_id,
            admission_id=admission_id,
            item=item,
            logical_path=filename,
            payload=svg,
            content_type="image/svg+xml",
            requested_by=owner.id,
        )
        await ownership.attach_original(db, item, allocation, original)
        await ownership.publish_resource(db, run_id, "item", item_id)
        task = Task(
            display_id=f"{task_display_prefix}{index + 1:03d}",
            project_id=project.id,
            status="pending",
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="image",
            dataset_item_id=item.id,
            annotation_contributor_ids=[],
        )
        tasks.append(task)
    tasks = await LabelingE2EFixtureWrites(db).insert_tasks(tasks)
    for index, task in enumerate(tasks):
        await ownership.reserve(
            db,
            run_id,
            "task",
            task.id,
            logical_key=f"roles-{key}-{index + 1}",
            published=True,
        )
    return project, tasks


class ProjectRolesSeedUser(BaseModel):
    id: str
    email: str
    platform_role: str


class ProjectRolesSeedProject(BaseModel):
    project_id: str
    display_id: str
    owner_email: str
    project_role: str | None
    membership_id: str | None
    batch_id: str
    task_ids: list[str]


class ProjectRolesSeed(BaseModel):
    """Accounts/projects for the two-project browser acceptance fixture.

    ``employee`` is an annotator in ``a`` and a reviewer in ``b``, has no
    membership in ``c`` (whose task is nevertheless assigned to them), and is an
    idle annotator in ``d`` so the test can revoke that membership cleanly.
    ``solo`` is an employee with no membership at all.
    """

    employee_email: str
    peer_email: str
    owner_email_a: str
    owner_email_b: str
    owner_email_c: str
    viewer_email: str
    viewer_unassigned_email: str
    spare_email: str
    solo_email: str
    users: dict[str, ProjectRolesSeedUser]
    projects: dict[str, ProjectRolesSeedProject]
    annotation_task_id: str
    review_task_id: str
    c_assigned_task_id: str
    open_pool_task_id: str


async def _build_project_roles(
    db: AsyncSession, scope: _OwnedFixtureScope, admission_id: UUID
) -> ProjectRolesSeed:
    """Build the deterministic multi-project employee-role acceptance fixture.

    The required namespace scopes accounts, projects, datasets and objects;
    owned cleanup converges only that namespace. Review work is intentionally
    left for the spec to submit through the real API so contributor evidence
    is complete.
    """
    from app.db.models.task_batch import TaskBatch
    from app.services.identity_e2e_fixtures import IdentityE2EFixtures
    from app.services import fixture_run_ownership as ownership

    def user_email(local: str) -> str:
        return f"{local}-{scope.namespace}@e2e.test"

    def project_display(key: str) -> str:
        return scope.pr_project_display_ids[key.lower()]

    def project_name(key: str) -> str:
        return f"E2E Project Roles {key.upper()} {scope.namespace}"

    def dataset_display(key: str) -> str:
        return scope.pr_dataset_display_ids[key.lower()]

    def batch_display(key: str) -> str:
        return f"B-E2E-PR-{key.upper()}-{scope.namespace}"

    def task_display_prefix(key: str) -> str:
        return scope.pr_task_display_prefix[key.lower()]

    identity = IdentityE2EFixtures(db)
    owner_a = await identity.create_user(
        "project_admin", user_email("owner-a"), "E2E Owner A"
    )
    owner_b = await identity.create_user(
        "project_admin", user_email("owner-b"), "E2E Owner B"
    )
    owner_c = await identity.create_user(
        "project_admin", user_email("owner-c"), "E2E Owner C"
    )
    employee = await identity.create_user(
        "employee", user_email("employee"), "E2E Employee"
    )
    peer = await identity.create_user("employee", user_email("peer"), "E2E Peer")
    spare = await identity.create_user("employee", user_email("spare"), "E2E Spare")
    viewer = await identity.create_user("viewer", user_email("viewer"), "E2E Viewer")
    viewer_unassigned = await identity.create_user(
        "viewer", user_email("viewer-unassigned"), "E2E Viewer Unassigned"
    )
    solo = await identity.create_user("employee", user_email("solo"), "E2E Solo")
    cleanup_admin = await identity.create_user(
        "super_admin", user_email("cleanup-admin"), "E2E Cleanup Administrator"
    )
    for key, user in (
        ("owner-a", owner_a),
        ("owner-b", owner_b),
        ("owner-c", owner_c),
        ("employee", employee),
        ("peer", peer),
        ("spare", spare),
        ("viewer", viewer),
        ("viewer-unassigned", viewer_unassigned),
        ("solo", solo),
        ("cleanup-admin", cleanup_admin),
    ):
        await ownership.reserve(
            db,
            scope.run_id,
            "user",
            user.id,
            logical_key=f"roles-{key}",
            published=True,
        )

    project_a, tasks_a = await _build_project_roles_image_project(
        db,
        run_id=scope.run_id,
        admission_id=admission_id,
        key="a",
        owner=owner_a,
        project_display_id=project_display("a"),
        dataset_display_id=dataset_display("a"),
        project_name=project_name("a"),
        task_display_prefix=task_display_prefix("a"),
        task_count=1,
    )
    project_b, tasks_b = await _build_project_roles_image_project(
        db,
        run_id=scope.run_id,
        admission_id=admission_id,
        key="b",
        owner=owner_b,
        project_display_id=project_display("b"),
        dataset_display_id=dataset_display("b"),
        project_name=project_name("b"),
        task_display_prefix=task_display_prefix("b"),
        task_count=1,
    )
    project_c, tasks_c = await _build_project_roles_image_project(
        db,
        run_id=scope.run_id,
        admission_id=admission_id,
        key="c",
        owner=owner_c,
        project_display_id=project_display("c"),
        dataset_display_id=dataset_display("c"),
        project_name=project_name("c"),
        task_display_prefix=task_display_prefix("c"),
        task_count=1,
    )
    project_d, tasks_d = await _build_project_roles_image_project(
        db,
        run_id=scope.run_id,
        admission_id=admission_id,
        key="d",
        owner=owner_a,
        project_display_id=project_display("d"),
        dataset_display_id=dataset_display("d"),
        project_name=project_name("d"),
        task_display_prefix=task_display_prefix("d"),
        task_count=1,
    )

    batch_a = TaskBatch(
        project_id=project_a.id,
        display_id=batch_display("a"),
        name="E2E Roles A Batch",
        status="annotating",
        annotator_id=employee.id,
        reviewer_id=peer.id,
        assigned_user_ids=[str(employee.id)],
        created_by=owner_a.id,
        total_tasks=1,
    )
    batch_b = TaskBatch(
        project_id=project_b.id,
        display_id=batch_display("b"),
        name="E2E Roles B Batch",
        status="annotating",
        annotator_id=peer.id,
        reviewer_id=employee.id,
        assigned_user_ids=[str(peer.id)],
        created_by=owner_b.id,
        total_tasks=1,
    )
    batch_c = TaskBatch(
        project_id=project_c.id,
        display_id=batch_display("c"),
        name="E2E Roles C Batch",
        status="annotating",
        annotator_id=peer.id,
        reviewer_id=None,
        assigned_user_ids=[str(peer.id)],
        created_by=owner_c.id,
        total_tasks=1,
    )
    batch_d = TaskBatch(
        project_id=project_d.id,
        display_id=batch_display("d"),
        name="E2E Roles D Batch",
        status="annotating",
        annotator_id=None,
        reviewer_id=None,
        assigned_user_ids=[],
        created_by=owner_a.id,
        total_tasks=1,
    )
    fixture_writes = LabelingE2EFixtureWrites(db)
    batch_a = (await fixture_writes.insert_batches([batch_a]))[0]
    batch_b = (await fixture_writes.insert_batches([batch_b]))[0]
    batch_c = (await fixture_writes.insert_batches([batch_c]))[0]
    batch_d = (await fixture_writes.insert_batches([batch_d]))[0]

    task_a_add = tasks_a[0]
    task_b_review = tasks_b[0]
    task_c_assigned = tasks_c[0]
    task_d_open = tasks_d[0]
    for task, project, batch, assignee in (
        (task_a_add, project_a, batch_a, employee),
        (task_b_review, project_b, batch_b, peer),
        (task_c_assigned, project_c, batch_c, employee),
    ):
        await fixture_writes.patch_tasks(
            [
                TaskFixturePatch(
                    id=task.id,
                    project_id=project.id,
                    batch_id=batch.id,
                    assignee_id=assignee.id,
                )
            ]
        )
    await fixture_writes.patch_tasks(
        [
            TaskFixturePatch(
                id=task_d_open.id,
                project_id=project_d.id,
                batch_id=batch_d.id,
            )
        ]
    )

    membership_writes = SeedMembershipWrites(db)

    def add_member(project: Any, user: Any, role: str):
        return membership_writes.add(
            project_id=project.id,
            user_id=user.id,
            role=role,
            assigned_by=project.owner_id,
        )

    memberships = {
        "employee_a": add_member(project_a, employee, "annotator"),
        "peer_a": add_member(project_a, peer, "reviewer"),
        "spare_a": add_member(project_a, spare, "viewer"),
        "viewer_a": add_member(project_a, viewer, "viewer"),
        "employee_b": add_member(project_b, employee, "reviewer"),
        "peer_b": add_member(project_b, peer, "annotator"),
        "peer_c": add_member(project_c, peer, "annotator"),
        "employee_d": add_member(project_d, employee, "annotator"),
        "peer_d": add_member(project_d, peer, "reviewer"),
    }
    await membership_writes.flush()
    await db.commit()

    def project_payload(
        project: Any,
        owner: Any,
        batch: Any,
        tasks: list[Any],
        membership: Any | None,
        project_role: str | None,
    ) -> ProjectRolesSeedProject:
        return ProjectRolesSeedProject(
            project_id=str(project.id),
            display_id=project.display_id,
            owner_email=owner.email,
            project_role=project_role,
            membership_id=str(membership.id) if membership else None,
            batch_id=str(batch.id),
            task_ids=[str(task.id) for task in tasks],
        )

    def user_payload(user: Any) -> ProjectRolesSeedUser:
        return ProjectRolesSeedUser(
            id=str(user.id), email=user.email, platform_role=user.role
        )

    return ProjectRolesSeed(
        employee_email=employee.email,
        peer_email=peer.email,
        owner_email_a=owner_a.email,
        owner_email_b=owner_b.email,
        owner_email_c=owner_c.email,
        viewer_email=viewer.email,
        viewer_unassigned_email=viewer_unassigned.email,
        spare_email=spare.email,
        solo_email=solo.email,
        users={
            "employee": user_payload(employee),
            "peer": user_payload(peer),
            "owner_a": user_payload(owner_a),
            "owner_b": user_payload(owner_b),
            "owner_c": user_payload(owner_c),
            "spare": user_payload(spare),
            "viewer": user_payload(viewer),
            "viewer_unassigned": user_payload(viewer_unassigned),
            "solo": user_payload(solo),
        },
        projects={
            "a": project_payload(
                project_a,
                owner_a,
                batch_a,
                tasks_a,
                memberships["employee_a"],
                "annotator",
            ),
            "b": project_payload(
                project_b,
                owner_b,
                batch_b,
                tasks_b,
                memberships["employee_b"],
                "reviewer",
            ),
            "c": project_payload(project_c, owner_c, batch_c, tasks_c, None, None),
            "d": project_payload(
                project_d,
                owner_a,
                batch_d,
                tasks_d,
                memberships["employee_d"],
                "annotator",
            ),
        },
        annotation_task_id=str(task_a_add.id),
        review_task_id=str(task_b_review.id),
        c_assigned_task_id=str(task_c_assigned.id),
        open_pool_task_id=str(task_d_open.id),
    )


@router.post(
    "/seed/project-roles",
    response_model=ProjectRolesSeed,
    status_code=200,
    include_in_schema=False,
)
async def seed_project_roles(
    request: Request, payload: SeedOwnedRequest, db: AsyncSession = Depends(get_db)
) -> ProjectRolesSeed:
    async def build(scope, admission_id):
        return await _build_project_roles(db, scope, admission_id)

    return await _run_owned_seed(db, payload, "project_roles_seed", build, request)


class SeedFilteringRequest(SeedOwnedRequest):
    video_ready: bool = False


@router.post(
    "/seed/filtering",
    response_model=FilteringSeedManifest,
    status_code=200,
    include_in_schema=False,
)
async def seed_filtering(
    request: Request,
    payload: SeedFilteringRequest,
    db: AsyncSession = Depends(get_db),
) -> FilteringSeedManifest:
    """Build the filtering acceptance fixture inside one owned namespace."""

    runtime = getattr(request.app.state, "fixture_native_runtime", None)

    async def stage_video_outputs(item, kind, specs):
        return await _stage_fixture_video_outputs(
            db, item, kind, specs, runtime=runtime
        )

    async def build(scope, admission_id):
        return await build_filtering_seed(
            db,
            scope.namespace,
            run_id=payload.run_id,
            admission_id=admission_id,
            stage_video_outputs=stage_video_outputs if payload.video_ready else None,
        )

    return await _run_owned_seed(db, payload, "filtering_seed", build, request)


class SeedLidar(BaseModel):
    """点云 E2E fixture。

    造 1 个 lidar 项目 + 2 帧 point_cloud task；点云优先来自 nuScenes mini，
    每帧关联两张确定性标定相机图，覆盖冒烟、PSR / gizmo / 点掩膜和相机投影编辑。
    """

    lidar_project_id: str
    lidar_task_ids: list[str]
    lidar_fixture_source: Literal["nuscenes_mini", "nuscenes_profile"]
    lidar_point_count: int


def _binary_pcd_bytes(points: bytes, count: int) -> bytes:
    header = (
        "# .PCD v0.7 - Point Cloud Data file format\n"
        "VERSION 0.7\n"
        "FIELDS x y z\n"
        "SIZE 4 4 4\n"
        "TYPE F F F\n"
        "COUNT 1 1 1\n"
        f"WIDTH {count}\n"
        "HEIGHT 1\n"
        "VIEWPOINT 0 0 0 1 0 0 0\n"
        f"POINTS {count}\n"
        "DATA binary\n"
    )
    return header.encode("ascii") + points


def _nuscenes_lidar_to_pcd(bin_path: Path) -> tuple[bytes, int]:
    """nuScenes LIDAR_TOP 的 5×float32 点转为前端消费的 binary PCD xyz。"""
    raw = bin_path.read_bytes()
    point_stride = 5 * 4
    if not raw or len(raw) % point_stride:
        raise ValueError(f"invalid nuScenes lidar frame: {bin_path}")
    count = len(raw) // point_stride
    xyz = bytearray(count * 12)
    for index in range(count):
        x, y, z = struct.unpack_from("<fff", raw, index * point_stride)
        struct.pack_into("<fff", xyz, index * 12, x, y, z)
    return _binary_pcd_bytes(bytes(xyz), count), count


def _make_nuscenes_profile_pcd_bytes() -> tuple[bytes, int]:
    """无外部数据时生成 32 线、34,752 点的确定性 CI 兜底，并保留框内目标点。"""
    import math

    count = 32 * 1086
    target_points = 4096
    xyz = bytearray(count * 12)
    for index in range(count):
        if index < target_points:
            face = index % 6
            u = ((index // 6) % 32) / 31
            v = ((index // (6 * 32)) % 22) / 21
            x, y, z = -1 + 4 * u, -1 + 2 * v, 0.25 + 1.5 * v
            if face == 0:
                x = -1
            elif face == 1:
                x = 3
            elif face == 2:
                y = -1
            elif face == 3:
                y = 1
            elif face == 4:
                z = 0.25
            else:
                z = 1.75
        else:
            scan_index = index - target_points
            ring = scan_index % 32
            azimuth = (scan_index // 32) * (2 * math.pi / 959)
            elevation = math.radians(-30.67 + ring * (41.34 / 31))
            radius = 8 + ((scan_index * 37) % 5200) / 100
            planar = radius * math.cos(elevation)
            x = planar * math.cos(azimuth)
            y = planar * math.sin(azimuth)
            z = 1.8 + radius * math.sin(elevation)
        struct.pack_into("<fff", xyz, index * 12, x, y, z)
    return _binary_pcd_bytes(bytes(xyz), count), count


def _make_test_pcd_frames() -> tuple[
    list[bytes], Literal["nuscenes_mini", "nuscenes_profile"], int
]:
    """优先取 seed 脚本下载的两个真实 nuScenes mini 帧，CI 无数据时兜底。"""
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    cache_base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    lidar_frames = sorted(
        (
            cache_base
            / "ai-annotation-platform/nuscenes-mini/content/samples/LIDAR_TOP"
        ).glob("*.pcd.bin")
    )
    if lidar_frames:
        converted = [_nuscenes_lidar_to_pcd(path) for path in lidar_frames[:2]]
        if len(converted) == 1:
            converted.append(converted[0])
        return [payload for payload, _ in converted], "nuscenes_mini", converted[0][1]
    payload, count = _make_nuscenes_profile_pcd_bytes()
    return [payload, payload], "nuscenes_profile", count


class SeedLidarRequest(SeedOwnedRequest):
    pass


@router.post(
    "/seed/lidar",
    response_model=SeedLidar,
    include_in_schema=False,
)
async def seed_lidar(
    payload: SeedLidarRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> SeedLidar:
    """Build point-cloud and camera fixtures within one explicit run."""

    async def build(scope, admission_id):
        return await _build_owned_lidar(db, scope, admission_id)

    return await _run_owned_seed(db, payload, "lidar_seed", build, request)


async def _build_owned_lidar(db, scope, admission_id):
    from app.db.models.annotation import Annotation
    from app.db.models.dataset import Dataset, DatasetItem, Scene
    from app.db.models.task import Task
    from app.db.models.task_batch import TaskBatch
    from app.db.models.fixture_run import FixtureRunResource
    from app.services import fixture_run_ownership as ownership
    from app.services.task_dataset_link import link_items
    from app.services.identity_e2e_fixtures import IdentityE2EFixtures

    assert scope.run_id is not None

    identity = IdentityE2EFixtures(db)

    async def _user(role: str, email: str, name: str, logical_key: str):
        existing = await identity.find(email)
        if existing:
            if (
                await db.get(FixtureRunResource, (scope.run_id, "user", existing.id))
                is None
            ):
                raise HTTPException(status_code=409, detail="fixture_user_not_owned")
            return existing
        user = await identity.create_user(role, email, name)
        await ownership.reserve(
            db, scope.run_id, "user", user.id, logical_key=logical_key, published=True
        )
        return user

    admin = await _user("super_admin", scope.admin_email, "E2E Admin", "lidar-admin")
    annotator = await _user(
        "employee", scope.annotator_email, "E2E Annotator", "lidar-annotator"
    )

    suffix = secrets.token_hex(3)
    pcd_frames, pcd_source, pcd_point_count = _make_test_pcd_frames()
    camera_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480" '
        'viewBox="0 0 640 480">'
        '<rect width="640" height="480" fill="#25313a"/>'
        '<path d="M0 240h640M320 0v480" stroke="#51616d" stroke-width="2"/>'
        "</svg>"
    ).encode()

    tool_bindings = {
        "lidar_box_3d": {
            "classes": [
                {"name": "car", "order": 0},
                {"name": "pedestrian", "order": 1},
            ],
            "enabled": True,
            "attribute_schema": {"fields": []},
        },
        "point_mask_3d": {
            "classes": [{"name": "ground", "order": 0}],
            "enabled": True,
            "attribute_schema": {"fields": []},
        },
    }
    project = await create_fixture_project(
        db,
        display_id=f"P-E2E-LIDAR-{suffix}",
        name=scope.lidar_project_name,
        type_label="点云标注",
        type_key="lidar",
        data_type="lidar",
        owner_id=admin.id,
        tool_bindings=tool_bindings,
    )
    await ownership.reserve(
        db, scope.run_id, "project", project.id, logical_key="lidar", published=True
    )

    membership_writes = SeedMembershipWrites(db)
    membership_writes.add(
        project_id=project.id,
        user_id=annotator.id,
        role="annotator",
        assigned_by=admin.id,
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-E2E-LIDAR-{suffix}",
        name="E2E Lidar Batch",
        status="annotating",
        annotator_id=annotator.id,
        assigned_user_ids=[str(annotator.id)],
        created_by=admin.id,
    )
    await membership_writes.flush()
    fixture_writes = LabelingE2EFixtureWrites(db)
    batch = (await fixture_writes.insert_batches([batch]))[0]

    dataset = Dataset(
        id=uuid4(),
        display_id=scope.lidar_dataset_display_id,
        name=f"E2E Lidar Dataset {scope.namespace}",
        data_type="point_cloud",
        file_count=6,
        created_by=admin.id,
        metadata_={"axis_convention": "iso_8855"},
    )
    await ownership.reserve_original_dataset(
        db, scope.run_id, dataset.id, logical_key="lidar"
    )
    db.add(dataset)
    await db.flush()
    await ownership.publish_resource(db, scope.run_id, "dataset", dataset.id)

    scene = Scene(
        display_id=f"SCN-E2E-{suffix}",
        dataset_id=dataset.id,
        name="nuScenes mini scene-0061",
        source_format="nuscenes",
        source_metadata={"fixture_source": pcd_source},
        created_by=admin.id,
    )
    db.add(scene)
    await db.flush()
    await db.commit()

    calibration = {
        "extrinsic": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        "intrinsic": [100, 0, 320, 0, 100, 240, 0, 0, 1],
    }
    tasks = []
    for idx in range(2):
        items = []
        sources = (
            (
                f"e2e-lidar-{suffix}-{idx}.pcd",
                pcd_frames[idx],
                "point_cloud",
                "application/octet-stream",
            ),
            (
                f"e2e-lidar-{suffix}-{idx}-front.svg",
                camera_svg,
                "image",
                "image/svg+xml",
            ),
            (
                f"e2e-lidar-{suffix}-{idx}-side.svg",
                camera_svg,
                "image",
                "image/svg+xml",
            ),
        )
        for name, content, file_type, content_type in sources:
            item_id = uuid4()
            await ownership.reserve(db, scope.run_id, "item", item_id, logical_key=name)
            await db.commit()
            item = DatasetItem(
                id=item_id,
                dataset_id=dataset.id,
                file_name=name,
                file_path="",
                file_type=file_type,
                file_size=len(content),
                scene_id=scene.id,
                frame_index=idx,
                width=640 if file_type == "image" else None,
                height=480 if file_type == "image" else None,
                metadata_={"calibration": calibration} if file_type == "image" else {},
            )
            allocation, original = await ownership.create_original(
                db,
                run_id=scope.run_id,
                admission_id=admission_id,
                item=item,
                logical_path=name,
                payload=content,
                content_type=content_type,
                requested_by=admin.id,
            )
            await ownership.attach_original(db, item, allocation, original)
            await ownership.publish_resource(db, scope.run_id, "item", item_id)
            items.append(item)
        lidar_item, camera_item, camera_side_item = items

        t = Task(
            display_id=f"T-E2E-LIDAR-{suffix}-{idx}",
            project_id=project.id,
            dataset_item_id=lidar_item.id,
            sequence_order=idx,
            status="pending",
            file_name=f"e2e-lidar-{suffix}-{idx}.pcd",
            file_path=lidar_item.file_path,
            file_type="point_cloud",
            batch_id=batch.id,
        )
        t = (await fixture_writes.insert_tasks([t]))[0]
        await ownership.reserve(
            db, scope.run_id, "task", t.id, logical_key=f"lidar-{idx}", published=True
        )
        tasks.append(t)
        await link_items(
            db,
            t.id,
            [
                (lidar_item.id, "primary_lidar", None),
                (camera_item.id, "camera_front", "front"),
                (camera_side_item.id, "camera_side", "side"),
            ],
        )
    await fixture_writes.patch_batch(batch, total_tasks=len(tasks))

    from app.services.scene_track_domain import ensure_scene_track

    scene_track = await ensure_scene_track(
        db,
        project_id=project.id,
        scene_id=scene.id,
        track_id="trk_e2e_lidar_car",
        class_name="car",
        frames=set(range(len(tasks))),
        actor_id=annotator.id,
        interval_source="imported",
    )
    annotations = [
        Annotation(
            task_id=t.id,
            project_id=project.id,
            user_id=annotator.id,
            source="manual",
            annotation_type="box_3d",
            tool_unit_id="lidar_box_3d",
            class_name="car",
            track_id="trk_e2e_lidar_car",
            scene_track_id=scene_track.id,
            temporal_role="sample",
            geometry={
                "type": "box_3d",
                "center": [1.0, 0.0, 1.0],
                "size": [4.0, 2.0, 1.5],
                "rotation": [0.0, 0.0, 0.0],
                "convention_at_create": "iso_8855",
            },
        )
        for t in tasks
    ]
    await fixture_writes.insert_annotations(annotations)
    await db.commit()

    return SeedLidar(
        lidar_project_id=str(project.id),
        lidar_task_ids=[str(t.id) for t in tasks],
        lidar_fixture_source=pcd_source,
        lidar_point_count=pcd_point_count,
    )


class SeedLoginRequest(BaseModel):
    email: str


class SeedLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


@router.post(
    "/seed/login",
    response_model=SeedLoginResponse,
    include_in_schema=False,
)
async def seed_login(
    payload: SeedLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> SeedLoginResponse:
    """跳过密码验证发 JWT（仅 E2E 测试用）。"""
    from app.services.identity_e2e_fixtures import IdentityE2EFixtures

    user = await IdentityE2EFixtures(db).find(payload.email)
    if not user:
        raise HTTPException(status_code=404, detail=f"user {payload.email} not found")

    token = create_access_token(subject=str(user.id), role=user.role)
    return SeedLoginResponse(
        access_token=token,
        user=UserOut.model_validate(user),
    )


class InjectPredictionRequest(BaseModel):
    """e2e 辅助：直接 INSERT 一条 polygon prediction，
    绕过 ml-backend 让 mask 编辑器「AI prediction 精修」入口可测。"""

    task_id: str
    project_id: str
    label: str
    polygon: list[list[float]]
    score: float = 0.9


class InjectPredictionResponse(BaseModel):
    prediction_id: str


@router.post(
    "/seed/inject-prediction",
    response_model=InjectPredictionResponse,
    include_in_schema=False,
)
async def seed_inject_prediction(
    payload: InjectPredictionRequest,
    db: AsyncSession = Depends(get_db),
) -> InjectPredictionResponse:
    """Create one polygon-label candidate through the labeling owner."""
    from app.services.labeling_write_scope import capture_labeling_write_scope
    from app.services.prediction import PredictionService

    task_id = UUID(payload.task_id)
    project_id = UUID(payload.project_id)
    task = await get_core_row(db, task_row, task_id)
    if task is None or task.project_id != project_id:
        raise HTTPException(status_code=404, detail="task not found")

    result = [
        {
            "type": "polygonlabels",
            "value": {
                "points": payload.polygon,
                "polygonlabels": [payload.label],
            },
            "score": payload.score,
        }
    ]
    write_scope = capture_labeling_write_scope(
        task,
        actor_id=None,
        actor_known=True,
        operation="test_seed",
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
    )
    pred = await PredictionService(db).create_test_seed_prediction(
        task_id=task.id,
        project_id=project_id,
        score=payload.score,
        result=result,
        write_scope=write_scope,
    )
    pred_id = str(pred.id)
    await db.commit()
    return InjectPredictionResponse(prediction_id=pred_id)


class ConfigureRasterMaskRequest(BaseModel):
    run_id: UUID
    project_id: UUID
    enabled: bool


class ConfigureRasterMaskResponse(BaseModel):
    project_id: str
    enabled: bool


@router.post(
    "/seed/configure-raster-mask",
    response_model=ConfigureRasterMaskResponse,
    include_in_schema=False,
)
async def seed_configure_raster_mask(
    payload: ConfigureRasterMaskRequest,
    db: AsyncSession = Depends(get_db),
) -> ConfigureRasterMaskResponse:
    """切换 E2E 项目的原生 Mask opt-in。

    部署级 read/create 总闸仍由当前 API 进程的环境变量决定，
    本辅助端点只改项目级灰度开关。
    """

    async def build(scope, admission_id):
        from app.db.models.fixture_run import FixtureRunResource

        project = await get_workspace_project(db, project_id=payload.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        owned_project = await db.get(
            FixtureRunResource, (payload.run_id, "project", project.id)
        )
        if owned_project is None or owned_project.state != "published":
            raise HTTPException(422, detail="expected run-owned project")
        register_seed_project_projection(db, project.id)
        project.raster_mask_native_editing_enabled = payload.enabled
        await db.commit()
        return ConfigureRasterMaskResponse(
            project_id=str(project.id),
            enabled=project.raster_mask_native_editing_enabled,
        )

    return await _run_existing_fixture_seed(
        db, payload.run_id, "raster_configure", build
    )


async def _require_fixture_video_item(db, item):
    from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
    from app.db.models.fixture_run import FixtureRunResource
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.services.fixture_run_ownership import lock_original_scope

    if (
        item is None
        or item.file_type != "video"
        or not isinstance((item.metadata_ or {}).get("_e2e_video_fixture"), str)
    ):
        raise HTTPException(422, detail="expected E2E-owned video fixture")
    run_id = await db.scalar(
        select(FixtureRunResource.run_id).where(
            FixtureRunResource.kind == "item",
            FixtureRunResource.resource_id == item.id,
            FixtureRunResource.state == "published",
        )
    )
    if run_id is None:
        raise HTTPException(422, detail="expected E2E-owned video fixture")
    await lock_original_scope(db, run_id)
    creation_id = await db.scalar(
        select(ManagedOriginalCreation.id)
        .join(
            FixtureRunResource,
            FixtureRunResource.creation_id == ManagedOriginalCreation.id,
        )
        .join(
            DatasetAssetLocation,
            DatasetAssetLocation.creation_id == ManagedOriginalCreation.id,
        )
        .join(DatasetAsset, DatasetAsset.id == DatasetAssetLocation.asset_id)
        .where(
            FixtureRunResource.run_id == run_id,
            FixtureRunResource.item_id == item.id,
            FixtureRunResource.kind == "creation",
            DatasetAsset.dataset_item_id == item.id,
            ManagedOriginalCreation.status == "verified",
            ManagedOriginalCreation.dataset_id == item.dataset_id,
            ManagedOriginalCreation.object_key == item.file_path,
            DatasetAssetLocation.object_key == item.file_path,
            DatasetAssetLocation.role == "managed_original",
            DatasetAssetLocation.reference_state == "present",
        )
    )
    if creation_id is None:
        raise HTTPException(422, detail="expected E2E-owned video fixture")
    return run_id


async def _publish_fixture_video_outputs(db, output_ids):
    """Complete only this fixture's existing claims through the production builder."""
    import sys

    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.models.dataset import DatasetItem
    from app.db.models.video_derivative import VideoDerivativeOutput
    from app.services import media_cache_accounting, storage_access
    from app.services import video_derivative_originals as originals
    from app.services import video_derivative_objects as objects, video_derivatives
    from app.services.media_native_stage import NativeStage
    from app.workers import media_derivatives as worker

    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    claims = []
    for identifier in output_ids:
        output = await db.get(VideoDerivativeOutput, identifier, populate_existing=True)
        item = await db.get(DatasetItem, output.dataset_item_id) if output else None
        await _require_fixture_video_item(db, item)
        if output.state != "building" or output.lease_token is None:
            raise HTTPException(409, detail="fixture output is not pending")
        authority = await originals.delegated(db, output.auth_ref)
        source = await originals.check(db, authority)
        metadata = (
            objects.snapshot(
                await db.get(VideoDerivativeOutput, output.metadata_output_id)
            )
            if output.metadata_output_id
            else None
        )
        claims.append(
            worker.Claimed(
                objects.snapshot(output),
                authority,
                source,
                output.lease_token,
                metadata,
            )
        )
    await db.commit()
    if not claims:
        return
    first = claims[0]
    if any(
        claim.output.original_ref != first.output.original_ref
        or claim.output.auth_ref != first.output.auth_ref
        for claim in claims
    ):
        raise HTTPException(409, detail="fixture output group changed")
    completed = []
    try:
        async with originals.media(db, first.authority, first.source) as stage:
            for claimed in claims:
                revisions = []
                native_stage = None

                async def stage_owner(permit):
                    nonlocal native_stage
                    native_stage = NativeStage(
                        db, permit, "video_output", claimed.output.id
                    )

                async def guard(revision, claimed=claimed):
                    async with factory() as fresh:
                        await video_derivatives.check(
                            fresh,
                            claimed.output,
                            claimed.authority,
                            token=claimed.token,
                            gate_revision=revision,
                        )
                    if revisions and revisions[0] != revision:
                        video_derivatives.reject("storage_maintenance_changed")
                    if not revisions:
                        revisions.append(revision)

                recorder = media_cache_accounting.Recorder(
                    factory,
                    "video_derivative_outputs",
                    claimed.output.id,
                    claimed.output.storage_ref["endpoint_url"],
                    claimed.output.storage_ref["bucket"],
                )
                settled = True
                try:
                    async with objects.access(
                        db,
                        claimed.output,
                        guard,
                        single_attempt=True,
                        on_acquired=stage_owner,
                        owned=True,
                    ) as storage:
                        result, inventory = await originals.physical(
                            worker.build,
                            storage,
                            claimed.output,
                            stage,
                            claimed.metadata,
                            accounting=recorder,
                            native_stage=native_stage,
                            timeout=originals.JOB_SECONDS,
                        )
                except BaseException as exc:
                    settled = not storage_access.failure_unsettled(exc)
                    raise
                finally:
                    primary = sys.exception()
                    try:
                        await recorder.finish(settled=settled)
                    except BaseException as accounting_error:
                        if primary is not None:
                            originals._raise_drain_failure(
                                primary,
                                accounting_error,
                                "Video derivative and accounting failed",
                            )
                        raise
                completed.append((claimed, result, inventory, revisions[0]))
        for claimed, result, inventory, revision in completed:
            async with factory() as final:
                await video_derivatives.publish(
                    final,
                    claimed.output,
                    claimed.authority,
                    claimed.token,
                    result,
                    inventory,
                    revision,
                )
                await final.commit()
    except BaseException as exc:
        if storage_access.failure_unsettled(exc):
            raise
        try:
            for claimed in claims:
                await originals.drain(
                    asyncio.create_task(
                        worker.fail_with_retry(
                            factory,
                            claimed.output.id,
                            claimed.token,
                            worker.error_code(exc),
                        )
                    )
                )
        except BaseException as settlement_failure:
            raise builtins.BaseExceptionGroup(
                "Video derivative generation and failure settlement failed",
                [exc, settlement_failure],
            ) from None
        raise


async def _stage_fixture_video_outputs(
    db, item, kind, specs=None, *, pending=False, runtime=None
):
    from app.services import video_derivative_originals as originals
    from app.services import video_derivative_objects as objects, video_derivatives

    run_id = await _require_fixture_video_item(db, item)
    source = await originals.pin(db, "dataset_item", item.id)
    authority = await originals.delegated(
        db, originals.system_authorization("dataset_item", item.id, source)
    )
    identifiers = []
    claims = {}
    for spec in specs or [None]:
        output, _ = await video_derivatives.prepare(
            db, item.id, authority, kind, spec=spec, force=runtime is not None
        )
        token = await objects.claim(db, output, ttl_seconds=originals.JOB_SECONDS)
        if token is None:
            raise HTTPException(409, detail="fixture output already claimed")
        identifiers.append(output.id)
        if runtime is not None:
            claims[str(output.id)] = {
                **_fixture_video_seed_claim(output),
                "dataset_id": str(item.dataset_id),
            }
    if claims:
        runtime.record_video_seed_claims(run_id, claims)
    await db.commit()
    if not pending:
        await _publish_fixture_video_outputs(db, identifiers)
    return identifiers


class SeedVideoTaskRequest(SeedOwnedRequest):
    project_id: str


class SeedVideoTaskResponse(BaseModel):
    task_id: str


@router.post(
    "/seed/video-task",
    response_model=SeedVideoTaskResponse,
    include_in_schema=False,
)
async def seed_video_task(
    payload: SeedVideoTaskRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SeedVideoTaskResponse:
    """Add one deterministic video task to an owned workbench fixture."""

    runtime = getattr(request.app.state, "fixture_native_runtime", None)

    async def build(scope, admission_id):
        return await _build_video_task(db, payload, scope, admission_id, runtime)

    return await _run_owned_seed(db, payload, "video_task_seed", build, request)


async def _build_video_task(db, payload, scope, admission_id, runtime):
    from pathlib import Path
    from uuid import UUID

    from sqlalchemy import select

    from app.db.models.dataset import Dataset, DatasetItem
    from app.db.models.task import Task
    from app.services import fixture_run_ownership as ownership

    project_id = UUID(payload.project_id)
    image_task = (
        (
            await db.execute(
                select(task_row)
                .where(
                    task_row.project_id == project_id,
                    task_row.dataset_item_id.is_not(None),
                )
                .order_by(task_row.created_at)
            )
        )
        .scalars()
        .first()
    )
    batch = (
        (
            await db.execute(
                select(batch_row)
                .where(batch_row.project_id == project_id)
                .order_by(batch_row.created_at)
            )
        )
        .scalars()
        .first()
    )
    if image_task is None or batch is None:
        raise HTTPException(status_code=404, detail="project fixture not found")
    image_item = await db.get(DatasetItem, image_task.dataset_item_id)
    if image_item is None:
        raise HTTPException(status_code=404, detail="dataset fixture not found")
    resolved = await _resolve_fixture_scope(db, scope)
    if (
        project_id not in resolved.project_ids
        or image_item.dataset_id not in resolved.dataset_ids
    ):
        raise HTTPException(422, detail="expected E2E-owned project and dataset")

    source_path = (
        Path(__file__).resolve().parents[5]
        / "docs-site/public/home/sam-tools/smart-point.webm"
    )
    video = source_path.read_bytes()
    dataset = await db.get(Dataset, image_item.dataset_id)
    if dataset is None or dataset.created_by is None:
        raise HTTPException(409, detail="fixture_dataset_changed")
    item_id = uuid4()
    await ownership.reserve(
        db, payload.run_id, "item", item_id, logical_key=f"video-{item_id.hex}"
    )
    await db.commit()
    item = DatasetItem(
        id=item_id,
        dataset_id=image_item.dataset_id,
        file_name="native-mask.webm",
        file_path="",
        file_type="video",
        file_size=len(video),
        metadata_={"_e2e_video_fixture": "native-mask"},
    )
    allocation, original = await ownership.create_original(
        db,
        run_id=payload.run_id,
        admission_id=admission_id,
        item=item,
        logical_path=item.file_name,
        payload=video,
        content_type="video/webm",
        requested_by=dataset.created_by,
    )
    await ownership.attach_original(db, item, allocation, original)
    await ownership.publish_resource(db, payload.run_id, "item", item.id)
    task = Task(
        project_id=project_id,
        batch_id=batch.id,
        dataset_item_id=item.id,
        display_id=f"T-E2E-VIDEO-{secrets.token_hex(3)}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="video",
        status="pending",
    )
    fixture_writes = LabelingE2EFixtureWrites(db)
    task = (await fixture_writes.insert_tasks([task]))[0]
    await ownership.reserve(
        db,
        payload.run_id,
        "task",
        task.id,
        logical_key=f"video-{task.id.hex}",
        published=True,
    )
    await fixture_writes.patch_batch(batch, total_tasks=int(batch.total_tasks or 0) + 1)
    task_id = str(task.id)
    await db.commit()
    await _stage_fixture_video_outputs(db, item, "metadata", runtime=runtime)
    return SeedVideoTaskResponse(task_id=task_id)


class SeedVideoWebCodecsRequest(SeedOwnedRequest):
    project_id: str
    batch_id: UUID | None = None
    fixture: str = "h264-baseline-gop12"
    chunk_status: Literal["ready", "pending"] = "ready"


class SeedVideoWebCodecsResponse(BaseModel):
    task_id: str
    dataset_item_id: str
    chunk_id: int
    chunk_size_frames: int
    frame_expectations: dict


@router.post(
    "/seed/video-webcodecs",
    response_model=SeedVideoWebCodecsResponse,
    include_in_schema=False,
)
async def seed_video_webcodecs(
    payload: SeedVideoWebCodecsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SeedVideoWebCodecsResponse:
    """Seed a deterministic H.264 WebCodecs fixture for precise-frame E2E.

    Known uploaded bytes establish the canonical original. Metadata and chunks
    use real independent owners and the production builder. A pending fixture
    retains its one claim until the explicit transition endpoint publishes it;
    malformed-response scenarios describe browser injection without corrupting
    a valid immutable result.
    """

    runtime = getattr(request.app.state, "fixture_native_runtime", None)

    async def build(scope, admission_id):
        return await _build_video_webcodecs(db, payload, scope, admission_id, runtime)

    return await _run_owned_seed(db, payload, "video_webcodecs_seed", build, request)


async def _build_video_webcodecs(db, payload, scope, admission_id, runtime):
    from tempfile import mkdtemp
    from uuid import UUID
    import shutil

    from sqlalchemy import select

    from app.api.v1._test_seed_webcodecs import (
        FIXTURE_SPECS,
        QUALIFICATION_FIXTURE_SPECS,
        frame_expectations,
        generate_fixture,
        generate_qualification_fixture,
    )
    from app.db.models.dataset import Dataset, DatasetItem
    from app.services import video_derivative_originals as originals, video_derivatives
    from app.db.models.task import Task
    from app.services import fixture_run_ownership as ownership

    valid_fixtures = (
        set(FIXTURE_SPECS)
        | set(QUALIFICATION_FIXTURE_SPECS)
        | {"unsupported-config", "malformed-samples"}
    )
    if payload.fixture not in valid_fixtures:
        raise HTTPException(status_code=422, detail="unknown_fixture")

    project_id = UUID(payload.project_id)
    batch_query = select(batch_row).where(batch_row.project_id == project_id)
    if payload.batch_id is not None:
        batch_query = batch_query.where(batch_row.id == payload.batch_id)
    batch = (
        (await db.execute(batch_query.order_by(batch_row.created_at))).scalars().first()
    )
    ref_task = (
        (
            await db.execute(
                select(task_row)
                .where(
                    task_row.project_id == project_id,
                    task_row.dataset_item_id.is_not(None),
                )
                .order_by(task_row.created_at)
            )
        )
        .scalars()
        .first()
    )
    if batch is None or ref_task is None or ref_task.dataset_item_id is None:
        raise HTTPException(status_code=404, detail="project fixture not found")
    ref_item = await db.get(DatasetItem, ref_task.dataset_item_id)
    if ref_item is None:
        raise HTTPException(status_code=404, detail="dataset fixture not found")

    resolved = await _resolve_fixture_scope(db, scope)
    if (
        project_id not in resolved.project_ids
        or ref_item.dataset_id not in resolved.dataset_ids
    ):
        raise HTTPException(422, detail="expected E2E-owned project and dataset")

    fixture = payload.fixture
    tmp = mkdtemp(prefix="webcodecs-seed-")
    try:
        qualification = fixture in QUALIFICATION_FIXTURE_SPECS
        if qualification:
            from app.services.storage_access import finish

            generation = asyncio.create_task(
                asyncio.to_thread(
                    generate_qualification_fixture, fixture, tmp, include_chunks=False
                )
            )
            try:
                meta = await asyncio.shield(generation)
            except BaseException as failure:
                try:
                    await finish(generation)
                except BaseException as late:
                    if late is failure:
                        raise
                    raise builtins.BaseExceptionGroup(
                        "Video fixture generation and its drain failed", [failure, late]
                    ) from None
                raise
        else:
            meta = generate_fixture(
                fixture,
                tmp,
                chunk_size_frames=settings.video_chunk_size_frames,
                include_chunks=False,
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    mp4_bytes = meta["mp4_bytes"]
    dataset = await db.get(Dataset, ref_item.dataset_id)
    if dataset is None or dataset.created_by is None:
        raise HTTPException(409, detail="fixture_dataset_changed")
    item_id = uuid4()
    await ownership.reserve(
        db, payload.run_id, "item", item_id, logical_key=f"video-{item_id.hex}"
    )
    await db.commit()

    item = DatasetItem(
        id=item_id,
        dataset_id=ref_item.dataset_id,
        file_name=f"{fixture}.mp4",
        file_path="",
        file_type="video",
        file_size=len(mp4_bytes),
        metadata_={"_e2e_video_fixture": fixture},
    )
    allocation, original = await ownership.create_original(
        db,
        run_id=payload.run_id,
        admission_id=admission_id,
        item=item,
        logical_path=item.file_name,
        payload=mp4_bytes,
        content_type="video/mp4",
        requested_by=dataset.created_by,
    )
    await ownership.attach_original(db, item, allocation, original)
    await ownership.publish_resource(db, payload.run_id, "item", item.id)
    task = Task(
        project_id=project_id,
        batch_id=batch.id,
        dataset_item_id=item.id,
        display_id=f"T-E2E-WC-{fixture[:10]}-{secrets.token_hex(3)}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="video",
        status="pending",
    )
    db.add(task)
    await db.flush()
    await ownership.reserve(
        db,
        payload.run_id,
        "task",
        task.id,
        logical_key=f"video-{task.id.hex}",
        published=True,
    )
    batch.total_tasks = int(batch.total_tasks or 0) + 1
    await db.flush()
    task_id, item_id = str(task.id), item.id
    await db.commit()
    await _stage_fixture_video_outputs(db, item, "metadata", runtime=runtime)
    item = await db.get(DatasetItem, item_id, populate_existing=True)
    source = await originals.pin(db, "dataset_item", item_id)
    generation = await video_derivatives.metadata(db, item, source)
    count = video_derivatives.metadata_result(generation)[0].frame_count
    chunk_size = max(1, settings.video_chunk_size_frames)
    specs = [
        video_derivatives.chunk_spec(generation, chunk_id)
        for chunk_id in range((count + chunk_size - 1) // chunk_size)
    ]
    chunk_outputs = await _stage_fixture_video_outputs(
        db,
        item,
        "chunk",
        specs,
        pending=payload.chunk_status == "pending",
        runtime=runtime,
    )
    expected_samples = meta.get("samples", [])
    if not qualification and payload.chunk_status == "ready":
        from app.db.models.video_derivative import VideoDerivativeOutput

        expected_samples = []
        for identifier in chunk_outputs:
            output = await db.get(
                VideoDerivativeOutput, identifier, populate_existing=True
            )
            expected_samples.extend(output.result["diagnostics"].get("samples", []))
    expectations = (
        {} if qualification else frame_expectations(fixture, expected_samples)
    )
    if not qualification:
        source_pts = {
            entry["frame_index"]: entry["pts_ms"] for entry in meta["frame_timetable"]
        }
        for frame in expectations["frames"]:
            frame["source_pts_ms"] = source_pts.get(frame["frame_index"])
    return SeedVideoWebCodecsResponse(
        task_id=task_id,
        dataset_item_id=str(item_id),
        chunk_id=0,
        chunk_size_frames=chunk_size,
        frame_expectations=expectations,
    )


class SeedVideoIssueContextHistoryRequest(SeedOwnedRequest):
    """Only simulate a future context version on an existing E2E video Issue."""

    model_config = {"extra": "forbid"}

    feedback_id: UUID


class SeedVideoIssueContextHistoryResponse(BaseModel):
    feedback_id: UUID
    anchor_position: dict[str, Any]


@router.post(
    "/seed/video-issue-context-history",
    response_model=SeedVideoIssueContextHistoryResponse,
    include_in_schema=False,
)
async def seed_video_issue_context_history(
    payload: SeedVideoIssueContextHistoryRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SeedVideoIssueContextHistoryResponse:
    async def build(scope, admission_id):
        from app.db.models.dataset import DatasetItem
        from app.db.models.fixture_run import FixtureRunResource
        from app.services.labeling_core_read_relations import execute_core_read
        from app.services.labeling_discussion_relations import annotation_feedback_facts

        feedback_result = await execute_core_read(
            db,
            select(annotation_feedback_facts)
            .where(annotation_feedback_facts.id == payload.feedback_id)
            .execution_options(populate_existing=True),
        )
        feedback = feedback_result.scalar_one_or_none()
        if feedback is None:
            raise HTTPException(status_code=404, detail="feedback fixture not found")
        task = (
            await get_core_row(db, task_row, feedback.task_id)
            if feedback.task_id
            else None
        )
        resolved = await _resolve_fixture_scope(db, scope)
        project_resource = await db.get(
            FixtureRunResource, (payload.run_id, "project", feedback.project_id)
        )
        anchor = feedback.anchor_position
        if (
            feedback.project_id not in resolved.project_ids
            or project_resource is None
            or project_resource.state != "published"
            or task is None
            or task.id not in resolved.task_ids
            or task.project_id != feedback.project_id
            or task.file_type != "video"
            or feedback.kind != "issue"
            or feedback.anchor_type != "pixel"
            or not feedback.is_active
            or not isinstance(anchor, dict)
            or not {"x", "y", "frame"} <= anchor.keys()
        ):
            raise HTTPException(
                status_code=422, detail="expected E2E video pixel Issue"
            )
        item = (
            await db.get(DatasetItem, task.dataset_item_id)
            if task.dataset_item_id
            else None
        )
        item_owned = item is not None and await db.scalar(
            select(FixtureRunResource.resource_id).where(
                FixtureRunResource.run_id == payload.run_id,
                FixtureRunResource.kind == "item",
                FixtureRunResource.resource_id == item.id,
                FixtureRunResource.state == "published",
            )
        )
        if not item_owned or item.dataset_id not in resolved.dataset_ids:
            raise HTTPException(
                status_code=422, detail="expected E2E video pixel Issue"
            )
        try:
            await _require_fixture_video_item(db, item)
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            raise HTTPException(
                status_code=422, detail="expected E2E video pixel Issue"
            ) from exc
        anchor_position = {
            **anchor,
            "video_context": {
                "schema_version": 999,
                "viewport": {"future_camera": "unsupported"},
                "frame_range": {"from_frame": 900, "to_frame": 901},
            },
        }
        anchor_position = await LabelingE2EFixtureWrites(db).patch_feedback_anchor(
            project_id=feedback.project_id,
            task_id=feedback.task_id,
            feedback_id=feedback.id,
            anchor_position=anchor_position,
        )
        await db.commit()
        return SeedVideoIssueContextHistoryResponse(
            feedback_id=feedback.id, anchor_position=anchor_position
        )

    return await _run_owned_seed(db, payload, "video_issue_history", build, request)


class SeedVideoWebCodecsTransitionRequest(BaseModel):
    dataset_item_id: str
    chunk_id: int = 0


class SeedVideoWebCodecsTransitionResponse(BaseModel):
    status: Literal["ready", "pending", "failed"]


@router.post(
    "/seed/video-webcodecs/transition-ready",
    response_model=SeedVideoWebCodecsTransitionResponse,
    include_in_schema=False,
)
async def seed_video_webcodecs_transition(
    payload: SeedVideoWebCodecsTransitionRequest,
    db: AsyncSession = Depends(get_db),
) -> SeedVideoWebCodecsTransitionResponse:
    """Publish the same fixture claim after physical generation really closes."""
    from sqlalchemy import select
    from app.db.models.dataset import DatasetItem, VideoChunk
    from app.db.models.video_derivative import VideoDerivativeOutput

    item = await db.get(DatasetItem, UUID(payload.dataset_item_id))
    await _require_fixture_video_item(db, item)
    row = await db.scalar(
        select(VideoChunk).where(
            VideoChunk.dataset_item_id == item.id,
            VideoChunk.chunk_id == payload.chunk_id,
        )
    )
    if row is None or row.output_id is None:
        raise HTTPException(404, detail="fixture chunk owner not found")
    output = await db.get(VideoDerivativeOutput, row.output_id)
    if output is None or output.dataset_item_id != item.id:
        raise HTTPException(409, detail="fixture output changed")
    if output.state != "ready":
        await _publish_fixture_video_outputs(db, [output.id])
    await db.refresh(row)
    if row.status != "ready":
        raise HTTPException(409, detail="fixture output was not published")
    return SeedVideoWebCodecsTransitionResponse(status="ready")


class SeedTrackerReviewRequest(BaseModel):
    task_id: str
    user_email: str


class SeedTrackerReviewResponse(BaseModel):
    job_id: str
    source_annotation_ids: list[str]


@router.post(
    "/seed/tracker-review",
    response_model=SeedTrackerReviewResponse,
    include_in_schema=False,
)
async def seed_tracker_review(
    payload: SeedTrackerReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> SeedTrackerReviewResponse:
    """Create a deterministic two-target staged tracker review for browser E2E."""
    from app.db.models.annotation import Annotation
    from app.db.models.dataset import DatasetItem
    from app.db.models.fixture_run import FixtureRunResource
    from app.db.models.video_tracker_job import VideoTrackerJob
    from app.services.identity_e2e_fixtures import IdentityE2EFixtures

    task = await get_core_row(db, task_row, UUID(payload.task_id))
    user = await IdentityE2EFixtures(db).find(payload.user_email)
    if task is None or task.file_type != "video" or user is None:
        raise HTTPException(status_code=404, detail="video task or user not found")

    item = await db.get(DatasetItem, task.dataset_item_id)
    run_id = await _require_fixture_video_item(db, item)
    owned_task = await db.scalar(
        select(FixtureRunResource.resource_id).where(
            FixtureRunResource.run_id == run_id,
            FixtureRunResource.kind == "task",
            FixtureRunResource.resource_id == task.id,
            FixtureRunResource.state == "published",
        )
    )
    if owned_task is None:
        raise HTTPException(422, detail="expected E2E-owned video task")

    def source(track_id: str, x: float, frame_index: int) -> Annotation:
        return Annotation(
            task_id=task.id,
            project_id=task.project_id,
            user_id=user.id,
            source="manual",
            annotation_type="video_track_bbox",
            class_name="car",
            tool_unit_id="bbox",
            track_id=track_id,
            geometry={
                "type": "video_track_bbox",
                "track_id": track_id,
                "keyframes": [
                    {
                        "frame_index": frame_index,
                        "bbox": {"x": x, "y": 0.2, "w": 0.16, "h": 0.18},
                        "source": "manual",
                        "occluded": False,
                    }
                ],
                "outside": [],
            },
        )

    async def build(_scope, _admission_id):
        from app.services.accepted_tracker_inputs import capture_video_tracker_input

        fixture_writes = LabelingE2EFixtureWrites(db)
        source_a, source_b = await fixture_writes.insert_annotations(
            [source("e2e-track-a", 0.16, 16), source("e2e-track-b", 0.62, 9)]
        )
        source_ids = {"A": str(source_a.id), "B": str(source_b.id)}
        source_versions = {str(row.id): row.version for row in (source_a, source_b)}
        job_id = uuid4()
        await fixture_writes.insert_video_tracker_jobs(
            [
                VideoTrackerJob(
                    id=job_id,
                    task_id=task.id,
                    dataset_item_id=item.id,
                    created_by=user.id,
                    model_key="mock_bbox",
                    direction="forward",
                    from_frame=10,
                    to_frame=19,
                    prompt={
                        "seeds": [
                            {"obj_id": instance, "source_annotation_id": annotation_id}
                            for instance, annotation_id in source_ids.items()
                        ],
                        "expected_source_versions": source_versions,
                        "output_geometry": "bbox",
                    },
                    event_channel=f"video-tracker-job:{job_id}",
                )
            ]
        )
        captured = await capture_video_tracker_input(
            db, actor_id=user.id, tracker_job_id=job_id, grid_step=1
        )
        await db.commit()
        await _stage_tracker_review_fixture(db, captured, source_ids, source_versions)
        return SeedTrackerReviewResponse(
            job_id=str(job_id), source_annotation_ids=list(source_ids.values())
        )

    return await _run_existing_fixture_seed(db, run_id, "tracker_review_seed", build)


async def _stage_tracker_review_fixture(db, captured, source_ids, source_versions):
    from yoao_assets_video_inputs.tracker import TrackerFailure
    from yoao_labeling.video_control_contract import StartWorkerJob
    from yoao_preannotation_results.contract import json_bytes
    from yoao_preannotation_results.video_execution_planner import (
        build_video_execution_plan,
        freeze_video_window_settings,
    )
    from yoao_preannotation_results.video_window_checkpoint import (
        make_video_window_checkpoint_payload,
    )

    from app.services.accepted_tracker_inputs import (
        TrackerInputDeferred,
        embedded_tracker_video_input_gateway,
        ensure_tracker_video_input_ready,
        raise_tracker_http_failure,
    )
    from app.services.video_tracking.backend_identity import (
        bind_video_normalization_policy,
        build_video_backend_compatibility,
    )
    from app.services.video_tracking.control import video_control_job
    from app.services.video_tracking.execution import (
        fence_tracker_write,
        tracker_execution,
    )
    from app.services.video_tracking.journal import TrackerWindowJournal
    from app.services.video_tracking.recovery import retire_terminal_tracker_execution
    from app.services.video_tracking.writer import commit_tracker_outcome

    scope = captured.scope
    job_id = scope.tracker_job_id
    try:
        async with embedded_tracker_video_input_gateway() as gateway:
            ready = await ensure_tracker_video_input_ready(
                gateway,
                actor_id=scope.origin_actor_id,
                tracker_job_id=job_id,
                task_id=scope.task.task_id,
            )
    except TrackerFailure as exc:
        raise_tracker_http_failure(exc)
    except TrackerInputDeferred as exc:
        raise_tracker_http_failure(TrackerFailure(exc.code))

    execution = ready.status.execution
    snapshots = {str(row.annotation_id): row for row in execution.source_annotations}
    accepted_ids = {
        seed["obj_id"]: seed["source_annotation_id"]
        for seed in execution.prompt_snapshot["seeds"]
    }
    if (
        ready.status.input_id != captured.input_id
        or ready.status.request_digest != scope.request_digest
        or accepted_ids != source_ids
        or {key: row.version for key, row in snapshots.items()} != source_versions
    ):
        raise HTTPException(409, detail={"reason": "fixture_tracker_sources_changed"})

    async with tracker_execution(db, job_id):
        await fence_tracker_write(db, job_id, "start")
        running = await video_control_job(
            db,
            StartWorkerJob(
                schema="aap-labeling-video-control/v1",
                operation="start_worker_job",
                job_id=job_id,
                celery_task_id=None,
                started_at=datetime.now(timezone.utc),
            ),
        )
        if running is None or running.status != "running":
            raise HTTPException(409, detail={"reason": "fixture_tracker_not_running"})
        await db.commit()

        journal = TrackerWindowJournal(db, job_id)
        facts = await journal.input_facts()
        backend = bind_video_normalization_policy(
            build_video_backend_compatibility(
                model_key=execution.model_key, selected_backend=None
            ),
            outside_threshold=settings.video_tracker_low_confidence_outside_threshold,
        )
        frame_count = execution.to_frame - execution.from_frame + 1
        plan = await journal.freeze(
            build_video_execution_plan(
                job_id=job_id,
                facts=facts,
                window_settings=freeze_video_window_settings(
                    requested_model_key=execution.model_key,
                    platform_default_frames=frame_count,
                    platform_sam3_frames=frame_count,
                    backend_compatibility=backend,
                ),
                backend_compatibility=backend,
                output_geometry="bbox",
                initial_seed_source="prompt",
                initial_seed_digest=facts.prompt_digest,
            )
        )
        (window,) = plan.windows
        boxes = {
            instance: snapshots[annotation_id].geometry["keyframes"][0]["bbox"]
            for instance, annotation_id in sorted(accepted_ids.items())
        }
        rows = []
        last = {}
        for index in range(window.from_frame, window.to_frame + 1):
            for instance, box in boxes.items():
                last[instance] = geometry = {
                    "type": "bbox",
                    **box,
                    "x": box["x"] + (index - execution.from_frame) * 0.005,
                }
                rows.append(
                    {
                        "frame_index": index,
                        "geometry": geometry,
                        "confidence": 0.92,
                        "outside": False,
                        "instance_id": instance,
                        "primary": instance == "A",
                    }
                )
        attempt = await journal.begin(window)
        await journal.save(
            window,
            attempt,
            make_video_window_checkpoint_payload(
                plan=plan,
                window_ordinal=window.ordinal,
                raw_response_digest=hashlib.sha256(json_bytes(rows)).hexdigest(),
                frame_results=rows,
                continuation={
                    "last_geometry": last["A"],
                    "last_geom_by_instance": last,
                    "mp_prev_boundary": {},
                    "mp_next_global": 1,
                },
            ),
        )
        receipt = await commit_tracker_outcome(
            db,
            job_id,
            "completed",
            status=ready.status,
            source_version=ready.reference.source_version,
        )
        await retire_terminal_tracker_execution(db, job_id)
        if receipt.status != "pending_review":
            raise HTTPException(409, detail={"reason": "fixture_tracker_not_staged"})


class SeedNativeMaskPromptSource(BaseModel):
    annotation_id: str
    source_version: int
    source_digest: str


class SeedNativeMaskCandidateRequest(BaseModel):
    run_id: UUID
    task_id: UUID
    variant: Literal[
        "default",
        "negative_scribble",
        "multimask_donut",
        "smart_scribble_refined",
    ] = "default"
    prompt_family: Literal["point", "scribble"] = "point"
    negative_scribbles: int = 0
    prompt_source: SeedNativeMaskPromptSource | None = None


class SeedNativeMaskCandidateResponse(BaseModel):
    response: dict
    rle: dict
    rles: list[dict]


@router.post(
    "/seed/native-mask-candidate",
    response_model=SeedNativeMaskCandidateResponse,
    include_in_schema=False,
)
async def seed_native_mask_candidate(
    payload: SeedNativeMaskCandidateRequest,
    db: AsyncSession = Depends(get_db),
) -> SeedNativeMaskCandidateResponse:
    """Issue a real signed receipt for a deterministic transient Mask candidate."""

    async def build(scope, admission_id):
        return await _seed_native_mask_candidate(db, payload, scope)

    return await _run_existing_fixture_seed(
        db, payload.run_id, "native_mask_candidate", build
    )


async def _seed_native_mask_candidate(db, payload, scope):
    import hashlib

    from yoao_protocol_v2 import (
        CocoRlePayload,
        canonical_rle_bytes,
        native_mask_candidate_id,
    )

    from app.services.inference_topology_data import InferenceTopologyData
    from app.services.ai_mask_receipt import issue_ai_mask_receipt
    from app.utils.raster_mask_rle import coco_rle_bbox_norm, encode_coco_rle

    task, _user, item = await _fixture_raster_task(
        db, scope, payload.run_id, payload.task_id, media_types=("image", "video")
    )
    project = await get_workspace_project(db, project_id=task.project_id)
    if item is None or project is None or item.width is None or item.height is None:
        raise HTTPException(status_code=404, detail="media fixture not found")
    if project.ml_backend_pool_id is None:
        raise HTTPException(status_code=404, detail="backend pool fixture not found")
    pool = await InferenceTopologyData(db).pool_reference(project.ml_backend_pool_id)
    if pool is None or pool.legacy_instance_id is None:
        raise HTTPException(status_code=404, detail="backend fixture not found")

    original_input = None
    if item.file_type == "image":
        from app.services import prediction_originals as originals
        from app.services.managed_image_inference import normalize_identity
        from app.services.storage_location import api_node_id

        original = originals.source_receipt(
            await originals.resolve(db, task.id, lock=True)
        )
        task_id = task.id
        await db.commit()

        async def guard(owned):
            await _require_e2e_seed_database(owned)

        opened = await originals.open_snapshot(
            db,
            task_id,
            original,
            guard=guard,
            purpose="e2e_native_mask_" + task_id.hex,
            node_id=api_node_id(),
        )
        try:
            original_input = normalize_identity(
                {
                    "schema": 1,
                    "task_id": str(task_id),
                    **{
                        name: original[name]
                        for name in (
                            "owner_kind",
                            "content_id",
                            "sha256",
                            "size_bytes",
                            "location_id",
                            "location_revision",
                            "maintenance_revision",
                            "facts_digest",
                        )
                    },
                    "width": opened.snapshot.width,
                    "height": opened.snapshot.height,
                }
            )
        finally:
            await originals.settled(opened.close())

    total = int(item.width) * int(item.height)
    foreground_start = total // 4
    foreground_length = max(
        1,
        total // 4 if payload.variant == "negative_scribble" else total // 3,
    )
    if payload.variant == "smart_scribble_refined":
        width = int(item.width)
        height = int(item.height)
        pixels = _make_smart_scribble_mask(width, height, refined=True)
        primary_rle = CocoRlePayload.model_validate(
            encode_coco_rle(pixels, width, height)
        )
    else:
        primary_rle = CocoRlePayload(
            encoding="coco_rle",
            size=(int(item.height), int(item.width)),
            counts=(
                foreground_start,
                foreground_length,
                total - foreground_start - foreground_length,
            ),
        )
    rle_models = [primary_rle]
    if payload.variant == "multimask_donut":
        secondary_start = total // 6
        secondary_length = max(1, total // 5)
        rle_models.append(
            CocoRlePayload(
                encoding="coco_rle",
                size=(int(item.height), int(item.width)),
                counts=(
                    secondary_start,
                    secondary_length,
                    total - secondary_start - secondary_length,
                ),
            )
        )
        width = int(item.width)
        height = int(item.height)
        pixels = bytearray(total)

        def rect(x0: float, y0: float, x1: float, y1: float, value: int = 1) -> None:
            left = max(0, min(width, int(width * x0)))
            top = max(0, min(height, int(height * y0)))
            right = max(left, min(width, int(width * x1)))
            bottom = max(top, min(height, int(height * y1)))
            for y in range(top, bottom):
                pixels[y * width + left : y * width + right] = bytes([value]) * (
                    right - left
                )

        rect(0.08, 0.08, 0.38, 0.42)
        rect(0.17, 0.17, 0.29, 0.31, 0)
        rect(0.53, 0.10, 0.70, 0.30)
        rect(0.72, 0.58, 0.90, 0.84)
        rle_models.append(
            CocoRlePayload.model_validate(encode_coco_rle(pixels, width, height))
        )
    rles = [rle_model.model_dump(mode="json") for rle_model in rle_models]
    prompt_revision = (
        f"e2e-native-mask:{task.id}:{payload.variant}:{payload.prompt_family}"
    )
    routing = {
        "requested_backend_id": str(pool.legacy_instance_id),
        "backend_pool_id": str(pool.id),
        "backend_instance_id": str(pool.legacy_instance_id),
        "model_id": "e2e-native-mask",
    }
    inference = {
        "model_version": "e2e-native-mask",
        "inference_time_ms": 7.0,
        "cache_hit": False,
        "model_load_ms": 0.0,
    }
    frame_index = 0 if item.file_type == "video" else None
    prompt_summary = {
        "family": payload.prompt_family,
        "positive_points": 1 if payload.prompt_family == "point" else 0,
        "negative_points": 0,
        "boxes": 0,
        "positive_scribbles": 0,
        "negative_scribbles": payload.negative_scribbles,
        "multimask": payload.prompt_family == "point",
        "parameters_digest": None,
    }
    prompt_source = (
        {
            "source_annotation_id": payload.prompt_source.annotation_id,
            "source_version": payload.prompt_source.source_version,
            "source_digest": payload.prompt_source.source_digest,
        }
        if payload.prompt_source is not None
        else None
    )
    accept_target = {
        "mode": "refine" if prompt_source is not None else "create",
        "source_annotation_id": (
            prompt_source["source_annotation_id"] if prompt_source else None
        ),
        "source_version": prompt_source["source_version"] if prompt_source else None,
        "frame_index": frame_index,
    }
    results = []
    accept_receipts = {}
    for candidate_index, rle_model in enumerate(rle_models):
        candidate_id = native_mask_candidate_id(
            rle_model,
            prompt_revision=prompt_revision,
            candidate_index=candidate_index,
        )
        content_digest = hashlib.sha256(canonical_rle_bytes(rle_model)).hexdigest()
        bounds = coco_rle_bbox_norm(rles[candidate_index])
        left = bounds["x"]
        top = bounds["y"]
        right = left + bounds["w"]
        bottom = top + bounds["h"]
        results.append(
            {
                "type": "mask",
                "value": {
                    "rle": rles[candidate_index],
                    "masklabels": ["object"],
                    "preview": {
                        "points": [
                            [left, top],
                            [right, top],
                            [right, bottom],
                            [left, bottom],
                        ]
                    },
                },
                "score": 0.95 - candidate_index * 0.05,
                "candidate_id": candidate_id,
            }
        )
        accept_receipts[candidate_id] = issue_ai_mask_receipt(
            {
                "task_id": str(task.id),
                "frame_index": frame_index,
                "candidate_id": candidate_id,
                "candidate_index": candidate_index,
                "content_digest": content_digest,
                "prompt_revision": prompt_revision,
                "score": 0.95 - candidate_index * 0.05,
                "routing": routing,
                "inference": inference,
                "prompt_summary": prompt_summary,
                "prompt_source": prompt_source,
                "accept_target": accept_target,
                **(
                    {"original_input": original_input}
                    if original_input is not None
                    else {}
                ),
            }
        )
    response = {
        "result": results,
        "score": 0.95,
        "model_version": inference["model_version"],
        "inference_time_ms": inference["inference_time_ms"],
        "cache_hit": inference["cache_hit"],
        "model_load_ms": inference["model_load_ms"],
        "mask_input_next": None,
        "diagnostic": None,
        "prompt_revision": prompt_revision,
        "output_geometry": "mask",
        "frame_index": frame_index,
        "routing": routing,
        "prompt_summary": prompt_summary,
        "accept_receipts": accept_receipts,
    }
    return SeedNativeMaskCandidateResponse(response=response, rle=rles[0], rles=rles)


RasterMaskFixtureVariant = Literal[
    "single",
    "donut_three",
    "diagonal_two",
    "island",
    "smart_scribble_source",
    "fault_source",
]
RasterMaskFixtureCanvas = Literal["default", "media", "5k", "8k"]


class InjectRasterMaskRequest(BaseModel):
    run_id: UUID
    task_id: UUID
    user_email: str
    variant: RasterMaskFixtureVariant = "single"
    label: str = "car"
    locked: bool = False
    canvas: RasterMaskFixtureCanvas = "default"


class InjectRasterMaskResponse(BaseModel):
    annotation_id: str
    variant: RasterMaskFixtureVariant
    mask: dict


class InjectRasterPredictionResponse(BaseModel):
    prediction_id: str
    mask: dict


class PreparedRasterFaultRequest(SeedOwnedRequest):
    project_id: UUID
    task_id: UUID
    user_email: str
    healthy_annotation_id: UUID
    corrupt_annotation_id: UUID


_SMART_SCRIBBLE_SOURCE_OUTLINE = (
    (0.527344, 0.494444),
    (0.438281, 0.498611),
    (0.421094, 0.540278),
    (0.404687, 0.545833),
    (0.415625, 0.5625),
    (0.410938, 0.602778),
    (0.421875, 0.738889),
    (0.434375, 0.7375),
    (0.439063, 0.711111),
    (0.490625, 0.701389),
    (0.5375, 0.705556),
    (0.545312, 0.730556),
    (0.560156, 0.727778),
    (0.560156, 0.620833),
    (0.546875, 0.559722),
    (0.558594, 0.536111),
    (0.539844, 0.530556),
)
_SMART_SCRIBBLE_REFINED_OUTLINE = (
    (0.528906, 0.494444),
    (0.439063, 0.497222),
    (0.421094, 0.538889),
    (0.404687, 0.545833),
    (0.415625, 0.563889),
    (0.410156, 0.6),
    (0.421094, 0.738889),
    (0.435156, 0.7375),
    (0.440625, 0.7125),
    (0.500781, 0.701389),
    (0.539062, 0.706944),
    (0.545312, 0.730556),
    (0.560156, 0.729167),
    (0.560937, 0.627778),
    (0.547656, 0.561111),
    (0.559375, 0.5375),
    (0.540625, 0.529167),
)


def _make_smart_scribble_mask(
    width: int, height: int, *, refined: bool = False
) -> bytearray:
    """根据 SAM3 对 screenshot_03 的真实候选轮廓生成确定性 Mask fixture。"""
    outline = (
        _SMART_SCRIBBLE_REFINED_OUTLINE if refined else _SMART_SCRIBBLE_SOURCE_OUTLINE
    )
    pixels = bytearray(width * height)
    for y in range(height):
        scan_y = (y + 0.5) / height
        crossings: list[float] = []
        for index, (x1, y1) in enumerate(outline):
            x2, y2 = outline[(index + 1) % len(outline)]
            if (y1 <= scan_y < y2) or (y2 <= scan_y < y1):
                crossings.append(x1 + (scan_y - y1) * (x2 - x1) / (y2 - y1))
        crossings.sort()
        for index in range(0, len(crossings) - 1, 2):
            left = max(0, round(crossings[index] * width))
            right = min(width, round(crossings[index + 1] * width))
            offset = y * width
            pixels[offset + left : offset + right] = b"\x01" * (right - left)
    return pixels


def _make_test_raster_mask(
    variant: RasterMaskFixtureVariant, width: int = 64, height: int = 48
) -> list[int]:
    """生成 row-major fixture；donut_three = 3 个分量 + 1 个孔洞。"""
    pixels = [0] * (width * height)

    def _rect(x0: int, y0: int, x1: int, y1: int, value: int = 1) -> None:
        left = round(x0 * width / 64)
        top = round(y0 * height / 48)
        right = round(x1 * width / 64)
        bottom = round(y1 * height / 48)
        for y in range(top, bottom):
            offset = y * width
            for x in range(left, right):
                pixels[offset + x] = value

    if variant == "single":
        _rect(12, 10, 43, 34)
    elif variant == "donut_three":
        _rect(3, 3, 21, 21)
        _rect(8, 8, 16, 16, 0)
        _rect(29, 5, 41, 18)
        _rect(46, 29, 60, 43)
    elif variant == "diagonal_two":
        _rect(18, 18, 22, 22)
        _rect(22, 22, 26, 26)
    elif variant == "island":
        _rect(24, 31, 28, 35)
    elif variant == "smart_scribble_source":
        return list(_make_smart_scribble_mask(width, height))
    else:
        if len(pixels) < 64:
            raise HTTPException(422, detail="fault source requires at least 64 pixels")
        _rect(6, 30, 18, 42)
        for offset, byte in enumerate(secrets.token_bytes(8)):
            for bit in range(8):
                pixels[offset * 8 + bit] = (byte >> bit) & 1
    return pixels


def _make_sparse_test_rle(width: int, height: int) -> dict:
    """Build a centered 64px square without allocating a full large-canvas buffer."""
    side = min(64, width, height)
    x0 = (width - side) // 2
    y0 = (height - side) // 2
    y1 = y0 + side
    counts: list[int] = []
    offset = 0
    for x in range(x0, x0 + side):
        foreground_start = x * height + y0
        counts.extend([foreground_start - offset, side])
        offset = x * height + y1
    counts.append(width * height - offset)
    return {
        "encoding": "coco_rle",
        "size": [height, width],
        "counts": counts,
    }


async def _fixture_raster_task(
    db, scope, run_id, task_id, user_email=None, *, media_types=("image",)
):
    from app.db.models.dataset import DatasetItem
    from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
    from app.db.models.fixture_run import FixtureRunResource
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.services.identity_e2e_fixtures import IdentityE2EFixtures

    task = await get_core_row(db, task_row, task_id)
    user = (
        await IdentityE2EFixtures(db).find(user_email)
        if user_email is not None
        else None
    )
    if task is None or (user_email is not None and user is None):
        raise HTTPException(404, detail="task or user not found")
    resolved = await _resolve_fixture_scope(db, scope)
    item = (
        await db.get(DatasetItem, task.dataset_item_id)
        if task.dataset_item_id
        else None
    )
    if (
        task.project_id not in resolved.project_ids
        or task.id not in resolved.task_ids
        or (user is not None and user.id not in resolved.user_ids)
        or item is None
        or item.dataset_id not in resolved.dataset_ids
        or task.file_type != item.file_type
        or item.file_type not in media_types
        or task.file_path != item.file_path
    ):
        raise HTTPException(422, detail="expected run-owned raster task and user")
    resources = [
        ("project", task.project_id),
        ("task", task.id),
        ("dataset", item.dataset_id),
        ("item", item.id),
    ]
    if user is not None:
        resources.append(("user", user.id))
    for kind, identifier in resources:
        resource = await db.get(FixtureRunResource, (run_id, kind, identifier))
        if resource is None or resource.state != "published":
            raise HTTPException(422, detail="expected run-owned raster resources")
    verified = await db.scalar(
        select(ManagedOriginalCreation.id)
        .join(
            FixtureRunResource,
            FixtureRunResource.creation_id == ManagedOriginalCreation.id,
        )
        .join(
            DatasetAssetLocation,
            DatasetAssetLocation.creation_id == ManagedOriginalCreation.id,
        )
        .join(DatasetAsset, DatasetAsset.id == DatasetAssetLocation.asset_id)
        .where(
            FixtureRunResource.run_id == run_id,
            FixtureRunResource.kind == "creation",
            FixtureRunResource.item_id == item.id,
            FixtureRunResource.state == "published",
            DatasetAsset.dataset_item_id == item.id,
            DatasetAsset.verification_state == "verified",
            DatasetAsset.checksum_value == ManagedOriginalCreation.sha256,
            DatasetAsset.size_bytes == ManagedOriginalCreation.size_bytes,
            ManagedOriginalCreation.status == "verified",
            ManagedOriginalCreation.settled_at.is_not(None),
            ManagedOriginalCreation.version_id.is_not(None),
            ManagedOriginalCreation.version_id != "null",
            ManagedOriginalCreation.dataset_id == item.dataset_id,
            ManagedOriginalCreation.object_key == item.file_path,
            DatasetAssetLocation.role == "managed_original",
            DatasetAssetLocation.reference_state == "present",
            DatasetAssetLocation.location_id == ManagedOriginalCreation.location_id,
            DatasetAssetLocation.bucket == ManagedOriginalCreation.bucket,
            DatasetAssetLocation.object_key == item.file_path,
        )
    )
    if verified is None:
        raise HTTPException(422, detail="expected run-owned raster original")
    return task, user, item


class PrepareRasterImagesRequest(BaseModel):
    run_id: UUID
    project_id: UUID
    user_email: str


@router.post(
    "/seed/prepare-raster-images", response_model=SeedCleanup, include_in_schema=False
)
async def prepare_raster_images(
    payload: PrepareRasterImagesRequest, db: AsyncSession = Depends(get_db)
) -> SeedCleanup:
    """Settle the current thumbnail work for every Task in the opened list."""
    from app.db.models.fixture_run import (
        FixtureNativeMediaWorkIntent,
        FixtureRunResource,
    )
    from app.services.assets_images import image_gateway, image_policy
    from yoao_assets_images.contract import (
        ImageSourceSelector,
        PrepareImageMediaRequest,
        ReadImageSourceRequest,
        ReadImageSummariesRequest,
        ReadImageWorkRequest,
    )

    async def build(scope, admission_id):
        resolved = await _resolve_fixture_scope(db, scope)
        if payload.project_id not in resolved.project_ids:
            raise HTTPException(422, detail="expected run-owned raster project")
        task_ids = list(
            await db.scalars(
                select(task_row.id)
                .where(task_row.project_id == payload.project_id)
                .order_by(task_row.id)
            )
        )
        if not task_ids:
            raise HTTPException(422, detail="expected run-owned raster tasks")
        targets = []
        for task_id in task_ids:
            resource = await db.get(
                FixtureRunResource, (payload.run_id, "task", task_id)
            )
            if resource is None or resource.state != "published":
                raise HTTPException(422, detail="expected run-owned raster task")
            task, actor, item = await _fixture_raster_task(
                db, scope, payload.run_id, task_id, payload.user_email
            )
            targets.append((task.id, actor.id, item.id, item.dataset_id))
        await db.commit()

        pending = []
        policy = image_policy()
        for task_id, actor_id, item_id, dataset_id in targets:
            intent = FixtureNativeMediaWorkIntent(
                operation_id=uuid4(),
                family="image",
                run_id=payload.run_id,
                admission_id=admission_id,
                dataset_id=dataset_id,
                state="prepared",
            )
            db.add(intent)
            await db.commit()
            async with image_gateway() as owner:
                before = await owner.call(
                    "read_image_source",
                    ReadImageSourceRequest(actor_id=actor_id, task_id=task_id),
                )
                purpose = (
                    "pyramid"
                    if policy.pyramid_eligible(before.width, before.height)
                    else "thumbnail"
                )
                accepted = await owner.call(
                    "prepare_image_media",
                    PrepareImageMediaRequest(
                        operation_id=intent.operation_id,
                        actor_id=actor_id,
                        task_id=task_id,
                        purpose=purpose,
                        expected_source_snapshot=before.source_snapshot,
                        policy=policy,
                    ),
                )
            source = accepted.source
            work = accepted.work
            if (
                source is None
                or source.task_id != task_id
                or source.project_id != payload.project_id
                or source.item_id != item_id
                or source.dataset_id != dataset_id
                or source.source_drifted
                or work.owner != source.owner
                or work.kind != purpose
                or work.source_generation != source.source_generation
                or accepted.spec.expected_source_snapshot != source.source_snapshot
            ):
                raise RuntimeError("Fixture image preparation source changed")
            previous = await db.scalar(
                select(FixtureNativeMediaWorkIntent).where(
                    FixtureNativeMediaWorkIntent.job_id == work.job_id
                )
            )
            if previous is not None:
                if previous.run_id != payload.run_id or previous.state != "settled":
                    raise RuntimeError("Fixture image work ownership changed")
            else:
                intent.job_id = work.job_id
                intent.generation = work.generation
                intent.state = "admitted"
                await db.commit()
            pending.append(
                (intent, previous is not None, actor_id, task_id, source, work)
            )

        deadline = asyncio.get_running_loop().time() + 45
        for intent, reused, actor_id, task_id, source, work in pending:
            while True:
                async with image_gateway() as owner:
                    status = await owner.call(
                        "read_image_work",
                        ReadImageWorkRequest(actor_id=actor_id, job_id=work.job_id),
                    )
                if (
                    status.work.job_id != work.job_id
                    or status.work.generation != work.generation
                    or status.work.owner != work.owner
                    or status.work.kind != work.kind
                    or status.work.source_generation != work.source_generation
                    or status.spec.expected_source_snapshot != source.source_snapshot
                ):
                    raise RuntimeError("Fixture image work generation changed")
                if status.work.status in {"failed", "canceled"}:
                    raise RuntimeError(
                        f"Fixture image work {status.work.status}: {status.error_code}"
                    )
                if status.work.status == "ready":
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError("Fixture image work is not ready")
                await asyncio.sleep(0.25)

        for intent, reused, actor_id, task_id, source, work in pending:
            async with image_gateway() as owner:
                status = await owner.call(
                    "read_image_work",
                    ReadImageWorkRequest(actor_id=actor_id, job_id=work.job_id),
                )
                summaries = await owner.call(
                    "read_image_summaries",
                    ReadImageSummariesRequest(
                        actor_id=actor_id,
                        selectors=[ImageSourceSelector(task_id=task_id)],
                        profile_version=policy.profile_version,
                    ),
                )
            if len(summaries.items) != 1:
                raise RuntimeError("Fixture image publication is not uniquely resolved")
            summary = summaries.items[0]
            current = summary.source
            if work.kind == "pyramid":
                publication_current = (
                    summary.pyramid.status == "ready"
                    and summary.pyramid.generation is not None
                    and (summary.pyramid.width, summary.pyramid.height)
                    == (current.width, current.height)
                )
            else:
                publication_current = current.thumbnail_current
            if (
                status.work.job_id != work.job_id
                or status.work.generation != work.generation
                or status.work.status != "ready"
                or status.work.owner != work.owner
                or status.work.kind != work.kind
                or status.work.source_generation != source.source_generation
                or status.spec.expected_source_snapshot != source.source_snapshot
                or current.owner != source.owner
                or current.task_id != task_id
                or current.project_id != payload.project_id
                or current.item_id != source.item_id
                or current.dataset_id != source.dataset_id
                or current.object_identity != source.object_identity
                or not current.upload_ready
                or current.source_generation != source.source_generation
                or current.source_snapshot != source.source_snapshot
                or current.source_drifted
                or not publication_current
            ):
                raise RuntimeError("Fixture image publication changed")
            if reused:
                await db.delete(intent)
            else:
                intent.state = "settled"
        await db.commit()
        return SeedCleanup()

    return await _run_existing_fixture_seed(
        db, payload.run_id, "raster_image_prepare", build
    )


@router.post(
    "/seed/raster-fault-verify", response_model=SeedCleanup, include_in_schema=False
)
async def verify_prepared_raster_fault(
    payload: PreparedRasterFaultRequest, db: AsyncSession = Depends(get_db)
) -> SeedCleanup:
    """Verify exact precreated fixture references without probing or mutating storage."""
    from app.db.models.fixture_run import FixtureRun, FixtureRunResource
    from app.services.labeling_core_read_relations import annotation_row

    async def build(scope, admission_id):
        run = await db.get(FixtureRun, payload.run_id, populate_existing=True)
        if (
            run is None
            or run.family != "owned"
            or run.parent_run_id != payload.suite_id
            or run.scope_key != payload.namespace
        ):
            raise HTTPException(422, detail="expected owned raster fault run")
        task, user, _item = await _fixture_raster_task(
            db, scope, payload.run_id, payload.task_id, payload.user_email
        )
        identifiers = (payload.healthy_annotation_id, payload.corrupt_annotation_id)
        if task.project_id != payload.project_id or identifiers[0] == identifiers[1]:
            raise HTTPException(422, detail="expected owned raster fault annotations")
        keys = []
        for identifier in identifiers:
            resource = await db.get(
                FixtureRunResource, (payload.run_id, "annotation", identifier)
            )
            annotation = await get_core_row(db, annotation_row, identifier)
            if (
                resource is None
                or resource.state != "published"
                or annotation is None
                or annotation.project_id != task.project_id
                or annotation.task_id != task.id
                or annotation.user_id != user.id
                or annotation.annotation_type != "raster_mask"
                or not annotation.is_active
                or not isinstance(annotation.geometry, dict)
            ):
                raise HTTPException(
                    422, detail="expected owned raster fault annotations"
                )
            mask = annotation.geometry.get("mask")
            if not isinstance(mask, dict) or not isinstance(
                mask.get("object_key"), str
            ):
                raise HTTPException(
                    422, detail="expected owned raster fault annotations"
                )
            keys.append(mask["object_key"])
        if keys[0] == keys[1]:
            raise HTTPException(
                422, detail="raster fault annotations must not share content"
            )
        return SeedCleanup()

    return await _run_existing_fixture_seed(
        db, payload.run_id, "raster_fault_verify", build
    )


@router.post(
    "/seed/inject-raster-mask",
    response_model=InjectRasterMaskResponse,
    include_in_schema=False,
)
async def seed_inject_raster_mask(
    payload: InjectRasterMaskRequest,
    db: AsyncSession = Depends(get_db),
) -> InjectRasterMaskResponse:
    """直插原生 raster annotation，用于 reader / corrupt / lock E2E。

    端点只在隔离 E2E / 测试数据库挂载，刻意绕过 create gate，以便在
    ``read=true/create=false`` 矩阵中构造“已有内容可读”的真实基线。
    """

    async def build(scope, admission_id):
        return await _seed_inject_raster_mask(db, payload, scope)

    return await _run_existing_fixture_seed(db, payload.run_id, "raster_mask", build)


async def _seed_inject_raster_mask(db, payload, scope):
    from app.db.models.annotation import Annotation
    from app.services.raster_mask_storage import (
        store_coco_rle,
        lock_raster_mask_references,
    )
    from app.utils.raster_mask_rle import encode_coco_rle

    task, user, item = await _fixture_raster_task(
        db, scope, payload.run_id, payload.task_id, payload.user_email
    )

    if payload.canvas in {"5k", "8k"} and payload.variant != "single":
        raise HTTPException(
            status_code=422,
            detail="large-canvas raster fixture only supports the single variant",
        )
    if payload.canvas == "media":
        if item is None or item.width is None or item.height is None:
            raise HTTPException(status_code=404, detail="media fixture not found")
        width, height = int(item.width), int(item.height)
    else:
        dimensions = {
            "default": (64, 48),
            "5k": (5120, 2880),
            "8k": (8192, 8192),
        }
        width, height = dimensions[payload.canvas]
    if payload.canvas in {"default", "media"}:
        rle = encode_coco_rle(
            _make_test_raster_mask(payload.variant, width, height), width, height
        )
    else:
        rle = _make_sparse_test_rle(width, height)
    if (item.width, item.height) != (width, height):
        raise HTTPException(422, detail="raster canvas does not match its original")
    reference = await store_coco_rle(rle, db=db)

    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        source="manual",
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name=payload.label,
        geometry={"type": "raster_mask", "mask": reference},
        confidence=1,
        is_locked=payload.locked,
    )
    annotation = (await LabelingE2EFixtureWrites(db).insert_annotations([annotation]))[
        0
    ]
    from app.services import fixture_run_ownership as ownership

    await ownership.reserve(
        db,
        payload.run_id,
        "annotation",
        annotation.id,
        logical_key=f"raster-{annotation.id.hex}",
        published=True,
    )
    await LabelingE2EFixtureWrites(db).patch_task(
        task, total_annotations=int(task.total_annotations or 0) + 1
    )
    annotation_id = str(annotation.id)
    await lock_raster_mask_references(db, reference)
    await db.commit()
    return InjectRasterMaskResponse(
        annotation_id=annotation_id,
        variant=payload.variant,
        mask=reference,
    )


@router.post(
    "/seed/inject-raster-prediction",
    response_model=InjectRasterPredictionResponse,
    include_in_schema=False,
)
async def seed_inject_raster_prediction(
    payload: InjectRasterMaskRequest,
    db: AsyncSession = Depends(get_db),
) -> InjectRasterPredictionResponse:
    """Create a raw raster candidate fixture for the accept write-gate E2E."""

    async def build(scope, admission_id):
        return await _seed_inject_raster_prediction(db, payload, scope)

    return await _run_existing_fixture_seed(
        db, payload.run_id, "raster_prediction", build
    )


async def _seed_inject_raster_prediction(db, payload, scope):
    from app.services.labeling_write_scope import capture_labeling_write_scope
    from app.services.prediction import PredictionService
    from app.services.raster_mask_storage import (
        lock_raster_mask_references,
        store_coco_rle,
    )
    from app.utils.raster_mask_rle import encode_coco_rle

    task, _user, item = await _fixture_raster_task(
        db, scope, payload.run_id, payload.task_id, payload.user_email
    )
    if (item.width, item.height) != (64, 48):
        raise HTTPException(422, detail="raster prediction requires a 64x48 original")
    rle = encode_coco_rle(_make_test_raster_mask("single"), 64, 48)
    reference = await store_coco_rle(rle, db=db)
    write_scope = capture_labeling_write_scope(
        task,
        actor_id=None,
        actor_known=True,
        operation="test_seed",
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
    )
    prediction = await PredictionService(db).create_test_seed_prediction(
        task_id=task.id,
        project_id=task.project_id,
        tool_unit_id="region",
        score=0.9,
        result=[
            {
                "type": "raster_mask",
                "tool_unit_id": "region",
                "class_name": payload.label,
                "geometry": {"type": "raster_mask", "mask": reference},
                "confidence": 0.9,
            }
        ],
        write_scope=write_scope,
    )
    prediction_id = str(prediction.id)
    await lock_raster_mask_references(db, reference)
    await db.commit()
    return InjectRasterPredictionResponse(
        prediction_id=prediction_id,
        mask=reference,
    )


class AdvanceTaskRequest(BaseModel):
    """E2E 辅助：直接把 task 推到目标状态，绕过 UI 链路。

    主要服务于 batch-flow.spec 的多角色串联（避免每个 spec 都重复画 bbox）。
    """

    task_id: str
    to_status: str
    annotator_email: str | None = None
    reviewer_email: str | None = None


class AdvanceTaskResponse(BaseModel):
    task_id: str
    status: str


@router.post(
    "/seed/advance_task",
    response_model=AdvanceTaskResponse,
    include_in_schema=False,
)
async def seed_advance_task(
    payload: AdvanceTaskRequest,
    db: AsyncSession = Depends(get_db),
) -> AdvanceTaskResponse:
    """绕过状态机直接置 task 到目标状态。E2E 写实化用，不调审计。"""
    from datetime import datetime, timezone
    from uuid import UUID
    from sqlalchemy import select
    from app.services.identity_e2e_fixtures import IdentityE2EFixtures

    res = await db.execute(select(task_row).where(task_row.id == UUID(payload.task_id)))
    task = res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"task {payload.task_id} not found")

    now = datetime.now(timezone.utc)
    changes: dict[str, Any] = {"status": payload.to_status}
    identity = IdentityE2EFixtures(db)

    if payload.annotator_email:
        anno = await identity.find(payload.annotator_email)
        if anno:
            changes["assignee_id"] = anno.id
            changes["assigned_at"] = task.assigned_at or now
    if payload.reviewer_email:
        rev = await identity.find(payload.reviewer_email)
        if rev:
            changes["reviewer_id"] = rev.id
            changes["reviewer_claimed_at"] = task.reviewer_claimed_at or now

    if payload.to_status == "submitted":
        changes["submitted_at"] = task.submitted_at or now
        changes["is_labeled"] = True
    elif payload.to_status in ("completed", "rejected"):
        changes["reviewed_at"] = now

    task = await LabelingE2EFixtureWrites(db).patch_task(task, **changes)
    await db.commit()
    return AdvanceTaskResponse(task_id=str(task.id), status=task.status)
