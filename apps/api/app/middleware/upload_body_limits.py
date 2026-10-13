"""Pre-parser request body limits for frame multipart and gzip mask JSON."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from email.message import Message
from typing import Any

from app.utils.raster_mask_gzip import (
    MAX_COMPRESSED_BYTES,
    MAX_UNCOMPRESSED_BYTES,
    decompress_mask_gzip,
)

ASGIApp = Callable[
    [dict[str, Any], Callable[..., Awaitable[dict]], Callable[..., Awaitable[None]]],
    Awaitable[None],
]

MAX_FRAME_FILE_BYTES = 32 * 1024 * 1024
# Raw multipart includes boundaries and small form fields in addition to the file.
MAX_FRAME_MULTIPART_BODY_BYTES = MAX_FRAME_FILE_BYTES + 1024 * 1024
# 头像:与 services/avatar_image.MAX_AVATAR_FILE_BYTES 对齐,另留 multipart 头部余量。
MAX_AVATAR_FILE_BYTES = 2 * 1024 * 1024
MAX_AVATAR_MULTIPART_BODY_BYTES = MAX_AVATAR_FILE_BYTES + 256 * 1024
# Inline RLE can consume the full canonical object budget; leave bounded room for
# receipt, lineage, prompt summary, and JSON field names before Pydantic parsing.
MAX_AI_MASK_ACCEPT_BODY_BYTES = MAX_UNCOMPRESSED_BYTES + 1024 * 1024
MAX_INTERACTIVE_CONTEXT_BODY_BYTES = 1024 * 1024
MAX_MASK_MUTATION_BODY_BYTES = 12 * 1024 * 1024


async def _json_error(send, status: int, detail: Any) -> None:
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _read_bounded(receive, limit: int) -> bytes:
    body = bytearray()
    more = True
    while more:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        if message["type"] != "http.request":
            continue
        body.extend(message.get("body", b""))
        if len(body) > limit:
            raise OverflowError
        more = bool(message.get("more_body", False))
    return bytes(body)


def _replacement_receive(body: bytes):
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _replace_body_headers(scope: dict[str, Any], body: bytes) -> dict[str, Any]:
    copied = dict(scope)
    copied["headers"] = [
        (name, value)
        for name, value in scope.get("headers", [])
        if name.lower() not in {b"content-length", b"content-encoding"}
    ] + [(b"content-length", str(len(body)).encode())]
    return copied


def _multipart_field_size(
    body: bytes,
    content_type: bytes,
    *,
    field_name: str,
) -> int | None:
    """Return one multipart field's raw byte size without copying file parts."""

    message = Message()
    message["content-type"] = content_type.decode("latin-1")
    boundary_value = message.get_param("boundary", header="content-type")
    if not isinstance(boundary_value, str) or not boundary_value:
        return None
    delimiter = b"--" + boundary_value.encode("latin-1")
    cursor = 0
    expected = f'name="{field_name}"'.encode()
    while True:
        part_start = body.find(delimiter, cursor)
        if part_start < 0:
            return None
        part_start += len(delimiter)
        if body[part_start : part_start + 2] == b"--":
            return None
        if body[part_start : part_start + 2] == b"\r\n":
            part_start += 2
        header_end = body.find(b"\r\n\r\n", part_start)
        if header_end < 0 or header_end - part_start > 16 * 1024:
            return None
        next_boundary = body.find(delimiter, header_end + 4)
        if next_boundary < 0:
            return None
        headers = body[part_start:header_end].lower()
        if b"content-disposition:" in headers and expected in headers:
            value_end = next_boundary
            if body[value_end - 2 : value_end] == b"\r\n":
                value_end -= 2
            return max(0, value_end - (header_end + 4))
        cursor = next_boundary


class UploadBodyLimitMiddleware:
    """Bound selected upload bodies before FastAPI invokes multipart/JSON parsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        is_frame = "/ml-backends/" in path and path.endswith(
            ("/predict-frame", "/interactive-annotating-frame")
        )
        is_interactive_frame = path.endswith("/interactive-annotating-frame")
        is_mask_gzip = (
            path.startswith("/api/v1/tasks/")
            and path.endswith("/mask-content")
            and headers.get(b"content-encoding", b"").lower() == b"gzip"
        )
        is_mask = path.startswith("/api/v1/tasks/") and path.endswith("/mask-content")
        is_ai_mask_accept = path.startswith("/api/v1/tasks/") and path.endswith(
            "/ai-mask-candidates/accept"
        )
        is_mask_mutation = path.startswith("/api/v1/tasks/") and path.endswith(
            "/annotations/mask-mutations:commit"
        )
        is_interactive_context = "/ml-backends/" in path and path.endswith(
            "/interactive-annotating"
        )
        is_avatar = path == "/api/v1/auth/me/avatar"
        if is_mask and headers.get(b"content-encoding", b"").lower() not in {
            b"",
            b"identity",
            b"gzip",
        }:
            await _json_error(send, 415, "Unsupported Content-Encoding")
            return
        if is_ai_mask_accept and headers.get(b"content-encoding", b"").lower() not in {
            b"",
            b"identity",
        }:
            await _json_error(send, 415, "Unsupported Content-Encoding")
            return
        if is_mask_mutation and headers.get(b"content-encoding", b"").lower() not in {
            b"",
            b"identity",
        }:
            await _json_error(send, 415, "Unsupported Content-Encoding")
            return
        if is_interactive_context and headers.get(
            b"content-encoding", b""
        ).lower() not in {
            b"",
            b"identity",
        }:
            await _json_error(send, 415, "Unsupported Content-Encoding")
            return
        if (
            not is_frame
            and not is_mask_gzip
            and not is_ai_mask_accept
            and not is_mask_mutation
            and not is_interactive_context
            and not is_avatar
        ):
            await self.app(scope, receive, send)
            return

        # 各受管路径互不重叠,按上表顺序取第一个命中的上限;都不命中时回退 mask 压缩体量。
        limit = next(
            (
                candidate
                for flag, candidate in (
                    (is_frame, MAX_FRAME_MULTIPART_BODY_BYTES),
                    (is_avatar, MAX_AVATAR_MULTIPART_BODY_BYTES),
                    (is_ai_mask_accept, MAX_AI_MASK_ACCEPT_BODY_BYTES),
                    (is_mask_mutation, MAX_MASK_MUTATION_BODY_BYTES),
                    (is_interactive_context, MAX_INTERACTIVE_CONTEXT_BODY_BYTES),
                )
                if flag
            ),
            MAX_COMPRESSED_BYTES,
        )
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > limit:
                    await _json_error(
                        send,
                        413,
                        {"reason": "request_body_too_large", "max_bytes": limit},
                    )
                    return
            except ValueError:
                await _json_error(send, 400, "Invalid Content-Length")
                return
        try:
            raw = await _read_bounded(receive, limit)
        except OverflowError:
            await _json_error(
                send,
                413,
                {"reason": "request_body_too_large", "max_bytes": limit},
            )
            return
        if is_interactive_frame:
            context_size = _multipart_field_size(
                raw,
                headers.get(b"content-type", b""),
                field_name="context",
            )
            if (
                context_size is not None
                and context_size > MAX_INTERACTIVE_CONTEXT_BODY_BYTES
            ):
                await _json_error(
                    send,
                    413,
                    {
                        "reason": "interactive_context_too_large",
                        "message": "interactive context must be <= 1 MiB",
                    },
                )
                return
        if is_avatar:
            file_size = _multipart_field_size(
                raw,
                headers.get(b"content-type", b""),
                field_name="file",
            )
            if file_size is not None and file_size > MAX_AVATAR_FILE_BYTES:
                await _json_error(
                    send,
                    413,
                    {
                        "reason": "avatar_too_large",
                        "message": "avatar must be <= 2 MiB",
                        "max_bytes": MAX_AVATAR_FILE_BYTES,
                    },
                )
                return
        if is_mask_gzip:
            try:
                body = decompress_mask_gzip(
                    raw,
                    max_compressed=MAX_COMPRESSED_BYTES,
                    max_uncompressed=MAX_UNCOMPRESSED_BYTES,
                )
            except ValueError as exc:
                status = 413 if "exceeds" in str(exc) else 400
                await _json_error(send, status, str(exc))
                return
            scope = _replace_body_headers(scope, body)
            receive = _replacement_receive(body)
        else:
            receive = _replacement_receive(raw)
        await self.app(scope, receive, send)
