"""Manager-owned automatic training policy and paginated trigger history."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from app.db.models.learning_policy import LearningPolicy, LearningTrigger
from app.db.models.project import Project
from app.db.models.training_run import TrainingRun
from app.deps import (
    get_current_user,
    get_db,
    get_training_dataset_dispatcher,
    get_training_authority,
    require_project_owner,
    require_scopes,
)
from app.schemas.learning_policy import (
    LearningPolicyOut,
    LearningPolicySave,
    LearningTriggerOut,
    LearningTriggerPage,
)
from app.services import learning_policies as service

from app.services.training_dataset_dispatch import TrainingDatasetDispatcher
from app.services.training_authority_contract import TrainingAuthorityPort
from app.services.identity_contracts import IdentityActorFacts

router = APIRouter(
    prefix="/projects/{project_id}/learning-policy",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


@router.get("")
async def get_policy(
    project: Project = Depends(require_project_owner),
    db=Depends(get_db),
    task: Literal["detect", "segment"] = "detect",
) -> LearningPolicyOut | None:
    return await db.scalar(
        select(LearningPolicy).where(
            LearningPolicy.project_id == project.id, LearningPolicy.task == task
        )
    )


@router.put(
    "",
    response_model=LearningPolicyOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def save_policy(
    body: LearningPolicySave,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db=Depends(get_db),
):
    policy = await service.save(
        db,
        project,
        user,
        body,
        api_key_id=getattr(request.state, "api_key_id", None),
    )
    await db.commit()
    return policy


@router.post(
    "/disable",
    response_model=LearningPolicyOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def disable_policy(
    task: Literal["detect", "segment"] = "detect",
    expected_revision: int = Query(ge=1),
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db=Depends(get_db),
):
    policy = await service.disable(db, project.id, user, expected_revision, task=task)
    await db.commit()
    return policy


@router.post(
    "/check",
    response_model=LearningPolicyOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def check_policy(
    task: Literal["detect", "segment"] = "detect",
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db=Depends(get_db),
    dataset_dispatcher: TrainingDatasetDispatcher = Depends(
        get_training_dataset_dispatcher
    ),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
):
    policy = await service.check_project(
        db,
        project,
        user,
        task=task,
        training_authority=training_authority,
    )
    await db.commit()
    await service.dispatch_pending(db, policy.id, dispatcher=dataset_dispatcher)
    await db.refresh(policy)
    return policy


@router.get("/triggers", response_model=LearningTriggerPage)
async def list_triggers(
    task: Literal["detect", "segment"] = "detect",
    project: Project = Depends(require_project_owner),
    db=Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    total = await db.scalar(
        select(func.count())
        .select_from(LearningTrigger)
        .where(
            LearningTrigger.project_id == project.id,
            LearningTrigger.policy_id.in_(
                select(LearningPolicy.id).where(
                    LearningPolicy.project_id == project.id, LearningPolicy.task == task
                )
            ),
        )
    )
    triggers = list(
        await db.scalars(
            select(LearningTrigger)
            .where(
                LearningTrigger.project_id == project.id,
                LearningTrigger.policy_id.in_(
                    select(LearningPolicy.id).where(
                        LearningPolicy.project_id == project.id,
                        LearningPolicy.task == task,
                    )
                ),
            )
            .order_by(LearningTrigger.created_at.desc(), LearningTrigger.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    run_ids = (
        {
            r.auto_trigger_id: str(r.id)
            for r in await db.scalars(
                select(TrainingRun).where(
                    TrainingRun.auto_trigger_id.in_([t.id for t in triggers])
                )
            )
        }
        if triggers
        else {}
    )
    return {
        "total": total,
        "items": [
            {
                **LearningTriggerOut.model_validate(t).model_dump(mode="json"),
                "run_id": run_ids.get(t.id),
            }
            for t in triggers
        ],
    }


@router.delete(
    "",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def delete_policy(
    task: Literal["detect", "segment"] = "detect",
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db=Depends(get_db),
):
    if await service.delete(db, project.id, user, task=task):
        await db.commit()
