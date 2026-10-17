"""Manager-owned finite search preview, admission, cancellation and results."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from app.services.project_write_guard import lock_project_manager
from app.db.models.project import Project
from app.db.models.training_study import TrainingStudy
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.schemas.training_study import TrainingStudyCreate
from app.services import training_studies as service
from app.services import training_claims as claims

router = APIRouter(
    prefix="/projects/{project_id}/training-studies",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


@router.post(":preview", dependencies=[Depends(require_scopes("datasets:write"))])
async def preview(
    body: TrainingStudyCreate,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    await claims.project_lock(db, project.id)
    await lock_project_manager(db, user, project.id)
    if project.status != "in_progress":
        raise HTTPException(status_code=409, detail="项目停用时不能启动参数搜索")
    plan, digest = await service.freeze_plan(db, project.id, body)
    return {"plan": plan, "preview_digest": digest}


@router.post(
    "", status_code=202, dependencies=[Depends(require_scopes("datasets:write"))]
)
async def create(
    body: TrainingStudyCreate,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    study, created = await service.create(
        db,
        project,
        user,
        body,
        api_key_id=getattr(request.state, "api_key_id", None),
    )
    if created:
        await db.commit()
    return await service.describe(db, study)


@router.get("")
async def list_studies(
    project: Project = Depends(require_project_owner),
    db=Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    query = select(TrainingStudy).where(TrainingStudy.project_id == project.id)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    items = list(
        await db.scalars(
            query.order_by(TrainingStudy.created_at.desc(), TrainingStudy.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return {
        "total": total,
        "items": [
            {
                "id": str(study.id),
                "name": study.name,
                "status": study.status,
                "scheduling": study.scheduling,
                "message": study.message,
                "created_at": study.created_at,
                "trial_count": len(study.plan["trials"]),
            }
            for study in items
        ],
    }


@router.get("/{study_id}")
async def get_study(
    study_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db=Depends(get_db),
) -> dict:
    return await service.describe(db, await service.load(db, project.id, study_id))


@router.post(
    "/{study_id}/cancel", dependencies=[Depends(require_scopes("datasets:write"))]
)
async def cancel(
    study_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    study = await service.cancel(db, project.id, study_id, user)
    await db.commit()
    return await service.describe(db, study)
