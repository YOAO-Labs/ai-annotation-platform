"""SQL-backed workspace access resolution and transaction locks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
import json
import uuid
from typing import NoReturn

from fastapi import HTTPException, status
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from aap_workspace_contract.access import (
    ProjectAccess as _ProjectAccess,
    ProjectAccessPolicyFailure,
    project_manager_access,
    project_member_access,
    ProjectWritePolicyFailure,
    ProjectWritePolicyRequest,
    ProjectWritePolicyResolver,
    membership_role_compatible as _membership_role_compatible,
    platform_role_is_manager as _platform_role_is_manager,
)
from aap_workspace_contract.member_values import (
    ProjectMemberRowsUnavailable,
    decode_project_member_rows,
)
from aap_workspace_contract.project_query_contract import (
    ProjectQueryUnavailable,
    decode_project_query_step,
    project_query_request,
)
from aap_workspace_contract.project_query_values import materialize_project_base_row
from aap_workspace_contract.wire import _reject_constant, _unique_object
from app.db.enums import (
    PlatformRole,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.services.task_creation_contract import (
    ProjectManagerFacts,
    decode_creation_reply,
)
from app.services.identity_contracts import IdentityActorFacts, IdentityActorStatus
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.project_workspace import (
    merge_project_snapshot as _merge_project_snapshot,
)
from app.services.workspace_project_query_data import WorkspaceProjectQueryData

_WORKSPACE_PROJECT_ACCESS_STEP = text(
    "SELECT aap_workspace_v1.project_access_step("
    "CAST(:actor_id AS uuid), CAST(:project_id AS uuid), CAST(:phase AS text))"
)
_WORKSPACE_PROJECT_READ = text(
    "SELECT aap_workspace_v1.read_project_row(CAST(:project_id AS uuid))"
)
_WORKSPACE_PROJECT_ACCESS_STEP_SCHEMA = "aap-workspace-project-access-step/v1"


def _decode_workspace_project_access_step(
    payload: object,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    phase: str,
) -> dict[str, object]:
    if type(payload) is str:
        try:
            payload = json.loads(
                payload,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("Invalid Workspace Project access step reply") from None
    if (
        type(payload) is not dict
        or set(payload) != {"schema", "actor_id", "project_id", "phase", "facts"}
        or type(payload["schema"]) is not str
        or payload["schema"] != _WORKSPACE_PROJECT_ACCESS_STEP_SCHEMA
        or type(payload["actor_id"]) is not str
        or payload["actor_id"].lower() != str(actor_id)
        or type(payload["project_id"]) is not str
        or payload["project_id"].lower() != str(project_id)
        or type(payload["phase"]) is not str
        or payload["phase"] != phase
        or type(payload["facts"]) is not dict
    ):
        raise ValueError("Invalid Workspace Project access step reply")

    facts = payload["facts"]
    if phase in {"read_project_row", "lock_project_key_share"}:
        if set(facts) != {"row_json"} or (
            facts["row_json"] is not None and type(facts["row_json"]) is not str
        ):
            raise ValueError("Invalid Workspace Project access project facts")
    elif phase == "lock_member_share":
        if set(facts) != {"rows_json"} or type(facts["rows_json"]) is not str:
            raise ValueError("Invalid Workspace Project access member facts")
    else:
        raise ValueError("Unexpected Workspace Project access step phase")
    return facts


async def _lock_project_actor(
    db: AsyncSession, *, actor_id: uuid.UUID
) -> IdentityActorFacts:
    try:
        accounts = await IdentityAccountLockData(db).lock_accounts(
            [actor_id], mode="share", nowait=True
        )
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
            exc.orig, "pgcode", None
        )
        if sqlstate in {"55P03", "40001", "40P01"}:
            raise HTTPException(
                status_code=409, detail={"reason": "account_busy"}
            ) from exc
        raise
    user = accounts.get(actor_id)
    if user is None or not user.is_active:
        raise _invalid_account()
    return user


async def _locked_workspace_project_access_phase(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    phase: str,
    busy_reason: str,
) -> dict[str, object]:
    return _decode_workspace_project_access_step(
        await _locked_access_row(
            db,
            _WORKSPACE_PROJECT_ACCESS_STEP,
            busy_reason=busy_reason,
            parameters={
                "actor_id": actor_id,
                "project_id": project_id,
                "phase": phase,
            },
        ),
        actor_id=actor_id,
        project_id=project_id,
        phase=phase,
    )


async def _read_unlocked_project_row(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
) -> Project | None:
    """Read the initial by-id row without locking or consulting actor status."""
    if db.autoflush:
        await db.flush()
    values = await _workspace_project_values(
        db,
        actor_id=actor_id,
        project_id=project_id,
    )
    if values is None:
        return None
    return await _merge_project_snapshot(db, Project, values)


async def _workspace_project_values(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> dict[str, object] | None:
    if actor_id is None or project_id is None:
        row_json = await db.scalar(_WORKSPACE_PROJECT_READ, {"project_id": project_id})
        if row_json is not None and type(row_json) is not str:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="项目查询服务暂不可用",
            )
    else:
        payload = await db.scalar(
            _WORKSPACE_PROJECT_ACCESS_STEP,
            {
                "actor_id": actor_id,
                "project_id": project_id,
                "phase": "read_project_row",
            },
        )
        facts = _decode_workspace_project_access_step(
            payload,
            actor_id=actor_id,
            project_id=project_id,
            phase="read_project_row",
        )
        row_json = facts["row_json"]
    try:
        return materialize_project_base_row(row_json, target_id=project_id)
    except ProjectQueryUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="项目查询服务暂不可用",
        ) from None


def _restore_unloaded_project_fields(
    project: Project, values: dict[str, object]
) -> None:
    unloaded = set(inspect(project).unloaded)
    for key, value in values.items():
        if key in unloaded:
            set_committed_value(project, key, value)


async def _load_after_expired_project_disappeared(
    db: AsyncSession,
    *,
    cached: Project,
    actor_id: uuid.UUID | None,
    project_id: uuid.UUID,
) -> Project | None:
    # Session.get removes a fully expired identity that vanished during refresh,
    # then performs its ordinary primary-key SELECT once more.
    db.sync_session._remove_newly_deleted([inspect(cached)])
    if db.autoflush:
        await db.flush()
    values = await _workspace_project_values(
        db,
        actor_id=actor_id,
        project_id=project_id,
    )
    if values is None:
        return None
    return await _merge_project_snapshot(db, Project, values)


async def get_workspace_project(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None,
    populate_existing: bool = False,
) -> Project | None:
    """Preserve Session.get semantics while reading rows through Workspace."""
    identity_key = Project.__mapper__.identity_key_from_primary_key((project_id,))
    cached = db.sync_session.identity_map.get(identity_key)
    state = inspect(cached) if cached is not None else None

    # Session.get returns any non-fully-expired identity directly, including an
    # object with only some unloaded/expired columns. This path emits no SQL and
    # must not flush unrelated pending work.
    if not populate_existing and cached is not None and not state.expired:
        return cached

    expired_dirty_autoflush = bool(
        db.autoflush and cached is not None and state.expired and state.modified
    )
    if expired_dirty_autoflush:
        # The old ORM refresh first restored expired PK columns before its unit
        # of work flushed. Reading once under no_autoflush avoids a private
        # Project SELECT during flush; the second owner read preserves the
        # refresh/read order after pending Project and Task writes complete.
        with db.no_autoflush:
            values = await _workspace_project_values(
                db,
                actor_id=actor_id,
                project_id=project_id,
            )
        if values is None:
            if populate_existing:
                return None
            return await _load_after_expired_project_disappeared(
                db,
                cached=cached,
                actor_id=actor_id,
                project_id=project_id,
            )
        _restore_unloaded_project_fields(cached, values)
        await db.flush()
        values = await _workspace_project_values(
            db,
            actor_id=actor_id,
            project_id=project_id,
        )
        if values is None:
            return None
        if populate_existing:
            return await _merge_project_snapshot(db, Project, values)
        _restore_unloaded_project_fields(cached, values)
        return cached

    if db.autoflush:
        await db.flush()
    values = await _workspace_project_values(
        db,
        actor_id=actor_id,
        project_id=project_id,
    )
    if values is None:
        if not populate_existing and cached is not None and state.expired:
            return await _load_after_expired_project_disappeared(
                db,
                cached=cached,
                actor_id=actor_id,
                project_id=project_id,
            )
        return None

    if populate_existing:
        return await _merge_project_snapshot(db, Project, values)
    if cached is not None:
        _restore_unloaded_project_fields(cached, values)
        return cached
    return await _merge_project_snapshot(db, Project, values)


async def _read_project_query_row(
    db: AsyncSession,
    *,
    request,
    transaction,
    project_id: uuid.UUID,
) -> Project:
    raw = await transaction.step(request, "get_row")
    facts = decode_project_query_step(request, "get_row", None, raw).facts
    values = materialize_project_base_row(facts.row_json, target_id=project_id)
    if values is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return await _merge_project_snapshot(db, Project, values)


async def _read_project_query_member(
    db: AsyncSession,
    *,
    request,
    transaction,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
) -> ProjectMember | None:
    raw = await transaction.step(request, "access_member_rows")
    facts = decode_project_query_step(request, "access_member_rows", None, raw).facts
    try:
        members = decode_project_member_rows(
            facts.rows_json,
            target_id=project_id,
            user_id=actor_id,
            max_rows=1,
        )
    except ProjectMemberRowsUnavailable:
        raise ProjectQueryUnavailable() from None
    member = members[0] if members else None
    if member is None:
        return None
    return await _merge_project_snapshot(db, ProjectMember, asdict(member))


async def lock_creation_project_manager(
    db: AsyncSession, *, actor_id: uuid.UUID, project_id: uuid.UUID
) -> ProjectManagerFacts:
    """Resolve manager facts through the Workspace-owned data capability."""
    with db.no_autoflush:
        payload = await db.scalar(
            text(
                "SELECT aap_workspace_v1.lock_project_manager("
                "CAST(:actor_id AS uuid), CAST(:project_id AS uuid))"
            ),
            {"actor_id": actor_id, "project_id": project_id},
        )
    result = decode_creation_reply(
        payload, "aap-project-manager/v1", ProjectManagerFacts
    )
    if result.actor_id != actor_id or result.project_id != project_id:
        raise ValueError("Creation authority does not match its request")
    return result


def assert_membership_role_compatible(platform_role: str, project_role: str) -> None:
    if _membership_role_compatible(platform_role, project_role):
        return
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，"
            "平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目"
        ),
    )


def _invalid_account() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="账号已停用或不存在",
        headers={"WWW-Authenticate": "Bearer"},
    )


def is_privileged_for_project(
    user: IdentityActorFacts | IdentityActorStatus, project: Project
) -> bool:
    """An active super administrator or a project owner with an admin role."""

    if not user.is_active:
        return False
    if user.role == PlatformRole.SUPER_ADMIN.value:
        return True
    return _platform_role_is_manager(user.role) and project.owner_id == user.id


async def resolve_project_access(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project: Project,
    lock_membership: bool = False,
) -> _ProjectAccess:
    """Resolve the immutable access context for ``actor_id`` on ``project``.

    Always re-reads current account and project state so a revoked account or a
    transferred project cannot authorize from a stale identity map entry.

    ``lock_membership=True`` fences account, project and membership authority in
    that order, so disable, ownership transfer and role change cannot commit
    between authorization and the caller's write.

    Raises 401 for an inactive/missing account, 404 when the account cannot see
    the project at all (existence is hidden) and 403 when a visible membership
    is internally inconsistent.
    """
    if lock_membership:
        _, _, access = await lock_current_project_access(
            db, actor_id=actor_id, project_id=project.id
        )
        return access

    actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
    if actor is None or not actor.is_active:
        raise _invalid_account()
    request = project_query_request(
        "get_project_access",
        actor.id,
        getattr(actor.role, "value", actor.role),
        target_id=project.id,
    )
    try:
        data = WorkspaceProjectQueryData(db)
        async with data.transaction() as transaction:
            fresh_project = await _read_project_query_row(
                db,
                request=request,
                transaction=transaction,
                project_id=project.id,
            )

            async def read_member():
                return await _read_project_query_member(
                    db,
                    request=request,
                    transaction=transaction,
                    actor_id=actor.id,
                    project_id=project.id,
                )

            return await _resolve_current_project_access(
                db,
                user=actor,
                project=fresh_project,
                lock_membership=False,
                member_reader=read_member,
            )
    except ProjectQueryUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="项目查询服务暂不可用",
        ) from None


async def _resolve_current_project_access(
    db: AsyncSession,
    *,
    user: IdentityActorFacts | IdentityActorStatus,
    project: Project,
    lock_membership: bool,
    member_reader: Callable[[], Awaitable[ProjectMember | None]] | None = None,
) -> _ProjectAccess:
    try:
        manager = project_manager_access(
            actor_id=user.id,
            platform_role=user.role,
            project_id=project.id,
            owner_id=project.owner_id,
        )
        if manager is not None:
            return manager

        # Retain the caller's refreshed row and optional lock before pure policy.
        if lock_membership:
            member_facts = await _locked_workspace_project_access_phase(
                db,
                actor_id=user.id,
                project_id=project.id,
                phase="lock_member_share",
                busy_reason="membership_busy",
            )
            members = decode_project_member_rows(
                member_facts["rows_json"],
                target_id=project.id,
                user_id=user.id,
                max_rows=1,
            )
            member = members[0] if members else None
            if member is not None:
                await _merge_project_snapshot(db, ProjectMember, asdict(member))
        else:
            assert member_reader is not None
            member = await member_reader()
        return project_member_access(
            actor_id=user.id,
            platform_role=user.role,
            project_id=project.id,
            membership_id=member.id if member is not None else None,
            project_role=member.role if member is not None else None,
            membership_version=member.version if member is not None else None,
        )
    except ProjectAccessPolicyFailure as failure:
        raise HTTPException(
            status_code=failure.status_code, detail=failure.detail
        ) from failure


async def _locked_access_row(
    db: AsyncSession,
    statement,
    *,
    busy_reason: str,
    parameters: dict[str, object] | None = None,
):
    try:
        result = (
            await db.execute(statement)
            if parameters is None
            else await db.execute(statement, parameters)
        )
        return result.scalar_one_or_none()
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
            exc.orig, "pgcode", None
        )
        if sqlstate in {"55P03", "40001", "40P01"}:
            raise HTTPException(
                status_code=409, detail={"reason": busy_reason}
            ) from exc
        raise


async def lock_current_project_rows(
    db: AsyncSession, *, actor_id: uuid.UUID, project_id: uuid.UUID
) -> tuple[IdentityActorFacts, Project]:
    """Refresh and fence account/project rows without granting project visibility."""
    with db.no_autoflush:
        user = await _lock_project_actor(db, actor_id=actor_id)
        project_facts = await _locked_workspace_project_access_phase(
            db,
            actor_id=actor_id,
            project_id=project_id,
            phase="lock_project_key_share",
            busy_reason="project_busy",
        )
        project_values = materialize_project_base_row(
            project_facts["row_json"], target_id=project_id
        )
        if project_values is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        project = await _merge_project_snapshot(db, Project, project_values)
    return user, project


async def lock_current_project_access(
    db: AsyncSession, *, actor_id: uuid.UUID, project_id: uuid.UUID
) -> tuple[IdentityActorFacts, Project, _ProjectAccess]:
    """Resolve authority under account -> project -> membership share locks.

    Locks are bounded and retained until the caller commits or rolls back.
    Project KEY SHARE fences explicit ownership/deletion FOR UPDATE locks while
    allowing ordinary counter deltas without a SHARE-to-UPDATE upgrade cycle.
    This is a local database admission boundary, not a portable authority token.
    """
    user, project = await lock_current_project_rows(
        db, actor_id=actor_id, project_id=project_id
    )
    with db.no_autoflush:
        access = await _resolve_current_project_access(
            db, user=user, project=project, lock_membership=True
        )
    return user, project, access


class SqlProjectWritePolicyResolver:
    """Bind write-policy resolution to the caller's transaction session."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def resolve(self, request: ProjectWritePolicyRequest) -> _ProjectAccess:
        try:
            _, _, access = await lock_current_project_access(
                self._db,
                actor_id=request.actor_id,
                project_id=request.project_id,
            )
        except HTTPException as exc:
            raise ProjectWritePolicyFailure(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers,
            ) from exc
        return access


def bind_project_write_policy_resolver(
    db: AsyncSession,
) -> ProjectWritePolicyResolver:
    """Bind the session-free policy port to the caller-owned SQL transaction."""
    return SqlProjectWritePolicyResolver(db)


def restore_project_write_policy_failure(
    failure: ProjectWritePolicyFailure,
) -> NoReturn:
    """Restore a resolver failure as the existing HTTP response at its boundary."""
    raise HTTPException(
        status_code=failure.status_code,
        detail=failure.detail,
        headers=failure.headers,
    ) from failure


async def resolve_project_access_by_id(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    lock_membership: bool = False,
) -> tuple[Project, _ProjectAccess]:
    try:
        project = await _read_unlocked_project_row(
            db, actor_id=actor_id, project_id=project_id
        )
    except ProjectQueryUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="项目查询服务暂不可用",
        ) from None
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    access = await resolve_project_access(
        db, actor_id=actor_id, project=project, lock_membership=lock_membership
    )
    return project, access


def assert_context_matches_project(
    access: _ProjectAccess, project_id: uuid.UUID
) -> None:
    """Reject a context/resource project mismatch before any read or write."""

    if access.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "access_context_mismatch",
                "detail": "访问上下文与资源项目不一致",
            },
        )


def assert_capability(access: _ProjectAccess, *capabilities: str) -> None:
    missing = sorted(cap for cap in capabilities if cap not in access.capabilities)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"缺少项目权限: {', '.join(missing)}",
        )
