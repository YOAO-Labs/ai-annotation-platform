"""Manager-owned finite search preview, admission, cancellation and results."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from app.api.v1.training_datasets import lock_owner
from app.db.models.project import Project
from app.db.models.training_study import TrainingStudy, TrainingTrial
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.schemas.training_study import TrainingStudyCreate
from app.services import training_initialization, training_studies as service
from app.services.learning_policies import project_lock
from app.services.training_dataset_bridge import content_digest

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
    await project_lock(db, project.id)
    await lock_owner(db, user, project)
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
    await project_lock(db, project.id)
    await lock_owner(db, user, project)
    request_digest = content_digest(body.request_payload())
    existing = await db.scalar(
        select(TrainingStudy).where(
            TrainingStudy.project_id == project.id,
            TrainingStudy.request_id == body.base.request_id,
        )
    )
    if existing:
        if existing.request_digest != request_digest:
            raise HTTPException(
                status_code=409, detail="同一请求标识不能启动不同参数搜索"
            )
        return await service.describe(db, existing)
    if project.status != "in_progress":
        raise HTTPException(status_code=409, detail="项目停用时不能启动参数搜索")
    plan, digest = await service.freeze_plan(db, project.id, body)
    if body.preview_digest != digest:
        raise HTTPException(
            status_code=409, detail="搜索计划已变化或尚未预览，请重新核对全部组合和预算"
        )
    first = plan["trials"][0]["config"]
    study = TrainingStudy(
        id=uuid.UUID(plan["study_id"]),
        project_id=project.id,
        creator_id=user.id,
        dataset_id=body.base.dataset_id,
        initialization_model_id=training_initialization.model_id(
            first.get("initialization")
        ),
        request_id=body.base.request_id,
        request_digest=request_digest,
        plan_digest=digest,
        name=body.base.name,
        plan=plan,
        origin=plan["origin"],
        authorization={"api_key_id": getattr(request.state, "api_key_id", None)},
    )
    db.add(study)
    await db.flush()
    for trial in plan["trials"]:
        db.add(
            TrainingTrial(
                id=uuid.UUID(trial["id"]),
                study_id=study.id,
                ordinal=trial["ordinal"],
                config=trial["config"],
                config_digest=trial["config_digest"],
                values=trial["values"],
            )
        )
    await db.flush()
    await service.coordinate(db, study.id)
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
    await project_lock(db, project.id)
    await lock_owner(db, user, project)
    study = await service.load(db, project.id, study_id, lock=True)
    study.scheduling = False
    study.status = "cancelling"
    study.message = "已停止后续试验；活动进程按普通训练取消协议退出"
    for run in await service.children(db, study.id, lock=True):
        await service.stop_child(db, run)
    await service.coordinate(db, study.id)
    await db.commit()
    return await service.describe(db, study)
