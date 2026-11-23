"""Authenticated full downloads; native media and image decoding stay separate."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.deps import get_current_user, get_db, require_scopes
from app.middleware.request_id import request_id_var
from app.schemas.original import OriginalIdentity
from app.services import original_binary as svc
from app.services.dataset_content import content_http_error
from app.services.original_binary_response import BinaryResponse, close_stage
from app.services.original_request import connected_snapshot

logger = logging.getLogger(__name__)
router = APIRouter()
_ROUTE_TEMPLATES = {
    ("task", "identity"): "/api/v1/files/tasks/{task_id}/original-identity",
    (
        "camera",
        "identity",
    ): "/api/v1/files/tasks/{task_id}/links/{link_id}/original-identity",
    (
        "item",
        "identity",
    ): "/api/v1/datasets/{dataset_id}/items/{item_id}/original-identity",
    ("task", "download"): "/api/v1/files/tasks/{task_id}/original",
    ("camera", "download"): "/api/v1/files/tasks/{task_id}/links/{link_id}/original",
    ("item", "download"): "/api/v1/datasets/{dataset_id}/items/{item_id}/original",
}
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


def _sanitized_route(request, resource, operation):
    route = request.scope.get("route") if request is not None else None
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(template, str) and template.startswith("/api/"):
        return template
    return _ROUTE_TEMPLATES[(resource.kind, operation)]


def _extract_error_fields(http_error):
    detail = http_error.detail
    if not isinstance(detail, dict):
        return None, None
    raw_code = detail.get("code")
    raw_reason = detail.get("reason")
    code = raw_code if isinstance(raw_code, str) and raw_code else None
    reason = raw_reason if isinstance(raw_reason, str) and raw_reason else None
    return code, reason


def _log_conflict(
    operation,
    resource,
    request,
    exc,
    http_error,
    *,
    expected_content_id=None,
    expected_sha256=None,
):
    code, reason = _extract_error_fields(http_error)
    item_id = resource.item_id or getattr(exc, "actual_item_id", None)
    fields = {
        "route": _sanitized_route(request, resource, operation),
        "request_id": request_id_var.get() or None,
        "status_code": http_error.status_code,
        "code": code,
        "reason": reason,
        "resource_kind": resource.kind,
        "task_id": str(resource.task_id) if resource.task_id is not None else None,
        "item_id": str(item_id) if item_id is not None else None,
    }
    if operation == "download":
        actual_content_id = getattr(exc, "actual_content_id", None)
        actual_sha256 = getattr(exc, "actual_sha256", None)
        fields.update(
            {
                "expected_content_id": (
                    str(expected_content_id)
                    if expected_content_id is not None
                    else None
                ),
                "expected_sha256_prefix": (
                    expected_sha256[:12]
                    if isinstance(expected_sha256, str) and expected_sha256
                    else None
                ),
                "actual_content_id": (
                    str(actual_content_id) if actual_content_id is not None else None
                ),
                "actual_sha256_prefix": (
                    actual_sha256[:12]
                    if isinstance(actual_sha256, str) and actual_sha256
                    else None
                ),
            }
        )
        logger.warning(
            "original_download_conflict route=%s request_id=%s status=%s code=%s "
            "reason=%s resource_kind=%s task_id=%s item_id=%s "
            "expected_content_id=%s expected_sha256_prefix=%s "
            "actual_content_id=%s actual_sha256_prefix=%s",
            fields["route"],
            fields["request_id"],
            fields["status_code"],
            fields["code"],
            fields["reason"],
            fields["resource_kind"],
            fields["task_id"],
            fields["item_id"],
            fields["expected_content_id"],
            fields["expected_sha256_prefix"],
            fields["actual_content_id"],
            fields["actual_sha256_prefix"],
            extra=fields,
        )
        return
    logger.warning(
        "original_identity_conflict route=%s request_id=%s status=%s code=%s "
        "reason=%s resource_kind=%s task_id=%s item_id=%s",
        fields["route"],
        fields["request_id"],
        fields["status_code"],
        fields["code"],
        fields["reason"],
        fields["resource_kind"],
        fields["task_id"],
        fields["item_id"],
        extra=fields,
    )


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
        http_error = content_http_error(exc)
        if http_error.status_code == 409:
            _log_conflict("identity", resource, request, exc, http_error)
        raise http_error from None
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
        stage = await connected_snapshot(
            request,
            svc.snapshot(
                db,
                resource,
                actor,
                expected_content_id=content_id,
                expected_sha256=sha256,
                request=request,
                api_key_id=getattr(request.state, "api_key_id", None),
            ),
        )
    except Exception as exc:
        http_error = content_http_error(exc)
        if http_error.status_code == 409:
            _log_conflict(
                "download",
                resource,
                request,
                exc,
                http_error,
                expected_content_id=content_id,
                expected_sha256=sha256,
            )
        raise http_error from None
    if stage is None:
        return Response(status_code=499)
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
