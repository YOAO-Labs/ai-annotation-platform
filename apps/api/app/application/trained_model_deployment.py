"""Compose durable trained-model installation checks around backend IO."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.services import trained_model_backend as backend_io
from app.services import trained_models as learning
from app.services.project_write_guard import lock_project_manager
from app.services.training_claims import project_lock


async def deploy_model(
    db: AsyncSession,
    project: Project,
    actor_id: uuid.UUID,
    version_id: uuid.UUID,
    registry_id: uuid.UUID,
):
    """Persist a fenced check, install/trial, then publish its matching result."""
    await project_lock(db, project.id)
    await lock_project_manager(db, actor_id, project.id)
    version = await learning.load_version(db, project.id, version_id)
    learning.validate_bindings(
        project, version.classes, version.bindings, learning.model_task(version)
    )
    await learning.enabled_backend(db, project.id, registry_id)
    deployment = await learning.lock_deployment(db, version.id, registry_id)
    if deployment and deployment.status in {"draining", "retired"}:
        raise HTTPException(status_code=409, detail="该部署已停止创建新引用")
    if deployment and deployment.status == "ready":
        try:
            receipt = await backend_io.live_receipt(db, registry_id, str(version.id))
            learning.check_receipt(version, receipt)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise HTTPException(
                    status_code=503, detail="暂时无法复查推理实例，请稍后重试"
                ) from exc
        except ValueError:
            pass
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="暂时无法复查推理实例，请稍后重试"
            ) from exc
        else:
            learning.check_trial_runtime(version, deployment, receipt)
            return deployment

    deployment, check_id = await learning.begin_deployment_check(
        db, version, registry_id, deployment
    )
    deployment_id = deployment.id
    await db.commit()

    error = None
    conflict = None
    try:
        receipt, trial = await backend_io.install_and_trial(
            db, project, version, deployment
        )
    except learning.PinnedRuntimeConflict as exc:
        receipt, trial = {}, {}
        error = str(exc.detail)
        conflict = exc
    except Exception as exc:
        receipt, trial = {}, {}
        error = f"加载或样本试用未通过（{type(exc).__name__}）"

    # Release the Inference registry fence before reacquiring the project lock;
    # the result is re-fenced against current installation state below.
    await db.commit()

    await project_lock(db, project.id)
    await lock_project_manager(db, actor_id, project.id)
    try:
        await learning.enabled_backend(db, project.id, registry_id)
    except HTTPException:
        error = "推理插件安装已停用"
    deployment = await learning.complete_deployment_check(
        db,
        deployment_id,
        check_id,
        receipt=receipt,
        trial=trial,
        error=error,
    )
    await db.commit()
    if conflict:
        raise conflict
    return deployment
