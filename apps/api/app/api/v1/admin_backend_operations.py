"""Superadministrator-only submission to a deployment-owned host maintenance queue."""

from typing import Literal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.user import User
from app.deps import get_db, require_roles
from app.services.audit import AuditService
from app.services.backend_operations import (
    JobStore,
    ManagerUnavailable,
    OperationConflict,
    TargetChanged,
    operations_directory,
)

router = APIRouter(
    prefix="/admin/ml-backend-operations", tags=["admin-ml-backend-operations"]
)
Provider = Literal["gsam2", "sam3", "yolo", "onnxtools", "rapidocr"]


class Event(BaseModel):
    at: str
    message: str
    kind: Literal["info", "success", "error"]


class Job(BaseModel):
    id: str
    provider: Provider
    status: Literal["queued", "running", "succeeded", "failed", "interrupted"]
    phase: Literal[
        "queued",
        "validating",
        "building",
        "stopping",
        "recreating",
        "checking",
        "complete",
    ]
    created_at: str
    updated_at: str
    deployment: str
    events: list[Event]
    error: str | None = None


class Service(BaseModel):
    provider: Provider
    can_rebuild: bool
    reason: str | None


class ManagerStatus(BaseModel):
    available: bool
    reason: str | None
    deployment: str | None
    runner_id: str | None
    services: list[Service]
    jobs: list[Job]


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Provider
    expected_deployment: str
    expected_runner_id: str


def get_store():
    return JobStore(operations_directory())


@router.get("/status", response_model=ManagerStatus)
async def manager_status(admin: User = Depends(require_roles(UserRole.SUPER_ADMIN))):
    return get_store().status()


@router.post("/jobs", status_code=202, response_model=Job)
async def submit_job(
    data: Submission,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    store = get_store()
    try:
        job = store.reserve(
            data.provider,
            expected_deployment=data.expected_deployment,
            expected_runner_id=data.expected_runner_id,
        )
    except TargetChanged:
        raise HTTPException(
            409,
            {
                "error_code": "backend_manager_target_changed",
                "message": "维护目标或宿主机程序已变化，请刷新后重新确认。",
            },
        ) from None
    except (ManagerUnavailable, OSError, ValueError):
        raise HTTPException(
            503,
            {
                "error_code": "backend_manager_unavailable",
                "message": "宿主机维护程序不可用，请先完成部署配置。",
            },
        ) from None
    except OperationConflict as exc:
        raise HTTPException(
            409,
            {
                "error_code": "backend_operation_conflict",
                "message": "已有维护任务或待核查的中断任务。",
                "job_id": exc.job_id,
            },
        ) from exc
    try:
        await AuditService.log(
            db,
            actor=admin,
            action="ml_backend.rebuild_requested",
            target_type="ml_backend_operation",
            target_id=job["id"],
            request=request,
            status_code=202,
            detail={"provider": data.provider, "deployment": job["deployment"]},
        )
        await db.commit()
    except BaseException:
        store.abort_reservation(job["id"])
        raise
    return store.publish(job["id"])


@router.get("/jobs/{job_id}", response_model=Job)
async def read_job(
    job_id: uuid.UUID, admin: User = Depends(require_roles(UserRole.SUPER_ADMIN))
):
    try:
        job = get_store().read(f"{job_id}.json")
        JobStore.validate_job(job, str(job_id))
        return job
    except FileNotFoundError:
        raise HTTPException(404, "维护任务不存在") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "维护队列异常，请部署管理员检查。") from None
