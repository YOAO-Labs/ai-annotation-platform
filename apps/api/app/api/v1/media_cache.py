"""Short-lived exact-output capabilities for browser image/video requests."""

from fastapi import APIRouter, HTTPException, Request, Response

from app.services import media_cache_response, media_cache_urls

router = APIRouter()


@router.head("/{kind}/{output_id}/{relative:path}", include_in_schema=False)
@router.get(
    "/{kind}/{output_id}/{relative:path}",
    response_class=Response,
    responses={
        200: {
            "description": "能力 URL 对应的媒体字节；同时支持 HEAD。",
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        206: {
            "description": "单段字节范围。",
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        400: {"description": "字节范围语法无效。"},
        404: {"description": "能力已过期、无效或对象不可读。"},
        416: {"description": "字节范围超出对象长度。"},
        503: {"description": "目标存储暂不可读。"},
        504: {"description": "媒体传输超时。"},
    },
)
async def read_media_cache(
    kind: str, output_id: str, relative: str, request: Request, token: str = ""
) -> Response:
    """The token replaces presigning, not the issuer's business authorization."""
    try:
        capability = media_cache_urls.decode(
            token, kind=kind, output_id=output_id, relative=relative
        )
    except media_cache_urls.MediaCacheCapabilityError:
        raise HTTPException(404, detail={"code": "media_cache_not_found"}) from None
    return await media_cache_response.response(
        capability,
        method=request.method,
        range_header=request.headers.get("range"),
        if_range=request.headers.get("if-range"),
    )
