"""Shared HTTP policy for already-authorized static mask references."""

from collections.abc import Awaitable, Callable
import logging
import uuid

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.services.raster_mask_storage import classify_raster_mask_content_error

logger = logging.getLogger(__name__)

MASK_CONTENT_RESPONSES = {
    304: {"description": "Not Modified"},
    409: {"description": "Mask reference or task context is invalid"},
    503: {"description": "Mask object storage is unavailable"},
}


def mask_etag_matches(request: Request, etag: str) -> bool:
    raw = request.headers.get("if-none-match")
    if not raw:
        return False
    normalized = etag.removeprefix("W/")
    return any(
        candidate == "*" or candidate.removeprefix("W/") == normalized
        for candidate in (part.strip() for part in raw.split(","))
    )


async def mask_content_response(
    *,
    owner_id: uuid.UUID,
    mask_ref: dict,
    request: Request,
    loader: Callable[[dict], Awaitable[dict]],
) -> Response:
    """Callers authorize context; owned content is verified even for a 304."""
    etag = f'"{mask_ref.get("sha256")}"'
    headers = {"Cache-Control": "private, no-cache", "ETag": etag}
    try:
        payload = await loader(mask_ref)
    except (KeyError, ValueError) as exc:
        code = getattr(exc, "code", None)
        owned_error = isinstance(code, str) and code.startswith(
            "raster_object_storage_"
        )
        status_code = (
            503 if owned_error and getattr(exc, "status_code", None) == 503 else 409
        )
        raise HTTPException(
            status_code=status_code,
            detail={
                "reason": code
                if owned_error
                else classify_raster_mask_content_error(exc),
                "retryable": True,
                "message": (
                    "Mask object storage is unavailable"
                    if status_code == 503
                    else "Mask object ownership or content is unavailable"
                    if owned_error
                    else f"mask object is invalid: {exc}"
                ),
            },
        ) from exc
    except Exception as exc:
        logger.warning("mask object read failed for owner %s", owner_id, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "mask_storage_unavailable",
                "retryable": True,
                "message": "Mask object storage is unavailable",
            },
        ) from exc
    if mask_etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(payload, headers=headers)
