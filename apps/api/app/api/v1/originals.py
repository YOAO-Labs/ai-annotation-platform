"""Authenticated full downloads; native media and image decoding stay separate."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.deps import get_current_user, get_db, require_scopes
from app.schemas.original import OriginalIdentity
from app.services import original_binary as svc
from app.services.dataset_content import content_http_error
from app.services.original_binary_response import BinaryResponse, close_stage

router = APIRouter()
_BINARY_RESPONSES = {
    200: {
        "description": "Full SHA-256 verified original download (up to 512 MiB)",
        "content": {
            "application/octet-stream": {
                "schema": {"type": "string", "format": "binary"}
            }
        },
    }
}
_SHA = Query(pattern=r"^[0-9a-f]{64}$")


async def _identity(resource, request, db, actor, response):
    try:
        result = await svc.public_identity(
            db,
            resource,
            actor,
            request=request,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except Exception as exc:
        raise content_http_error(exc) from None
    result["download_path"] = (
        f"{request.url.path.removesuffix('-identity')}"
        f"?content_id={result['content_id']}&sha256={result['sha256']}"
    )
    response.headers["Cache-Control"] = "no-store"
    return result


async def _download(resource, request, db, actor, content_id, sha256):
    if request.headers.get("range") is not None:
        raise HTTPException(416, detail={"code": "original_range_unsupported"})
    try:
        stage = await svc.snapshot(
            db,
            resource,
            actor,
            expected_content_id=content_id,
            expected_sha256=sha256,
            request=request,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except Exception as exc:
        raise content_http_error(exc) from None
    try:
        return BinaryResponse(stage)
    except BaseException:
        await close_stage(stage)
        raise


@router.get(
    "/files/tasks/{task_id}/original-identity",
    response_model=OriginalIdentity,
    dependencies=[Depends(require_scopes("tasks:read"))],
)
async def task_original_identity(
    task_id: UUID,
    request: Request,
    response: Response,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    return await _identity(
        svc.Resource(kind="task", task_id=task_id), request, db, actor, response
    )


@router.get(
    "/files/tasks/{task_id}/original",
    response_class=Response,
    responses=_BINARY_RESPONSES,
    dependencies=[Depends(require_scopes("tasks:read"))],
)
async def download_task_original(
    task_id: UUID,
    request: Request,
    content_id: UUID,
    sha256: str = _SHA,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    return await _download(
        svc.Resource(kind="task", task_id=task_id),
        request,
        db,
        actor,
        content_id,
        sha256,
    )


@router.get(
    "/files/tasks/{task_id}/links/{link_id}/original-identity",
    response_model=OriginalIdentity,
    dependencies=[Depends(require_scopes("tasks:read"))],
)
async def camera_original_identity(
    task_id: UUID,
    link_id: UUID,
    request: Request,
    response: Response,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    return await _identity(
        svc.Resource(kind="camera", task_id=task_id, link_id=link_id),
        request,
        db,
        actor,
        response,
    )


@router.get(
    "/files/tasks/{task_id}/links/{link_id}/original",
    response_class=Response,
    responses=_BINARY_RESPONSES,
    dependencies=[Depends(require_scopes("tasks:read"))],
)
async def download_camera_original(
    task_id: UUID,
    link_id: UUID,
    request: Request,
    content_id: UUID,
    sha256: str = _SHA,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    return await _download(
        svc.Resource(kind="camera", task_id=task_id, link_id=link_id),
        request,
        db,
        actor,
        content_id,
        sha256,
    )


@router.get(
    "/datasets/{dataset_id}/items/{item_id}/original-identity",
    response_model=OriginalIdentity,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def item_original_identity(
    dataset_id: UUID,
    item_id: UUID,
    request: Request,
    response: Response,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    return await _identity(
        svc.Resource(kind="item", dataset_id=dataset_id, item_id=item_id),
        request,
        db,
        actor,
        response,
    )


@router.get(
    "/datasets/{dataset_id}/items/{item_id}/original",
    response_class=Response,
    responses=_BINARY_RESPONSES,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def download_item_original(
    dataset_id: UUID,
    item_id: UUID,
    request: Request,
    content_id: UUID,
    sha256: str = _SHA,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    return await _download(
        svc.Resource(kind="item", dataset_id=dataset_id, item_id=item_id),
        request,
        db,
        actor,
        content_id,
        sha256,
    )
