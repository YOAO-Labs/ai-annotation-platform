"""Owner-only SQL oracle for pre-extraction Jobs list behavior."""

import uuid

from sqlalchemy import and_, func, or_, select

from app.db.enums import PlatformRole, ProjectRole
from app.db.models.async_job import AsyncJob
from app.services.async_job_access import EXPORT_JOB_KIND, GLOBAL_ASYNC_JOB_KINDS
from app.services.identity_contracts import IdentityActorFacts, IdentityActorStatus
from app.services.project_aggregates import project_scope_clause
from app.services.workspace_read_relations import project_search_scope


def legacy_async_job_query(
    *,
    current_user: IdentityActorFacts | IdentityActorStatus,
    status: list[str] | None,
    kind: list[str] | None,
    project_id: uuid.UUID | None,
    search: str | None,
):
    project_scope_id = func.coalesce(AsyncJob.project_id, AsyncJob.retained_project_id)
    workspace_projects = project_search_scope(current_user)
    project_exists = (
        select(workspace_projects.c.project_id)
        .where(workspace_projects.c.project_id == project_scope_id)
        .correlate(AsyncJob)
        .exists()
    )
    unresolved_scope = and_(
        project_scope_id.is_(None),
        AsyncJob.kind.not_in(GLOBAL_ASYNC_JOB_KINDS),
    )
    query = select(AsyncJob).where(
        ~unresolved_scope,
        or_(project_scope_id.is_(None), project_exists),
    )

    if current_user.role != PlatformRole.SUPER_ADMIN.value:
        project_scope = project_scope_clause(
            current_user, project_scope_id, project_roles=()
        )
        export_scope = project_scope_clause(
            current_user,
            project_scope_id,
            project_roles=(ProjectRole.REVIEWER.value,),
        )
        query = query.where(
            AsyncJob.user_id == current_user.id,
            or_(
                and_(
                    AsyncJob.kind != EXPORT_JOB_KIND,
                    project_scope_id.is_(None),
                ),
                project_scope,
            ),
            or_(
                AsyncJob.kind != EXPORT_JOB_KIND,
                and_(project_scope_id.is_not(None), export_scope),
            ),
        )
    if status:
        query = query.where(AsyncJob.status.in_(status))
    if kind:
        query = query.where(AsyncJob.kind.in_(kind))
    if project_id:
        query = query.where(project_scope_id == project_id)

    search_text = search.strip() if search else ""
    if search_text:
        pattern = f"%{search_text}%"
        query = query.where(
            or_(
                AsyncJob.payload["prompt"].astext.ilike(pattern),
                AsyncJob.payload["batch_display_id"].astext.ilike(pattern),
                AsyncJob.payload["task_display_id"].astext.ilike(pattern),
                AsyncJob.payload["model_key"].astext.ilike(pattern),
                AsyncJob.payload["ml_backend_name"].astext.ilike(pattern),
                AsyncJob.payload["error_type"].astext.ilike(pattern),
            )
        )
    return query
