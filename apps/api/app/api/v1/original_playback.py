"""Bearer preparation and path-scoped cookie access for verified video stages."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import APIKeyCookie

from app.deps import get_current_user, get_db, require_scopes
from app.schemas.original import (
    OriginalPlaybackLease,
    OriginalPlaybackPrepare,
    OriginalPlaybackRelease,
)
from app.services import original_binary as originals
from app.services import original_playback as playback
from app.services.dataset_content import content_http_error

router = APIRouter()
COOKIE_NAME = "aap_video_lease"
playback_cookie = APIKeyCookie(
    name=COOKIE_NAME,
    scheme_name="OriginalPlaybackCookie",
    description="Short-lived capability scoped to this exact video lease path; obtain through Bearer preparation.",
    auto_error=False,
)


def media_path(lease_id):
    return f"/api/v1/media/original-playback/{lease_id}"


def playback_registry(request: Request):
    registry = getattr(request.app.state, "original_playback_registry", None)
    if registry is None:
        raise HTTPException(503, {"code": "original_playback_unavailable"})
    return registry


def _http_error(exc):
    error = content_http_error(exc)
    error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
    if error.status_code == 429:
        error.headers = {**(error.headers or {}), "Retry-After": "1"}
    return error


def _same_origin(request):
    # Fetch metadata describes the browser origin even when Vite's existing
    # changeOrigin proxy rewrites Host to the API's loopback address.
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in {"same-origin", "none"}:
        raise HTTPException(403, {"code": "original_playback_origin_denied"})
    if site == "same-origin":
        return
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, {"code": "original_playback_origin_denied"})


def _secure_cookie(request):
    return request.url.scheme == "https" or (
        request.headers.get("sec-fetch-site") == "same-origin"
        and request.headers.get("origin", "").startswith("https://")
    )


async def _prepare(resource, payload, request, response, db, actor, registry):
    _same_origin(request)
    lease = None
    try:
        lease = await playback.prepare(db, resource, actor, payload, request, registry)
        # Reusing a ready stage can wait for unrelated retired files to close.
        # The saved authority/source still must hold after that real await.
        await originals.recheck_original(db, lease.context.original)
        registry.validate_lease(lease)
        path = media_path(lease.id)
        response.set_cookie(
            COOKIE_NAME,
            lease.secret,
            path=path,
            max_age=max(
                0, int((lease.expires_at - datetime.now(timezone.utc)).total_seconds())
            ),
            httponly=True,
            samesite="strict",
            secure=_secure_cookie(request),
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return OriginalPlaybackLease(
            lease_id=lease.id, media_path=path, expires_at=lease.expires_at
        )
    except BaseException as exc:
        if lease is not None:
            await registry.revoke_lease(lease.id)
        if isinstance(exc, Exception):
            raise _http_error(exc) from None
        raise


async def _release(resource, lease_id, payload, request, db, actor, registry):
    _same_origin(request)
    try:
        context = registry.context_for_release(lease_id)
        if (
            not isinstance(context, playback.PlaybackContext)
            or context.original.resource != resource
            or context.original.proof.actor_id != actor.id
            or context.session_id != payload.session_id
        ):
            raise HTTPException(404, {"code": "original_playback_not_found"})
        await registry.revoke_lease(lease_id)
    except Exception as exc:
        raise _http_error(exc) from None
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        COOKIE_NAME, path=media_path(lease_id), httponly=True, samesite="strict"
    )
    return response


@router.post(
    "/files/tasks/{task_id}/original-playback",
    response_model=OriginalPlaybackLease,
    dependencies=[Depends(require_scopes("tasks:read"))],
)
async def prepare_task_original(
    task_id: UUID,
    payload: OriginalPlaybackPrepare,
    request: Request,
    response: Response,
    db=Depends(get_db),
    actor=Depends(get_current_user),
    registry=Depends(playback_registry),
):
    return await _prepare(
        originals.Resource(kind="task", task_id=task_id),
        payload,
        request,
        response,
        db,
        actor,
        registry,
    )


@router.post(
    "/datasets/{dataset_id}/items/{item_id}/original-playback",
    response_model=OriginalPlaybackLease,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def prepare_item_original(
    dataset_id: UUID,
    item_id: UUID,
    payload: OriginalPlaybackPrepare,
    request: Request,
    response: Response,
    db=Depends(get_db),
    actor=Depends(get_current_user),
    registry=Depends(playback_registry),
):
    return await _prepare(
        originals.Resource(kind="item", dataset_id=dataset_id, item_id=item_id),
        payload,
        request,
        response,
        db,
        actor,
        registry,
    )


@router.delete(
    "/files/tasks/{task_id}/original-playback/{lease_id}",
    status_code=204,
    dependencies=[Depends(require_scopes("tasks:read"))],
)
async def release_task_original(
    task_id: UUID,
    lease_id: UUID,
    payload: OriginalPlaybackRelease,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_current_user),
    registry=Depends(playback_registry),
):
    return await _release(
        originals.Resource(kind="task", task_id=task_id),
        lease_id,
        payload,
        request,
        db,
        actor,
        registry,
    )


@router.delete(
    "/datasets/{dataset_id}/items/{item_id}/original-playback/{lease_id}",
    status_code=204,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def release_item_original(
    dataset_id: UUID,
    item_id: UUID,
    lease_id: UUID,
    payload: OriginalPlaybackRelease,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_current_user),
    registry=Depends(playback_registry),
):
    return await _release(
        originals.Resource(kind="item", dataset_id=dataset_id, item_id=item_id),
        lease_id,
        payload,
        request,
        db,
        actor,
        registry,
    )


@router.get(
    "/media/original-playback/{lease_id}",
    response_class=Response,
    responses={
        200: {
            "description": "Verified original video representation",
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        206: {
            "description": "Single byte range",
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        410: {
            "description": "Lease expired, retired or belongs to another API process"
        },
    },
)
@router.head(
    "/media/original-playback/{lease_id}",
    response_class=Response,
    responses={
        200: {"description": "Current authorized representation headers without a body"}
    },
)
async def read_original_video(
    lease_id: UUID,
    request: Request,
    db=Depends(get_db),
    registry=Depends(playback_registry),
    capability: str | None = Depends(playback_cookie),
):
    from app.services.original_playback_response import PlaybackResponse

    _same_origin(request)
    pin = None
    transferred = False
    try:
        pin = registry.pin(lease_id, capability or "")
        context = pin.context
        if not isinstance(context, playback.PlaybackContext):
            raise HTTPException(410, {"code": "original_playback_expired"})
        await originals.recheck_original(db, context.original)
        registry.validate_pin(pin)
        response = PlaybackResponse(
            pin.stage,
            media_type=pin.media_type,
            request_method=request.method,
            range_header=request.headers.get("range"),
            if_range=request.headers.get("if-range"),
            release=lambda: registry.release_pin(pin),
        )
        transferred = True
        return response
    except Exception as exc:
        raise _http_error(exc) from None
    finally:
        if pin is not None and not transferred:
            await registry.release_pin(pin)
